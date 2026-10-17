"""module29 —— P4-10：会话滚动摘要（窗口之外不再失忆）。

覆盖：
    1. 边界：未溢出不调 LLM；溢出才压「新滑出的那部分」（增量）；
    2. fail-open：LLM 抛异常 / 返回空 → 不抛、旧摘要原样保留；
    3. 读取侧：get_recent_messages 前置 SystemMessage 且窗口条数不变；
       无摘要时行为与旧版完全一致（不多塞任何消息）；
    4. 全量历史仍在（摘要只影响送进模型的窗口，不删库）；
    5. 钩子：开关关 → 不调 LLM。

清理锚点：MemorySessionStore（进程内），无外部依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.logging_config import setup_logging

setup_logging()

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


from core.memory_manager import MemoryManager  # noqa: E402
from core.session_store import MemorySessionStore  # noqa: E402
from core.session_summary import summarize_overflow  # noqa: E402
import core.session_summary as ss  # noqa: E402


class _StubLLM:
    """记录每次 invoke 的 prompt，按脚本返回摘要文本。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.exc: Exception | None = None

    def invoke(self, pv):
        self.prompts.append(str(pv))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.outputs.pop(0) if self.outputs else "")


class _StubClient:
    provider = "fake"
    model_name = "fake"

    def __init__(self, llm):
        self._llm = llm

    def get_llm(self):
        return self._llm

    def get_model_info(self):
        return {"provider": "fake"}


def _mk_memory(max_turns: int = 2, turns: int = 0):
    mem = MemoryManager(max_turns=max_turns, ttl_seconds=3600,
                        store=MemorySessionStore(ttl_seconds=3600))
    for i in range(turns):
        mem.add_exchange("s-summary", f"第{i}轮问题", f"第{i}轮回答", owner_id=1)
    return mem


print("== 1. 边界与增量 ==")
# 1a. 未溢出（3 轮 ≤ 窗口 2 轮？ 3 > 2 → 溢出；先用 2 轮测不溢出）
llm = _StubLLM(["不该被调用"])
import core.rag_chain as rc  # noqa: E402

_orig = rc.get_llm_client
rc.get_llm_client = lambda: _StubClient(llm)
try:
    mem = _mk_memory(max_turns=2, turns=2)
    changed = summarize_overflow(mem, "s-summary", 1)
    check("未溢出不生成摘要（不调 LLM）", changed is False and not llm.prompts,
          f"changed={changed} calls={len(llm.prompts)}")

    # 1b. 溢出：第 3 轮写入后应有 1 轮滑出窗口 → 生成摘要
    mem.add_exchange("s-summary", "第3轮问题", "第3轮回答", owner_id=1)
    llm.outputs = ["用户问过流程与扣薪标准，助手答了流程。"]
    changed = summarize_overflow(mem, "s-summary", 1)
    meta = mem.get_session_meta("s-summary", owner_id=1)
    check("溢出后生成摘要并落 session_meta",
          changed is True and meta.get("summary", "").endswith("助手答了流程。"),
          f"meta={meta}")
    check("summary_upto = 滑出窗口的消息数（6-4=2）", meta.get("summary_upto") == 2,
          f"实际 {meta.get('summary_upto')}")

    # 1c. 增量：再写一轮，只压「新滑出的」+ 旧摘要（prompt 应含旧摘要）
    mem.add_exchange("s-summary", "第4轮问题", "第4轮回答", owner_id=1)
    llm.outputs = ["合并后的摘要：流程与扣薪，另加了年假。"]
    changed = summarize_overflow(mem, "s-summary", 1)
    meta2 = mem.get_session_meta("s-summary", owner_id=1)
    check("第二次只压新增滑出部分（prompt 含已有摘要）",
          changed and "【已有摘要】" in llm.prompts[-1], f"末次 prompt 无已有摘要标记")
    check("upto 前进到 4", meta2.get("summary_upto") == 4, f"实际 {meta2.get('summary_upto')}")

    # 1d. 无新增滑出 → 不重复压（幂等）
    n_before = len(llm.prompts)
    changed = summarize_overflow(mem, "s-summary", 1)
    check("已压完则不重复调用 LLM", changed is False and len(llm.prompts) == n_before)

    # 1e. fail-open：LLM 抛异常
    llm.exc = RuntimeError("siliconflow down")
    meta_before = mem.get_session_meta("s-summary", owner_id=1)
    changed = summarize_overflow(mem, "s-summary", 1)   # 先制造新的滑出
    mem.add_exchange("s-summary", "第5轮问题", "第5轮回答", owner_id=1)
    changed = summarize_overflow(mem, "s-summary", 1)
    meta_after = mem.get_session_meta("s-summary", owner_id=1)
    check("LLM 异常 fail-open：不抛、摘要原样",
          changed is False and meta_after.get("summary") == meta_before.get("summary"),
          f"{meta_after}")
    llm.exc = None

    # 1f. fail-open：返回空
    llm.outputs = ["   "]
    mem.add_exchange("s-summary", "第6轮问题", "第6轮回答", owner_id=1)
    changed = summarize_overflow(mem, "s-summary", 1)
    check("空摘要不写入", changed is False)
finally:
    rc.get_llm_client = _orig


print("== 2. 读取侧 ==")
mem2 = _mk_memory(max_turns=2, turns=4)          # 8 条消息，窗口 4 条
snap = mem2.store.load("s-summary", owner_id=1)
snap.session_meta.update({"summary": "早先聊过请假流程", "summary_upto": 4})
mem2.store.save("s-summary", snap, owner_id=1)
recent = mem2.get_recent_messages("s-summary", owner_id=1)
check("窗口原文条数不变（4 条 + 1 摘要）", len(recent) == 5, f"实际 {len(recent)}")
check("首条是摘要 SystemMessage",
      type(recent[0]).__name__ == "SystemMessage" and "早先聊过请假流程" in str(recent[0].content),
      f"实际 {type(recent[0]).__name__} {str(recent[0].content)[:40]}")
check("窗口内仍是最近的原文", "第3轮问题" in str(recent[-2].content), f"实际 {recent[-2].content!r}")

mem3 = _mk_memory(max_turns=2, turns=4)
check("无摘要时行为与旧版一致（不多塞消息）", len(mem3.get_recent_messages("s-summary", owner_id=1)) == 4)
check("全量历史仍在（摘要不删库）", len(mem3.get_messages("s-summary", owner_id=1)) == 8)


print("== 3. 钩子与开关 ==")
from core.config_service import config_service  # noqa: E402

llm2 = _StubLLM(["摘要文本"])
rc.get_llm_client = lambda: _StubClient(llm2)
try:
    mem4 = _mk_memory(max_turns=1, turns=0)
    mem4._summarizer = ss.make_summarizer_hook(mem4)
    config_service.set_flag("session_summary", False)
    mem4.add_exchange("s-hook", "问1", "答1", owner_id=1)
    mem4.add_exchange("s-hook", "问2", "答2", owner_id=1)   # 溢出 1 轮
    check("开关关 → 钩子不调 LLM", len(llm2.prompts) == 0, f"实际 {len(llm2.prompts)}")

    config_service.set_flag("session_summary", True)
    mem4.add_exchange("s-hook", "问3", "答3", owner_id=1)
    meta4 = mem4.get_session_meta("s-hook", owner_id=1)
    # max_turns=1 → 窗口 2 条；3 轮=6 条 → 滑出 4 条（upto=4）
    check("开关开 → 写记忆后自动生成摘要",
          bool(meta4.get("summary")) and meta4.get("summary_upto") == 4, f"实际 {meta4}")
finally:
    config_service.unset_flag("session_summary")
    rc.get_llm_client = _orig


print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
sys.exit(1 if FAIL else 0)
