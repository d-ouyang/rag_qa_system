"""
向量库管理模块 —— 用同一套接口封装 Chroma 与 FAISS 两种实现。

把「文档片段」交给嵌入模型转成向量，再存进本地向量库持久化，
供后续检索（召回）使用。

--------------------------------------------------------------------------
Chroma 与 FAISS 的关键差异（面试常问，也是本模块写法的由来）
--------------------------------------------------------------------------
1. 持久化方式
   · Chroma：自带嵌入式数据库（sqlite + hnsw 索引），指定 persist_directory 后**写入即自动落盘**。
     不要调用 store.persist()——langchain 已把它标记为 deprecated（Chroma 0.4+ 自动持久化）。
   · FAISS：**纯内存索引**，必须显式 save_local() 才会落盘。
     所以每一次「增 / 删」之后都必须立刻保存，否则进程退出后新数据全部丢失。
     这是两者最容易踩的坑。

2. 删除能力
   · Chroma：原生支持按 id、也支持按元数据条件删除（where={"source": ...}）。
   · FAISS：只有按 id 删除的能力（delete(ids)），没有元数据条件删除，
     要「按来源删除」必须自己先筛出目标 id 再删。

3. 元数据过滤
   · Chroma：在数据库侧**先过滤再召回**，返回条数精确。
   · FAISS：**先取 fetch_k 个候选、再在 Python 侧按元数据过滤**，
     过滤后返回条数可能不足 k——所以开启过滤时必须放大 fetch_k。

4. 相似度分数
   · 两者都是「分数越小越相似」，但量纲不同（FAISS 是 L2 距离），
     不要跨库比较分数，也不要写死阈值。

--------------------------------------------------------------------------
职责边界
--------------------------------------------------------------------------
本模块只负责「存 / 删 / 查」，**不接大模型**：
    检索返回的是原始片段（Document 或 Document + 分数），
    重排（rerank）与答案生成属于 retriever / rag_chain 的职责。
"""
import logging
import threading
from pathlib import Path
from typing import Any

from langchain_community.vectorstores import FAISS, Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

from config.settings import settings
from core.embedding import get_embedding_model, get_embedding_model_info

logger = logging.getLogger(__name__)


class VectorStoreManager:
    """向量库管理器：统一 Chroma / FAISS 的初始化、增删查、状态与清空操作。"""

    # 默认 collection 名（Chroma 的概念；FAISS 没有 collection，仅用于状态展示）
    DEFAULT_COLLECTION_NAME: str = "rag_qa_knowledge"

    # FAISS 落盘会写出这两个文件，清空时按名字精确删除
    FAISS_INDEX_FILE: str = "index.faiss"
    FAISS_DOCSTORE_FILE: str = "index.pkl"

    def __init__(
        self,
        store_type: str | None = None,
        persist_dir: str | Path | None = None,
        collection_name: str | None = None,
    ) -> None:
        """
        :param store_type: 向量库类型，chroma / faiss；缺省读 settings.VECTOR_STORE_TYPE
        :param persist_dir: 持久化目录；缺省读 settings.VECTOR_DB_DIR
        :param collection_name: Chroma 的 collection 名；缺省用 DEFAULT_COLLECTION_NAME

        为什么把这三个参数开放出来：
            1) 可测试——单测要同时验证 chroma 与 faiss，且必须写到临时目录，不能污染真实 vector_db/；
            2) 多知识库——将来要做「多个隔离的知识库」时，换个 persist_dir 就是一个新库。
        生产代码统一走模块底部的 get_vector_store_manager() 单例入口。
        """
        # 后端类型固化在实例上：避免每个方法都去读全局 settings，
        # 否则运行中改配置会让同一个管理器前后使用不同后端（行为不可预测）
        self.store_type: str = store_type or settings.VECTOR_STORE_TYPE
        self.persist_dir: Path = (
            Path(persist_dir) if persist_dir is not None else settings.VECTOR_DB_DIR
        )
        self.collection_name: str = collection_name or self.DEFAULT_COLLECTION_NAME

        # 嵌入模型是单例，这里拿到的是同一个对象，不会重复加载
        self.embedding_model: HuggingFaceEmbeddings = get_embedding_model()

        # 底层向量库对象：Chroma 实例 / FAISS 实例。
        # 注意：FAISS 在「索引文件还不存在」时保持 None，表示「空库」，
        # 等第一次写入时再创建索引（原因见 _init_faiss 的注释）
        self._store: Chroma | FAISS | None = None

        logger.info("初始化向量库 | 类型=%s | 目录=%s | collection=%s",
                    self.store_type, self.persist_dir, self.collection_name)
        self._initialize_vector_store()

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _initialize_vector_store(self) -> None:
        """按配置分派到对应后端的初始化逻辑。"""
        if self.store_type == "chroma":
            self._init_chroma()
        elif self.store_type == "faiss":
            self._init_faiss()
        else:
            raise ValueError(
                f"不支持的向量库类型：{self.store_type}（可选 chroma / faiss）"
            )

    def _init_chroma(self) -> None:
        """初始化 Chroma：指定持久化目录即自动落盘。"""
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self._store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embedding_model,
            persist_directory=str(self.persist_dir),
        )
        # ⚠️ 不要调用 self._store.persist()：Chroma 0.4+ 写入即持久化，
        #    该方法已被 langchain 标记 deprecated（调用会报 DeprecationWarning 且没实际作用）
        logger.info("Chroma 已就绪 | 目录=%s | collection=%s | 现有片段=%d",
                    self.persist_dir, self.collection_name, self.count())

    def _init_faiss(self) -> None:
        """初始化 FAISS：有索引文件就加载，没有就标记为空库（延迟创建）。"""
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        index_file = self.persist_dir / self.FAISS_INDEX_FILE

        if index_file.exists():
            # allow_dangerous_deserialization=True 是必须的：FAISS 的 docstore 用 pickle 存，
            # 反序列化有代码执行风险。这里加载的是我们自己生成的本地文件，所以可以接受。
            self._store = FAISS.load_local(
                str(self.persist_dir),
                self.embedding_model,
                allow_dangerous_deserialization=True,
            )
            logger.info("FAISS 索引已加载 | 目录=%s | 向量数=%d", self.persist_dir, self.count())
            return

        # 索引不存在 → 空库。
        # 这里刻意**不**像参考实现那样塞一条假文档（Document(page_content="初始化向量库")）来建索引：
        #   假文档会被真正写进索引，既能被检索命中、又会让 count() 多算一条，
        #   属于「为了绕开 API 限制而污染业务数据」。
        # 正确做法：空库就让它空着，等第一次 add_documents 时用 from_documents 真正创建。
        self._store = None
        logger.info("FAISS 索引不存在，初始化为空库（首次写入时创建索引）| 目录=%s", self.persist_dir)

    def _save_faiss(self) -> None:
        """
        把 FAISS 内存索引落盘。

        FAISS 是纯内存索引，不调用这个方法，进程退出后新增的数据会全部丢失。
        所以 add_documents / delete_by_source 之后都必须调用它。
        Chroma 会自动持久化，走到这里直接跳过。
        """
        store = self._store
        # 用 isinstance 而不是比较 store_type 字符串：既让类型收窄（Chroma 没有 save_local），
        # 又保证「后端类型与实例不匹配」这种异常情况不会误操作
        if not isinstance(store, FAISS):
            return
        store.save_local(str(self.persist_dir))
        logger.debug("FAISS 索引已落盘 | 目录=%s | 向量数=%d", self.persist_dir, self.count())

    # ------------------------------------------------------------------ #
    # 统计辅助
    # ------------------------------------------------------------------ #
    def count(self) -> int:
        """当前向量库中的片段总数。"""
        store = self._store
        if store is None:            # FAISS 空库
            return 0
        if isinstance(store, Chroma):
            # langchain 的 Chroma 没有暴露公开的 count()，只能拿到它内部持有的 chromadb Collection。
            # 用 getattr 而不是 store._collection 的原因：① _collection 是私有属性，
            # 点语法会触发「访问私有成员」的静态告警；② 依赖内部结构变化时 getattr 不会直接抛异常。
            # 但 getattr 的代价是「属性名写错也静默拿到 None」，所以这里显式告警——
            # 否则 count() 会变成永远返回 0 的无声故障（比报错难查得多）。
            collection = getattr(store, "_collection", None)
            if collection is None:
                logger.warning("无法从 Chroma 实例取到 _collection，count 暂返回 0（请检查依赖版本）")
                return 0
            return int(collection.count())

        # FAISS：index.ntotal 就是索引里的向量条数（同理用 getattr 并显式告警）
        index = getattr(store, "index", None)
        if index is None:
            logger.warning("无法从 FAISS 实例取到 index，count 暂返回 0（请检查依赖版本）")
            return 0
        return int(index.ntotal)

    def _collect_metadatas(self) -> list[dict[str, Any]]:
        """取出库里所有片段的元数据（用于统计收录了哪些来源文件）。"""
        store = self._store
        if store is None:
            return []
        if isinstance(store, Chroma):
            # include=["metadatas"] 表示只要元数据，不要正文和向量，减少数据传输
            result = store.get(include=["metadatas"])
            return [m for m in (result.get("metadatas") or []) if m]
        # FAISS：docstore 里保存着 id → Document 的映射
        docstore = getattr(store, "docstore", None)
        return [
            doc.metadata
            for doc in getattr(docstore, "_dict", {}).values()
            if getattr(doc, "metadata", None)
        ]

    # ------------------------------------------------------------------ #
    # 新增
    # ------------------------------------------------------------------ #
    def add_documents(self, documents: list[Document]) -> int:
        """
        新增文档片段到向量库（内部会自动完成「文本 → 向量」的嵌入计算）。

        :param documents: 文档片段列表（一般来自 DocumentLoader，元数据里带 source/file_name/file_type）
        :return: 实际写入的片段数；失败返回 0（不抛异常，避免单个文件写库失败拖垮整批导入）
        """
        if not documents:
            logger.warning("add_documents 收到空列表，跳过写入")
            return 0

        try:
            # 只有 FAISS 的「空库」需要特殊处理：此时还没有索引对象，没法 add，
            # 必须用 from_documents 顺手把索引建出来。
            # Chroma（初始化时一定已是实例）和「已有索引的 FAISS」都走下面通用的 add_documents。
            if self._store is None:
                self._store = FAISS.from_documents(documents, self.embedding_model)
            else:
                self._store.add_documents(documents)

            # FAISS 必须立刻落盘；Chroma 会自动持久化，这里调用是空操作
            self._save_faiss()

            logger.info("新增 %d 个片段成功 | 当前总量=%d", len(documents), self.count())
            return len(documents)
        except Exception as e:
            logger.error("新增片段失败：%s", e, exc_info=True)
            return 0

    # ------------------------------------------------------------------ #
    # 删除
    # ------------------------------------------------------------------ #
    def delete_by_source(self, source: str) -> int:
        """
        按来源（文件路径）删除该文件的全部片段。

        :param source: 元数据里的 source 字段，即文件的完整路径。
                       **必须用 source（唯一），不要用 file_name（可重复）**——
                       否则不同目录下的同名文件会被一起删掉。
        :return: 删除的片段数
        """
        store = self._store
        if store is None:
            logger.warning("FAISS 为空库，无需删除 | source=%s", source)
            return 0

        try:
            # 用 isinstance 分派：既让类型收窄，也避免 store_type 与真实实例不一致时误操作
            if isinstance(store, Chroma):
                return self._delete_by_source_chroma(store, source)
            logger.warning("FAISS 模式下按来源删除需要遍历全部 id，数据量大时较慢，建议优先用 Chroma")
            return self._delete_by_source_faiss(store, source)
        except Exception as e:
            logger.error("按来源删除失败 | source=%s | 错误：%s", source, e, exc_info=True)
            return 0

    def _delete_by_source_chroma(self, store: Chroma, source: str) -> int:
        """Chroma：先按元数据条件查出 id，再按 id 删除（数据库侧过滤，效率高）。"""
        result = store.get(where={"source": source})
        ids_to_delete = list(result.get("ids") or [])
        if not ids_to_delete:
            logger.info("未找到该来源的片段，无需删除 | source=%s", source)
            return 0

        store.delete(ids=ids_to_delete)
        logger.info("按来源删除成功 | source=%s | 删除片段=%d | 剩余=%d",
                    source, len(ids_to_delete), self.count())
        return len(ids_to_delete)

    def _delete_by_source_faiss(self, store: FAISS, source: str) -> int:
        """
        FAISS：没有元数据条件删除，只能自己筛出 id 再删。

        步骤：
            1. 从 docstore 取出 id → Document 的全部映射；
            2. 挑出 metadata.source 等于目标来源的 id；
            3. 调用 FAISS.delete(ids)——它会同时从索引和 docstore 中移除（注意这里传的是 **id 列表**，
               不是 Document 列表，这是参考实现里传错类型导致删除必崩的地方）；
            4. 立刻 save_local() 落盘，否则重启后「删掉的数据又回来了」。
        """
        docstore = getattr(store, "docstore", None)
        id_to_doc: dict[str, Document] = dict(getattr(docstore, "_dict", {}))

        # 用 (metadata or {}) 兜底：个别片段可能没有元数据，直接 .get 会抛 AttributeError
        target_ids = [
            doc_id
            for doc_id, doc in id_to_doc.items()
            if (getattr(doc, "metadata", None) or {}).get("source") == source
        ]
        if not target_ids:
            logger.info("未找到该来源的片段，无需删除 | source=%s", source)
            return 0

        store.delete(target_ids)
        self._save_faiss()              # 关键：删除后立即落盘
        logger.info("按来源删除成功（FAISS） | source=%s | 删除片段=%d | 剩余=%d",
                    source, len(target_ids), self.count())
        return len(target_ids)

    # ------------------------------------------------------------------ #
    # 检索（只做向量召回，不接大模型）
    # ------------------------------------------------------------------ #
    def similarity_search(
        self,
        query: str,
        k: int | None = None,
        filter_dict: dict[str, Any] | None = None,
    ) -> list[Document]:
        """
        相似度检索：返回最相似的 k 个片段（**不调用大模型**，只做向量召回）。

        :param query: 用户问题
        :param k: 返回条数，缺省读 settings.SEARCH_TOP_K
        :param filter_dict: 元数据过滤条件，如 {"source": "/path/a.txt"} 或 {"file_type": "pdf"}
        :return: Document 列表（相似度由高到低）
        """
        store = self._store
        if store is None:
            logger.warning("向量库为空（FAISS 空库），检索直接返回空结果")
            return []

        k = k or settings.SEARCH_TOP_K
        kwargs = self._build_filter_kwargs(k, filter_dict)
        logger.debug("执行相似度检索 | k=%d | filter=%s | 后端=%s", k, filter_dict, self.store_type)

        results = store.similarity_search(query, k=k, **kwargs)
        logger.info("检索完成 | 命中=%d 条 | k=%d", len(results), k)
        return results

    def similarity_search_with_score(
        self,
        query: str,
        k: int | None = None,
        filter_dict: dict[str, Any] | None = None,
    ) -> list[tuple[Document, float]]:
        """
        相似度检索并返回相关性分数（**不调用大模型**）。

        :return: [(Document, 分数), ...]，分数**越小越相似**。
                 注意：Chroma 与 FAISS 的分数量纲不同，不要跨库比较，也不要在业务里写死阈值。
        """
        store = self._store
        if store is None:
            logger.warning("向量库为空（FAISS 空库），检索直接返回空结果")
            return []

        k = k or settings.SEARCH_TOP_K
        kwargs = self._build_filter_kwargs(k, filter_dict)
        logger.debug("执行带分数的相似度检索 | k=%d | filter=%s | 后端=%s", k, filter_dict, self.store_type)

        raw_results = store.similarity_search_with_score(query, k=k, **kwargs)
        # 统一转成 Python 原生 float：
        # FAISS 返回的是 numpy.float32，直接放进 FastAPI 响应会报
        # 「Object of type float32 is not JSON serializable」；Chroma 返回的本来就是 float。
        # 这是两个后端又一个隐蔽差异，在这里一次性抹平。
        results = [(doc, float(score)) for doc, score in raw_results]
        logger.info("检索完成（带分数） | 命中=%d 条 | 最高相似度=%.4f",
                    len(results), results[0][1] if results else -1.0)
        return results

    def _build_filter_kwargs(self, k: int, filter_dict: dict[str, Any] | None) -> dict[str, Any]:
        """
        构造检索的额外参数，抹平两个后端在「元数据过滤」上的差异。

        · 不传过滤条件：两边都不用额外参数。
        · Chroma：直接在数据库侧用 where 过滤（精确）。
        · FAISS：先按 fetch_k 取候选、再在内存里过滤，所以候选池必须放大，
          否则「过滤后不足 k 条」——这是 FAISS 过滤最容易踩的坑。
        """
        if not filter_dict:
            return {}
        if self.store_type == "faiss":
            # 放大候选池：经验值是 k 的 4 倍，且不低于 FAISS 的默认值 20
            return {"filter": filter_dict, "fetch_k": max(k * 4, 20)}
        return {"filter": filter_dict}

    # ------------------------------------------------------------------ #
    # 状态元数据
    # ------------------------------------------------------------------ #
    def get_stats(self) -> dict[str, Any]:
        """
        获取向量库的状态元数据（供知识库管理页 / 健康检查 / 调试接口展示）。

        返回字段：
            vector_store_type    当前后端类型（chroma / faiss）
            persist_directory    持久化目录
            collection_name      Chroma 的 collection 名（FAISS 无此概念，返回 None）
            total_vectors        片段总数
            source_count         收录的文件数（按 source 去重）
            sources              收录的文件路径列表
            embedding_*          嵌入模型信息（来源 / 设备 / 维度），来自 embedding 模块
        """
        metadatas = self._collect_metadatas()
        # 用 set 去重：同一个文件会被切成很多片段，它们的 source 是相同的
        sources = sorted({str(m.get("source")) for m in metadatas if m.get("source")})
        embedding_info = get_embedding_model_info()

        stats: dict[str, Any] = {
            "vector_store_type": self.store_type,
            "persist_directory": str(self.persist_dir),
            "collection_name": self.collection_name if self.store_type == "chroma" else None,
            "total_vectors": self.count(),
            "source_count": len(sources),
            "sources": sources,
            "embedding_model": embedding_info["model_name"],
            "embedding_loaded_from": embedding_info["loaded_from"],
            "embedding_device": embedding_info["device"],
            "embedding_dimension": embedding_info["dimension"],
        }
        logger.debug("向量库状态：%s", stats)
        return stats

    # ------------------------------------------------------------------ #
    # 清空
    # ------------------------------------------------------------------ #
    def clear_all(self) -> None:
        """清空向量库中的全部数据，并把管理器恢复到「空库可用」状态。"""
        store = self._store
        try:
            if isinstance(store, Chroma):
                # 删掉整张 collection，再新建一张空的——
                # 比「先查全部 id 再逐个 delete」快得多，也干净
                store.delete_collection()
                self._init_chroma()
            else:
                # 只删 FAISS 自己的两个文件。
                # 不要用 shutil.rmtree(整个目录)：persist_dir 里可能还放着别的数据
                # （例如你曾把 VECTOR_STORE_TYPE 切成 chroma，那目录里就有 chroma.sqlite3），
                # 一把梭会把不属于 FAISS 的数据也删掉。
                for file_name in (self.FAISS_INDEX_FILE, self.FAISS_DOCSTORE_FILE):
                    target = self.persist_dir / file_name
                    if target.exists():
                        target.unlink()
                        logger.debug("已删除 FAISS 索引文件：%s", target)
                # 回到「空库」状态，下次写入时重建索引
                self._store = None

            logger.info("向量库已清空 | 类型=%s | 目录=%s", self.store_type, self.persist_dir)
        except Exception as e:
            logger.error("清空向量库失败：%s", e, exc_info=True)
            raise


# --------------------------------------------------------------------------- #
# 全局单例
# --------------------------------------------------------------------------- #
# 为什么也要单例：向量库对象持有数据库连接 / 内存索引，
# 每次请求都新建会重复打开连接、且 FAISS 每次都要重新加载索引文件。
_vector_store_manager: "VectorStoreManager | None" = None
_vector_store_lock: threading.Lock = threading.Lock()


def get_vector_store_manager() -> VectorStoreManager:
    """获取全局唯一的向量库管理器（业务代码统一从这里拿）。"""
    global _vector_store_manager
    if _vector_store_manager is None:
        with _vector_store_lock:
            if _vector_store_manager is None:
                _vector_store_manager = VectorStoreManager()
    return _vector_store_manager


def reset_vector_store_manager() -> None:
    """
    丢弃当前单例。

    适用场景：
      1) 运行中把 settings.VECTOR_STORE_TYPE 从 chroma 切到 faiss 后，需要重建管理器；
      2) 测试需要隔离——避免用例之间互相影响。
    """
    global _vector_store_manager
    with _vector_store_lock:
        _vector_store_manager = None
    logger.info("向量库管理器单例已重置，下次调用将按当前配置重建")
