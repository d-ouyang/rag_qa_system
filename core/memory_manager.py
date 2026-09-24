"""
会话记忆模块 —— 按 session_id 维护多轮对话历史，解决「上下文丢失」问题。

--------------------------------------------------------------------------
为什么需要它（面试常问）
--------------------------------------------------------------------------
LLM 本身是无状态的：每次 invoke 看到的只有这一次传入的消息。
用户说「它多少钱？」，如果没有前几轮对话，模型根本不知道「它」指什么。

所以应用层必须自己保存对话历史，每次提问时把「历史消息 + 当前问题」
一起塞进 prompt。这个「历史存哪、怎么存、存多久」就是本模块的职责。

--------------------------------------------------------------------------
v2.0.0 的分层：本模块只管「怎么算」，不管「存哪里」
--------------------------------------------------------------------------
v1.0.0 把存储实现（进程内 dict）和会话逻辑（裁剪/对齐/截断）揉在一起，
结果是「想换 Redis 就得动经过测试的语义代码」。

v2.0.0 拆成两层：
    · core/session_store.py  MemorySessionStore   —— 进程内，开发/测试用
    · core/redis_store.py    RedisSessionStore    —— Redis，生产用，重启不丢
    · 本模块                 MemoryManager        —— 会话语义，与存储后端无关
换后端只改一行配置（MEMORY_BACKEND=redis），本模块与所有调用方零改动。

--------------------------------------------------------------------------
四个关键设计决策
--------------------------------------------------------------------------
1. 为什么按 session_id 隔离，而不是全局一份历史
   多用户/多标签页共用一个服务时，全局历史会把 A 的对话串给 B —— 既泄露隐私
   又污染上下文。按 session_id 隔离，是最小可用、也最常见的方案。

2. 为什么模型只看最近 N 轮，库里却要留全部
   · token 成本：历史越长，每次请求的 prompt 越大，越慢越贵；
   · 相关性：很久之前的对话多数对当前问题没帮助，代词指代靠最近几轮就够。
   MEMORY_MAX_TURNS（默认 5）只决定送进模型和问题重写的窗口。
   会话记录是持久化的：闲置多久都不从列表消失，轮数上限也不删消息行。

3. 为什么每个会话一把锁，而不是全局一把大锁
   全局锁会让「用户 A 写历史」阻塞「用户 B 读历史」，并发度直接归零。
   按 session_id 粒度加锁：同一会话内串行（保证消息顺序不乱、不丢轮），
   不同会话之间完全并行。锁由存储层提供 —— 内存版是 threading.Lock，
   Redis 版是分布式锁（多 worker 下同样成立）。

4. 为什么写入失败不停服务（只读降级）
   记忆是增强项，不是核心链路：没有历史上下文，RAG 问答照样能回答。
   所以 Redis 写失败（内存满/网络断）时选择「丢掉这一轮记忆 + 告警」，
   而不是把用户请求变成 500。这条取舍在 core/redis_monitor.py 里有完整说明。

--------------------------------------------------------------------------
读操作不再隐式创建会话（v2.0.0 修正）
--------------------------------------------------------------------------
v1.0.0 的 get_messages / get_history 会在会话不存在时顺手创建一个空会话。
副作用是：任何人 GET /sessions/{随便一个 id} 都会凭空多出一条空会话记录
（内存版里表现为会话数虚增；换 Redis 后还会真的写一个空 key + 索引项，
在会话列表里出现「点进去什么都没有」的幽灵条目）。
现在统一为：读就是读，不存在就返回空；只有 add_exchange / add_usage
这类真实写入才会创建会话。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from langchain_core.chat_history import BaseChatMessageHistory, InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from config.settings import settings
from core.session_store import SessionSnapshot, SessionStore, build_session_store

logger = logging.getLogger(__name__)

# 角色的序列化映射（存储层只认 "user"/"assistant" 这两个字符串，
# 好处是落 Redis 的 JSON 与 LangChain 的类名解耦，将来升级 LangChain 不影响存量数据）
_ROLE_TO_CLASS: dict[str, type[BaseMessage]] = {"user": HumanMessage, "assistant": AIMessage}


def _payload_to_messages(payload: list[dict[str, str]]) -> list[BaseMessage]:
    """存储格式（dict）→ LangChain Message 对象。"""
    messages: list[BaseMessage] = []
    for item in payload:
        cls = _ROLE_TO_CLASS.get(str(item.get("role", "")).lower())
        if cls is None:
            # 未知角色不是致命问题：跳过并告警，不让一条脏数据把整个会话读崩
            logger.warning("历史消息角色未知，已跳过 | role=%r", item.get("role"))
            continue
        messages.append(cls(content=str(item.get("content", ""))))
    return messages


def _messages_to_payload(messages: list[BaseMessage]) -> list[dict[str, str]]:
    """LangChain Message 对象 → 存储格式（dict）。"""
    payload: list[dict[str, str]] = []
    for m in messages:
        role = "user" if m.type == "human" else "assistant"
        payload.append({"role": role, "content": str(m.content)})
    return payload


class MemoryManager:
    """
    多轮对话记忆管理器（存储后端可插拔）。

    对外接口（与 v1.0.0 完全一致，调用方零改动）：
        · get_messages(session_id)       取历史消息（LangChain Message 列表）
        · get_history(session_id)        取 BaseChatMessageHistory 对象
        · add_exchange(session_id, q, a) 一轮问答结束后写入（user + ai 两条）
        · clear_session(session_id)      清空某个会话
        · session_count() / list_sessions()  运维/接口层用

    线程/进程模型：不同 session 并行；同一 session 串行（锁由存储层提供）。
    """

    _USAGE_ZERO: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "requests": 0,
    }

    def __init__(
        self,
        max_turns: int | None = None,
        ttl_seconds: int | None = None,
        store: SessionStore | None = None,
    ) -> None:
        """
        :param max_turns: 每会话最多保留的对话轮数，缺省读 settings.MEMORY_MAX_TURNS
        :param ttl_seconds: 会话闲置多少秒后过期，缺省读 settings.MEMORY_SESSION_TTL_SECONDS
        :param store: 存储后端；缺省按 settings.MEMORY_BACKEND 构建
                      （测试里可显式注入 MemorySessionStore 或注入 fakeredis 的 RedisSessionStore）
        """
        # 注意不能用 `or` 兜底：0 是合法值（测试里 ttl_seconds=0 表示「立刻过期」），
        # `0 or 默认值` 会把 0 静默吞掉换成默认值
        self.max_turns: int = int(max_turns if max_turns is not None else settings.MEMORY_MAX_TURNS)
        self.ttl_seconds: int = int(
            ttl_seconds if ttl_seconds is not None else settings.MEMORY_SESSION_TTL_SECONDS
        )
        self.store: SessionStore = (
            store if store is not None else build_session_store(ttl_seconds=self.ttl_seconds)
        )
        # 只读降级告警去重标记：水位不恢复就只提示一次，避免每轮问答刷一条 WARN
        self._readonly_warned = False

        logger.info(
            "MemoryManager 初始化完成 | 后端=%s 最大轮数=%d 会话TTL=%ds",
            self.store.name,
            self.max_turns,
            self.ttl_seconds,
        )

        # Redis 后端自动挂上内存预警（谁引入容量风险谁负责建监控）
        if self.store.name == "redis":
            from core.redis_monitor import start_redis_memory_monitor

            start_redis_memory_monitor(cleanup_hook=self.cleanup_expired)

    # ------------------------------------------------------------------ #
    # 存储层适配
    # ------------------------------------------------------------------ #
    def _load(self, session_id: str, touch: bool = True) -> SessionSnapshot | None:
        """
        读快照；不存在或已过期返回 None。

        :param touch: 是否把本次读取计为「活跃」（刷新 TTL）。
                      会话列表这类「扫一眼」的读传 False —— 否则有人反复刷新列表
                      就等于给所有会话无限续命，「闲置过期」彻底失效。
        """
        return self.store.load(session_id, touch=touch)

    def _save(self, session_id: str, snapshot: SessionSnapshot) -> bool:
        """
        写快照。返回是否真的写成功。

        写入前先问存储层「现在能写吗」：Redis 内存水位到 fatal 时会被拒，
        此时**不抛异常**——问答主链路继续，只是这一轮不进记忆。
        """
        allowed, reason = self.store.write_allowed()
        if not allowed:
            if not self._readonly_warned:
                logger.warning("记忆写入被容量保护拒绝，本次跳过写入（问答不受影响）| 原因：%s", reason)
                self._readonly_warned = True
            return False
        self._readonly_warned = False
        self.store.save(session_id, snapshot)
        return True

    # ------------------------------------------------------------------ #
    # 会话逻辑（与存储无关）
    # ------------------------------------------------------------------ #
    def _normalize_meta(self, snapshot: SessionSnapshot) -> None:
        """
        把 exchange_meta 补齐到「已有轮数」的长度（缺的轮用空 dict 占位）。

        为什么需要：元数据数组必须与轮一一对应，历史接口才能用
        `轮号 = 消息下标 // 2` 直接索引。两种会破坏对齐的情况：
          · v1.0.0 存量数据里只有「带 meta 的轮」被记录过（稀疏数组）；
          · 未来某条调用链忘了传 meta。
        统一在这里补齐，比在每个读取点写兜底判断更不容易漏。

        ⚠️ 调用时机：必须在**本轮消息 append 之前**调用。
        它的语义是「补齐历史」，不是「给当前轮预留位置」——
        放错位置会让每轮多补一个占位，最终把最老那轮的 meta 挤掉。
        详见 `add_exchange()` 里的注释。
        """
        expected = len(snapshot.messages) // 2
        missing = expected - len(snapshot.exchange_meta)
        if missing > 0:
            snapshot.exchange_meta.extend({} for _ in range(missing))
            logger.debug("元数据补齐占位 | 补齐=%d 轮", missing)

    def _trim(self, snapshot: SessionSnapshot) -> None:
        """
        对齐元数据长度。不再按轮数或字节删消息。

        会话记录要一直留在列表里，模型窗口在读取时另切（见 get_recent_messages）。
        用户编辑重发仍走 truncate_session，那是显式截断，不是这里的自动清理。
        """
        turns = len(snapshot.messages) // 2
        if len(snapshot.exchange_meta) > turns:
            del snapshot.exchange_meta[: len(snapshot.exchange_meta) - turns]

    # ------------------------------------------------------------------ #
    # 对外接口：历史读写
    # ------------------------------------------------------------------ #
    def get_history(self, session_id: str) -> BaseChatMessageHistory:
        """
        返回会话的 ChatMessageHistory 对象（只读快照语义）。

        暴露 LangChain 标准接口对象，是为了将来想换 RunnableWithMessageHistory
        这类官方组件时能直接对接（它要求的正是 BaseChatMessageHistory）。
        注意：这里是**快照**，对它的改动不会写回存储 —— 写入统一走
        add_exchange / clear_session / truncate_session，避免绕过裁剪逻辑。
        """
        snapshot = self._load(session_id)
        payload = snapshot.messages if snapshot is not None else []
        return InMemoryChatMessageHistory(messages=_payload_to_messages(payload))

    def get_messages(self, session_id: str) -> list[BaseMessage]:
        """取某个会话的全部历史（新列表：外部改动不会写回存储）。"""
        snapshot = self._load(session_id)
        if snapshot is None:
            return []
        return _payload_to_messages(snapshot.messages)

    def get_recent_messages(self, session_id: str, turns: int | None = None) -> list[BaseMessage]:
        """
        取送进模型和问题重写的最近若干轮。

        库里的全文不动。默认轮数是 MEMORY_MAX_TURNS。
        """
        messages = self.get_messages(session_id)
        keep_turns = self.max_turns if turns is None else int(turns)
        keep = max(0, keep_turns) * 2
        if keep == 0 or len(messages) <= keep:
            return messages
        return messages[-keep:]

    def add_exchange(
        self,
        session_id: str,
        question: str,
        answer: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """
        一轮问答结束后写入记忆（用户问 + AI 答，两条一起，原子语义）。

        为什么是「一轮一起写」而不是分开写：
        如果先写 user 消息、AI 回答时挂了，历史里就留下一条没有回答的问题，
        下一轮模型会误以为「这个问题还没回答」而重复作答。
        要么两条都写成功，要么都不写 —— 现在整条快照一次覆盖写，
        这个「原子」是由存储层保证的（Redis 走 MULTI/EXEC）。

        :param meta: 本轮展示元数据（sources/intent/usage/elapsed_ms/ts 等），
                     与该轮消息一起存，历史接口按轮回填
        """
        with self.store.session_lock(session_id):
            snapshot = self._load(session_id) or SessionSnapshot()
            # ⚠️ 这一句必须在 append 消息**之前**（2026-09-24 修复，别再挪回去）。
            #
            # 放错位置的后果（p0.1 起潜伏，p0.4 才在真实会话里暴露）：
            #   `expected = len(messages) // 2` 在 append 之后就等于「含当前轮」的轮数，
            #   于是每轮都凭空补一个空占位；紧接着 append 本轮 meta 让数组比轮数多 1，
            #   再由 `_trim()` 末尾的防御分支从头部砍掉 1 条 —— 净效果是
            #   **每一轮都把最老那轮的 meta 挤掉**，数组永远停留在「错位 + 一个空洞」。
            #   连写 3 轮实测：`[轮2meta, {}, 轮3meta]`（应为 `[轮1, {}, 轮3]`）。
            #   用户侧现象：刷新后只有第一轮有引用资料，后面几轮全空。
            #
            # 放在 append 之前，补齐的对象就是「历史遗留的稀疏数组」，
            # 补完再 append 本轮，长度恰好等于新轮数 —— 索引语义才成立。
            self._normalize_meta(snapshot)
            snapshot.messages.append({"role": "user", "content": question})
            snapshot.messages.append({"role": "assistant", "content": answer})
            # 元数据**必须与轮对齐**：没传 meta 的轮也占一个空位。
            # 为什么：历史接口是按 `轮号 = 消息下标 // 2` 回填元数据的，
            # 如果只在「有 meta 时」append，一旦中间某轮没带 meta，
            # 后面所有轮的元数据都会整体前移错配（引用资料显示到别的问题上）。
            # 空 dict 占位把「稀疏数组」变成「等长数组」，索引语义才成立。
            snapshot.exchange_meta.append(dict(meta) if meta is not None else {})
            self._trim(snapshot)
            snapshot.last_active = time.time()
            ok = self._save(session_id, snapshot)
        if ok:
            logger.info("记忆已更新 | session_id=%s 历史条数=%d", session_id, len(snapshot.messages))

    def clear_session(self, session_id: str) -> bool:
        """
        清空某个会话的全部记忆。

        :return: 会话存在并清除成功返回 True；会话本就不存在返回 False
        """
        return self.store.delete(session_id)

    def truncate_session(self, session_id: str, keep_messages: int) -> bool:
        """
        把会话历史截断到前 keep_messages 条消息（编辑重发用）。

        前端「编辑某条历史提问并重新发送」的语义是：该提问及其后的所有
        消息作废、从改写后的问题重新开始。所以截断点必须是**偶数**
        （一轮的边界），否则会留下半轮残缺对话。keep_messages 为奇数时
        向下取偶——宁可少保留半轮，也不留「只有问没有答」的脏历史。

        :param keep_messages: 保留前 N 条消息（N 为该轮 user 消息在消息数组中的下标）
        :return: 会话存在返回 True（无论是否真裁了）；不存在返回 False
        """
        with self.store.session_lock(session_id):
            snapshot = self._load(session_id)
            if snapshot is None:
                return False
            keep = max(0, int(keep_messages))
            keep -= keep % 2  # 轮边界对齐
            if keep < len(snapshot.messages):
                snapshot.messages = snapshot.messages[:keep]
                if snapshot.exchange_meta:
                    del snapshot.exchange_meta[keep // 2:]
                snapshot.last_active = time.time()
                self._save(session_id, snapshot)
        logger.info("会话已截断 | session_id=%s 保留 %d 条消息", session_id, keep)
        return True

    def get_exchange_meta(self, session_id: str) -> list[dict[str, Any]]:
        """取某会话每轮问答的展示元数据（深拷贝语义：返回每轮 dict 的副本）。"""
        snapshot = self._load(session_id)
        if snapshot is None:
            return []
        return [dict(m) for m in snapshot.exchange_meta]

    # ------------------------------------------------------------------ #
    # 会话管理元数据（置顶 / 自定义标题）
    # ------------------------------------------------------------------ #
    def update_session_meta(
        self,
        session_id: str,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any] | None:
        """
        更新会话管理元数据（重命名 / 置顶），返回更新后的元数据。

        :return: 会话存在返回更新后的 meta dict；不存在返回 None
                 （接口层据此返回 404，而不是悄悄造一个空会话出来）
        """
        with self.store.session_lock(session_id):
            snapshot = self._load(session_id)
            if snapshot is None:
                return None
            meta = snapshot.session_meta or {"pinned": False}
            if title is not None:
                meta["title"] = title.strip()[:60] or meta.get("title")
            if pinned is not None:
                meta["pinned"] = bool(pinned)
            snapshot.session_meta = meta
            snapshot.last_active = time.time()
            self._save(session_id, snapshot)
            return dict(meta)

    def get_session_meta(self, session_id: str) -> dict[str, Any]:
        """取会话管理元数据（无记录时返回默认值）。"""
        snapshot = self._load(session_id)
        if snapshot is None or not snapshot.session_meta:
            return {"pinned": False}
        return dict(snapshot.session_meta)

    # ------------------------------------------------------------------ #
    # token 用量统计
    # ------------------------------------------------------------------ #
    # 统计口径：每次 LLM 调用（重写 + 主回答）的 usage_metadata 累加进会话。
    # 为什么放 MemoryManager 而不是接口层：用量与会话同生命周期（清空/过期回收），
    # 且 MemoryManager 本来就是「按 session_id 隔离的状态」的归宿。
    def add_usage(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
    ) -> None:
        """累加一次问答的 token 用量（一次问答可能含多次 LLM 调用，由调用方合并后传入）。"""
        with self.store.session_lock(session_id):
            snapshot = self._load(session_id) or SessionSnapshot()
            acc = dict(self._USAGE_ZERO)
            acc.update(snapshot.usage or {})
            acc["input_tokens"] += int(input_tokens)
            acc["output_tokens"] += int(output_tokens)
            acc["cache_read_tokens"] += int(cache_read_tokens)
            acc["requests"] += 1
            snapshot.usage = acc
            self._save(session_id, snapshot)

    def get_usage(self, session_id: str) -> dict[str, int]:
        """取某个会话的累计 token 用量（无记录时返回全零副本）。"""
        snapshot = self._load(session_id)
        if snapshot is None or not snapshot.usage:
            return dict(self._USAGE_ZERO)
        merged = dict(self._USAGE_ZERO)
        merged.update({k: int(v) for k, v in snapshot.usage.items() if k in merged})
        return merged

    # ------------------------------------------------------------------ #
    # 运维接口
    # ------------------------------------------------------------------ #
    def session_count(self) -> int:
        """当前存活会话数（接口层/健康检查用）。"""
        stats = self.store.stats()
        count = stats.get("session_count")
        return int(count) if isinstance(count, int) else len(self.store.list_ids())

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        列出全部会话及其基本信息（运维/接口层用，不含消息正文）。

        注意这里用 touch=False 读：翻一下会话列表不应该把所有会话的 TTL 都续上，
        否则「闲置过期」在有人反复刷新列表的场景下形同虚设。
        """
        items: list[dict[str, Any]] = []
        for session_id in self.store.list_ids():
            snapshot = self._load(session_id, touch=False)
            if snapshot is None:
                continue
            items.append({
                "session_id": session_id,
                "message_count": len(snapshot.messages),
                "last_active": snapshot.last_active,
                "usage": dict(snapshot.usage or self._USAGE_ZERO),
                "pinned": bool((snapshot.session_meta or {}).get("pinned", False)),
                "title": (snapshot.session_meta or {}).get("title"),
            })
        return items

    def cleanup_expired(self) -> int:
        """
        主动清理过期会话（惰性清理之外的兜底，内存预警高压时也会被调）。

        :return: 被清理的数量（内存版是删掉的会话数；Redis 版是摘掉的索引项数，
                 key 本身由 Redis 的 TTL 自己回收）
        """
        return self.store.purge_expired()

    def memory_report(self) -> dict[str, Any]:
        """
        记忆子系统的完整运行报告（``GET /api/v1/system/memory`` 的数据源）。

        为什么放这里而不是接口层：接口层不该知道「后端是 redis 还是 memory」
        以及「Redis 内存怎么查」这些细节，它只要一份能直接展示的报告。
        """
        report: dict[str, Any] = {
            "backend": self.store.name,
            "max_turns": self.max_turns,
            "ttl_seconds": self.ttl_seconds,
            "max_session_bytes": int(settings.MEMORY_MAX_SESSION_BYTES),
            "degrade_on_error": bool(settings.MEMORY_DEGRADE_ON_ERROR),
            "store": self.store.stats(),
            "health": self.store.health(),
        }
        if self.store.name == "redis":
            from core.redis_monitor import get_redis_monitor

            report["redis_memory"] = get_redis_monitor().inspect()
        return report


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_memory_manager: MemoryManager | None = None
_memory_manager_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    """获取全局唯一的记忆管理器（双检锁，与项目内其他单例同风格）。"""
    global _memory_manager
    if _memory_manager is None:
        with _memory_manager_lock:
            if _memory_manager is None:
                _memory_manager = MemoryManager()
                logger.debug("MemoryManager 单例已创建 | 后端=%s", _memory_manager.store.name)
    return _memory_manager


def reset_memory_manager() -> None:
    """重置单例（测试切换配置时用）。"""
    global _memory_manager
    with _memory_manager_lock:
        _memory_manager = None
        logger.info("MemoryManager 单例已重置")
