"""module28 —— P4-6：答案依据校验（Groundedness）。

覆盖三块：
    1. `core/groundedness.py` 单元：低置信带边界穷举 + judge 输出解析
       （裸 JSON / 代码块围栏 / 口径矛盾 / 解析失败 fail-open / 调用异常 fail-open）；
    2. rag_chain 集成：query() 在 rag_qa 轮触发校验、结果进 exchange_meta 与
       qa_trace（含镜像透传），zero_recall 轮不校验；
    3. 低置信带与 flag 联动：开关关 → 不校验（grounded=None 不进 meta）。

清理锚点：session_id `p46_` 前缀；judge 走桩 LLM，不碰真实模型。
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


# --------------------------------------------------------------------------- #
# 1. 低置信带 + judge 单元
# --------------------------------------------------------------------------- #
print("== 1. 低置信带与 judge 解析 ==")
from core.groundedness import (  # noqa: E402
    LOW_CONFIDENCE_BAND,
    check_groundedness,
    is_low_confidence,
)

check("低置信带 = (0.25, 0.35)（扫档报告的犬牙交错区）",
      LOW_CONFIDENCE_BAND == (0.25, 0.35), f"实际 {LOW_CONFIDENCE_BAND}")
check("带内下沿命中", is_low_confidence(0.25))
check("带内上沿命中", is_low_confidence(0.35))
check("带外不命中（下）", not is_low_confidence(0.2499))
check("带外不命中（上）", not is_low_confidence(0.3501))
check("None（未跑检索/未重排）恒 False", not is_low_confidence(None))
check("真命中 0.99 不算低置信", not is_low_confidence(0.99))


class _ScriptedJudgeLLM:
    """judge 专用假 LLM：按脚本依次返回原始输出，记录调用次数。"""

    def __init__(self, outputs, exc: Exception | None = None):
        self.outputs = list(outputs)
        self.exc = exc
        self.calls = 0

    def invoke(self, _pv):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.outputs.pop(0), usage_metadata=None)


class _ScriptedClient:
    provider = "fake"
    model_name = "fake"

    def __init__(self, llm):
        self._llm = llm

    def get_llm(self):
        return self._llm

    def get_model_info(self):
        return {"provider": "fake"}


_KW = dict(question="请病假扣工资吗？", context="第八条 旷工处理……", answer="病假1天内不扣工资")

# 1a. 裸 JSON
llm = _ScriptedJudgeLLM(['{"grounded": false, "unsupported": ["病假1天内不扣工资"]}'])
import core.rag_chain as rcmod  # noqa: E402

# judge 的 get_llm_client 是**调用时**从 rag_chain 取的（局部导入防环）——
# 桩打在 rag_chain 模块属性上
orig_client = rcmod.get_llm_client
rcmod.get_llm_client = lambda: _ScriptedClient(llm)
try:
    r = rcmod.check_groundedness(**_KW)
    check("裸 JSON 解析：grounded=False", r["grounded"] is False)
    check("unsupported 提取", r["unsupported"] == ["病假1天内不扣工资"])

    # 1b. 代码块围栏
    llm.outputs = ['```json\n{"grounded": true, "unsupported": []}\n```']
    r = rcmod.check_groundedness(**_KW)
    check("代码块围栏剥掉后解析成功", r["grounded"] is True and r["unsupported"] == [])

    # 1c. 口径矛盾（grounded=true 却有 unsupported）→ 保守按 false
    llm.outputs = ['{"grounded": true, "unsupported": ["某论断"]}']
    r = rcmod.check_groundedness(**_KW)
    check("口径矛盾按未通过处理（宁误报不漏报）", r["grounded"] is False)

    # 1d. 输出无法解析 → fail-open（grounded=None，不抛）
    llm.outputs = ["我觉得答案是可信的。"]
    r = rcmod.check_groundedness(**_KW)
    check("解析失败 fail-open：grounded=None", r["grounded"] is None and r["note"] != "")

    # 1e. 调用异常 → fail-open
    llm2 = _ScriptedJudgeLLM([], exc=RuntimeError("siliconflow down"))
    rcmod.get_llm_client = lambda: _ScriptedClient(llm2)
    r = rcmod.check_groundedness(**_KW)
    check("调用异常 fail-open：grounded=None", r["grounded"] is None)
finally:
    rcmod.get_llm_client = orig_client


# --------------------------------------------------------------------------- #
# 2. rag_chain 集成：rag_qa 轮校验进 meta/qa_trace；zero_recall 轮不校验
# --------------------------------------------------------------------------- #
print("== 2. rag_chain 集成 ==")
from langchain_core.documents import Document  # noqa: E402

import core.rag_chain as rc  # noqa: E402
from core.intent_router import Intent, IntentResult  # noqa: E402
from core.memory_manager import MemoryManager  # noqa: E402
from core.session_store import MemorySessionStore  # noqa: E402


class _JudgeScriptedLLM:
    """问答链路全程脚本化：rewrite（若触发）→ 主答 → judge 依次出词。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def invoke(self, _pv):
        self.calls += 1
        return SimpleNamespace(content=self.outputs.pop(0), usage_metadata=None)

    def stream(self, _pv):
        # 流式主答：消耗一个输出词位，逐字 yield
        text = self.outputs.pop(0)
        self.calls += 1
        for ch in text:
            yield SimpleNamespace(content=ch, usage_metadata=None)


class _JudgeClient:
    provider = "fake"
    model_name = "fake"

    def __init__(self, llm):
        self._llm = llm

    def get_llm(self):
        # 主答路径 llm.invoke/stream 都是直接调用，脚本对象本身就是接口
        return self._llm

    def get_model_info(self):
        return {"provider": "fake"}


class _ChainStubRetriever:
    def retrieve(self, query, top_k=None, filter_dict=None, return_score=False,
                 vector_query=None, trace_out=None):
        # low_confidence 从 retrieval_trace.rerank.top1 读（query 链路的真实来源）
        if trace_out is not None:
            trace_out["rerank"] = {"top1_before_filter": 0.9}
        return [Document(page_content="第八条 旷工半天扣除当日全部工资。",
                         metadata={"doc_id": 812, "chunk_index": 1,
                                   "file_name": "05_考勤管理制度.md",
                                   "rerank_score": 0.9})]

    def get_last_rerank_trace(self):
        return {"top1_before_filter": 0.9}

    def get_last_hybrid_trace(self):
        return None

    reranker = object()


class _ChainIntent:
    def __init__(self, intent=Intent.KNOWLEDGE_QUERY):
        self.intent = intent

    def classify(self, _q):
        return IntentResult(intent=self.intent, source="rule")


def _mk_chain(outputs):
    llm = _JudgeScriptedLLM(outputs)
    chain = rc.RAGChain(
        retriever=_ChainStubRetriever(),
        llm_client=_JudgeClient(llm),
        memory=MemoryManager(max_turns=5, ttl_seconds=3600,
                             store=MemorySessionStore(ttl_seconds=3600)),
    )
    return chain, llm


_orig_glc = rc.get_llm_client
_orig_classifier = rc.get_intent_classifier
try:
    # judge 的 get_llm_client 也从 rag_chain 模块属性取（局部导入防环）——
    # 指到与问答链路同一个脚本化客户端，输出序列才能对上
    # 2a. rag_qa 轮：answer → judge → meta/qa_trace 带校验结果
    rc.get_intent_classifier = lambda: _ChainIntent(Intent.KNOWLEDGE_QUERY)
    # 输出脚本：[rewrite（空 history 不触发）→ 主答 → judge JSON]
    chain, llm = _mk_chain([
        "病假1天内不扣工资。依据：05_考勤管理制度.md 第八条",
        '{"grounded": false, "unsupported": ["病假1天内不扣工资"]}',
    ])
    rc.get_llm_client = lambda: _JudgeClient(llm)
    res = chain.query("那病假呢？", "p46-rag", owner_id=None)
    check("query 返回 grounded 结果", res["grounded"] is not None
          and res["grounded"]["grounded"] is False,
          f"实际 {res.get('grounded')!r}")
    check("low_confidence=False（top1=0.9 不在带内）", res["low_confidence"] is False)
    meta = chain.memory.get_exchange_meta("p46-rag", owner_id=None)[-1]
    check("exchange_meta 带 grounded", (meta.get("grounded") or {}).get("grounded") is False)
    qt = meta.get("qa_trace") or {}
    check("qa_trace 带 grounded/unsupported",
          qt.get("grounded") is False and qt.get("unsupported") == ["病假1天内不扣工资"],
          f"实际 grounded={qt.get('grounded')!r} unsupported={qt.get('unsupported')!r}")

    # 2b. zero_recall 轮：不校验（grounded 不进 meta）
    rc.get_intent_classifier = lambda: _ChainIntent(Intent.POLICY_CONSULT)
    chain2, llm2 = _mk_chain(["根据现有制度未查到相关规定。"])
    rc.get_llm_client = lambda: _JudgeClient(llm2)
    # docs 为空 → zero_recall；llm 不该被 judge 消耗
    chain2.retriever.retrieve = lambda *a, **k: []  # type: ignore[assignment]
    res2 = chain2.query("那调休呢？", "p46-zero", owner_id=None)
    check("zero_recall 轮不校验（grounded=None）", res2["grounded"] is None)
    meta2 = chain2.memory.get_exchange_meta("p46-zero", owner_id=None)[-1]
    check("zero_recall 轮 meta 无 grounded", meta2.get("grounded") is None)

    # 2c. 开关关：rag_qa 轮也不校验
    rc.get_intent_classifier = lambda: _ChainIntent(Intent.KNOWLEDGE_QUERY)
    chain3, llm3 = _mk_chain(["病假1天内不扣工资。"])
    rc.get_llm_client = lambda: _JudgeClient(llm3)
    from core.config_service import config_service  # noqa: E402

    config_service.set_flag("groundedness_check", False)
    try:
        res3 = chain3.query("那病假呢？", "p46-off", owner_id=None)
        check("开关关：rag_qa 轮不校验", res3["grounded"] is None)
    finally:
        config_service.unset_flag("groundedness_check")

    # 2d. 低置信带：top1=0.30 → low_confidence=True 进 meta
    chain4, llm4 = _mk_chain([
        "病假1天内不扣工资。",
        '{"grounded": true, "unsupported": []}',
    ])
    rc.get_llm_client = lambda: _JudgeClient(llm4)
    def _low_retrieve(query, top_k=None, filter_dict=None, return_score=False,
                      vector_query=None, trace_out=None):
        if trace_out is not None:
            trace_out["rerank"] = {"top1_before_filter": 0.30}
        return [Document(page_content="第八条 旷工半天扣除当日全部工资。",
                         metadata={"doc_id": 812, "chunk_index": 1,
                                   "file_name": "05_考勤管理制度.md",
                                   "rerank_score": 0.30})]

    chain4.retriever.retrieve = _low_retrieve  # type: ignore[assignment]
    res4 = chain4.query("那病假呢？", "p46-low", owner_id=None)
    check("top1=0.30 挂低置信", res4["low_confidence"] is True,
          f"实际 {res4.get('low_confidence')!r}")
finally:
    rc.get_llm_client = _orig_glc
    rc.get_intent_classifier = _orig_classifier



# --------------------------------------------------------------------------- #
# 第 3 组：更正块（P4-6b）——未通过/自认未查到 → 据实追加更正并落库
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：更正块 ==")
from core.groundedness import (  # noqa: E402
    answer_admits_no_basis,
    append_groundedness_correction,
)

check("依据行自认未查到被识别", answer_admits_no_basis("……依据：未查到"))
check("正常依据行不误伤", not answer_admits_no_basis("……依据：05_考勤管理制度.md 第八条"))
check("无依据行不误伤", not answer_admits_no_basis("病假1天内不扣工资。"))

_g = {"grounded": False, "unsupported": ["病假1天内不扣工资"]}
corrected = append_groundedness_correction("编造的回答。", _g)
check("更正块含勿采信警示", "请勿采信" in corrected)
check("更正块含未支撑论断清单", "病假1天内不扣工资" in corrected)
check("更正块含补充资料建议", "请补充对应制度文档" in corrected)
check("grounded=True 不加更正", append_groundedness_correction("好的回答。", {"grounded": True}) == "好的回答。")
check("grounded=None 不加更正", append_groundedness_correction("回答。", None) == "回答。")

# 集成：judge 判 false → 最终答案含更正块且落库一致
rc.get_intent_classifier = lambda: _ChainIntent(Intent.KNOWLEDGE_QUERY)
chain5, llm5 = _mk_chain([
    "病假1天内不扣工资。依据：未查到",
    '{"grounded": true, "unsupported": []}',   # judge 被骗，但模型自认未查到要兜住
])
rc.get_llm_client = lambda: _JudgeClient(llm5)
res5 = chain5.query("那病假呢？", "p46-corr", owner_id=None)
check("judge 被骗但模型自认未查到 → 强制未通过",
      res5["grounded"] is not None and res5["grounded"]["grounded"] is False,
      f"实际 {res5.get('grounded')!r}")
check("最终答案含更正块", "请勿采信" in res5["answer"] and "请补充对应制度文档" in res5["answer"])
_last = chain5.memory.get_messages("p46-corr", owner_id=None)[-1]
check("落库的 assistant 消息与返回一致（含更正块）", "请勿采信" in str(_last.content))


print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
sys.exit(1 if FAIL else 0)
