# pyright: basic
"""
模块23测试文件：检索前 Query 增强（路线图 §6.4 D，v3.1.0-p3.7d）。

--------------------------------------------------------------------------
它与相邻模块的分工
--------------------------------------------------------------------------
    module19  eval 脚本本身（指标定义 / 实验协议 / 落盘）
    module20  混合检索（向量 + BM25 → RRF）
    module23  检索前 Query 增强：单轮改写 + 可选 HyDE 的「开关 / 分支 / 透传」逻辑

覆盖点
--------------------------------------------------------------------------
1.  **rewrite_query 纯函数**：口语/指代问题被改写出独立检索句（含消毒兜底）。
2.  **generate_hypo_answer 纯函数**：HyDE 生成假设答案；空输出退化回原问题（不污染 embedding）。
3.  **retriever.vector_query 透传**：HyDE 时向量召回用假设答案的 embedding，
    BM25 仍用真实问句（retrieve 的 query 参数不变）。
4.  **_prepare_parallel 分支**（生产链路前段）：
    - 开关全关 + 无历史 → 不重写、不 HyDE，retrieve 只拿原问题（零行为变化）；
    - QUERY_REWRITE 开 + 无历史 → 单轮也过一次改写，改写后重新检索；
    - QUERY_HYDE 开 → 改写后再生成假设答案，retrieve 透传 vector_query。

运行：
    .venv/bin/python tests/test_module23_query_enhance.py
（本模块不依赖 MySQL / Chroma / Redis：retriever 与 LLM 全部用 fake 注入）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.documents import Document
from langchain_core.runnables import Runnable
from unittest.mock import patch

from config.settings import settings
from core.rag_chain import RAGChain, rewrite_query, generate_hypo_answer
from core.retriever import RAGRetriever

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
# Fake 基础设施
# --------------------------------------------------------------------------- #
class _FakeChat(Runnable):
    """最小的 LLM 替身：invoke 直接返回一个带 content 的对象，绕过真实网络。"""

    def __init__(self, responder):
        self._responder = responder

    def invoke(self, inputs, config=None, **kwargs):  # noqa: ANN001
        return SimpleNamespace(content=self._responder(inputs))


class _FakeLLMClient:
    def __init__(self, responder):
        self.provider = "siliconflow"
        self.model_name = "fake-model"
        self._responder = responder

    def get_llm(self):
        return _FakeChat(self._responder)


class _FakeRetriever:
    """记录每次 retrieve 的入参，方便断言 HyDE 的 vector_query 是否透传。"""

    def __init__(self):
        self.reranker = None
        self.calls: list[tuple[str, object]] = []

    def retrieve(self, query: str, *, vector_query: str | None = None, **_kwargs):
        self.calls.append((query, vector_query))
        return [Document(page_content="fake-chunk", metadata={"file_name": "x.md"})]


class _FakeVS:
    """RAGRetriever 的向量库替身，只记录 similarity_search_with_score 收到的文本。"""

    store_type = "fake"

    def __init__(self):
        self.last_query: str | None = None

    def similarity_search_with_score(self, query: str, k: int, filter_dict=None):  # noqa: ANN001
        self.last_query = query
        return [(Document(page_content="c", metadata={}), 0.9)]


class _FakeClassifier:
    def classify(self, question: str):  # noqa: ANN001, ARG002
        return SimpleNamespace(route="rag_qa", intent=SimpleNamespace(value="knowledge_query"))


def _make_chain(retriever, rewrite_returns="改写后的问题", hypo_returns="假设答案文本"):
    """构造 RAGChain，注入 fake retriever / llm / classifier，并可选 patch 改写与 HyDE。"""
    chain = RAGChain(
        retriever=retriever,
        llm_client=_FakeLLMClient(lambda _: "改写后的问题"),
        memory=object(),
    )
    # _rewrite_question 是生产路径里真正调 LLM 的那一步，单测里固定其返回值以隔离分支逻辑
    # P4-9：_rewrite_question 契约改为返回「子查询列表」——桩同步（否则
    # " / ".join(字符串) 会逐字符拆成单字子查询，2026-10-17 沙箱实测）
    chain._rewrite_question = lambda q, h: ([rewrite_returns], {"input_tokens": 0,
                                                               "output_tokens": 0,
                                                               "cache_read_tokens": 0})
    return chain


# --------------------------------------------------------------------------- #
# 1. rewrite_query 纯函数
# --------------------------------------------------------------------------- #
def test_rewrite_query():
    print("== 1. rewrite_query 纯函数 ==")
    fake = _FakeLLMClient(lambda _: "年假多少天（独立检索句）")
    with patch("core.rag_chain.get_llm_client", return_value=fake):
        out = rewrite_query("那个年假到底能休几天来着")
    check("改写非空且等于模型输出", out == "年假多少天（独立检索句）", f"got={out!r}")
    # 消毒兜底：模型若输出作答痕迹，应回退原问题
    fake2 = _FakeLLMClient(lambda _: "无法回答您的这个问题。")
    with patch("core.rag_chain.get_llm_client", return_value=fake2):
        out2 = rewrite_query("随便问问")
    check("作答痕迹触发消毒回退", out2 == "随便问问", f"got={out2!r}")


# --------------------------------------------------------------------------- #
# 2. generate_hypo_answer 纯函数
# --------------------------------------------------------------------------- #
def test_generate_hypo():
    print("== 2. generate_hypo_answer 纯函数 ==")
    fake = _FakeLLMClient(lambda _: "根据规定，员工每年享有10天带薪年假。")
    with patch("core.rag_chain.get_llm_client", return_value=fake):
        out = generate_hypo_answer("年假多少天")
    check("HyDE 假设答案非空", out == "根据规定，员工每年享有10天带薪年假。", f"got={out!r}")
    # 空输出退化兜底：绝不带脏文本去 embedding
    fake_empty = _FakeLLMClient(lambda _: "   ")
    with patch("core.rag_chain.get_llm_client", return_value=fake_empty):
        out_empty = generate_hypo_answer("年假多少天")
    check("空假设答案退化回原问题", out_empty == "年假多少天", f"got={out_empty!r}")


# --------------------------------------------------------------------------- #
# 3. retriever 的 vector_query 透传
# --------------------------------------------------------------------------- #
def test_retriever_vector_query():
    print("== 3. RAGRetriever.vector_query 透传 ==")
    vs = _FakeVS()
    retr = RAGRetriever(vector_store=vs, use_reranker=False)
    retr.retrieve("真实问句")
    check("无 vector_query 时向量召回用原问句", vs.last_query == "真实问句", f"got={vs.last_query!r}")
    vs2 = _FakeVS()
    retr2 = RAGRetriever(vector_store=vs2, use_reranker=False)
    retr2.retrieve("真实问句", vector_query="假设答案文本")
    check("有 vector_query 时向量召回用假设答案", vs2.last_query == "假设答案文本",
          f"got={vs2.last_query!r}")


# --------------------------------------------------------------------------- #
# 4. _prepare_parallel 分支
# --------------------------------------------------------------------------- #
def test_prepare_no_enhance():
    print("== 4a. _prepare_parallel：开关全关 + 无历史 ==")
    retr = _FakeRetriever()
    chain = _make_chain(retr, rewrite_returns="改写后的问题")
    old_rw, old_hy = settings.QUERY_REWRITE, settings.QUERY_HYDE
    settings.QUERY_REWRITE, settings.QUERY_HYDE = False, False
    try:
        with patch("core.rag_chain.get_intent_classifier", return_value=_FakeClassifier()):
            intent, prepared = chain._prepare_parallel("原问题", [])
    finally:
        settings.QUERY_REWRITE, settings.QUERY_HYDE = old_rw, old_hy
    check("无历史且关开关 → 不重写（standalone==原问题）",
          prepared["standalone_question"] == "原问题", f"got={prepared['standalone_question']!r}")
    check("vector_query 为 None（未启用 HyDE）", prepared["vector_query"] is None)
    check("retrieve 只以原问题调用一次", retr.calls == [("原问题", None)], f"calls={retr.calls!r}")


def test_prepare_rewrite_on():
    print("== 4b. _prepare_parallel：QUERY_REWRITE 开 + 无历史 ==")
    retr = _FakeRetriever()
    chain = _make_chain(retr, rewrite_returns="改写后的问题")
    old_rw, old_hy = settings.QUERY_REWRITE, settings.QUERY_HYDE
    settings.QUERY_REWRITE, settings.QUERY_HYDE = True, False
    try:
        with patch("core.rag_chain.get_intent_classifier", return_value=_FakeClassifier()):
            intent, prepared = chain._prepare_parallel("原问题", [])
    finally:
        settings.QUERY_REWRITE, settings.QUERY_HYDE = old_rw, old_hy
    check("单轮也过改写（standalone==改写结果）",
          prepared["standalone_question"] == "改写后的问题",
          f"got={prepared['standalone_question']!r}")
    check("HyDE 未开 → vector_query 为 None", prepared["vector_query"] is None)
    # 投机检索用原问题 + 改写后重新检索：两次调用
    check("retrieve 投机(原问题)+改写后重检",
          ("原问题", None) in retr.calls and ("改写后的问题", None) in retr.calls,
          f"calls={retr.calls!r}")


def test_prepare_hyde_on():
    print("== 4c. _prepare_parallel：QUERY_HYDE 开 ==")
    retr = _FakeRetriever()
    chain = _make_chain(retr, rewrite_returns="改写后的问题", hypo_returns="假设答案文本")
    old_rw, old_hy = settings.QUERY_REWRITE, settings.QUERY_HYDE
    settings.QUERY_REWRITE, settings.QUERY_HYDE = False, True
    try:
        with patch("core.rag_chain.get_intent_classifier", return_value=_FakeClassifier()), \
             patch("core.rag_chain.generate_hypo_answer", return_value="假设答案文本"):
            intent, prepared = chain._prepare_parallel("原问题", [])
    finally:
        settings.QUERY_REWRITE, settings.QUERY_HYDE = old_rw, old_hy
    check("HyDE 开 → vector_query 透传假设答案",
          prepared["vector_query"] == "假设答案文本", f"got={prepared['vector_query']!r}")
    # 最终检索应带 vector_query（BM25 仍用真实问句由 retriever 内部处理）
    final = retr.calls[-1]
    check("最终 retrieve 带 vector_query", final == ("改写后的问题", "假设答案文本"), f"final={final!r}")


if __name__ == "__main__":
    test_rewrite_query()
    test_generate_hypo()
    test_retriever_vector_query()
    test_prepare_no_enhance()
    test_prepare_rewrite_on()
    test_prepare_hyde_on()
    print(f"\n== 模块23 检索前 Query 增强：PASS={PASS} FAIL={FAIL} ==")
    sys.exit(1 if FAIL else 0)
