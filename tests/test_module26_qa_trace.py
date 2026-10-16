# pyright: basic
"""
P4-5 问答流程回溯（Q&A Trace）测试

运行：
    .venv/bin/python tests/test_module26_qa_trace.py   （或 make test-sandbox s=26）

覆盖点：
1. `_build_qa_trace` 纯函数：三个非缓存终端（chitchat / zero_recall / rag_qa）
   的 decision、意图、检索字段；且**与 `_retrieval_debug_event` 同源**
   （流式看到的和落库存的必须是同一份数据 —— 靠同构断言防两处漂移）。
2. `_cache_qa_trace`：cache_hit 终端形态（无候选、intent_source=cache）。
3. `retrieve(trace_out=...)`：按请求采集轨迹；重排关时 `rerank=None`；
   重排开时轨迹来自**本次**调用（桩重排器写 last_trace）。
4. `_prepare_parallel`：把本次检索轨迹透传进 prepared["retrieval_trace"]；chitchat 不检索。
5. 端到端 `query` / `stream`：四个终端状态的 qa_trace 都随轮落库（get_exchange_meta 可读）。

说明：全程不联网、不连 MySQL/Redis（检索/记忆/LLM 全用桩或进程内实现）。
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

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


from langchain_core.documents import Document

from core.intent_router import Intent, IntentResult
from core.memory_manager import MemoryManager
from core.rag_chain import (
    RAGChain,
    _build_qa_trace,
    _cache_qa_trace,
    _retrieval_debug_event,
    _usage_zero,
)
from core.session_store import MemorySessionStore

STUB_TRACE = {
    "threshold": 0.5,
    "top1_before_filter": 0.4321,
    "filtered_count": 2,
    "candidates": [
        {"chunk_id": "1:0", "file_name": "a.md", "rerank_score": 0.6, "passed_threshold": True},
        {"chunk_id": "1:1", "file_name": "b.md", "rerank_score": 0.4, "passed_threshold": False},
    ],
}


# --------------------------------------------------------------------------- #
# 第 1 组：_build_qa_trace 纯函数
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：_build_qa_trace ==")
ir = IntentResult(intent=Intent.POLICY_CONSULT, source="rule")


def _prepared(docs, rerank_trace):
    return {
        "standalone_question": "年假有几天？",
        "docs": docs,
        "context": "ctx",
        "rewrite_usage": _usage_zero(),
        "vector_query": None,
        "retrieval_trace": {"rerank": rerank_trace, "hybrid": None},
    }


# rag_qa：有文档、有轨迹
t_rag = _build_qa_trace(
    "年假有几天？", ir, _prepared([Document(page_content="x", metadata={})], STUB_TRACE),
    "rag_qa", 12.3, 1,
)
check("rag_qa：decision 正确", t_rag["decision"] == "rag_qa", f"实际 {t_rag['decision']}")
check("rag_qa：意图/路由原样", t_rag["intent"] == "policy_consult" and t_rag["route"] == "rag_qa",
      f"实际 {t_rag['intent']}/{t_rag['route']}")
check("rag_qa：intent_source 透传", t_rag["intent_source"] == "rule")
check("rag_qa：阈值带过来", t_rag["threshold"] == 0.5, f"实际 {t_rag['threshold']}")
check("rag_qa：top1 带过来", t_rag["top1_before_filter"] == 0.4321)
check("rag_qa：候选含被过滤项", [c["passed_threshold"] for c in t_rag["candidates"]] == [True, False])
check("rag_qa：elapsed_ms/sources_count", t_rag["elapsed_ms"] == 12.3 and t_rag["sources_count"] == 1)
check("rag_qa：rejected=False", t_rag["rejected"] is False)
check("rag_qa：hyde_used=False（vector_query 为 None）", t_rag["hyde_used"] is False)

# 与调试事件同源（同一份检索数据）
_ev = _retrieval_debug_event(ir, _prepared([Document(page_content="x", metadata={})], STUB_TRACE),
                             STUB_TRACE, None)
_same_keys = ("candidates", "threshold", "top1_before_filter", "filtered_count",
              "docs_returned", "bm25_hits", "bm25_only", "rrf_top")
check("落库轨迹与流式调试事件字段同源",
      all(t_rag[k] == _ev[k] for k in _same_keys),
      f"差异键={[k for k in _same_keys if t_rag[k] != _ev[k]]}")

# zero_recall：有 prepared 但 docs 为空
t_zero = _build_qa_trace("年假有几天？", ir, _prepared([], STUB_TRACE), "zero_recall", 5.0, 0)
check("zero_recall：decision 正确", t_zero["decision"] == "zero_recall")
check("zero_recall：rejected=True", t_zero["rejected"] is True)
check("zero_recall：docs_returned=0", t_zero["docs_returned"] == 0)
check("zero_recall：仍带 top1（拒答证据）", t_zero["top1_before_filter"] == 0.4321)

# chitchat：prepared=None（压根没检索）
t_chit = _build_qa_trace("你好", IntentResult(intent=Intent.CHITCHAT, source="rule"), None,
                         "chitchat", 2.0, 0)
check("chitchat：decision 正确", t_chit["decision"] == "chitchat")
check("chitchat：无候选", t_chit["candidates"] == [])
check("chitchat：阈值/top1 为 None（没跑检索，不是 0）",
      t_chit["threshold"] is None and t_chit["top1_before_filter"] is None)
check("chitchat：route=chitchat", t_chit["route"] == "chitchat")


# --------------------------------------------------------------------------- #
# 第 2 组：_cache_qa_trace
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：_cache_qa_trace ==")
t_cache = _cache_qa_trace("问题", "knowledge_query", "rag_qa", 3.0, 2)
check("cache：decision=cache_hit", t_cache["decision"] == "cache_hit")
check("cache：intent_source=cache", t_cache["intent_source"] == "cache")
check("cache：无候选/无 top1", t_cache["candidates"] == [] and t_cache["top1_before_filter"] is None)
check("cache：rejected=False", t_cache["rejected"] is False)
check("cache：docs_returned=sources_count", t_cache["docs_returned"] == 2)


# --------------------------------------------------------------------------- #
# 第 3 组：retrieve(trace_out=...) 按请求采集轨迹
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：retrieve 轨迹采集 ==")
import core.retriever as rt
from core.retriever import RAGRetriever


class _FakeVectorStore:
    store_type = "fake"

    def __init__(self, pairs):
        self._pairs = pairs

    def similarity_search_with_score(self, query, k=4, filter_dict=None):
        return list(self._pairs)[:k]


class _StubReranker:
    """rerank(query, documents, top_k=None) —— 与 module20 打桩器同签名。"""

    def __init__(self):
        self.score_threshold = None
        self.last_trace = None

    def rerank(self, query, documents, top_k=None):
        scored = [(d, round(0.9 - i * 0.1, 6)) for i, d in enumerate(documents)]
        self.last_trace = {
            "threshold": self.score_threshold,
            "top1_before_filter": scored[0][1] if scored else None,
            "filtered_count": 0,
            "candidates": [
                {"chunk_id": None, "file_name": None, "rerank_score": s, "passed_threshold": True}
                for _, s in scored
            ],
        }
        return scored[:top_k] if top_k else scored


_pairs = [(Document(page_content="x", metadata={"doc_id": 1, "chunk_index": 0}), 0.5)]

# 3a. 重排关闭：rerank 轨迹为 None（没有阈值过滤，就没有「差多少分」）
r_off = RAGRetriever(vector_store=_FakeVectorStore(_pairs), use_reranker=False, top_k=2)
tr_off: dict = {}
docs_off = r_off.retrieve("问题", trace_out=tr_off)
check("重排关闭：返回 docs", len(docs_off) == 1)
check("重排关闭：rerank 轨迹为 None", tr_off.get("rerank") is None, f"实际 {tr_off.get('rerank')!r}")
check("重排关闭：hybrid 轨迹为 None", tr_off.get("hybrid") is None)

# 3b. 重排开启（注入桩重排器 + 强制 rerank_enabled）：轨迹来自本次调用
r_on = RAGRetriever(vector_store=_FakeVectorStore(_pairs), use_reranker=False, top_k=2)
r_on.reranker = _StubReranker()
r_on._threshold_override = 0.5
_orig_flag = rt.flag_value
rt.flag_value = lambda *a, **k: True  # type: ignore[assignment]
try:
    tr_on: dict = {}
    docs_on = r_on.retrieve("问题", trace_out=tr_on)
finally:
    rt.flag_value = _orig_flag
check("重排开启：返回 docs", len(docs_on) == 1)
check("重排开启：rerank 轨迹带 top1", (tr_on.get("rerank") or {}).get("top1_before_filter") == 0.9,
      f"实际 {tr_on.get('rerank')!r}")
check("重排开启：rerank 轨迹带阈值", (tr_on.get("rerank") or {}).get("threshold") == 0.5)
check("不传 trace_out 时行为不变（返回仍是 docs 列表）",
      isinstance(r_on.retrieve("问题"), list))


# --------------------------------------------------------------------------- #
# 第 4 组：桩件（RAGChain 端到端）
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：_prepare_parallel 透传 + 端到端落库 ==")
import core.rag_chain as rc

# P4-5b：镜像写入走桩 —— 本模块不碰 MySQL（真库不该被测试轨迹污染）；
# 「镜像行的内容与筛选」由 module27 对 qa_trace_repo 的专项测试守。
_TRACE_MIRROR: list[dict] = []
_orig_record_qa_trace = rc._record_qa_trace
rc._record_qa_trace = lambda session_id, owner_id, trace: (  # type: ignore[assignment]
    _TRACE_MIRROR.append({
        "session_id": session_id, "owner_id": owner_id,
        "decision": (trace or {}).get("decision"),
    })
)


class _FakeLLM:
    def invoke(self, _pv):
        return SimpleNamespace(content="答案。", usage_metadata=None)

    def stream(self, _pv):
        yield SimpleNamespace(content="答案。", usage_metadata=None)


class _FakeLLMClient:
    provider = "fake"
    model_name = "fake-model"

    def get_llm(self):
        return _FakeLLM()

    def get_model_info(self) -> dict:
        return {"provider": "fake"}


class _StubRetriever:
    """支持 trace_out 出参的检索器桩：把本实例的轨迹写进调用方那份 dict。"""

    def __init__(self, docs, rerank_trace=None, hybrid_trace=None):
        self._docs = list(docs)
        self._rerank_trace = rerank_trace
        self._hybrid_trace = hybrid_trace
        self.calls: list[dict] = []
        self.reranker = object() if rerank_trace is not None else None

    def retrieve(self, query, top_k=None, filter_dict=None, return_score=False,
                 vector_query=None, trace_out=None):
        self.calls.append({"query": query, "vector_query": vector_query, "trace_out": trace_out})
        if trace_out is not None:
            trace_out["rerank"] = self._rerank_trace
            trace_out["hybrid"] = self._hybrid_trace
        return list(self._docs)

    def get_last_rerank_trace(self):
        return self._rerank_trace

    def get_last_hybrid_trace(self):
        return self._hybrid_trace


class _C:
    def __init__(self, intent):
        self._intent = intent

    def classify(self, q):
        return IntentResult(intent=self._intent, source="rule")


def _mk_chain(docs, rerank_trace=None):
    stub = _StubRetriever(docs, rerank_trace=rerank_trace)
    chain = RAGChain(
        retriever=stub,
        llm_client=_FakeLLMClient(),
        memory=MemoryManager(max_turns=5, ttl_seconds=3600, store=MemorySessionStore(ttl_seconds=3600)),
    )
    chain._maybe_store_cache = lambda *a, **k: None  # type: ignore[method-assign]
    return chain, stub


_orig_gic = rc.get_intent_classifier
try:
    # 4a. _prepare_parallel：轨迹透传进 prepared；chitchat 不检索
    rc.get_intent_classifier = lambda: _C(Intent.POLICY_CONSULT)
    chain, stub = _mk_chain([Document(page_content="x", metadata={})], rerank_trace=STUB_TRACE)
    _intent, prepared = chain._prepare_parallel("年假有几天？", [])
    check("_prepare_parallel 把轨迹放进 retrieval_trace",
          (prepared or {}).get("retrieval_trace", {}).get("rerank") is not None)
    check("检索器收到了 trace_out 出参",
          stub.calls and stub.calls[0]["trace_out"] is prepared["retrieval_trace"])

    rc.get_intent_classifier = lambda: _C(Intent.CHITCHAT)
    stub.calls.clear()
    _i2, prep2 = chain._prepare_parallel("你好", [])
    check("chitchat：prepared=None（不检索）", prep2 is None)

    # 4b. rag_qa 端到端
    rc.get_intent_classifier = lambda: _C(Intent.POLICY_CONSULT)
    chain, _ = _mk_chain(
        [Document(page_content="x", metadata={"doc_id": 1, "chunk_index": 0, "file_name": "a.md"})],
        rerank_trace=STUB_TRACE,
    )
    chain.query("年假有几天？", "s-rag", owner_id=None)
    meta_rag = chain.memory.get_exchange_meta("s-rag", owner_id=None)[-1]
    qt = meta_rag.get("qa_trace") or {}
    check("query(rag_qa)：落库 decision=rag_qa", qt.get("decision") == "rag_qa", f"实际 {qt.get('decision')}")
    check("query(rag_qa)：落库带候选与 top1",
          len(qt.get("candidates") or []) == 2 and qt.get("top1_before_filter") == 0.4321)
    check("query(rag_qa)：落库 intent/route 正确",
          qt.get("intent") == "policy_consult" and qt.get("route") == "rag_qa")

    # 4c. zero_recall 端到端（docs 为空 → 拒答短路）
    chain, _ = _mk_chain([], rerank_trace=STUB_TRACE)
    chain.query("年假有几天？", "s-zero", owner_id=None)
    qt_zero = chain.memory.get_exchange_meta("s-zero", owner_id=None)[-1].get("qa_trace") or {}
    check("query(zero_recall)：落库 decision=zero_recall", qt_zero.get("decision") == "zero_recall")
    check("query(zero_recall)：rejected=True", qt_zero.get("rejected") is True)

    # 4d. chitchat 端到端
    rc.get_intent_classifier = lambda: _C(Intent.CHITCHAT)
    chain, _ = _mk_chain([Document(page_content="x", metadata={})], rerank_trace=None)
    chain.query("你好", "s-chit", owner_id=None)
    qt_chit = chain.memory.get_exchange_meta("s-chit", owner_id=None)[-1].get("qa_trace") or {}
    check("query(chitchat)：落库 decision=chitchat", qt_chit.get("decision") == "chitchat")
    check("query(chitchat)：无候选", qt_chit.get("candidates") == [])

    # 4e. cache_hit 落库（直接调 _finish_cached，不依赖 Redis）
    chain, _ = _mk_chain([Document(page_content="x", metadata={})])
    chain._finish_cached(
        "问题", "s-cache", time.perf_counter(),
        {"answer": "a", "sources": [], "intent": "knowledge_query",
         "route": "rag_qa", "standalone_question": "问题"},
        owner_id=None,
    )
    qt_cache = chain.memory.get_exchange_meta("s-cache", owner_id=None)[-1].get("qa_trace") or {}
    check("_finish_cached：落库 decision=cache_hit", qt_cache.get("decision") == "cache_hit")
    check("_finish_cached：intent_source=cache", qt_cache.get("intent_source") == "cache")

    # 4f. stream 路径同样落库（历史接口按轮读同一份）
    rc.get_intent_classifier = lambda: _C(Intent.POLICY_CONSULT)
    chain, _ = _mk_chain(
        [Document(page_content="x", metadata={"doc_id": 1, "chunk_index": 0, "file_name": "a.md"})],
        rerank_trace=STUB_TRACE,
    )
    events = list(chain.stream("年假有几天？", "s-stream", owner_id=None, debug=True))
    types = [e["type"] for e in events]
    check("stream：debug 帧序 meta→retrieval→chunk→done",
          types[0] == "meta" and "retrieval" in types and types[-1] == "done", f"实际 {types}")
    _retr = next(e for e in events if e["type"] == "retrieval")
    check("stream：调试帧 top1 来自本次轨迹", _retr["top1_before_filter"] == 0.4321)
    qt_stream = chain.memory.get_exchange_meta("s-stream", owner_id=None)[-1].get("qa_trace") or {}
    check("stream：落库 decision=rag_qa", qt_stream.get("decision") == "rag_qa")
    check("stream：落库与调试帧同源（top1 一致）",
          qt_stream.get("top1_before_filter") == _retr["top1_before_filter"])

    # 4g. 镜像调用（P4-5b）：4b~4f 共 5 轮，每轮恰好一条、decision 与落库一致
    by_session = {m["session_id"]: m["decision"] for m in _TRACE_MIRROR}
    check("镜像：五轮各一条且 decision 正确",
          by_session == {"s-rag": "rag_qa", "s-zero": "zero_recall",
                         "s-chit": "chitchat", "s-cache": "cache_hit",
                         "s-stream": "rag_qa"},
          f"实际 {by_session}")
    check("镜像：条数没有多余（无重复镜像）", len(_TRACE_MIRROR) == 5,
          f"实际 {len(_TRACE_MIRROR)}")
    check("镜像：owner_id 透传", all(m["owner_id"] is None for m in _TRACE_MIRROR))
finally:
    rc.get_intent_classifier = _orig_gic
    rc._record_qa_trace = _orig_record_qa_trace  # type: ignore[assignment]



# --------------------------------------------------------------------------- #
# 第 5 组：改写重试（P4-7b）——「那 X 呢？」被原样返回 = 改写失败，必须触发重试
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：改写重试（指代问句原样返回 = 失败）==")


class _ScriptedLLM:
    """按脚本依次返回内容的假 LLM；记录每次收到的完整 prompt。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def invoke(self, pv):
        self.prompts.append(str(pv))
        return SimpleNamespace(content=self.outputs.pop(0), usage_metadata=None)

    def stream(self, _pv):
        yield SimpleNamespace(content="", usage_metadata=None)


class _ScriptedClient:
    provider = "fake"
    model_name = "fake"

    def __init__(self, llm):
        self._llm = llm

    def get_llm(self):
        # rewrite_query 走 (prompt | llm) 管道，llm 必须是 Runnable；
        # 用 RunnableLambda 包一层，返回值保持 SimpleNamespace(.content) 形态
        from langchain_core.runnables import RunnableLambda

        def _invoke(pv):
            self._llm.prompts.append(str(pv))
            return SimpleNamespace(content=self._llm.outputs.pop(0), usage_metadata=None)

        return RunnableLambda(_invoke)

    def get_model_info(self):
        return {"provider": "fake"}


_orig_glc = rc.get_llm_client
try:
    # 5a. 指代问句 + 模型偷懒原样返回 → 必须触发带强化指令的重试
    llm = _ScriptedLLM(["那病假呢？", "病假如何申请？"])
    rc.get_llm_client = lambda: _ScriptedClient(llm)
    out = rc.rewrite_query(
        "那病假呢？",
        chat_history=[("human", "怎么请事假？"), ("ai", "……请假申请单流程……")],
    )
    check("指代问句原样返回触发重试，第二次拿到改写结果",
          out == "病假如何申请？", f"实际 {out!r}")
    check("重试确实发生（LLM 被调 2 次）", len(llm.prompts) == 2, f"实际 {len(llm.prompts)}")
    check("重试带强化指令（prompt 含『必须消解指代』）",
          len(llm.prompts) == 2 and "必须消解指代" in llm.prompts[1])

    # 5b. 非指代问题原样返回 = 合法（本就独立），不重试
    llm2 = _ScriptedLLM(["怎么请事假"])
    rc.get_llm_client = lambda: _ScriptedClient(llm2)
    out2 = rc.rewrite_query("怎么请事假", chat_history=[])
    check("非指代问题原样返回不重试（只调 1 次）",
          out2 == "怎么请事假" and len(llm2.prompts) == 1,
          f"实际 {out2!r} 调用 {len(llm2.prompts)} 次")

    # 5c. 指代问句 + 模型一次改写到位 → 不重试
    llm3 = _ScriptedLLM(["病假如何申请？"])
    rc.get_llm_client = lambda: _ScriptedClient(llm3)
    out3 = rc.rewrite_query("那病假呢？", chat_history=[("human", "怎么请事假？")])
    check("正常改写不触发重试", out3 == "病假如何申请？" and len(llm3.prompts) == 1)
finally:
    rc.get_llm_client = _orig_glc


print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
sys.exit(1 if FAIL else 0)
