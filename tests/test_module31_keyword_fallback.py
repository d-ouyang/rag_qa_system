# pyright: basic
"""
p4.2d 测试：问句形态关键词兜底 + 混合召回逐条来源标注。

    .venv/bin/python tests/test_module31_keyword_fallback.py
    .venv/bin/python tests/test_module31_keyword_fallback.py --reverse

--------------------------------------------------------------------------
钉住的不变量
--------------------------------------------------------------------------
① 关键词兜底是**纯句尾剥离**：句中的疑问词有语义（「怎么申请报销」剥"怎么"
   会变味），绝不全局替换；剥不出名词核心（空/太短/与原问相同）就不兜底。
② 兜底用**原阈值**再检索——提高的是 query 质量，不是放宽质量门槛
   （"降阈值抢救"已被用户否决，见 v4.1.0-p4.2c 迭代文档）。
③ 混合召回的逐条来源标注：trace["sources"][chunk_id] ∈ {vector, bm25, both}，
   且与聚合计数一致（bm25_only == sources 里值为 bm25 的条数）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
REVERSE = "--reverse" in sys.argv


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------- #
# 第 1 组：_keyword_fallback_query 纯函数（句尾剥离语义）
# --------------------------------------------------------------------------- #
from core.rag_chain import _keyword_fallback_query  # noqa: E402

section("关键词兜底：句尾剥离")

cases_hit = [
    ("年假有几天？", "年假"),
    ("公司管理制度是什么？", "公司管理制度"),
    ("报销流程有哪些", "报销流程"),
    ("请问报销的流程是什么？", "报销的流程"),
]
for src, want in cases_hit:
    got = _keyword_fallback_query(src)
    check(f"{src!r} -> {want!r}", got == want, f"实际={got!r}")

# 无疑问词的名词问句：剥完与原问相同 → 不兜底（原样重查等于浪费一次检索）
check("'年假规定' -> 不兜底(None)", _keyword_fallback_query("年假规定") is None)

cases_skip = [
    "怎么申请报销",          # 句中疑问词不许动（全局替换会变味）
    "什么情况下可以离职",    # 句首"什么"有语义
    "你好",                  # 太短
    "是什么？",              # 剥完为空
]
for src in cases_skip:
    got = _keyword_fallback_query(src)
    check(f"{src!r} -> 不兜底(None)", got is None, f"实际={got!r}")

# --------------------------------------------------------------------------- #
# 第 2 组：混合召回逐条来源标注（真 BM25Okapi + 假向量库，同 module20 模式）
# --------------------------------------------------------------------------- #
section("混合召回 sources 标注")

from config.settings import settings as _settings  # noqa: E402

_settings.RETRIEVAL_HYBRID = True

from core.bm25_index import BM25Index, invalidate_bm25_index, reset_bm25_index  # noqa: E402
from core.retriever import RAGRetriever  # noqa: E402
from core.vector_store import chunk_id_of_metadata  # noqa: E402
from langchain_core.documents import Document  # noqa: E402

CORPUS = [
    ("599:0", "GQ-AD-2026-003 居住证申请流程指引。本指引规定上海市居住证的申请材料与办理流程。"),
    ("617:0", "GQ-HR-2026-005 考勤管理制度。第一条为规范公司考勤管理而制定本制度。"),
    ("609:1", "费用报销管理制度 差旅费标准。员工出差的住宿费按城市分档报销。"),
    ("606:0", "差旅费管理办法。出差住宿标准：一线城市 500 元/晚。"),
]


class FakeStore:
    store_type = "fake"

    def __init__(self) -> None:
        self.chunks = [
            {"chunk_id": cid, "text": text, "metadata": {"chunk_id": cid}}
            for cid, text in CORPUS
        ]

    def get_all_chunks(self) -> list[dict]:
        return [dict(c) for c in self.chunks]

    def similarity_search_with_score(self, query: str, k: int, filter_dict=None):
        """只认「差旅」字面，与 BM25 不同源，保证两路可辨认。"""
        scored = [
            (Document(page_content=c["text"], metadata=dict(c["metadata"])), 1.0)
            for c in self.chunks
            if "差旅" in c["text"]
        ]
        return scored[:k]


reset_bm25_index()
store = FakeStore()
invalidate_bm25_index()
retriever = RAGRetriever(
    vector_store=store,  # type: ignore[arg-type]
    use_reranker=False,
    top_k=3,
    candidate_multiplier=2,
    bm25_index=BM25Index(vector_store=store),  # type: ignore[arg-type]
)

vec_pairs = store.similarity_search_with_score("差旅 住宿", k=6)
vec_ids = {chunk_id_of_metadata(d.metadata) for d, _ in vec_pairs}
bm_hits = retriever._bm25_override.search("差旅 住宿", 6)  # type: ignore[union-attr]
bm_ids = [cid for cid, _ in bm_hits]

merged = retriever._hybrid_merge("差旅 住宿", vec_pairs, 6)  # type: ignore[arg-type]
trace = retriever.last_hybrid_trace or {}
sources = trace.get("sources") or {}

check("trace 带 sources 键", isinstance(sources, dict) and len(sources) > 0, str(trace))
check("sources 覆盖全部融合候选",
      set(sources) >= {chunk_id_of_metadata(d.metadata) for d, _ in merged},
      f"sources={list(sources)} merged={len(merged)}")
check("取值只可能是 vector/bm25/both",
      set(sources.values()) <= {"vector", "bm25", "both"}, str(set(sources.values())))
check("向量路的候选标为 vector/both",
      all(sources.get(c) in ("vector", "both") for c in vec_ids),
      str({c: sources.get(c) for c in vec_ids}))
check("BM25 独有候选标为 bm25",
      all(sources.get(c) == "bm25" for c in bm_ids if c not in vec_ids),
      str({c: sources.get(c) for c in bm_ids if c not in vec_ids}))
check("bm25_only 计数与 sources 一致",
      trace["bm25_only"] == sum(1 for v in sources.values() if v == "bm25"),
      f"bm25_only={trace['bm25_only']}")

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
print("\n== 汇总 ==")
print(f"==== p4.2d 测试结果：通过 {PASS} 项，失败 {FAIL} 项 ====")
if FAIL:
    sys.exit(1)
if REVERSE:
    print("（--reverse 变异模式：本模块的不变量靠正向断言钉住，无反向拆桩用例）")
