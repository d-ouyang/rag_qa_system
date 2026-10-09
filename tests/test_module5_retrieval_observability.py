# pyright: basic
"""
P2-23 检索可观测性测试：拒答轨迹 + NDJSON 调试事件

运行：
    .venv/bin/python tests/test_module5_retrieval_observability.py   （或 make test-sandbox s=5）

覆盖点：
1. _rerank_trace 纯函数：过滤前 top1 / 被阈值过滤条数 / 候选 passed 标记
2. _candidate_brief 与 rag_chain._resolve_chunk_id 的 chunk_id 判定**同构**
   （两处刻意不互相 import，靠本组断言防漂移）
3. CrossEncoderReranker（注入假模型，不联网）：last_trace 落盘 + 重排完成
   日志行带「过滤前top1=」「阈值过滤=」；全过滤时 top1 不丢
4. _log_rejection 结构化拒答日志（带/不带轨迹两种口径）
5. RAGChain.stream 的 debug 事件：debug=False 零可见变化；debug=True 在
   chunk 前下发 retrieval 帧；拒答场景同样下发且 top1 来自轨迹
6. AskRequest.debug 字段契约

说明：全程不联网（重排模型注入桩、意图/检索/记忆用桩或进程内实现）。
"""
import logging
import sys
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


class _LogCapture(logging.Handler):
    """捕获指定 logger 的记录，供日志行断言。"""

    def __init__(self, logger_name: str) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.__logger = logging.getLogger(logger_name)
        self.__logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def close(self) -> None:
        self.__logger.removeHandler(self)
        super().close()


# --------------------------------------------------------------------------- #
# 第 1 组：_rerank_trace 纯函数
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：_rerank_trace ==")
from langchain_core.documents import Document

from core.retriever import _candidate_brief, _rerank_trace

docs = [
    Document(page_content=f"正文{i}", metadata={"doc_id": 1, "chunk_index": i, "file_name": f"f{i}.md"})
    for i in range(4)
]
# 分数：0.9 / 0.7 / 0.4 / 0.2，阈值 0.5 → 过滤 2 条，top1=0.9
scored = [(docs[0], 0.9), (docs[1], 0.7), (docs[2], 0.4), (docs[3], 0.2)]

trace = _rerank_trace(scored, threshold=0.5)
check("过滤前 top1 分数正确", trace["top1_before_filter"] == 0.9, f"实际 {trace['top1_before_filter']}")
check("被阈值过滤条数正确", trace["filtered_count"] == 2, f"实际 {trace['filtered_count']}")
check("阈值原样记录", trace["threshold"] == 0.5, f"实际 {trace['threshold']}")
check("候选清单含全部 4 条（含被过滤的）", len(trace["candidates"]) == 4, f"实际 {len(trace['candidates'])}")
passed_flags = [c["passed_threshold"] for c in trace["candidates"]]
check("passed 标记正确", passed_flags == [True, True, False, False], f"实际 {passed_flags}")
check("候选带 chunk_id（现拼 doc_id:chunk_index）", trace["candidates"][0]["chunk_id"] == "1:0",
      f"实际 {trace['candidates'][0]['chunk_id']}")
check("候选带原始文件名", trace["candidates"][0]["file_name"] == "f0.md",
      f"实际 {trace['candidates'][0]['file_name']}")

trace_no_threshold = _rerank_trace(scored, threshold=None)
check("未启用阈值时过滤数为 0", trace_no_threshold["filtered_count"] == 0)
check("未启用阈值时全部标记过阈值", all(c["passed_threshold"] for c in trace_no_threshold["candidates"]))

trace_empty = _rerank_trace([], threshold=0.5)
check("空候选：top1 为 None", trace_empty["top1_before_filter"] is None)
check("空候选：清单为空", trace_empty["candidates"] == [])


# --------------------------------------------------------------------------- #
# 第 2 组：_candidate_brief 与 rag_chain._resolve_chunk_id 同构
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：chunk_id 判定同构 ==")
from core.rag_chain import _resolve_chunk_id

cases = [
    {"chunk_id": "9:9"},                                   # 直接有键
    {"doc_id": 3, "chunk_index": 2},                       # 现拼
    {"doc_id": "3", "chunk_index": 2},                     # doc_id 非整数（bool/str 防御）
    {"source": "/upload/x.md"},                            # 两级都拿不到
    {},                                                    # 空
]
for i, meta in enumerate(cases):
    brief = _candidate_brief(Document(page_content="x", metadata=meta))
    resolved = _resolve_chunk_id(meta)
    same = (brief["chunk_id"] is None and resolved is None) or brief["chunk_id"] == resolved
    check(f"同构用例 {i}: {meta}", same, f"brief={brief['chunk_id']} resolve={resolved}")


# --------------------------------------------------------------------------- #
# 第 3 组：CrossEncoderReranker（假模型注入，不联网）
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：CrossEncoderReranker 轨迹与日志 ==")
from core.retriever import CrossEncoderReranker


class _FakeCrossEncoder:
    """predict 返回固定分数序列（与输入对数等长）。"""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def predict(self, pairs):
        assert len(pairs) == len(self._scores), "候选数与分数序列不等长"
        return list(self._scores)


reranker = CrossEncoderReranker(model_path="stub", top_k=2, score_threshold=0.5)
reranker.model = _FakeCrossEncoder([0.9, 0.4, 0.7, 0.2])   # 排序后应为 0.9 / 0.7 / 0.4 / 0.2

cap = _LogCapture("core.retriever")
result = reranker.rerank("问题", docs)
check("rerank 返回 top_k=2 条", result is not None and len(result) == 2,
      f"实际 {None if result is None else len(result)}")
check("返回按分数降序", result is not None and [s for _, s in result] == [0.9, 0.7],
      f"实际 {None if result is None else [s for _, s in result]}")
trace = reranker.last_trace
assert trace is not None
check("last_trace.top1_before_filter = 0.9", trace["top1_before_filter"] == 0.9)
check("last_trace.filtered_count = 2", trace["filtered_count"] == 2)

lines = cap.messages()
rerank_logs = [m for m in lines if "重排完成" in m]
check("重排完成日志存在", bool(rerank_logs), f"实际日志 {lines!r}")
if rerank_logs:
    log_line = rerank_logs[-1]
    check("日志带过滤前top1", "过滤前top1=0.9000" in log_line, f"实际 {log_line!r}")
    check("日志带阈值过滤条数", "阈值过滤=2" in log_line, f"实际 {log_line!r}")

# 全过滤场景（p2.22 现场形态：分数区间退化 [0,0]，原始 top1 必须活着）
reranker_all_fail = CrossEncoderReranker(model_path="stub", top_k=2, score_threshold=0.95)
reranker_all_fail.model = _FakeCrossEncoder([0.9, 0.4, 0.7, 0.2])
empty_result = reranker_all_fail.rerank("问题", docs)
check("全过滤时返回空列表", empty_result == [], f"实际 {empty_result!r}")
trace2 = reranker_all_fail.last_trace
assert trace2 is not None
check("全过滤时 top1 仍为 0.9（观测盲点消除）", trace2["top1_before_filter"] == 0.9,
      f"实际 {trace2['top1_before_filter']}")
check("全过滤时 filtered_count = 4", trace2["filtered_count"] == 4, f"实际 {trace2['filtered_count']}")
cap.close()


# --------------------------------------------------------------------------- #
# 第 4 组：_log_rejection 结构化拒答日志
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：拒答结构化日志 ==")
from core.rag_chain import _log_rejection

cap4 = _LogCapture("core.rag_chain")
_log_rejection("公司的年假有几天？", {"top1_before_filter": 0.4321, "threshold": 0.5})
rejection_logs = [m for m in cap4.messages() if "拒答" in m]
check("拒答日志存在", bool(rejection_logs))
if rejection_logs:
    line = rejection_logs[-1]
    check("日志含原因=检索为空", "原因=检索为空" in line, f"实际 {line!r}")
    check("日志含改写后查询", "改写后查询=公司的年假有几天？" in line, f"实际 {line!r}")
    check("日志含 top1 分数（4 位小数）", "top1分数=0.4321" in line, f"实际 {line!r}")
    check("日志含阈值", "阈值=0.5000" in line, f"实际 {line!r}")

_log_rejection("问题", None)
line_no_trace = [m for m in cap4.messages() if "拒答" in m][-1]
check("无轨迹时 top1 如实写「不可得」", "top1分数=不可得" in line_no_trace, f"实际 {line_no_trace!r}")
check("无轨迹时阈值写「未启用」", "阈值=未启用" in line_no_trace, f"实际 {line_no_trace!r}")
_log_rejection("", {"threshold": None})
line_empty = [m for m in cap4.messages() if "拒答" in m][-1]
check("空查询回退「（无）」", "改写后查询=（无）" in line_empty, f"实际 {line_empty!r}")
cap4.close()


# --------------------------------------------------------------------------- #
# 第 5 组：RAGChain.stream debug 事件
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：stream debug 检索事件 ==")
from core.intent_router import Intent, IntentResult
from core.memory_manager import MemoryManager
from core.rag_chain import RAGChain, _NO_CONTEXT_ANSWER, _usage_zero
from core.session_store import MemorySessionStore


class _FakeLLM:
    def stream(self, _prompt_value):
        yield SimpleNamespace(content="基于资料的答案。", usage_metadata=None)


class _FakeLLMClient:
    provider = "fake"
    model_name = "fake-model"

    def get_llm(self):
        return _FakeLLM()

    def get_model_info(self) -> dict:
        return {"provider": "fake"}


class _FakeRetriever:
    """只暴露 RAGChain 用到的三个面：reranker / get_last_rerank_trace / 真实度不重要。"""

    def __init__(self, trace: dict | None) -> None:
        self.reranker = object() if trace is not None else None
        self._trace = trace

    def get_last_rerank_trace(self):
        return self._trace


def _probe_stream(debug: bool, docs: list[Document], trace: dict | None) -> list[dict]:
    memory = MemoryManager(max_turns=5, ttl_seconds=3600, store=MemorySessionStore(ttl_seconds=3600))
    probe = RAGChain(retriever=_FakeRetriever(trace), llm_client=_FakeLLMClient(), memory=memory)
    probe._prepare_parallel = lambda q, h: (  # type: ignore[method-assign]
        IntentResult(intent=Intent.POLICY_CONSULT, source="rule"),
        {
            "standalone_question": q,
            "docs": docs,
            "context": "\n".join(d.page_content for d in docs),
            "rewrite_usage": _usage_zero(),
        },
    )
    probe._maybe_store_cache = lambda *a, **k: None  # type: ignore[method-assign]
    return list(probe.stream("年假有几天？", "p23", owner_id=None, debug=debug))


stub_trace = {
    "threshold": 0.5,
    "top1_before_filter": 0.4321,
    "filtered_count": 2,
    "candidates": [
        {"chunk_id": "1:0", "file_name": "a.md", "rerank_score": 0.6, "passed_threshold": True},
        {"chunk_id": "1:1", "file_name": "b.md", "rerank_score": 0.4, "passed_threshold": False},
    ],
}

# 场景一：debug=False（不传 debug 的调用方）——零可见变化
events_off = _probe_stream(debug=False, docs=[Document(page_content="正文", metadata={})], trace=stub_trace)
types_off = [e["type"] for e in events_off]
check("debug=False 无 retrieval 帧", "retrieval" not in types_off, f"实际 {types_off}")
check("debug=False 事件序列仍是 meta/chunk/done",
      types_off[0] == "meta" and "chunk" in types_off and types_off[-1] == "done", f"实际 {types_off}")

# 场景二：debug=True 且正常召回 —— meta 之后、chunk 之前下发 retrieval
events_on = _probe_stream(debug=True, docs=[Document(page_content="正文", metadata={})], trace=stub_trace)
types_on = [e["type"] for e in events_on]
check("debug=True 下发 retrieval 帧", "retrieval" in types_on, f"实际 {types_on}")
check("retrieval 在 meta 之后 chunk 之前",
      types_on.index("retrieval") == types_on.index("meta") + 1
      and types_on.index("retrieval") < types_on.index("chunk"), f"实际 {types_on}")
retrieval = next(e for e in events_on if e["type"] == "retrieval")
check("事件带改写后查询", retrieval["rewritten_query"] == "年假有几天？", f"实际 {retrieval!r}")
check("事件带意图分类结果", retrieval["intent"] == "policy_consult" and retrieval["route"] == "rag_qa",
      f"实际 intent={retrieval['intent']} route={retrieval['route']}")
check("事件带阈值配置", retrieval["threshold"] == 0.5, f"实际 {retrieval['threshold']}")
check("事件带过滤前 top1", retrieval["top1_before_filter"] == 0.4321, f"实际 {retrieval['top1_before_filter']}")
check("事件带过滤条数", retrieval["filtered_count"] == 2, f"实际 {retrieval['filtered_count']}")
check("事件候选含未过阈值者", [c["passed_threshold"] for c in retrieval["candidates"]] == [True, False],
      f"实际 {retrieval['candidates']!r}")
check("事件带 docs_returned", retrieval["docs_returned"] == 1, f"实际 {retrieval['docs_returned']}")

# 场景三：debug=True 且零召回（p2.22 Q2 现场）——拒答气泡也要有检索详情
cap5 = _LogCapture("core.rag_chain")
events_reject = _probe_stream(debug=True, docs=[], trace=stub_trace)
types_reject = [e["type"] for e in events_reject]
check("拒答场景仍下发 retrieval 帧", "retrieval" in types_reject, f"实际 {types_reject}")
chunk_texts = [e["content"] for e in events_reject if e["type"] == "chunk"]
check("拒答唯一 chunk 是固定模板", chunk_texts == [_NO_CONTEXT_ANSWER], f"实际 {chunk_texts!r}")
retrieval_r = next(e for e in events_reject if e["type"] == "retrieval")
check("拒答事件 top1 来自轨迹", retrieval_r["top1_before_filter"] == 0.4321,
      f"实际 {retrieval_r['top1_before_filter']}")
check("拒答事件 docs_returned=0", retrieval_r["docs_returned"] == 0)
rejection = [m for m in cap5.messages() if "拒答" in m]
check("拒答场景打出结构化拒答日志", bool(rejection) and "top1分数=0.4321" in rejection[-1],
      f"实际 {rejection!r}")
cap5.close()

# 场景四：debug=True 但无重排轨迹（重排关闭）——事件照发，top1 如实为 None
events_no_trace = _probe_stream(debug=True, docs=[Document(page_content="正文", metadata={})], trace=None)
retrieval_nt = next(e for e in events_no_trace if e["type"] == "retrieval")
check("无轨迹时事件仍下发", retrieval_nt["top1_before_filter"] is None)
check("无轨迹时候选从 sources 凑且全标过阈值",
      all(c["passed_threshold"] for c in retrieval_nt["candidates"]), f"实际 {retrieval_nt['candidates']!r}")


# --------------------------------------------------------------------------- #
# 第 6 组：AskRequest.debug 字段契约
# --------------------------------------------------------------------------- #
print("\n== 第 6 组：AskRequest.debug 契约 ==")
from api.routes.qa import AskRequest

req_default = AskRequest(question="q")
check("debug 默认 None（不传 → 按角色默认）", req_default.debug is None)
req_true = AskRequest(question="q", debug=True)
check("debug 可显式 true", req_true.debug is True)
req_false = AskRequest(question="q", debug=False)
check("debug 可显式 false", req_false.debug is False)

# 接口层的「显式 > 默认」解析逻辑与 qa.ask_stream 内联保持一致，这里复算一遍：
for debug_value, role, expected in [
    (None, "admin", True), (None, "user", False), (False, "admin", False), (True, "user", True),
]:
    actual = debug_value if debug_value is not None else (role == "admin")
    check(f"解析规则 debug={debug_value} role={role} -> {expected}", actual == expected,
          f"实际 {actual}")


print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
sys.exit(1 if FAIL else 0)
