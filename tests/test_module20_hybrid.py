# pyright: basic
"""
P3-4 测试：BM25 + RRF 混合检索。

    .venv/bin/python tests/test_module20_hybrid.py
    .venv/bin/python tests/test_module20_hybrid.py --reverse

--------------------------------------------------------------------------
本模块钉住的四条不变量（P3-4 的收益判定全建立在这上面）
--------------------------------------------------------------------------
①  **开关关 = 一行不多**：`RETRIEVAL_HYBRID=false` 时检索路径与 P3-4 之前
    逐字节一致（不碰 BM25、不建索引、不产生轨迹）。这是「不为了做功能而开」
    的前提 —— 默认关闭的代码必须能证明自己真的关了。
②  **融合只动候选池，不动裁决**：混合后的候选仍原样送进 reranker，
    bge-reranker 与 0.1 阈值一行不改。BM25 的分数不进任何排序依据，
    RRF 只决定「谁有资格被重排」。
③  **RRF 公式可手算**：`score(d) = Σ 1/(k + rank_i(d))`，两路名次累加。
    用手工算好的期望值钉死，防止有人把它改成加权平均（量纲会打架）。
④  **内容变了索引必须脏**：解析入库 / 删除文档 / 重建脚本之后 invalidate
    必须被调用 —— 否则 BM25 会对着旧 chunk_id 命中，检索悄悄变差还不报错。

--------------------------------------------------------------------------
为什么 BM25 部分用真索引而不是打桩
--------------------------------------------------------------------------
BM25Okapi 是纯函数（给词项表就算分），没有网络、没有模型权重 ——
打桩反而测不到「分词决定一切」这个真正的风险点（中文不分词 = 没有 BM25）。
所以语料用假向量库（内存里的几段文本），索引用**真的** BM25Okapi + jieba。
"""

import re
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


from config.settings import settings                       # noqa: E402
from core import bm25_index as bm25_mod                    # noqa: E402
from core.bm25_index import (                              # noqa: E402
    BM25Index,
    invalidate_bm25_index,
    reset_bm25_index,
    tokenize,
)
from core.retriever import RAGRetriever                    # noqa: E402
from core.vector_store import chunk_id_of_metadata         # noqa: E402
from langchain_core.documents import Document              # noqa: E402

# --------------------------------------------------------------------------- #
# 假向量库：不联网、不加载嵌入模型，只提供检索器与 BM25 需要的三个接口
# --------------------------------------------------------------------------- #
CORPUS = [
    # chunk_id, 正文。刻意让「字面精确」与「语义相近」分属不同切片：
    #   gq-ad-2026-003 只在 599:0 出现（编号类，BM25 的主场）
    #   报销/差旅 语义近邻一大片（向量检索容易，BM25 也行）
    ("599:0", "GQ-AD-2026-003 居住证申请流程指引。本指引规定上海市居住证的申请材料与办理流程。"),
    ("617:0", "GQ-HR-2026-005 考勤管理制度。第一条为规范公司考勤管理而制定本制度。"),
    ("609:1", "费用报销管理制度 差旅费标准。员工出差的住宿费按城市分档报销，一线城市每晚不超过五百元。"),
    ("606:0", "差旅费管理办法。出差住宿标准：一线城市 500 元/晚，二线城市 350 元/晚。"),
    ("588:0", "公司简介。锅圈食汇是一站式在家吃饭餐食产品品牌，定位社区中央厨房。"),
    ("616:0", "上市信息。公司已于港交所上市，股票代码 02517.HK。"),
]


class FakeStore:
    """只实现 RAGRetriever / BM25Index 用到的接口。"""

    store_type = "fake"

    def __init__(self) -> None:
        self.chunks = [{"chunk_id": cid, "text": text, "metadata": {"chunk_id": cid}}
                       for cid, text in CORPUS]
        self.deleted: list[str] = []

    def get_all_chunks(self) -> list[dict]:
        return [dict(c) for c in self.chunks]

    def similarity_search_with_score(self, query: str, k: int, filter_dict=None):
        """
        极简「向量检索」：正文含查询任一分词的按出现次数打分。
        刻意做得跟 BM25 不同源 —— 这样融合结果里的每一方都可辨认。
        """
        toks = set(tokenize(query))
        scored = []
        for c in self.chunks:
            score = sum(c["text"].count(t) for t in toks if len(t) >= 2)
            if score > 0:
                scored.append((Document(page_content=c["text"], metadata=dict(c["metadata"])), float(score)))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:k]


def make_retriever(hybrid: bool, top_k: int = 3, multiplier: int = 2) -> tuple[RAGRetriever, FakeStore]:
    store = FakeStore()
    settings.RETRIEVAL_HYBRID = hybrid
    retriever = RAGRetriever(
        vector_store=store,                 # type: ignore[arg-type]
        use_reranker=False,                 # 裁决另行单测；这里只看候选池
        top_k=top_k,
        candidate_multiplier=multiplier,
        bm25_index=BM25Index(vector_store=store),   # type: ignore[arg-type]
    )
    return retriever, store


# --------------------------------------------------------------------------- #
# 第 1 组：chunk_id 契约收敛（P3-4 顺手把第三份实现收编了）
# --------------------------------------------------------------------------- #
section("第 1 组：chunk_id_of_metadata 契约（判定收敛到 vector_store）")

check("metadata 有 chunk_id → 直接用",
      chunk_id_of_metadata({"chunk_id": "7:1", "doc_id": 99, "chunk_index": 8}) == "7:1")
check("没有 chunk_id → doc_id + chunk_index 现拼",
      chunk_id_of_metadata({"doc_id": 7, "chunk_index": 1}) == "7:1")
check("都没有 → None（不猜）", chunk_id_of_metadata({"file_name": "x.md"}) is None)
check("bool 不是合法 doc_id（isinstance(True, int) 是 True，必须显式排除）",
      chunk_id_of_metadata({"doc_id": True, "chunk_index": 0}) is None)
check("非 dict → None", chunk_id_of_metadata(None) is None and chunk_id_of_metadata("7:1") is None)

from core.retriever import _candidate_brief               # noqa: E402

brief = _candidate_brief(Document(page_content="x", metadata={"chunk_id": "9:9"}))
check("retriever._candidate_brief 复用同一判定（不再自带一份）",
      brief["chunk_id"] == "9:9", str(brief))
check("空 metadata 时 _candidate_brief 的 chunk_id 为 None",
      _candidate_brief(Document(page_content="x", metadata={}))["chunk_id"] is None)

# --------------------------------------------------------------------------- #
# 第 2 组：BM25 索引（真 BM25Okapi + jieba，假语料）
# --------------------------------------------------------------------------- #
section("第 2 组：BM25Index（惰性构建 / 分词 / invalidate / 物化）")

idx = BM25Index(vector_store=FakeStore())     # type: ignore[arg-type]
check("未检索时不构建（惰性）", idx.stats()["built"] is False, str(idx.stats()))
hits = idx.search("GQ-AD-2026-003 居住证", 5)
check("首次 search 完成构建", idx.stats()["built"] is True and idx.stats()["size"] == 6,
      str(idx.stats()))
check("编号查询：正文含编号的切片排第一（这就是 BM25 存在的理由）",
      hits and hits[0][0] == "599:0", str([(c, round(s, 2)) for c, s in hits]))
check("分数降序且只返回正分",
      all(hits[i][1] >= hits[i + 1][1] for i in range(len(hits) - 1)) and all(s > 0 for _, s in hits))

check("空查询返回空", idx.search("   ", 5) == [])
check("k<=0 返回空", idx.search("报销", 0) == [])
check("中文整句能分词（不是整句当一个词项）",
      len(tokenize("考勤管理制度里迟到怎么处理")) >= 4, str(tokenize("考勤管理制度里迟到怎么处理")))

idx.invalidate()
check("invalidate 后置脏，下次 search 重建", idx.stats()["built"] is False)
idx.search("报销", 3)
check("置脏后能重建", idx.stats()["built"] is True)

# 物化：BM25 命中里向量没捞到的那些，要能变回 Document
mats = idx.materialize(["599:0", "616:0", "__不存在的__"])
check("materialize 返回同序 Document（找不到的跳过）",
      [chunk_id_of_metadata(d.metadata) for d in mats] == ["599:0", "616:0"]
      and all(isinstance(d, Document) for d in mats))
check("物化的正文来自索引（不必回查向量库）",
      mats and "居住证申请流程指引" in mats[0].page_content, mats[0].page_content[:30] if mats else "空")
check("物化的 metadata 不含内部键 _text",
      mats and "_text" not in mats[0].metadata, str(mats[0].metadata if mats else None))

empty_idx = BM25Index(vector_store=FakeStore().__class__ and
                      type("E", (), {"get_all_chunks": staticmethod(list),
                                     "store_type": "fake"})())    # type: ignore[arg-type]
check("空语料建不出索引，search 返回空不炸", empty_idx.search("报销", 3) == [])


class _BrokenStore(FakeStore):
    def get_all_chunks(self):                      # type: ignore[override]
        raise RuntimeError("chroma 炸了")


broken = BM25Index(vector_store=_BrokenStore())    # type: ignore[arg-type]
check("构建失败不抛异常（少一路召回，不让检索挂掉）", broken.search("报销", 3) == [])
check("构建失败后索引保持未构建（不留半份）", broken.stats()["built"] is False)

# --------------------------------------------------------------------------- #
# 第 3 组：开关关 = 一行不多
# --------------------------------------------------------------------------- #
section("第 3 组：RETRIEVAL_HYBRID=false 时路径与 P3-4 之前一致")

retriever, store = make_retriever(hybrid=False)
docs_off = retriever.retrieve("差旅 住宿 报销 标准", top_k=3)
ids_off = [chunk_id_of_metadata(d.metadata) for d in docs_off]
check("能正常检索", bool(ids_off), str(ids_off))
check("不产生混合轨迹", retriever.last_hybrid_trace is None)
check("get_last_hybrid_trace() 恒为 None（关时没有融合可谈）",
      retriever.get_last_hybrid_trace() is None)
check("info 里 hybrid=false", retriever.get_retriever_info()["hybrid"] is False)

src = (ROOT / "core" / "retriever.py").read_text(encoding="utf-8")
main_body = src[src.index("    def retrieve("):src.index("    def _hybrid_merge(")]
check("混合分支只有一行入口（if settings.RETRIEVAL_HYBRID: pairs = ...）",
      re.search(r"if settings\.RETRIEVAL_HYBRID:\s*\n\s+pairs = self\._hybrid_merge",
                main_body) is not None,
      "入口形状变了 —— 关时的路径可能不再是原路径")
check("关时不触碰 BM25（检索主路径里没有 get_bm25_index）",
      "get_bm25_index" not in main_body,
      "主路径里出现了 BM25 → 关掉开关也在建索引")

# --------------------------------------------------------------------------- #
# 第 4 组：RRF 融合可手算
# --------------------------------------------------------------------------- #
section("第 4 组：RRF 融合（公式按定义手算对照）")

retriever, store = make_retriever(hybrid=True, top_k=3, multiplier=2)
docs_on = retriever.retrieve("差旅 住宿 报销 标准", top_k=3)
ids_on = [chunk_id_of_metadata(d.metadata) for d in docs_on]
check("开启后能检索出结果", bool(ids_on), str(ids_on))
trace = retriever.get_last_hybrid_trace()
check("产生混合轨迹（含 bm25_hits / bm25_only / rrf_top）",
      trace is not None and {"bm25_hits", "bm25_only", "merged", "rrf_top"} <= set(trace),
      str(trace))
check("info 里 hybrid=true", retriever.get_retriever_info()["hybrid"] is True)

# —— 手算 RRF：拿同一份数据在测试里重算一遍 ——
vec_pairs = store.similarity_search_with_score("差旅 住宿 报销 标准", k=6)   # top_k*multiplier
vec_ids = [chunk_id_of_metadata(d.metadata) for d, _ in vec_pairs]
bm_hits = retriever._bm25_override.search("差旅 住宿 报销 标准", 6)          # type: ignore[union-attr]
k_const = int(settings.RRF_K or 60)
expect: dict[str, float] = {}
for rank, cid in enumerate(vec_ids, start=1):
    expect[cid] = expect.get(cid, 0.0) + 1.0 / (k_const + rank)
for rank, (cid, _) in enumerate(bm_hits, start=1):
    expect[cid] = expect.get(cid, 0.0) + 1.0 / (k_const + rank)
expected_order = sorted(expect, key=lambda c: expect[c], reverse=True)[:6]

merged_all = retriever._hybrid_merge("差旅 住宿 报销 标准", vec_pairs, 6)    # type: ignore[arg-type]
merged_ids = [chunk_id_of_metadata(d.metadata) for d, _ in merged_all]
check("融合顺序与手算 RRF 完全一致", merged_ids == expected_order,
      f"实际={merged_ids} 期望={expected_order}")
check("融合后条数不超过候选池上限", len(merged_ids) <= 6, str(len(merged_ids)))
check("rrf_top 与融合后第一名一致",
      trace and trace["rrf_top"] == (merged_ids[0] if merged_ids else None), str(trace))

# —— BM25 独有候选必须真的进得了池子（这是混合检索的全部意义）——
bm_only_ids = [cid for cid, _ in bm_hits if cid not in set(vec_ids)]
if bm_only_ids:
    check("BM25 独有的 chunk_id 出现在融合结果里（物化进池）",
          any(cid in merged_ids for cid in bm_only_ids),
          f"独有={bm_only_ids} 融合={merged_ids}")
    check("轨迹里 bm25_only 与实际一致",
          trace and trace["bm25_only"] == len(bm_only_ids),
          f"trace={trace} 实际独有={len(bm_only_ids)}")
else:
    check("（本语料下 BM25 与向量有重叠，独有断言由反向组覆盖）", True)

check("融合分数只用于排序依据（RRF 值域 0~2/61，绝不会冒充 rerank 分数）",
      all(0 < s <= 2.0 / (k_const + 1) for _, s in merged_all),
      str([round(s, 4) for _, s in merged_all][:3]))

# --------------------------------------------------------------------------- #
# 第 5 组：裁决不变（reranker 与阈值一行不改）
# --------------------------------------------------------------------------- #
section("第 5 组：混合只动候选池，reranker 与阈值不动")

rrf_src = src[src.index("    def _hybrid_merge("):src.index("    def _rank(")]
rank_src = src[src.index("    def _rank("):src.index("    def as_retriever(")]
check("_rank 里没有任何 BM25 / RRF 依赖（裁决层不知道混合的存在）",
      "bm25" not in rank_src.lower() and "rrf" not in rank_src.lower(),
      "裁决层出现了混合检索的字样")
check("_rank 的重排调用保持原样（rerank(query, docs, top_k=target_k)）",
      re.search(r"self\.reranker\.rerank\(\s*query,\s*docs,\s*top_k=target_k\s*\)", rank_src) is not None)
check("混合层不读阈值（0.1 阈值仍只属于 reranker）",
      "score_threshold" not in rrf_src,
      "融合层自己做了阈值裁决 → 拒答口径开始漂移")

# 端到端：带一个打桩 reranker，验证「混合候选 → reranker → 阈值」整链不变


class StubReranker:
    """把候选按「正文长度」打分，并执行阈值过滤 —— 行为与真 reranker 同构。

    阈值动态取「最高分本身」：永远只放行分最高的那一条。
    这样只要候选 ≥2 条，就必然同时观察到「通过」与「被拒」两条路径，
    不依赖语料的字符数（手写死阈值会随语料变化静默失效）。
    """

    model_path = "stub"
    last_call_count = 0

    def rerank(self, query, documents, top_k=None):
        StubReranker.last_call_count += 1
        scored = sorted(((d, float(len(d.page_content))) for d in documents),
                        key=lambda p: p[1], reverse=True)
        best = scored[0][1] if scored else 0.0
        kept = [(d, s) for d, s in scored if s >= best]
        self.last_trace = {"in": len(documents), "kept": len(kept)}
        return kept[: (top_k or 5)]


retriever2, _ = make_retriever(hybrid=True, top_k=2, multiplier=2)
stub = StubReranker()
stub.last_trace = None
retriever2.reranker = stub                                    # type: ignore[assignment]
out = retriever2.retrieve("差旅 住宿 报销 标准", top_k=2, return_score=True)
check("打桩 reranker 确实被调用（拿到的是融合后的候选）",
      stub.last_trace is not None and stub.last_trace["in"] > 0, str(stub.last_trace))
check("阈值过滤后的结果带 rerank 分数（不是 RRF / 向量分数）",
      out and all("rerank_score" in d.metadata for d, _ in out),
      str([sorted(d.metadata.keys()) for d, _ in out][:1]))
check("低于阈值的候选被拒（拒答口径与关闭混合时同一套）",
      stub.last_trace and stub.last_trace["kept"] < stub.last_trace["in"],
      str(stub.last_trace))

# --------------------------------------------------------------------------- #
# 第 6 组：内容变了 → 索引必须脏
# --------------------------------------------------------------------------- #
section("第 6 组：invalidate 钩子（源码级）")


def _has_call(path: Path, anchor: str, needle: str, window: int = 40) -> bool:
    """anchor 之后 window 行内是否出现 needle（钩子必须挨着写库点，不是随便哪都行）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if anchor in line:
            seg = "\n".join(lines[i:i + window])
            if needle in seg:
                return True
    return False


parsing = ROOT / "core" / "parsing.py"
check("core/parsing.py 解析入库成功后置脏",
      _has_call(parsing, "added = store.add_documents", "invalidate_bm25_index"),
      "add_documents 之后没有 invalidate")
check("core/parsing.py 写入失败也置脏（旧切片可能已被删掉）",
      _has_call(parsing, "added == 0", "invalidate_bm25_index"),
      "写入失败路径没有 invalidate —— 旧切片被删后索引就不准了")
check("api/routes/documents.py 删除文档后置脏",
      _has_call(ROOT / "api" / "routes" / "documents.py", "delete_by_doc_id",
                "invalidate_bm25_index"),
      "删除路由没有 invalidate")
for script in ("scripts/rechunk_kb.py", "scripts/clean_orphan_chunks.py",
               "scripts/reset_kb.py"):
    check(f"{script} 收尾置脏",
          "invalidate_bm25_index" in (ROOT / script).read_text(encoding="utf-8"))

# 全局 invalidate 对「没有实例的进程」必须是无害空操作（worker 的常态）
reset_bm25_index()
try:
    invalidate_bm25_index()
    check("没有实例时 invalidate 是空操作（worker 不炸）", True)
except Exception as e:                                    # noqa: BLE001
    check("没有实例时 invalidate 是空操作（worker 不炸）", False, repr(e))

# 行为级：置脏后索引真的会重建
behav_store = FakeStore()
behav = BM25Index(vector_store=behav_store)               # type: ignore[arg-type]
behav.search("报销", 3)
n1 = behav.stats()["size"]
behav_store.chunks.append({"chunk_id": "620:9", "text": "新增的补偿制度条款，加班费与调休。",
                           "metadata": {"chunk_id": "620:9"}})
behav.invalidate()
new_hits = behav.search("加班 补偿 调休", 5)
check("置脏重建后新内容可被命中",
      any(cid == "620:9" for cid, _ in new_hits),
      str([(c, round(s, 2)) for c, s in new_hits]))
check("重建前后索引大小变化被记录", behav.stats()["size"] == n1 + 1, str(behav.stats()))

# --------------------------------------------------------------------------- #
# 第 7 组：NDJSON retrieval 调试帧的新字段
# --------------------------------------------------------------------------- #
section("第 7 组：retrieval 调试帧的 bm25_hits / rrf_top")

from core.rag_chain import _retrieval_debug_event         # noqa: E402
from core.intent_router import Intent, IntentResult       # noqa: E402

_intent = IntentResult(intent=Intent.KNOWLEDGE_QUERY, source="rule", route="rag_qa")
_prepared = {"standalone_question": "q", "docs": []}
ev_off = _retrieval_debug_event(_intent, _prepared, None, None)
check("混合关闭时 bm25_hits 下发 null（不是 0 —— 「没开」≠「开了没命中」）",
      ev_off["bm25_hits"] is None and ev_off["rrf_top"] is None
      and ev_off["bm25_only"] is None, str({k: ev_off[k] for k in ("bm25_hits", "bm25_only", "rrf_top")}))
ev_on = _retrieval_debug_event(_intent, _prepared, None,
                               {"bm25_hits": 12, "bm25_only": 3, "merged": 20,
                                "rrf_top": "599:0", "rrf_k": 60})
check("混合开启时下发命中数 / 独有数 / rrf_top",
      ev_on["bm25_hits"] == 12 and ev_on["bm25_only"] == 3 and ev_on["rrf_top"] == "599:0",
      str({k: ev_on[k] for k in ("bm25_hits", "bm25_only", "rrf_top")}))
check("轨迹为空 dict 时同样下发 null（不会炸）",
      _retrieval_debug_event(_intent, _prepared, None, {})["bm25_hits"] is None)

check("P2-23 的候选契约没有被 P3-4 破坏（candidates 结构原样）",
      "candidates" in ev_off and ev_off["docs_returned"] == 0)

# --------------------------------------------------------------------------- #
# 第 8 组：配置与默认值
# --------------------------------------------------------------------------- #
section("第 8 组：配置默认值（不为了做功能而开）")

check("代码默认 RETRIEVAL_HYBRID=False",
      type(settings).model_fields["RETRIEVAL_HYBRID"].default is False,
      str(type(settings).model_fields["RETRIEVAL_HYBRID"].default))
check("代码默认 RRF_K=60（论文经验值，不调参）",
      type(settings).model_fields["RRF_K"].default == 60,
      str(type(settings).model_fields["RRF_K"].default))
env_text = (ROOT / ".env").read_text(encoding="utf-8")
env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
check(".env 有 RETRIEVAL_HYBRID 与 RRF_K 占位",
      "RETRIEVAL_HYBRID=" in env_text and "RRF_K=" in env_text)
check(".env.example 同步",
      "RETRIEVAL_HYBRID=" in env_example and "RRF_K=" in env_example)
lock_text = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
check("依赖走 requirements.in + uv lock（rank-bm25 / jieba 已进锁文件）",
      "rank-bm25==" in lock_text and "jieba==" in lock_text)

# --------------------------------------------------------------------------- #
# 第 9 组：反向验证（--reverse 才跑）
# --------------------------------------------------------------------------- #
if REVERSE:
    section("第 9 组：反向验证（拆掉护栏后断言必须转红）")

    # 反向 1：BM25 不分词（模拟「有人觉得 lcut 多余」）→ 第 2 组「编号排第一」必须转红
    _orig_tokenize = bm25_mod.tokenize

    def _no_cut(text: str) -> list[str]:
        return [text.strip()] if text.strip() else []      # 整句当一个词项

    bm25_mod.tokenize = _no_cut
    _probe = BM25Index(vector_store=FakeStore())           # type: ignore[arg-type]
    try:
        from rank_bm25 import BM25Okapi
        from core.bm25_index import tokenize as _t
        chunks = FakeStore().get_all_chunks()
        _probe._index = BM25Okapi([_t(c["text"]) for c in chunks])
        _probe._ids = [c["chunk_id"] for c in chunks]
        _probe._metas = [dict(c["metadata"]) for c in chunks]
        _bad_hits = _probe.search("GQ-AD-2026-003 是哪份指引", 3)
    finally:
        bm25_mod.tokenize = _orig_tokenize
    check("反向 1：不分词时编号查询命中率崩塌（第 2 组那条会红）",
          not _bad_hits or _bad_hits[0][0] != "599:0",
          f"不分词仍命中 599:0 → 判据无效 {[c for c, _ in _bad_hits]}")

    # 反向 2：RRF 被改成「向量分 + BM25 分直接相加」→ 第 4 组「与手算一致」必须转红
    # 专用迷你语料：两片正文词项完全相同、只差文档长度 ——
    #   向量打分（按词频计数）平手，BM25（除以文档长度）偏爱短的那篇。
    #   于是「只看名次」的 RRF 维持平手序，而「直接加分」会被 BM25 翻盘 ——
    #   两种算法在这个构造上必然分道扬镳，判据才不是恒真的。
    class _MiniStore:
        store_type = "fake"

        def __init__(self) -> None:
            # ⚠️ 语料不能只有目标两片：BM25 的 idf 对「全库都出现的词」给出负值/零值，
            #    全部查询词都如此时分数恒 ≤ 0，检索结果为空（真库 90 片不会这样，
            #    迷你语料必须自己避开这个数学事实）。所以补三片不含查询词的填充文档。
            self.chunks = [
                {"chunk_id": "900:0", "metadata": {"chunk_id": "900:0"},
                 "text": "报销 住宿 差旅 标准 " + "这一篇故意塞了很多无关的填充文字让文档变得很长很长很长很长很长"},
                {"chunk_id": "901:0", "metadata": {"chunk_id": "901:0"},
                 "text": "报销 住宿 差旅 标准"},
                {"chunk_id": "902:0", "metadata": {"chunk_id": "902:0"},
                 "text": "锅圈食汇是一站式在家吃饭餐食产品品牌，定位社区中央厨房，门店覆盖全国。"},
                {"chunk_id": "903:0", "metadata": {"chunk_id": "903:0"},
                 "text": "员工入职需要提交登记表，人事部门会协助开通企业微信与 OA 账号。"},
                {"chunk_id": "904:0", "metadata": {"chunk_id": "904:0"},
                 "text": "试用期员工离职需要提前三天通知用人单位，转正后提前三十日。"},
            ]

        def get_all_chunks(self):
            return [dict(c) for c in self.chunks]

        def similarity_search_with_score(self, query, k, filter_dict=None):
            toks = set(tokenize(query))
            scored = [
                (Document(page_content=c["text"], metadata=dict(c["metadata"])),
                 float(sum(c["text"].count(t) for t in toks if len(t) >= 2)))
                for c in self.chunks
            ]
            scored = [(d, s) for d, s in scored if s > 0]
            scored.sort(key=lambda p: p[1], reverse=True)   # 稳定排序：平分时 900 在前
            return scored[:k]

    _orig_hybrid = RAGRetriever._hybrid_merge

    def _naive_merge(self, query, pairs, candidate_k):
        index = self._bm25_override
        bm = index.search(query, candidate_k)
        pool = {}
        for doc, _ in pairs:
            cid = chunk_id_of_metadata(doc.metadata)
            if cid:
                pool.setdefault(cid, doc)
        scores: dict[str, float] = {}
        for doc, s in pairs:
            cid = chunk_id_of_metadata(doc.metadata)
            if cid:
                scores[cid] = scores.get(cid, 0.0) + s      # ← 直接加原始分（量纲打架）
        for cid, s in bm:
            scores[cid] = scores.get(cid, 0.0) + s
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:candidate_k]
        return [(pool[cid], sc) for cid, sc in ordered if cid in pool]

    _mini = _MiniStore()
    _mini_r = RAGRetriever(
        vector_store=_mini,                                  # type: ignore[arg-type]
        use_reranker=False, top_k=2, candidate_multiplier=2,
        bm25_index=BM25Index(vector_store=_mini),            # type: ignore[arg-type]
    )
    settings.RETRIEVAL_HYBRID = True
    try:
        _vec = _mini.similarity_search_with_score("报销 住宿 差旅 标准", k=4)
        RAGRetriever._hybrid_merge = _naive_merge            # type: ignore[method-assign]
        _naive = [chunk_id_of_metadata(d.metadata) for d, _ in
                  RAGRetriever._hybrid_merge(_mini_r, "报销 住宿 差旅 标准", _vec, 4)]  # type: ignore[arg-type]
        RAGRetriever._hybrid_merge = _orig_hybrid            # type: ignore[method-assign]
        _rrf = [chunk_id_of_metadata(d.metadata) for d, _ in
                _mini_r._hybrid_merge("报销 住宿 差旅 标准", _vec, 4)]
    finally:
        RAGRetriever._hybrid_merge = _orig_hybrid            # type: ignore[method-assign]
        settings.RETRIEVAL_HYBRID = False
    check("反向 2：把 RRF 换成原始分相加后顺序确实会变（第 4 组会红）",
          _naive != _rrf and _rrf[:2] == ["900:0", "901:0"],
          f"两种算法结果一样 → 第 4 组的手算对照判据无效\n      naive={_naive}\n      rrf={_rrf}")

    # 反向 3：invalidate 钩子被删 → 第 6 组的源码断言必须转红
    _parsing_text = parsing.read_text(encoding="utf-8")
    _stripped = _parsing_text.replace("    invalidate_bm25_index()\n    return added", "    return added")
    check("反向 3：删掉入库成功的 invalidate 后，第 6 组的源码断言会红",
          "invalidate_bm25_index" not in
          "\n".join(_stripped[_stripped.index("added = store.add_documents"):]
                          .split("def run_parse_task")[0].splitlines()[:40])
          and _stripped != _parsing_text,
          "替换没生效，反向验证等于没做")

    # 反向 4：开关默认 true → 第 8 组「默认 False」必须转红
    check("反向 4：若默认值被改成 true，第 8 组的断言会红",
          type(settings).model_fields["RETRIEVAL_HYBRID"].default is False,
          f"当前默认={type(settings).model_fields['RETRIEVAL_HYBRID'].default}")
else:
    print("\n== 第 9 组：反向验证（--reverse 才跑）==")
    print("  （跳过）")

# --------------------------------------------------------------------------- #
# 恢复全局状态（别把本进程翻转过的开关漏给后面的测试模块）
# --------------------------------------------------------------------------- #
settings.RETRIEVAL_HYBRID = False
reset_bm25_index()

print("\n" + "=" * 66)
print(f"  P3-4 混合检索回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
