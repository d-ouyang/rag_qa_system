"""会话滚动摘要（v4.0.0 P4-10）——让 LLM 看见「窗口之外」的对话。

--------------------------------------------------------------------------
它补的洞
--------------------------------------------------------------------------
现状：MySQL 全量持久化会话（`_trim` 不删消息），但 `get_recent_messages`
只把**最近 MEMORY_MAX_TURNS 轮**送进模型和改写——窗口外的内容**用户看得见、
模型看不见**。于是第 6 轮问「刚才第一轮说的那个流程呢？」模型一脸茫然。

本模块在**写入侧**做增量压缩：每当有对话滑出窗口，就把「上次摘要之后新滑出的
那部分」连同旧摘要一起交给模型重压成一段新摘要，存进 `session_meta`：

    session_meta["summary"]      str  —— 窗口外对话的压缩结果
    session_meta["summary_upto"] int  —— 已压缩到第几条消息（不含）

读取侧（`MemoryManager.get_recent_messages`）把它作为 **SystemMessage 前置**
拼到窗口原文前面 —— 模型因此同时看到「压缩过的过去」和「原文的最近几轮」。

--------------------------------------------------------------------------
姿态约定
--------------------------------------------------------------------------
· **增量摘要**：只压「新滑出的部分 + 旧摘要」，不每轮全量重压（省 token、稳）。
· **fail-open**：LLM 挂了 / 返回空 → 保留旧摘要、不抛异常。摘要坏了的最坏结果是
  「没有摘要」，不是「会话不可用」——与 qa_trace 镜像、依据校验同一姿态。
· **存 session_meta 而不加迁移**：三套存储（memory / redis / mysql）都已经
  原样往返 `session_meta` 这个 dict，加字段即全后端可用，不必动表结构。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。把给定的对话片段压缩成一段简洁摘要，供后续对话参考。\n"
    "要求：\n"
    "- 只保留可复用的事实：用户问过什么、结论/流程/数字、已确认的偏好；\n"
    "- 丢弃寒暄、重复、以及已经被更新的旧结论；\n"
    "- 用中文陈述句，不超过 300 字，不要编号、不要开场白；\n"
    "- 若给了【已有摘要】，把它与新片段**合并**成一段（不是并列罗列）。"
)

#: 单条消息进入摘要时的截断长度（防止超长答案把 prompt 撑爆）
_PER_MESSAGE_CHARS = 300


def _render(messages: list[Any]) -> str:
    lines = []
    for m in messages:
        role = "用户" if getattr(m, "type", "") == "human" else "助手"
        content = str(getattr(m, "content", ""))[:_PER_MESSAGE_CHARS]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def summarize_overflow(
    memory: Any,
    session_id: str,
    owner_id: int | None,
    max_turns: int | None = None,
) -> bool:
    """
    把滑出窗口的对话增量压进 `session_meta.summary`。

    :return: True = 摘要已更新；False = 无需摘要（未溢出/已压完）或摘要失败（fail-open）
    """
    turns = memory.max_turns if max_turns is None else int(max_turns)
    keep = max(0, turns) * 2
    messages = memory.get_messages(session_id, owner_id=owner_id)
    if keep == 0 or len(messages) <= keep:
        return False

    meta = memory.get_session_meta(session_id, owner_id=owner_id) or {}
    upto = int(meta.get("summary_upto") or 0)
    boundary = len(messages) - keep          # 滑出窗口的消息条数
    if boundary <= upto:
        return False                          # 没有新滑出的内容

    chunk = messages[upto:boundary]
    old_summary = str(meta.get("summary") or "")
    payload = _render(chunk)
    if old_summary:
        payload = f"【已有摘要】\n{old_summary}\n\n【新增对话片段】\n{payload}"

    try:
        from core.rag_chain import get_llm_client  # 局部导入避免环

        llm = get_llm_client().get_llm()
        message = llm.invoke(f"{_SUMMARY_SYSTEM_PROMPT}\n\n{payload}")
        new_summary = str(getattr(message, "content", "") or "").strip()
    except Exception:
        # fail-open：摘要挂了不影响问答，旧摘要原样保留
        logger.warning("会话摘要失败（保留旧摘要，不影响问答）", exc_info=True)
        return False
    if not new_summary:
        return False

    # update_session_meta 只认 title/pinned（接口层语义），摘要直写存储层
    _write_summary(memory, session_id, owner_id, new_summary, boundary)
    logger.info(
        "会话摘要已更新 | session=%s 已压缩到 %d 条（共 %d 条，窗口 %d 轮）",
        session_id, boundary, len(messages), turns,
    )
    return True


def _write_summary(
    memory: Any, session_id: str, owner_id: int | None, summary: str, upto: int
) -> None:
    """写回 session_meta（update_session_meta 只认 title/pinned，这里直写存储层）。"""
    with memory.store.session_lock(session_id):
        snapshot = memory._load(session_id, owner_id=owner_id)
        if snapshot is None:
            return
        meta = snapshot.session_meta or {"pinned": False}
        meta["summary"] = summary
        meta["summary_upto"] = int(upto)
        snapshot.session_meta = meta
        memory._save(session_id, snapshot, owner_id=owner_id)


def make_summarizer_hook(memory: Any) -> Any:
    """
    生成「每轮写记忆后」的摘要钩子（P4-10）。

    为什么做成钩子而不是让 MemoryManager 直接调 LLM：MemoryManager 是存储/窗口
    逻辑层，不该反向依赖 rag_chain（LLM 客户端）——依赖方向倒过来会造成环。
    开关在钩子里读（每次调用都读 → 管理端改了即时生效）。
    """

    def _hook(session_id: str, owner_id: Any) -> None:
        from core.config_flags import flag_value

        if not flag_value("session_summary"):
            return
        summarize_overflow(memory, session_id, owner_id)

    return _hook
