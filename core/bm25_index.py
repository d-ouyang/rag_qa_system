"""
BM25 关键词索引（v3.0.0 P3-4 混合检索的「另一路」）。

--------------------------------------------------------------------------
为什么向量检索旁边还要一路关键词检索
--------------------------------------------------------------------------
向量检索擅长「意思相近」，不擅长「字面精确」。知识库里恰恰有大量字面精确的需求：

    · 编号 / 条款号：`GQ-HR-2026-005`、`第三章 第六条`
    · 专有名词：品牌名、制度名、部门名
    · 数字与单位：`120 分`、`500 元/晚`、`02517.HK`

这些东西在向量空间里彼此很近（都是「一串制度里的编号」），
于是「GQ-AD-2026-003」和「GQ-HR-2026-005」的余弦距离可能比它和正确答案还小。
而 BM25 走的是完全相反的路子：词项出现 / 词频 / 逆文档频率 ——
编号里那几个字符只要对上，它就一定能捞出来。

两路各有所长，标准做法是**融合**：RRF（Reciprocal Rank Fusion）只看名次不看分数，
天然适合把两路量纲完全不同的结果合在一起（见 core/retriever.py）。

--------------------------------------------------------------------------
为什么不增量维护
--------------------------------------------------------------------------
全库 **90 片**，jieba 分词 + 建 BM25Okapi 索引是**毫秒级**的。
为了省这点时间去做「增量更新」，要额外背三件事：
    ① 增量与全量的边界条件（删一片、改一片、批量导入中途失败）；
    ② 「增量算错了」这种不会报错、只会让检索悄悄变差的问题；
    ③ 一套用来验证增量 == 全量的测试。
所以：**只做全量重建，用一个 invalidate() 置脏标记触发**。
这是本项目一贯的取舍 —— 用可证明的正确性换掉微不足道的性能。

--------------------------------------------------------------------------
为什么是进程内单例（以及它的边界）
--------------------------------------------------------------------------
· backend 是**单进程**（Dockerfile 禁多 worker），进程内索引不会有
  「A 进程改了库、B 进程的索引还是旧的」这种分裂 —— 这是能这么做的**前提**。
· worker **不做检索**，所以它根本不会建这个索引（惰性构建：只在 search 时建）。
  worker 侧的 invalidate() 调用是安全空操作（没有实例就不存在脏数据）。
· 索引只在 backend 进程内，重启即重建，没有落盘 —— 也就没有
  「磁盘上的索引和库里对不上」这类持久化一致性问题。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Sequence

from langchain_core.documents import Document

from core.vector_store import VectorStoreManager, get_vector_store_manager

logger = logging.getLogger(__name__)


def tokenize(text: str) -> list[str]:
    """
    中文分词 → BM25 的词项表。

    ⚠️ 必须先分词：BM25Okapi 按**空白**切词，直接喂中文整句的话，
    「考勤管理制度里迟到怎么处理」会被当成一个词项 —— 词项匹配退化成整句匹配，
    只有逐字相同的查询才命中，等于没有 BM25。

    为什么不再过滤停用词 / 单字：
        · 编号类查询（`GQ-HR-2026-005`）里每一个片段都是关键信号，
          通用停用词表反而会把 `-`、`2026` 这类东西弄丢；
        · IDF 本身已经在压低高频词（「的」「了」几乎不贡献分数），
          额外过滤是重复劳动，而且会引入「过滤表没覆盖到」的未知偏差。
    """
    import jieba

    return [tok for tok in jieba.lcut(text or "") if tok.strip()]


class BM25Index:
    """
    进程内 BM25 索引。**惰性构建**：第一次 search 时才从向量库拉全量切片。

    对外只有三个方法（多了就会有人拿它做不该做的事）：
        `search(query, k)`  → [(chunk_id, 分数), ...]，分数降序
        `invalidate()`      → 置脏，下次 search 前全量重建
        `stats()`           → 观测用（索引大小、最近一次构建耗时）
    """

    def __init__(self, vector_store: VectorStoreManager | None = None) -> None:
        self._vector_store: VectorStoreManager = vector_store or get_vector_store_manager()
        # 锁保护的是「索引对象」本身：构建与置脏都要排他。
        # 构建期间会阻塞其它 search —— 90 片毫秒级，可接受；
        # 换成秒级就要改成读写锁，那时应该先做的是缩小索引而不是换锁。
        self._lock = threading.Lock()
        self._index: Any = None                 # rank_bm25.BM25Okapi 实例
        self._ids: list[str] = []               # 与语料同序的 chunk_id
        self._metas: list[dict[str, Any]] = []  # 与语料同序的元数据（物化 Document 用）
        self._built_at: float | None = None
        self._last_build_ms: float = 0.0

    # ------------------------------------------------------------------ #
    # 对外：置脏
    # ------------------------------------------------------------------ #
    def invalidate(self) -> None:
        """
        标记索引为脏，下次 search 前全量重建。

        调用点：任何改动了向量库内容的地方（解析入库、按文档删除、清孤儿、
        整库重建）。**不重建、只置脏** —— 调用方可能在批量循环里，
        立刻重建会把「写 35 篇」变成「重建 35 次索引」。
        """
        with self._lock:
            if self._index is not None or self._ids:
                logger.info("BM25 索引已置脏，下次检索前全量重建")
            self._index = None
            self._ids = []
            self._metas = []

    # ------------------------------------------------------------------ #
    # 对外：检索
    # ------------------------------------------------------------------ #
    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        """
        关键词检索。

        :param query: 查询文本（会被分词）
        :param k: 最多返回几条
        :return: [(chunk_id, bm25_score), ...]，分数降序。
                 空库 / 空查询 / 无词项命中时返回空列表 —— **不返回 0 分的候选**：
                 0 分意味着一个词项都没命中，把它们塞进 RRF 只会稀释真实命中的名次。
        """
        if k <= 0 or not (query or "").strip():
            return []

        self._ensure_built()
        if self._index is None:
            return []

        tokens = tokenize(query)
        if not tokens:
            return []

        scores = self._index.get_scores(tokens)
        ranked = sorted(
            zip(self._ids, scores), key=lambda item: item[1], reverse=True
        )
        hits = [(cid, float(score)) for cid, score in ranked if score > 0]
        return hits[:k]

    # ------------------------------------------------------------------ #
    # 对外：物化（把命中的 chunk_id 变回 Document）
    # ------------------------------------------------------------------ #
    def materialize(self, chunk_ids: Sequence[str]) -> list[Document]:
        """
        按 chunk_id 取回 Document。

        为什么需要它：BM25 可能命中**向量召回没捞到**的切片（这正是它的价值），
        而 RRF 融合要的是 Document 对象，不是 chunk_id 字符串。
        索引里存着正文与元数据，所以不必再回查向量库 ——
        一次全量拉取已经把这些数据都拿在手上了。

        :return: 与入参同序；找不到的 chunk_id 直接跳过（索引刚被置脏时的正常现象）
        """
        if not chunk_ids:
            return []
        wanted = {str(cid) for cid in chunk_ids}
        if not wanted:
            return []
        # 不锁也能读（list 与 dict 的读取是原子的），但为了让「读到一半被置脏」
        # 不会出现半份索引，这里与构建共用同一把锁。
        with self._lock:
            pairs = [
                (cid, meta) for cid, meta in zip(self._ids, self._metas)
                if cid in wanted
            ]
        # ⚠️ 按入参顺序返回，不是按索引顺序 —— 调用方要的是「我点的这些」
        by_id = {cid: meta for cid, meta in pairs}
        return [
            Document(page_content=meta.get("_text", ""), metadata=_public_meta(meta))
            for cid in chunk_ids
            if (meta := by_id.get(str(cid))) is not None
        ]

    # ------------------------------------------------------------------ #
    # 对外：观测
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        """索引当前状态（日志 / 调试事件用）。"""
        return {
            "built": self._index is not None,
            "size": len(self._ids),
            "built_at": self._built_at,
            "last_build_ms": round(self._last_build_ms, 1),
        }

    # ------------------------------------------------------------------ #
    # 内部：惰性构建
    # ------------------------------------------------------------------ #
    def _ensure_built(self) -> None:
        """双检锁 + 惰性构建。"""
        if self._index is not None:
            return
        with self._lock:
            if self._index is not None:      # 双检：等锁期间可能已被别人建好
                return
            self._build()

    def _build(self) -> None:
        """全量重建。失败时不抛异常 —— 索引不可用只是「少一路召回」，不该让检索整体挂掉。"""
        start = time.perf_counter()
        try:
            from rank_bm25 import BM25Okapi

            chunks = self._vector_store.get_all_chunks()
            if not chunks:
                logger.warning("BM25 索引构建：库里没有可用切片，索引保持为空")
                self._index = None
                self._ids = []
                self._metas = []
                return

            tokenized = [tokenize(chunk["text"]) for chunk in chunks]
            # 全是空词表的语料会让 BM25Okapi 算出 avgdl=0，打分时除零。
            # 这种切片本身也检索不到，直接剔掉。
            keep = [i for i, toks in enumerate(tokenized) if toks]
            if len(keep) < len(tokenized):
                logger.warning(
                    "BM25 索引构建：%d 片分词后为空，已剔除", len(tokenized) - len(keep)
                )
                chunks = [chunks[i] for i in keep]
                tokenized = [tokenized[i] for i in keep]

            self._index = BM25Okapi(tokenized)
            self._ids = [chunk["chunk_id"] for chunk in chunks]
            self._metas = [
                {**chunk["metadata"], "_text": chunk["text"]} for chunk in chunks
            ]
            self._built_at = time.time()
            self._last_build_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "BM25 索引构建完成 | 切片=%d 耗时=%.1fms",
                len(self._ids), self._last_build_ms,
            )
        except Exception as e:
            # 兜底：保持「索引为空」而不是留下一个半建的索引
            logger.error("BM25 索引构建失败，本次检索退化为纯向量召回：%s", e, exc_info=True)
            self._index = None
            self._ids = []
            self._metas = []


def _public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """去掉内部键 `_text`（正文不属于元数据），避免它混进引用与日志。"""
    return {k: v for k, v in meta.items() if k != "_text"}


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_bm25_index: BM25Index | None = None
_bm25_index_lock = threading.Lock()


def get_bm25_index() -> BM25Index:
    """
    取进程内唯一的 BM25 索引（惰性构建，首次 search 才真的建）。

    ⚠️ 只在**做检索的进程**里会走到构建：worker 只写库不检索，
    所以它永远不会持有这个实例 —— 也就不会背着一份用不上的索引占内存。
    """
    global _bm25_index
    if _bm25_index is None:
        with _bm25_index_lock:
            if _bm25_index is None:          # 双检锁
                _bm25_index = BM25Index()
                logger.debug("BM25Index 单例已创建（尚未构建索引）")
    return _bm25_index


def invalidate_bm25_index() -> None:
    """
    通知「内容变了」。

    **没有实例时是空操作**（worker 进程就是这种情况）—— 本进程没建过索引，
    也就不存在需要失效的数据。这个空操作是有意的：调用点（解析 / 删除 / 重建脚本）
    不该关心自己在哪个进程里。
    """
    global _bm25_index
    with _bm25_index_lock:
        if _bm25_index is None:
            logger.debug("invalidate_bm25_index：本进程没有 BM25 实例，跳过（worker 侧的常态）")
            return
        _bm25_index.invalidate()


def reset_bm25_index() -> None:
    """丢弃整个实例（测试切换配置时用）。"""
    global _bm25_index
    with _bm25_index_lock:
        _bm25_index = None
        logger.info("BM25Index 单例已重置")


__all__ = [
    "BM25Index",
    "get_bm25_index",
    "invalidate_bm25_index",
    "reset_bm25_index",
    "tokenize",
]
