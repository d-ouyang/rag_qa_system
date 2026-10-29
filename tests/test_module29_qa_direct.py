# pyright: basic
"""
优化 2（PLAN-v4.0.0）：QA 问答集直击测试

运行：
    .venv/bin/python tests/test_module29_qa_direct.py   （或 make test-sandbox s=29）

覆盖点：
1. QA 集文件契约：真实 data/qa/kb_qa_set.jsonl 加载、字段完整、id 唯一。
2. 精确层：标准问法 / 规范化变体（空格·全角·末尾标点）/ alt_questions 命中；未收录不命中。
3. 语义层（注入打桩嵌入器）：高分命中、低于阈值不命中、嵌入失败 fail-open 返回 None。
4. 门控：热开关缺省关（fail-closed）；指代问句（「那婚假呢」）与过短问句不直击。
5. RAGChain 端到端（真实索引 + 真实精确匹配）：
   命中轮 0 tokens、不调 LLM、不检索、decision=qa_hit、溯源带 qa_id；
   未命中轮回落正常检索链（rag_qa / zero_recall 无回归）。

说明：全程不联网、不连 Redis（检索/记忆/LLM 全用桩或进程内实现）；
直击开关用 config_service 进程内配置，结束时 unset 复原。
"""
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


import core.qa_direct as qd
from core.config_service import config_service
from core.intent_router import Intent, IntentResult
from core.memory_manager import MemoryManager
from core.qa_direct import (
    SEMANTIC_THRESHOLD,
    QaDirectIndex,
    QaEntry,
    get_qa_index,
    load_qa_entries,
    reset_qa_index,
    try_qa_direct_hit,
)
from core.qa_flow import QaFlowState, TERMINAL_STATES, flow_path
from core.session_store import MemorySessionStore

# --------------------------------------------------------------------------- #
# 第 1 组：QA 集文件契约（真实文件）
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：QA 集文件契约 ==")
from config.settings import settings  # noqa: E402

entries = load_qa_entries(settings.QA_SET_PATH)
check("真实 QA 集可加载且 48 条", len(entries) == 48, f"实际 {len(entries)}")
ids = [e.id for e in entries]
check("id 唯一", len(set(ids)) == len(ids))
check("question/answer 全部非空", all(e.question and e.answer for e in entries))
check("alt_questions 均为元组且非空条目存在",
      isinstance(entries[0].alt_questions, tuple)
      and any(e.alt_questions for e in entries))
check("溯源字段存在（source_doc 非空占比 100%）", all(e.source_doc for e in entries))

index = QaDirectIndex(entries)
check("精确层键数 = 标准问法+同义问法去重（≥ 条目数）",
      len(index._exact) >= len(entries))

# --------------------------------------------------------------------------- #
# 第 2 组：精确层匹配
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：精确层匹配 ==")
hit = index.lookup_exact("事假最多能请多少天？")
check("标准问法命中 qa005", hit is not None and hit[0].id == "qa005",
      f"实际 {hit[0].id if hit else None}")
hit = index.lookup_exact("事假最多能请多少天")
check("去末尾问号变体命中（normalize 口径与 qa_cache 逐字一致）",
      hit is not None and hit[0].id == "qa005")
hit = index.lookup_exact("事假是无薪的吗？")
check("alt_questions 同义问法命中", hit is not None and hit[0].id == "qa005")
hit = index.lookup_exact("婚假多少天")
check("全角问号问法去标点后命中", hit is not None and hit[0].id == "qa007",
      f"实际 {hit[0].id if hit else None}")
check("未收录的第三种问法不命中（宁缺毋滥）",
      index.lookup_exact("请事假会扣工资吗") is None)

# --------------------------------------------------------------------------- #
# 第 3 组：语义层（打桩嵌入器，受控向量空间）
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：语义层（打桩嵌入器） ==")


class _StubEmbeddings:
    """受控三维向量空间：年假→[1,0,0]、报销→[0,1,0]、其他→[0,0,1]；raises=True 全抛。"""

    def __init__(self, raises: bool = False):
        self.raises = raises

    def embed_documents(self, texts):
        if self.raises:
            raise RuntimeError("嵌入服务不可用")
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        if self.raises:
            raise RuntimeError("嵌入服务不可用")
        return self._vec(text)

    @staticmethod
    def _vec(t: str) -> list[float]:
        if "年假" in t:
            return [1.0, 0.0, 0.0]
        if "报销" in t:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


_se = [
    QaEntry(id="t1", question="年假有几天？", answer="5-15 天。"),
    QaEntry(id="t2", question="发票怎么报销？", answer="30 个工作日内。"),
]
idx_sem = QaDirectIndex(_se, embeddings=_StubEmbeddings())
hit = idx_sem.lookup_semantic("带薪年假有几天")  # 措辞不同但向量同向 → cos=1.0
check("语义层高分命中（cos=1.0 ≥ 0.92）", hit is not None and hit[0].id == "t1",
      f"实际 {hit}")
hit = idx_sem.lookup_semantic("考勤打卡怎么用")  # 与两条目正交 → cos=0
check("语义层正交问句不命中", hit is None)
hit = idx_sem.lookup_semantic("报销票据丢了怎么办", threshold=0.0)
check("阈值放到 0 时同空间条目可命中（阈值语义正确）",
      hit is not None and hit[0].id == "t2")
check("SEMANTIC_THRESHOLD 常量为 0.92", SEMANTIC_THRESHOLD == 0.92)

idx_err = QaDirectIndex(_se, embeddings=_StubEmbeddings(raises=True))
check("嵌入失败 fail-open：语义层返回 None 而非抛异常", idx_err.lookup_semantic("年假有几天") is None)

# --------------------------------------------------------------------------- #
# 第 4 组：门控（开关 / 指代问句 / 过短问句）
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：try_qa_direct_hit 门控 ==")
reset_qa_index()
_orig_flag = qd.flag_value

qd.flag_value = lambda key: False  # 开关关（缺省档）
check("开关关闭 → 不直击（fail-closed）", try_qa_direct_hit("事假最多能请多少天？") is None)

qd.flag_value = lambda key: True
check("指代问句不直击（须过改写层）", try_qa_direct_hit("那婚假呢") is None)
check("过短问句不直击（standalone 门控）", try_qa_direct_hit("你好呀") is None)
hit = try_qa_direct_hit("事假最多能请多少天？")
check("开关开 + 独立完整问句 → 精确命中",
      hit is not None and hit["via"] == "exact" and hit["score"] == 1.0
      and hit["entry"].id == "qa005")
qd.flag_value = _orig_flag

# --------------------------------------------------------------------------- #
# 第 5 组：RAGChain 端到端（真实索引 + 真实精确匹配 + 桩检索/LLM）
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：端到端（命中不调 LLM / 未命中无回归） ==")
from langchain_core.documents import Document  # noqa: E402

import core.rag_chain as rc  # noqa: E402


class _BoomLLM:
    """直击轮若触碰 LLM 直接炸——比计数断言更强的证据。"""

    def invoke(self, _pv):
        raise AssertionError("直击轮不应调用 LLM")

    def stream(self, _pv):
        raise AssertionError("直击轮不应调用 LLM")
        yield  # pragma: no cover


class _BoomLLMClient:
    provider = "fake"
    model_name = "fake-model"

    def get_llm(self):
        return _BoomLLM()

    def get_model_info(self) -> dict:
        return {"provider": "fake"}


class _BoomRetriever:
    reranker = None  # RAGChain 构造日志会读这个属性决定「重排=开/关」展示

    def retrieve(self, *a, **k):
        raise AssertionError("直击轮不应触发检索")

    def get_last_rerank_trace(self):
        return None

    def get_last_hybrid_trace(self):
        return None


class _StubRetriever:
    """未命中轮的回归验证用：返回固定 docs。"""

    reranker = None  # RAGChain 构造日志读取

    def __init__(self, docs):
        self._docs = list(docs)

    def retrieve(self, query, top_k=None, filter_dict=None, return_score=False,
                 vector_query=None, trace_out=None):
        return list(self._docs)

    def get_last_rerank_trace(self):
        return None

    def get_last_hybrid_trace(self):
        return None


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


def _mk_chain(retriever, llm_client=None):
    chain = rc.RAGChain(
        retriever=retriever,
        llm_client=llm_client or _BoomLLMClient(),
        memory=MemoryManager(max_turns=5, ttl_seconds=3600,
                             store=MemorySessionStore(ttl_seconds=3600)),
    )
    chain._maybe_store_cache = lambda *a, **k: None  # type: ignore[method-assign]
    return chain


config_service.set_flag("qa_direct_hit", True)
try:
    hit_chain = _mk_chain(_BoomRetriever())
    res = hit_chain.query("事假是无薪的吗？", "s-qa", owner_id=None)
    check("query 直击：答案 = QA 集标准答案（未调 LLM/检索）",
          "无薪" in res["answer"] and res["usage"] == {"input_tokens": 0, "output_tokens": 0,
                                                       "cache_read_tokens": 0},
          f"answer={res['answer'][:40]} usage={res['usage']}")
    meta = hit_chain.memory.get_exchange_meta("s-qa", owner_id=None)[-1]
    trace = meta.get("qa_trace") or {}
    check("query 直击：decision=qa_hit 且随轮落库", trace.get("decision") == "qa_hit",
          f"实际 {trace.get('decision')}")
    check("query 直击：intent_source=qa_set（溯源可辨）", trace.get("intent_source") == "qa_set")
    check("query 直击：sources 带 qa_id", (res["sources"] or [{}])[0].get("qa_id") == "qa005")
    from core.qa_trace_repo import describe as _describe_trace
    check("query 直击：describe 派生 flow_path = 直击路径（溯源页高亮）",
          _describe_trace(trace).get("flow_path") == flow_path("qa_hit"),
          f"实际 {_describe_trace(trace).get('flow_path')}")

    # 流式：帧序列 meta → chunk，答案与落库一致
    frames = list(hit_chain.stream("事假最多能请多少天？", "s-qa-stream", owner_id=None))
    types = [f["type"] for f in frames]
    check("stream 直击：帧序列为 meta → chunk → done", types == ["meta", "chunk", "done"], f"实际 {types}")
    meta_s = hit_chain.memory.get_exchange_meta("s-qa-stream", owner_id=None)[-1]
    check("stream 直击：落库 decision=qa_hit",
          (meta_s.get("qa_trace") or {}).get("decision") == "qa_hit")
    chunk_text = "".join(f["content"] for f in frames if f["type"] == "chunk")
    check("stream 直击：正文与 qa005 标准答案一致（含关键数字）",
          "20 天" in chunk_text and "无薪" in chunk_text,
          f"实际 {chunk_text[:60]}")
finally:
    config_service.unset_flag("qa_direct_hit")

# 未命中回落：检索链路无回归（rag_qa / zero_recall）
fb_chain = _mk_chain(_StubRetriever([
    Document(page_content="x", metadata={"doc_id": 1, "chunk_index": 0, "file_name": "a.md"}),
]), llm_client=_FakeLLMClient())
res_fb = fb_chain.query("请事假会扣工资吗", "s-fb", owner_id=None)
qt_fb = fb_chain.memory.get_exchange_meta("s-fb", owner_id=None)[-1].get("qa_trace") or {}
check("未命中回落 rag_qa（检索链路无回归）", qt_fb.get("decision") == "rag_qa",
      f"实际 {qt_fb.get('decision')}")

zr_chain = _mk_chain(_StubRetriever([]), llm_client=_FakeLLMClient())
zr_chain.query("请事假会扣工资吗", "s-zr", owner_id=None)
qt_zr = zr_chain.memory.get_exchange_meta("s-zr", owner_id=None)[-1].get("qa_trace") or {}
check("未命中且零召回 → zero_recall（短路行为不变）", qt_zr.get("decision") == "zero_recall")

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
print("\n" + "=" * 50)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
if FAIL:
    sys.exit(1)
