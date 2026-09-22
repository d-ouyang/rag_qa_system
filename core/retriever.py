"""
检索模块 —— 封装底层向量库的相似度检索，并按配置决定是否做重排序（Rerank）。

--------------------------------------------------------------------------
为什么需要重排（面试常问）
--------------------------------------------------------------------------
向量检索用的是「双塔 / 句向量」：query 和文档各自独立编码成一个向量，
再用余弦或距离比较。优点是快（可预先建索引），缺点是两个向量之间**没有交互**，
丢掉了词级匹配信息，所以常见现象是：

    「正确的那条排在第 8 位，前 5 条里没有它」  → 召回率够，但排序不准。

重排（CrossEncoder）把「query + 文档」拼成一对一起送进模型打分，
有完整交互，精度高得多，但**必须对每个候选单独算一次**，代价是慢。

所以工业界的标准做法是两阶段：
    第一阶段（召回）：向量检索，快，多召回一些（比如目标条数的 4 倍）
    第二阶段（精排）：CrossEncoder 对候选打分，慢，只挑最好的几条返回

--------------------------------------------------------------------------
本模块要避开的三个经典错误
--------------------------------------------------------------------------
1. 只召回 TOP_K 就直接重排
   候选集本来就是那 K 条，重排只是「把同样的 K 条换个顺序」，召回率没有任何提升。
   → 必须先按 candidate_multiplier 放大候选池，再精排回到 TOP_K。

2. 重排后仍然返回向量分数
   bge-reranker 输出的是 sigmoid 后的 0~1 相关性（越大越相关），
   向量库那边经 vector_store 换算后也是「越大越像」的余弦相似度 [-1, 1]。
   两者数值范围不同（0~1 vs -1~1），但方向一致，都按降序排。
   重排完却把原向量分数返回去，上层就分不清拿到的是哪一套分。
   → 本模块把两种分数都放进 metadata，返回值统一用「那一刻真正用于排序的分数」。
   ⚠️ 注意：向量库**原生**返回的是平方 L2 距离（越小越像），换算发生在
      VectorStoreManager.similarity_search_with_score 里，不要绕过它直接读后端。

3. 用 Document 相等去反查原分数
   Document 的 __eq__ 比较的是 page_content + metadata，
   如果两个不同文件的正文恰好一样，就会匹配到错误的那条。
   → 用 id() 建立「对象 → 分数」的映射来反查。
"""

import logging
import threading
import time
from typing import Any, Sequence

from langchain.retrievers import ContextualCompressionRetriever
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from config.settings import settings
from core.vector_store import VectorStoreManager, get_vector_store_manager

logger = logging.getLogger(__name__)

# 本地目录名 -> HuggingFace 仓库名（本地缺失、需要联网下载时的兜底映射）
# 与嵌入模型同样的坑：settings 里填的是「本地目录名」，不能直接当 HF 仓库 id 用
RERANKER_HF_REPOS: dict[str, str] = {
    "bge_reranker_base": "BAAI/bge-reranker-base",
    "bge_reranker_large": "BAAI/bge-reranker-large",
}

# CrossEncoder 的输入最大长度（超出会被截断，与 bge-reranker 的训练设置一致）
RERANKER_MAX_LENGTH: int = 512


def resolve_reranker_model_path(
    model_name: str | None = None,
) -> tuple[str, str]:
    """
    解析重排模型路径：**本地优先，本地没有才回退到 HuggingFace 仓库名**。

    :param model_name: settings 里配置的模型名（缺省取 settings.RERANKER_MODEL_NAME）
    :return: (实际用于加载的路径, 来源标记 "local" / "remote")
    :raises ValueError: 本地没有、且不知道该模型的远程仓库名时抛出
    """
    name = model_name or settings.RERANKER_MODEL_NAME

    # 本地目录命名规则与嵌入模型一致：把仓库名里的 "/" 换成 "_"
    local_dir = settings.MODELS_DIR / name.replace("/", "_")
    if local_dir.is_dir():
        logger.info("使用本地重排模型：%s", local_dir)
        return str(local_dir), "local"

    # 本地没有 -> 查表拿远程仓库名
    remote_repo = RERANKER_HF_REPOS.get(name)
    if remote_repo is None:
        raise ValueError(
            f"重排模型 '{name}' 本地不存在（期望目录：{local_dir}），"
            f"且未登记对应的 HuggingFace 仓库名（可在 core/retriever.py 的 "
            f"RERANKER_HF_REPOS 中补充）"
        )
    logger.warning("本地未找到重排模型，将从 HuggingFace 下载：%s", remote_repo)
    return remote_repo, "remote"


class CrossEncoderReranker(BaseDocumentCompressor):
    """
    基于交叉编码器（CrossEncoder）的重排序器。

    同时满足两种用法：
        ① 作为 LangChain 的 BaseDocumentCompressor，塞进 ContextualCompressionRetriever
           （实现 compress_documents）
        ② 单独调用 rerank()，拿到「文档 + 重排分数」，便于上层做阈值过滤与溯源
           （实现 rerank，返回 None 表示重排不可用）
    """

    # —— pydantic 字段 ——
    # BaseDocumentCompressor 继承自 pydantic BaseModel，成员变量必须声明成字段，
    # 否则赋值不会生效（或直接被当成未声明属性而报错）。
    model_path: str = ""                       # 模型加载路径（本地目录或 HF 仓库名）
    top_k: int = 5                             # 精排后保留的条数
    score_threshold: float | None = None       # 重排分数阈值，低于它的候选直接丢弃
    model: Any = None                          # 延迟加载的 CrossEncoder 实例

    # CrossEncoder 不是 pydantic 能识别的标准类型，需开放任意类型
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ------------------------------------------------------------------ #
    # 模型加载（延迟）
    # ------------------------------------------------------------------ #
    def _load_model(self) -> Any:
        """
        首次调用时加载模型，失败则返回 None（由调用方降级处理）。

        为什么延迟加载：模型权重 1GB+，进程启动时就加载会让首屏很慢，
        而「没配 USE_RERANKER」或「用户从不提问」的场景根本用不到它。
        """
        if self.model is not None:
            return self.model
        try:
            from sentence_transformers import CrossEncoder

            logger.info("开始加载重排模型（首次调用时加载，之后复用）：%s", self.model_path)
            start = time.perf_counter()
            self.model = CrossEncoder(self.model_path, max_length=RERANKER_MAX_LENGTH)
            logger.info(
                "重排模型加载完成 | 路径=%s 耗时=%.1fs",
                self.model_path,
                time.perf_counter() - start,
            )
        except Exception as e:
            # 加载失败不应该让检索整个挂掉 —— 明确标记不可用，交给上层降级
            logger.error(
                "重排模型加载失败，重排功能将自动关闭：%s | 错误：%s",
                self.model_path,
                e,
                exc_info=True,
            )
            self.model = None
        return self.model

    # ------------------------------------------------------------------ #
    # 对外：带分数的重排
    # ------------------------------------------------------------------ #
    def rerank(
        self,
        query: str,
        documents: Sequence[Document],
        top_k: int | None = None,
    ) -> list[tuple[Document, float]] | None:
        """
        对候选文档按「与 query 的相关性」重新打分并排序。

        :param query: 用户问题
        :param documents: 候选文档
        :param top_k: 返回条数，缺省用 self.top_k
        :return: [(文档, 重排分数), ...]，按分数降序；
                 返回 None 表示「重排不可用」（模型没加载成功），调用方应保留原顺序
        """
        if not documents:
            return []

        if self._load_model() is None:
            return None

        keep = int(top_k or self.top_k)
        start = time.perf_counter()
        try:
            # 交叉编码器的输入必须是「查询-文档对」，而不是单独两个文本
            pairs = [[query, doc.page_content] for doc in documents]
            raw_scores = self.model.predict(pairs)

            # 统一转 Python 原生 float：模型返回的是 numpy.float32，
            # 直接返回会在接口层 JSON 序列化时报错（与模块3 的向量分数是同一个坑）
            scored = [
                (doc, float(score)) for doc, score in zip(documents, raw_scores)
            ]
            # 分数越高越相关 -> 降序
            scored.sort(key=lambda item: item[1], reverse=True)

            if self.score_threshold is not None:
                scored = [item for item in scored if item[1] >= self.score_threshold]

            selected = scored[:keep]
            logger.info(
                "重排完成 | 候选=%d 保留=%d 耗时=%.2fs 分数区间=[%.4f, %.4f]",
                len(documents),
                len(selected),
                time.perf_counter() - start,
                selected[-1][1] if selected else 0.0,
                selected[0][1] if selected else 0.0,
            )
            return selected
        except Exception as e:
            # 打分失败（如超长输入导致 OOM）也不能中断检索链路
            logger.error("重排打分失败，降级为原顺序：%s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # LangChain 压缩器接口
    # ------------------------------------------------------------------ #
    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Any = None,
    ) -> Sequence[Document]:
        """
        LangChain BaseDocumentCompressor 要求的接口，供 ContextualCompressionRetriever 调用。

        注意：这里返回的是 Document 列表（不含分数）。
        需要分数时用上面的 rerank()。
        """
        if not documents:
            return []

        reranked = self.rerank(query, documents, top_k=self.top_k)
        if reranked is None:
            # 重排不可用：保持向量检索给出的顺序，只截断到 top_k
            logger.warning("重排不可用，保持原顺序返回前 %d 条", self.top_k)
            return list(documents[: self.top_k])

        # 把重排分数写进元数据，方便链下游 / 前端展示溯源依据
        results: list[Document] = []
        for doc, score in reranked:
            metadata = dict(doc.metadata)
            metadata["rerank_score"] = round(score, 6)
            results.append(Document(page_content=doc.page_content, metadata=metadata))
        return results


class RAGRetriever:
    """
    RAG 检索器 —— 串联「向量召回」与「可选重排」。

    职责边界：只负责把相关片段找出来并重排，
    不负责拼接 prompt、不调用大模型（那是 rag_chain 的事）。
    """

    def __init__(
        self,
        vector_store: VectorStoreManager | None = None,
        use_reranker: bool | None = None,
        reranker_model: str | None = None,
        top_k: int | None = None,
        candidate_multiplier: int | None = None,
        score_threshold: float | None = None,
    ) -> None:
        """
        :param vector_store: 向量库管理器，缺省用全局单例
        :param use_reranker: 是否启用重排，缺省读 settings.USE_RERANKER
        :param reranker_model: 重排模型名，缺省读 settings.RERANKER_MODEL_NAME
        :param top_k: 最终返回条数，缺省读 settings.SEARCH_TOP_K
        :param candidate_multiplier: 候选池倍数，缺省读 settings.RERANK_CANDIDATE_MULTIPLIER
        :param score_threshold: 重排分数阈值，缺省读 settings.RERANK_SCORE_THRESHOLD
        """
        self.vector_store: VectorStoreManager = vector_store or get_vector_store_manager()
        self.top_k: int = int(top_k or settings.SEARCH_TOP_K)
        self.candidate_multiplier: int = int(
            candidate_multiplier or settings.RERANK_CANDIDATE_MULTIPLIER
        )
        self.score_threshold: float | None = (
            score_threshold if score_threshold is not None else settings.RERANK_SCORE_THRESHOLD
        )

        enable: bool = settings.USE_RERANKER if use_reranker is None else use_reranker
        self.reranker: CrossEncoderReranker | None = (
            self._build_reranker(reranker_model) if enable else None
        )
        if not enable:
            logger.info("重排未启用（settings.USE_RERANKER=False）")

        logger.info(
            "检索器初始化完成 | top_k=%d 候选池倍数=%d 重排=%s 后端=%s",
            self.top_k,
            self.candidate_multiplier,
            self.reranker.model_path if self.reranker else "关闭",
            self.vector_store.store_type,
        )

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _build_reranker(self, reranker_model: str | None) -> CrossEncoderReranker | None:
        """
        构建重排序器。

        这里**故意不在初始化时加载模型**（CrossEncoderReranker 内部延迟加载），
        所以「配置了某个模型路径」与「模型真能加载」是两件事：
        路径解析失败在这里记错并返回 None（自动降级为不重排），
        模型加载失败在首次检索时处理，同样降级。
        """
        try:
            model_path, source = resolve_reranker_model_path(reranker_model)
        except ValueError as e:
            logger.error("重排序器初始化失败，将降级为纯向量检索：%s", e)
            return None

        reranker = CrossEncoderReranker(
            model_path=model_path,
            top_k=self.top_k,
            score_threshold=self.score_threshold,
        )
        logger.info("重排序器已就绪 | 来源=%s 路径=%s top_k=%d", source, model_path, self.top_k)
        return reranker

    # ------------------------------------------------------------------ #
    # 核心检索
    # ------------------------------------------------------------------ #
    def _candidate_k(self, target_k: int) -> int:
        """
        计算向量召回阶段要取多少候选。

        启用重排时必须放大候选池，否则「重排」退化成「给同样的 K 条换个顺序」，
        召回率没有任何提升 —— 这是重排落地时最常见的无效实现。
        """
        if self.reranker is None or self.candidate_multiplier <= 1:
            return target_k
        candidate_k = target_k * self.candidate_multiplier
        logger.debug(
            "启用重排，扩大候选池：%d -> %d（倍数=%d）",
            target_k,
            candidate_k,
            self.candidate_multiplier,
        )
        return candidate_k

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filter_dict: dict[str, str] | None = None,
        return_score: bool = False,
    ) -> list[Document] | list[tuple[Document, float]]:
        """
        检索与 query 相关的文档片段。

        :param query: 用户问题
        :param top_k: 最终返回条数，缺省用 self.top_k
        :param filter_dict: 元数据过滤条件，如 {"source": "/path/to/file.txt"}
        :param return_score: True 时返回 [(Document, 分数), ...]，分数**越大越相关**
        :return: Document 列表，或 (Document, 分数) 列表
        """
        if not query or not query.strip():
            logger.warning("收到空查询，直接返回空结果")
            return []

        # 目标条数做一次防御：0 或负数会让底层检索行为不可预期
        target_k = int(top_k or self.top_k)
        if target_k <= 0:
            logger.warning("top_k 必须为正数（收到 %d），已回退为 %d", target_k, self.top_k)
            target_k = self.top_k

        start = time.perf_counter()
        candidate_k = self._candidate_k(target_k)

        # ① 向量召回：拿到候选 + 向量分数
        pairs = self.vector_store.similarity_search_with_score(
            query, k=candidate_k, filter_dict=filter_dict
        )

        # ② 精排 / 或直接截断
        scored = self._rank(query, pairs, target_k)

        logger.info(
            "检索完成 | 候选=%d 返回=%d 重排=%s 耗时=%.2fs | query=%.24s",
            len(pairs),
            len(scored),
            "开" if self.reranker else "关",
            time.perf_counter() - start,
            query,
        )
        return scored if return_score else [doc for doc, _ in scored]

    def _rank(
        self,
        query: str,
        pairs: Sequence[tuple[Document, float]],
        target_k: int,
    ) -> list[tuple[Document, float]]:
        """
        把「候选 + 向量分数」整理成最终结果，并处理重排的降级路径。

        返回的分数语义（两者都是**越大越相关**）：
            · 未重排 -> 余弦相似度 [-1, 1]，vector_store 已从后端距离换算好
            · 已重排 -> 重排分数 0~1（越大越相关）
        两种分数都写进了 Document.metadata，避免上层只能二选一。
        """
        if not pairs:
            return []

        docs = [doc for doc, _ in pairs]

        # 不重排：保持向量库的顺序，只截断到目标条数
        if self.reranker is None:
            return [(doc, float(score)) for doc, score in pairs[:target_k]]

        # 用 id() 建立「对象 -> 余弦相似度」映射，后续按同一个对象反查。
        # 不能靠 Document.__eq__ 反查：内容相同的两个不同文件会被判定为相等而错配。
        vector_similarities: dict[int, float] = {id(doc): float(score) for doc, score in pairs}

        reranked = self.reranker.rerank(query, docs, top_k=target_k)
        if reranked is None:
            # 重排不可用（模型没加载成功或打分失败）-> 降级为纯向量结果
            logger.warning("重排不可用，已降级为纯向量检索结果")
            return [(doc, vector_similarities[id(doc)]) for doc in docs[:target_k]]

        results: list[tuple[Document, float]] = []
        for doc, rerank_score in reranked:
            vector_similarity = vector_similarities.get(id(doc))
            # 重新构造 Document：避免改动上层可能还在引用的同一批对象
            metadata = dict(doc.metadata)
            # 键名用 vector_similarity 而不是 vector_score：明确它是「越大越像」的相似度，
            # 免得上层再按「越小越像的距离」去用（后端原生给的确实是距离，已在 vector_store 换算过）
            metadata["vector_similarity"] = round(vector_similarity, 6) if vector_similarity is not None else None
            metadata["rerank_score"] = round(rerank_score, 6)
            scored_doc = Document(page_content=doc.page_content, metadata=metadata)
            results.append((scored_doc, rerank_score))
        return results

    # ------------------------------------------------------------------ #
    # 适配 LangChain 链
    # ------------------------------------------------------------------ #
    def as_retriever(self, search_kwargs: dict[str, Any] | None = None) -> BaseRetriever:
        """
        返回可直接接入 LCEL 链的 Retriever。

        启用重排时包一层 ContextualCompressionRetriever：
        它会先取回候选，再调用重排器压缩（压缩器就是 CrossEncoderReranker）。

        注意 search_kwargs 里的 k 要按候选池放大后的值传，
        否则链在线时又退化成「只召回 TOP_K 再重排」。
        """
        kwargs = search_kwargs or {"k": self._candidate_k(self.top_k)}
        base_retriever = self.vector_store.as_retriever(search_kwargs=kwargs)
        if self.reranker is None:
            return base_retriever
        return ContextualCompressionRetriever(
            base_compressor=self.reranker,
            base_retriever=base_retriever,
        )

    # ------------------------------------------------------------------ #
    # 状态信息
    # ------------------------------------------------------------------ #
    def get_retriever_info(self) -> dict[str, Any]:
        """返回检索器当前配置（供日志 / 状态页 / 接口展示）。"""
        return {
            "top_k": self.top_k,
            "candidate_multiplier": self.candidate_multiplier,
            "candidate_k": self._candidate_k(self.top_k),
            "use_reranker": self.reranker is not None,
            "reranker_model": self.reranker.model_path if self.reranker else None,
            "score_threshold": self.score_threshold,
            "vector_store_type": self.vector_store.store_type,
        }


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_rag_retriever: RAGRetriever | None = None
_rag_retriever_lock = threading.Lock()


def get_rag_retriever() -> RAGRetriever:
    """获取全局唯一的检索器实例（内部持有已连接的向量库，避免重复初始化）。"""
    global _rag_retriever
    if _rag_retriever is None:
        with _rag_retriever_lock:
            if _rag_retriever is None:          # 双检锁
                _rag_retriever = RAGRetriever()
                logger.debug("RAGRetriever 单例已创建")
    return _rag_retriever


def reset_rag_retriever() -> None:
    """重置单例（测试切换配置时用）。"""
    global _rag_retriever
    with _rag_retriever_lock:
        _rag_retriever = None
        logger.info("RAGRetriever 单例已重置")
