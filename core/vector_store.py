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
   · 后端原生返回的是「距离」：Chroma 默认 hnsw:space="l2"、FAISS 的 IndexFlatL2
     返回的都是**平方 L2**（FAISS 刻意不开方省算力），两者量纲其实一致，都是越小越相似。
   · 本模块在返回前统一换算成**余弦相似度**（越大越像，范围 [-1, 1]），见 _distance_to_similarity。
     换算前提是嵌入向量已归一化（normalize_embeddings=True，见 core/embedding.py）：
     归一化后 L2² = ‖a‖² + ‖b‖² - 2a·b = 2 - 2cosθ，故 cos θ = 1 - L2²/2。
     不归一化时距离里混着模长项，这个等式不成立 —— 所以初始化时会做一次自检。
   · 坑：Chroma 若被配成 hnsw:space="cosine"，返回的是 cosine distance = 1 - cos（范围 [0, 2]），
     与上面的换算不兼容。本项目统一用默认 l2，不要动 collection_metadata。

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

import chromadb
from langchain_community.vectorstores import FAISS, Chroma
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_huggingface import HuggingFaceEmbeddings

from config.settings import settings
from core.embedding import get_embedding_model, get_embedding_model_info

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 切片引用键 chunk_id（v2.0.0 P0-4a）
# --------------------------------------------------------------------------- #
# 形态：`"<doc_id>:<chunk_index>"`，例如 `"12:3"` = 第 12 号文档的第 3 个切片。
#
# 为什么**不用 Chroma 自己那条记录的 UUID** 当引用键：
#
#   ① 拿不到。`langchain_community` 0.3.x 的 similarity_search_with_score 会把
#      Chroma 的 id 丢掉（返回的 Document.id 是 None），而重排与 _extract_sources
#      只透传 metadata（见 core/retriever.py 的 _rank / rag_chain.py 的 _extract_sources）。
#      要用上 UUID，要么换 langchain_chroma（会牵动 langchain-core 的版本闸），
#      要么绕过 LangChain 直连 _collection.query()（会动 module3 / module4 的既有断言）。
#      两条路的成本都远大于收益。
#
#   ② 更要命的是**重建稳定性**。UUID 是随机生成的，reindex 跑一次就全变；
#      而 chat_message.ref_ids 里存的是历史引用的 chunk_id —— 一旦引用键会变，
#      「重建知识库」这个操作就等于把所有老会话的引用集体作废。
#      `doc_id:chunk_index` 由业务数据推导，同一份文件重解析得到同一组键，
#      历史引用过一遍 reindex 仍然点得开。
#
#   ③ 自描述。`12:3` 直接读得出归属，排查时不用先回库反查。
#
# 代价（已记入 docs/iterations/v2.0.0-p0.4a-*.md §3「否掉的方案」）：
#   若将来改动**切分参数**，chunk_index → 正文的对应关系会漂移，
#   老引用会解析到「另一个切片」也就是**错误的正文**，而不是干脆查不到。
#   这条只能靠约定兜住：改切分参数 = 必须整库重建 + 清空历史引用。
CHUNK_ID_SEP = ":"


def build_chunk_id(doc_id: int, chunk_index: int) -> str:
    """由「文档 + 片内序号」拼出引用键。写入侧与读取侧共用这一个拼法。"""
    return f"{int(doc_id)}{CHUNK_ID_SEP}{int(chunk_index)}"


def parse_chunk_id(chunk_id: str) -> tuple[int, int]:
    """
    把引用键解析回 `(doc_id, chunk_index)`，**格式不对直接抛 ValueError**。

    :raises ValueError: 格式非法时。message 是给接口层直接回 400 用的，要能读懂。

    为什么不静默容错（例如截掉多余的分段、或者把非数字当成 0）：
    引用反查是「拿一个键去取一段正文」，容错在这里等于「猜用户想要哪个切片」。
    猜错的后果是把 A 文档的正文展示成 B 文档的引用 —— 一个安静的错误，
    比一个 400 难查得多。

    为什么用 `isascii() and isdigit()` 而不是只 `isdigit()`：
    `isdigit()` 对全角数字（"１２"）与上标（"²"）都返回 True，
    但 `int("²")` 会抛 ValueError —— 于是「校验通过、转换炸掉」，
    表现为 500 而不是 400。加上 isascii() 才是真的只放行 ASCII 数字。
    """
    text = (chunk_id or "").strip()
    parts = text.split(CHUNK_ID_SEP)
    if len(parts) != 2:
        raise ValueError(
            f"chunk_id 格式应为「doc_id{CHUNK_ID_SEP}chunk_index」（如 12{CHUNK_ID_SEP}3），收到：{chunk_id!r}"
        )

    raw_doc_id, raw_index = parts
    if not (raw_doc_id.isascii() and raw_doc_id.isdigit()):
        raise ValueError(f"chunk_id 的 doc_id 部分必须是数字，收到：{raw_doc_id!r}")
    if not (raw_index.isascii() and raw_index.isdigit()):
        raise ValueError(f"chunk_id 的 chunk_index 部分必须是数字，收到：{raw_index!r}")

    doc_id, chunk_index = int(raw_doc_id), int(raw_index)
    if doc_id <= 0:
        raise ValueError(f"doc_id 必须是正整数，收到：{doc_id}")
    return doc_id, chunk_index


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
        chroma_host: str | None = None,
        chroma_port: int | None = None,
    ) -> None:
        """
        :param store_type: 向量库类型，chroma / faiss；缺省读 settings.VECTOR_STORE_TYPE
        :param persist_dir: 持久化目录；缺省读 settings.VECTOR_DB_DIR
        :param collection_name: Chroma 的 collection 名；缺省用 DEFAULT_COLLECTION_NAME
        :param chroma_host: Chroma server 地址；**显式传了 persist_dir 时缺省为嵌入式**，
                            两个都没传才读 settings.CHROMA_HOST（见下）
        :param chroma_port: Chroma server 端口；缺省读 settings.CHROMA_PORT

        为什么把这三个参数开放出来：
            1) 可测试——单测要同时验证 chroma 与 faiss，且必须写到临时目录，不能污染真实 vector_db/；
            2) 多知识库——将来要做「多个隔离的知识库」时，换个 persist_dir 就是一个新库。
        生产代码统一走模块底部的 get_vector_store_manager() 单例入口。

        chroma_host 的三态约定（p1.5c 新增，防「单测随 .env 漂移」）：
            · 显式传 chroma_host        → server 模式（连指定地址）；
            · 没传 chroma_host 但显式传了 persist_dir → **嵌入式**。
              单测全都显式传临时目录；若此时默认去读 settings.CHROMA_HOST，
              「单元测试连上真实服务」的事故就会重演（p0.4c 已经踩过一次：
              单元测试不该碰业务服务，也不该随 .env 漂移）；
            · 两个都没传（生产无参单例）→ 读 settings.CHROMA_HOST（空 = 嵌入式）。
        """
        # 后端类型固化在实例上：避免每个方法都去读全局 settings，
        # 否则运行中改配置会让同一个管理器前后使用不同后端（行为不可预测）
        self.store_type: str = store_type or settings.VECTOR_STORE_TYPE
        self.persist_dir: Path = (
            Path(persist_dir) if persist_dir is not None else settings.VECTOR_DB_DIR
        )
        self.collection_name: str = collection_name or self.DEFAULT_COLLECTION_NAME
        if chroma_host is not None:
            self.chroma_host: str = chroma_host
        elif persist_dir is not None:
            self.chroma_host = ""
        else:
            self.chroma_host = settings.CHROMA_HOST
        self.chroma_port: int = chroma_port if chroma_port is not None else settings.CHROMA_PORT

        # 嵌入模型是单例，这里拿到的是同一个对象，不会重复加载
        self.embedding_model: HuggingFaceEmbeddings = get_embedding_model()

        # 底层向量库对象：Chroma 实例 / FAISS 实例。
        # 注意：FAISS 在「索引文件还不存在」时保持 None，表示「空库」，
        # 等第一次写入时再创建索引（原因见 _init_faiss 的注释）
        self._store: Chroma | FAISS | None = None

        logger.info("初始化向量库 | 类型=%s | 目录=%s | collection=%s",
                    self.store_type, self.persist_dir, self.collection_name)
        self._initialize_vector_store()
        # 分数换算（_distance_to_similarity）依赖「嵌入向量已归一化」这个前提，
        # 建库后立刻自检一次，别让错误前提静默地污染所有检索分数
        self._check_normalization()

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
        """
        初始化 Chroma：server 模式连远端容器；嵌入式写本地目录。

        server 模式（p1.5c）解决的是「worker 写入后 backend 检索不到」：
        嵌入式时每个进程各自加载一份 HNSW 索引到内存，worker 落盘后
        backend 内存里的索引还是启动时那份；server 模式下索引由服务端
        单点持有，所有进程经 HTTP 共享，写入即刻可见（无需重启）。
        """
        if self.chroma_host:
            client = chromadb.HttpClient(host=self.chroma_host, port=self.chroma_port)
            self._store = Chroma(
                collection_name=self.collection_name,
                embedding_function=self.embedding_model,
                client=client,
            )
            # 初始化即探活：server 不可达时在这里就报错（count 会发一次真实请求），
            # 比「服务起来了、第一次检索才失败」好定位得多
            logger.info("Chroma(server) 已就绪 | %s:%d | collection=%s | 现有片段=%d",
                        self.chroma_host, self.chroma_port, self.collection_name, self.count())
            return

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

    def _check_normalization(self) -> None:
        """
        自检一次：确认嵌入向量确实做了 L2 归一化。

        _distance_to_similarity 的换算完全依赖这个前提。不归一化时距离里混着模长项，
        换算出的相似度是错的 —— 而且不报错、不崩溃，只是检索结果悄悄变差，
        属于最难查的那类静默故障，所以宁可在启动时多算一个向量也要提前发现。
        """
        try:
            vec = self.embedding_model.embed_query("归一化自检")
            norm_sq = sum(float(x) * float(x) for x in vec)
        except Exception as e:
            # 自检失败不影响主流程：顶多少一道校验，不能让向量库起不来
            logger.warning("归一化自检未能完成（不影响检索，仅跳过校验）| 错误：%s", e)
            return

        if abs(norm_sq - 1.0) > 1e-3:
            logger.error(
                "嵌入向量未归一化（‖v‖²=%.4f，期望 1.0）：similarity_search_with_score 返回的"
                "余弦相似度不可信，请检查 core/embedding.py 的 normalize_embeddings",
                norm_sq,
            )
        else:
            logger.debug("归一化自检通过（‖v‖²=%.6f），分数换算前提成立", norm_sq)

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
    # 按 doc_id 删除 / 查询（v2.0.0 P0-3 新增）
    # ------------------------------------------------------------------ #
    # 为什么在已有 delete_by_source 之外再加一套按 doc_id 的：
    # P0-3 之后磁盘文件名是 uuid（防重名覆盖），于是「按 source 删」在语义上退化成
    # 「按磁盘路径删」—— 路径一旦变动（迁移目录、改名），删除就静默失效，
    # 而且是「删 0 条也返回 200」这种最难受的失败方式。
    # doc_id 是 MySQL 里的主键，是文档的**身份**而不是它的存放位置，
    # 用它做删除键，磁盘怎么搬都不影响。
    #
    # 两者并存不删旧的那个：老数据（P0-3 之前入库的片段）没有 doc_id，
    # 只能靠 source 清；module3 的既有断言也还在用它。
    @staticmethod
    def _normalize_doc_id(doc_id: int | str) -> int:
        """
        把 doc_id 归一成 int，并**在类型不对时直接报错**。

        为什么不静默容错（比如转成 str 再试一次）：
        写入侧统一写 int（见 core/parsing.py），读取侧若容忍 str，
        就会出现「写入写的是 int、查询查的是 str，Chroma 判不相等，
        于是删不掉但也不报错」—— 结果是一份文档的旧切片永远留在库里，
        新切片又写进去，检索时同内容出现两份。
        宁可这里抛一个说明白了的异常。
        """
        if isinstance(doc_id, bool):        # bool 是 int 的子类，得先挡掉
            raise TypeError(f"doc_id 不能是布尔值：{doc_id!r}")
        if isinstance(doc_id, int):
            return doc_id
        if isinstance(doc_id, str) and doc_id.strip().isdigit():
            return int(doc_id.strip())
        raise TypeError(f"doc_id 必须是整数（或纯数字字符串），收到 {type(doc_id).__name__}: {doc_id!r}")

    def delete_by_doc_id(self, doc_id: int | str) -> int:
        """
        按 doc_id 删除该文档在向量库中的全部片段。

        这是 P0-3「同一份文档重复解析不产生重复切片」的执行点：
        Worker 抢到任务后，先调本方法清掉上一次的切片，再写入新的。

        :param doc_id: document 表的主键
        :return: 删除的片段数
        """
        target = self._normalize_doc_id(doc_id)
        store = self._store
        if store is None:
            logger.warning("FAISS 为空库，无需删除 | doc_id=%s", target)
            return 0

        try:
            if isinstance(store, Chroma):
                result = store.get(where={"doc_id": target})
                ids_to_delete = list(result.get("ids") or [])
                if not ids_to_delete:
                    logger.debug("该 doc_id 没有残留片段（首次解析时是正常情况） | doc_id=%s", target)
                    return 0
                store.delete(ids=ids_to_delete)
                logger.info("按 doc_id 删除成功 | doc_id=%s | 删除片段=%d | 剩余=%d",
                            target, len(ids_to_delete), self.count())
                return len(ids_to_delete)

            # FAISS 没有元数据条件删除，只能遍历 docstore 自己筛
            docstore = getattr(store, "docstore", None)
            id_to_doc: dict[str, Document] = dict(getattr(docstore, "_dict", {}))
            target_ids = [
                key
                for key, doc in id_to_doc.items()
                if (getattr(doc, "metadata", None) or {}).get("doc_id") == target
            ]
            if not target_ids:
                logger.debug("该 doc_id 没有残留片段 | doc_id=%s", target)
                return 0
            store.delete(target_ids)
            self._save_faiss()
            logger.info("按 doc_id 删除成功（FAISS） | doc_id=%s | 删除片段=%d | 剩余=%d",
                        target, len(target_ids), self.count())
            return len(target_ids)
        except Exception as e:
            logger.error("按 doc_id 删除失败 | doc_id=%s | 错误：%s", target, e, exc_info=True)
            return 0

    def get_chunks_by_doc_id(self, doc_id: int | str) -> list[dict[str, Any]]:
        """
        取出某文档的全部切片，**按 chunk_index 升序**（知识库管理页「查看片段」用）。

        与 get_chunks(source) 的区别不只是查询键：
        get_chunks 是按库内自然顺序返回的，而 Chroma 的返回顺序**没有承诺**。
        对于「排查模型看到了什么」这个用途，顺序错乱会让人误以为切片乱序，
        所以这里按我们自己写进去的 chunk_index 显式排序。
        """
        target = self._normalize_doc_id(doc_id)
        store = self._store
        if store is None:
            return []

        items: list[tuple[int, str, dict[str, Any]]] = []
        if isinstance(store, Chroma):
            result = store.get(where={"doc_id": target}, include=["documents", "metadatas"])
            docs = result.get("documents") or []
            metas = result.get("metadatas") or []
            for doc, meta in zip(docs, metas):
                m = meta or {}
                items.append((self._chunk_index_of(m), doc, m))
        else:
            docstore = getattr(store, "docstore", None)
            for doc in getattr(docstore, "_dict", {}).values():
                m = getattr(doc, "metadata", None) or {}
                if m.get("doc_id") == target:
                    items.append((self._chunk_index_of(m), doc.page_content, m))

        # 没有 chunk_index 的老片段排到最后（用一个极大的哨兵值），
        # 保证「有索引的按索引排、没索引的稳定地落在末尾」而不是随机插队
        items.sort(key=lambda t: t[0])
        return [
            {
                "index": idx,
                "content": content,
                "char_count": len(content),
                "page": meta.get("page"),
                "chunk_index": meta.get("chunk_index"),
            }
            for idx, (_, content, meta) in enumerate(items, start=1)
        ]

    def count_by_doc_id(self, doc_id: int | str) -> int:
        """
        数某文档当前在库里的切片数。

        存在的理由：解析成功时我们把切片数写进了 MySQL `chunk_count`，
        但那个数是「写入时声称写了多少」。本方法是**从向量库实地数一遍**，
        用于对账 —— 两者不一致说明写入过程中出过问题（例如中途崩了）。
        """
        return len(self.get_chunks_by_doc_id(doc_id))

    @staticmethod
    def _chunk_index_of(meta: dict[str, Any]) -> int:
        """取元数据里的 chunk_index，缺失/非数字时返回一个极大值（排到末尾）。"""
        value = meta.get("chunk_index")
        if isinstance(value, bool) or not isinstance(value, int):
            return 1 << 30
        return value

    # ------------------------------------------------------------------ #
    # 引用反查 / 孤儿清理（v2.0.0 P0-4a）
    # ------------------------------------------------------------------ #
    # 为什么按 (doc_id, chunk_index) 定位，而不是先解析 chunk_id 再字符串比对：
    # chunk_id 是**派生**值（= 上面 build_chunk_id 的拼法），权威事实是
    # doc_id 与 chunk_index 两个元数据字段。按字段比对，那些「有 doc_id
    # 与 chunk_index、但写入时还没带 chunk_id」的切片（P0-3a 期间入库的）
    # 一样能被定位到，不必因为一个派生字段缺了就判它不可用。
    @staticmethod
    def _match_chunk_index(meta: dict[str, Any], chunk_index: int) -> bool:
        """
        元数据里的 chunk_index 是否等于目标值。

        **类型不对一律视为不匹配**：Chroma 里存的是 int，但老数据/手写数据
        可能落成字符串 "3"。`"3" == 3` 为假，所以这里天然只认 int；
        显式挡掉 bool 是因为 `True == 1` 为真，会让 `True` 误命中第 1 片。
        """
        value = meta.get("chunk_index")
        return isinstance(value, int) and not isinstance(value, bool) and value == chunk_index

    @classmethod
    def _chunk_payload(
        cls, doc_id: int, chunk_index: int, content: str, meta: dict[str, Any]
    ) -> dict[str, Any]:
        """
        组装一个切片的返回体。

        这个形状是给「引用反查」接口用的：正文 + 定位信息 + 展示用元数据。
        `chunk_id` 在这里**重新拼一次**而不是读元数据 —— 保证出口的键永远是
        规范形态（`01:3` 这类非规范写法不会从接口漏出去）。
        """
        return {
            "chunk_id": build_chunk_id(doc_id, chunk_index),
            "doc_id": doc_id,
            "chunk_index": chunk_index,
            "content": content,
            "char_count": len(content),
            "page": meta.get("page"),
            "project_id": meta.get("project_id"),
            "source": meta.get("source"),
            "file_name": meta.get("file_name"),
            "file_type": meta.get("file_type"),
        }

    def get_chunk_by_position(self, doc_id: int | str, chunk_index: int) -> dict[str, Any] | None:
        """
        取一个切片的正文与元数据。定位键是「文档 + 片内序号」。

        返回 None 表示**这个位置没有切片** —— 可能是文档已被删除，
        也可能是文档重新解析后切片数变少了。调用方（接口层）拿它去区分
        「引用内容已随文档删除」与「引用片段已失效」，所以这里不做任何兜底猜测。

        为什么不做成 `get_chunk_by_id(chunk_id)` 一步到位：调用方解析出 doc_id
        之后还要拿它去 MySQL 查文件名/上传时间，本来就需要这两个字段分开；
        让 store 再解析一次等于把同一个解析做两遍。
        """
        target = self._normalize_doc_id(doc_id)
        store = self._store
        if store is None:                       # FAISS 空库
            return None

        if isinstance(store, Chroma):
            # include 三个都要：正文用于展示，元数据用于定位与兜底展示字段。
            # 这里是「按 doc_id 取全部切片再筛」，不是按 id 点查 —— 一篇文档
            # 通常只有几十片，代价可忽略；换来的是不必依赖 Chroma 的内部 id。
            result = store.get(where={"doc_id": target}, include=["documents", "metadatas"])
            docs = result.get("documents") or []
            metas = result.get("metadatas") or []
            for content, meta in zip(docs, metas):
                m = meta or {}
                if self._match_chunk_index(m, chunk_index):
                    return self._chunk_payload(target, chunk_index, content, m)
            return None

        docstore = getattr(store, "docstore", None)
        for doc in getattr(docstore, "_dict", {}).values():
            m = getattr(doc, "metadata", None) or {}
            if self._normalize_doc_id_safe(m.get("doc_id")) == target and self._match_chunk_index(m, chunk_index):
                return self._chunk_payload(target, chunk_index, doc.page_content, m)
        return None

    @staticmethod
    def _normalize_doc_id_safe(value: Any) -> int | None:
        """遍历时的宽松版 doc_id 归一：拿不出 int 就返回 None（不抛异常）。"""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            return int(value.strip())
        return None

    def list_orphan_chunks(self, valid_doc_ids: set[int] | frozenset[int]) -> list[tuple[str, dict[str, Any]]]:
        """
        列出向量库里「不指向任何有效文档」的切片，返回 `[(库内 id, 元数据), ...]`。

        「孤儿」有两种，合在一个口径里：
            · 元数据里**根本没有** doc_id —— P0-3 之前入库的遗留切片，
              它们按 source（磁盘路径）组织，文档一旦改名/搬目录就再也删不掉。
            · doc_id 存在但**不在** valid_doc_ids 里 —— MySQL 里的文档记录
              已经被删，切片却留下来了（删除链路半途失败）。

        两者都是「检索能召回、但溯源与删除都对不上号」的切片，
        也就是 P0-4 要清理干净的对象。

        为什么不提供 FAISS/Chroma 各一套口径：这里返回的 id 只在传给
        `store.delete(...)` 时有意义，调用方不需要理解两个后端 id 的差别。
        """
        store = self._store
        if store is None:
            return []

        orphans: list[tuple[str, dict[str, Any]]] = []

        def _is_orphan(meta: dict[str, Any]) -> bool:
            # 显式排除 bool：True 会被 int 检查放过去，然后 `True in {1,2}` 命中，
            # 于是一个脏值就把本该清掉的切片当成了「有效文档 1 的切片」。
            value = meta.get("doc_id")
            if isinstance(value, bool) or not isinstance(value, int):
                return True
            return value not in valid_doc_ids

        if isinstance(store, Chroma):
            # 注意：这里不带 limit，等于把整张 collection 的元数据读进内存。
            # 对「知识库重建」这种离线运维动作可以接受；不要把它接进请求路径。
            result = store.get(include=["metadatas"])
            ids = result.get("ids") or []
            metas = result.get("metadatas") or []
            for cid, meta in zip(ids, metas):
                m = meta or {}
                if _is_orphan(m):
                    orphans.append((cid, m))
            return orphans

        docstore = getattr(store, "docstore", None)
        for key, doc in getattr(docstore, "_dict", {}).items():
            m = getattr(doc, "metadata", None) or {}
            if _is_orphan(m):
                orphans.append((key, m))
        return orphans

    def purge_orphan_chunks(self, valid_doc_ids: set[int] | frozenset[int]) -> int:
        """
        删除全部孤儿切片，返回删除条数。

        语义就是 `list_orphan_chunks` 的写版：先列出再删，保证「报告的数量」
        与「实际删的数量」出自同一套判断，不会出现「干跑说 15 条、执行删了 3 条」。
        """
        orphan_ids = [cid for cid, _ in self.list_orphan_chunks(valid_doc_ids)]
        if not orphan_ids:
            return 0

        store = self._store
        if store is None:
            return 0

        if isinstance(store, Chroma):
            store.delete(ids=orphan_ids)
        else:
            store.delete(orphan_ids)
            self._save_faiss()              # FAISS 是纯内存索引，删完必须立刻落盘

        logger.info("已清理孤儿切片 | 删除=%d | 剩余=%d", len(orphan_ids), self.count())
        return len(orphan_ids)

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

    @staticmethod
    def _distance_to_similarity(distance: float) -> float:
        """
        把后端返回的平方 L2 距离换算成余弦相似度（越大越像，范围 [-1, 1]）。

        推导：向量归一化后 ‖a‖ = ‖b‖ = 1，
            L2² = ‖a‖² + ‖b‖² - 2a·b = 2 - 2cosθ   ⟹   cos θ = 1 - L2²/2
        换算与余弦严格单调对应，**排序完全不变**，只是数值和方向变了
        （越小越像 → 越大越像），这样上层才能写死阈值、也能跨后端比较分数。

        :param distance: 后端原生返回的平方 L2 距离
        :return: 余弦相似度；前提不成立时数值不可信，见 _check_normalization 的告警
        """
        return 1.0 - distance / 2.0

    def similarity_search_with_score(
        self,
        query: str,
        k: int | None = None,
        filter_dict: dict[str, Any] | None = None,
    ) -> list[tuple[Document, float]]:
        """
        相似度检索并返回相关性分数（**不调用大模型**）。

        :return: [(Document, 分数), ...]，分数**越大越相似**，范围 [-1, 1]。
                 后端原生给的是平方 L2 距离（越小越像），这里已统一换算成余弦相似度，
                 见 _distance_to_similarity —— 上层不要再自己做一次「越小越像」的假设。
        """
        store = self._store
        if store is None:
            logger.warning("向量库为空（FAISS 空库），检索直接返回空结果")
            return []

        k = k or settings.SEARCH_TOP_K
        kwargs = self._build_filter_kwargs(k, filter_dict)
        logger.debug("执行带分数的相似度检索 | k=%d | filter=%s | 后端=%s", k, filter_dict, self.store_type)

        raw_results = store.similarity_search_with_score(query, k=k, **kwargs)
        # 一次性抹平两个后端的两处差异：
        #   ① 类型：FAISS 返回 numpy.float32，直接放进 FastAPI 响应会报
        #      「Object of type float32 is not JSON serializable」；Chroma 返回的本来就是 float。
        #   ② 语义：两者返回的都是平方 L2 距离（越小越像），换算成余弦相似度（越大越像），
        #      上层设阈值、跨后端比较才有一致语义。
        results = [(doc, self._distance_to_similarity(float(score))) for doc, score in raw_results]
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

    def as_retriever(self, search_kwargs: dict[str, Any] | None = None) -> BaseRetriever:
        """
        返回一个 LangChain 标准的 Retriever，供 LCEL 链使用。

        这里刻意对外暴露这个方法，而不是让外部访问 self._store，原因有两个：
            ① _store 是私有属性，外部依赖它会导致封装失效；
            ② FAISS 空库时 _store 是 None，外部直接访问会拿到 AttributeError，
               而这里能给出「请先入库」这种可操作的明确报错。

        :param search_kwargs: 传给底层检索的参数，如 {"k": 10}
        """
        if self._store is None:
            raise RuntimeError(
                "向量库为空（FAISS 空库时尚未创建索引），请先入库再构建 Retriever"
            )
        kwargs = search_kwargs or {"k": settings.SEARCH_TOP_K}
        logger.debug("构建 LangChain Retriever | 后端=%s search_kwargs=%s", self.store_type, kwargs)
        return self._store.as_retriever(search_kwargs=kwargs)

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
            # server 模式要在状态里看得见——「连的是容器还是本地目录」是排查
            # 「写进去却查不到」这类问题时第一个要确认的事
            "vector_store_type": self.store_type + ("(server)" if self.store_type == "chroma" and self.chroma_host else ""),
            "persist_directory": (
                f"server://{self.chroma_host}:{self.chroma_port}" if self.chroma_host else str(self.persist_dir)
            ),
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

    def get_chunks(self, source: str) -> list[dict[str, Any]]:
        """
        取出某个来源文件的全部切分片段（知识库管理页「查看片段」用）。

        溯源与检索都是片段级的：召回的是片段、拼进 prompt 的是片段、
        sources 里的 snippet 也是片段开头 —— 原文档本身从未整体进过模型。
        所以排查「模型到底看到了什么」必须能看到片段全文。

        :param source: 元数据里的 source 字段（文件完整路径，与删除接口同一键）
        :return: [{index, content, char_count, page}]，按库内顺序
        """
        store = self._store
        if store is None:
            return []

        if isinstance(store, Chroma):
            result = store.get(where={"source": source}, include=["documents", "metadatas"])
            docs = result.get("documents") or []
            metas = result.get("metadatas") or []
            return [
                {
                    "index": i,
                    "content": doc,
                    "char_count": len(doc),
                    "page": (meta or {}).get("page"),
                }
                for i, (doc, meta) in enumerate(zip(docs, metas), start=1)
            ]

        # FAISS：没有元数据条件查询，遍历 docstore 自己筛
        docstore = getattr(store, "docstore", None)
        chunks: list[dict[str, Any]] = []
        for doc in getattr(docstore, "_dict", {}).values():
            meta = getattr(doc, "metadata", None) or {}
            if meta.get("source") == source:
                chunks.append(
                    {
                        "index": len(chunks) + 1,
                        "content": doc.page_content,
                        "char_count": len(doc.page_content),
                        "page": meta.get("page"),
                    }
                )
        return chunks

    def list_documents(self) -> list[dict[str, Any]]:
        """
        按来源（source）分组列出知识库中的全部文档（知识库管理页用）。

        一个文件入库后会被切成多个片段，这里把同一 source 的片段聚合为一条记录：
            · source       文件完整路径（唯一键，删除接口按它删）
            · file_name    文件名（展示用，可能重名）
            · file_type    文件类型（无点后缀）
            · chunk_count  该文件被切成的片段数
        """
        metadatas = self._collect_metadatas()
        grouped: dict[str, dict[str, Any]] = {}
        for m in metadatas:
            src = str(m.get("source", ""))
            if not src:
                continue
            entry = grouped.setdefault(
                src,
                {
                    "source": src,
                    "file_name": str(m.get("file_name") or Path(src).name),
                    "file_type": str(m.get("file_type") or Path(src).suffix.lstrip(".")),
                    "chunk_count": 0,
                },
            )
            entry["chunk_count"] += 1
        return sorted(grouped.values(), key=lambda d: d["file_name"].lower())

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
