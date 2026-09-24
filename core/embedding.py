"""
嵌入模型加载模块 —— 单例模式 + 「本地优先、远程兜底」两级加载。

【为什么必须用单例】
    HuggingFaceEmbeddings 初始化会把权重读进内存（bge-small-zh 约 92 MB），首次加载耗时数秒。
    而向量化是高频调用（入库时要嵌入成百上千个片段），如果每次调用都 new 一个：
      · 内存/显存成倍增长；
      · 每次都要重新读一遍权重，白白浪费时间。
    所以这里用「双检锁（double-checked locking）」保证**进程内只加载一次模型**。

【为什么本地优先】
    教学 / 内网 / 离线环境下访问不了 HuggingFace；
    项目 models/ 目录里已经放好权重（用软链共享，不重复占磁盘），优先读它既快又稳。
    只有当本地目录不存在、或本地加载失败时，才回退到 HuggingFace 仓库名让框架去下载缓存。

使用方式：
    from core.embedding import get_embedding_model
    embeddings = get_embedding_model()          # 传 Chroma / FAISS 的 embedding_function
"""
import logging
import threading
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings

from config.settings import settings

logger = logging.getLogger(__name__)


class _L2NormalizedEmbeddings:
    """把任意 Embeddings 的输出收成单位向量。Chroma 的分数换算依赖这一点。"""

    def __init__(self, inner: Embeddings) -> None:
        self.inner = inner

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_l2_normalize(v) for v in self.inner.embed_documents(texts)]

    def embed_query(self, text: str) -> list[float]:
        return _l2_normalize(self.inner.embed_query(text))


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = sum(float(x) * float(x) for x in vector) ** 0.5
    if norm == 0:
        return [float(x) for x in vector]
    return [float(x) / norm for x in vector]


class EmbeddingModel:
    """嵌入模型单例容器（对外只暴露 get_model()，业务代码一般用模块级 get_embedding_model()）。"""

    # ---- 单例状态（类变量，全进程共享）----
    _instance: "EmbeddingModel | None" = None
    # 并发保护锁：多个线程同时首次请求时，只允许一个真正去加载模型
    _lock: threading.Lock = threading.Lock()

    # 本地目录名 → HuggingFace 仓库名 的映射。
    # 原因：settings.EMBEDDING_MODEL_NAME 填的是「本地目录名」（如 bge_small_zh），
    # 而 HuggingFace 需要的是「仓库名」（如 BAAI/bge-small-zh-v1.5），两者必须映射。
    # 如果 settings 里直接写的是仓库名（名字里带 /），则视为仓库名、不再查表。
    HF_REPO_MAP: dict[str, str] = {
        "bge_small_zh": "BAAI/bge-small-zh-v1.5",
        "bge_reranker_base": "BAAI/bge-reranker-base",
    }

    # 批量嵌入的批大小：太大吃内存、太小拖慢吞吐，32 是中文短文本的经验值
    ENCODE_BATCH_SIZE: int = 32

    # ---- 实例属性（真正的赋值发生在 _initialize 中，这里给默认值）----
    # 给默认值有两个好处：① 类型检查器能确认属性已初始化；② 便于判断「是否已加载完成」
    model: Embeddings | None = None   # 真正的嵌入模型对象
    loaded_from: str = ""             # 加载来源：siliconflow / local / remote
    model_path: str = ""              # 实际用于加载的路径 / 仓库名
    _dimension: int | None = None

    def __new__(cls) -> "EmbeddingModel":
        """创建（或复用）单例。"""

        # 第一重检查：绝大多数情况下实例已存在，直接返回，不必每次调用都去抢锁
        if cls._instance is None:
            # 第二重检查：加锁后再判一次，防止两个线程同时通过第一重检查、各自加载一遍模型
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    # 加载失败会抛异常，此时不会写入 _instance，下次调用可以重试
                    instance._initialize()
                    # 初始化**完成之后**才发布实例，避免其他线程拿到半成品
                    cls._instance = instance
        return cls._instance

    def _initialize(self) -> None:
        """执行真正的加载流程。运行路径走硅基流动；local 只留给回归测试。"""
        self.model = None
        self.loaded_from = ""
        self.model_path = ""
        self._dimension = None

        if settings.EMBEDDING_BACKEND == "siliconflow":
            self._init_siliconflow()
            return

        self._init_local()

    def _init_siliconflow(self) -> None:
        """
        用硅基流动的 OpenAI 兼容嵌入接口，进程内不加载权重。

        外包一层归一化：bge 系列通常已经是单位向量，再除一次范数无害；
        若接口没归一化，检索分数换算会静默变差（见 vector_store._check_normalization）。
        tiktoken 只认识 OpenAI 自家模型名，必须关掉，否则会按错误的词表切中文。
        """
        if not settings.SILICONFLOW_API_KEY:
            raise RuntimeError("EMBEDDING_BACKEND=siliconflow 但未配置 SILICONFLOW_API_KEY")

        from langchain_openai import OpenAIEmbeddings

        model_name = settings.SILICONFLOW_EMBEDDING_MODEL
        inner = OpenAIEmbeddings(
            model=model_name,
            openai_api_key=settings.SILICONFLOW_API_KEY,
            openai_api_base=settings.SILICONFLOW_BASE_URL,
            tiktoken_enabled=False,
            check_embedding_ctx_length=False,
        )
        self.model = _L2NormalizedEmbeddings(inner)
        self.loaded_from = "siliconflow"
        self.model_path = model_name
        self._dimension = len(self.model.embed_query("维度探测"))
        logger.info(
            "嵌入模型就绪 | 来源=siliconflow | 模型=%s | 维度=%s",
            model_name,
            self._dimension,
        )

    def _init_local(self) -> None:
        """本地 HuggingFace 权重。运行路径不走这里，make test 会把 EMBEDDING_BACKEND 改回 local。"""
        model_name = settings.EMBEDDING_MODEL_NAME
        local_dir = settings.MODELS_DIR / model_name.replace("/", "_")
        remote_name = self.HF_REPO_MAP.get(model_name, model_name)

        if local_dir.is_dir():
            try:
                logger.info("尝试从本地加载嵌入模型：%s", local_dir)
                self.model = self._build(str(local_dir))
                self.loaded_from = "local"
                self.model_path = str(local_dir)
            except Exception as e:
                logger.warning("本地模型加载失败，将回退到远程：%s | 错误：%s", local_dir, e)
                self.model = None
        else:
            logger.info("本地未找到模型目录（%s），直接走远程加载", local_dir)

        if self.model is None:
            logger.info("尝试从 HuggingFace 加载嵌入模型：%s（首次会下载并缓存，可能较慢）", remote_name)
            try:
                self.model = self._build(remote_name)
                self.loaded_from = "remote"
                self.model_path = remote_name
            except Exception as e:
                logger.error("嵌入模型加载失败：本地与远程都不可用 | 错误：%s", e, exc_info=True)
                raise RuntimeError(
                    f"嵌入模型加载失败：本地目录「{local_dir}」不可用，远程仓库「{remote_name}」也不可用（检查网络或本地模型文件）"
                ) from e

        logger.info(
            "嵌入模型就绪 | 来源=%s | 路径=%s | 设备=%s | 维度=%s",
            self.loaded_from,
            self.model_path,
            settings.EMBEDDING_DEVICE,
            self.get_dimension(),
        )

    @staticmethod
    def _build(model_path: str) -> HuggingFaceEmbeddings:
        """
        按统一参数构造 HuggingFaceEmbeddings（本地路径与仓库名共用同一套参数）。

        :param model_path: 本地目录路径 或 HuggingFace 仓库名
        """
        return HuggingFaceEmbeddings(
            model_name=model_path,
            # 运行设备：cpu / mps（Mac GPU）/ cuda，由 settings 统一控制
            model_kwargs={"device": settings.EMBEDDING_DEVICE},
            encode_kwargs={
                # 归一化非常关键：归一化后「向量点积 == 余弦相似度」，
                # 检索打分才具有可比性（FAISS 的 L2 距离、Chroma 的默认距离都依赖这一点）
                "normalize_embeddings": True,
                "batch_size": EmbeddingModel.ENCODE_BATCH_SIZE,
            },
        )

    def get_model(self) -> Embeddings:
        """返回 LangChain Embeddings 实例（可直接作为 Chroma / FAISS 的 embedding_function）。"""
        if self.model is None:
            raise RuntimeError("嵌入模型尚未加载完成")
        return self.model

    def get_dimension(self) -> int | None:
        """返回向量维度。硅基流动在初始化时已经探测过；本地模型问 SentenceTransformer。"""
        if self._dimension is not None:
            return self._dimension
        if self.model is None:
            return None
        client = getattr(self.model, "_client", None) or getattr(self.model, "client", None)
        if client is None:
            return None
        try:
            self._dimension = int(client.get_sentence_embedding_dimension())
            return self._dimension
        except Exception:
            logger.debug("无法获取向量维度（不影响主流程）", exc_info=True)
            return None


# --------------------------------------------------------------------------- #
# 模块级便捷入口
# --------------------------------------------------------------------------- #
def get_embedding_model() -> Embeddings:
    """获取全局唯一的嵌入模型实例（业务代码统一从这里拿）。"""
    return EmbeddingModel().get_model()


def get_embedding_model_info() -> dict[str, Any]:
    """返回嵌入模型的加载信息，供 /system/config、知识库状态页之类的展示接口使用。"""
    instance = EmbeddingModel()
    return {
        "model_name": instance.model_path or settings.EMBEDDING_MODEL_NAME,
        "loaded_from": instance.loaded_from,
        "model_path": instance.model_path,
        "device": "api" if settings.EMBEDDING_BACKEND == "siliconflow" else settings.EMBEDDING_DEVICE,
        "dimension": instance.get_dimension(),
        "normalize_embeddings": True,
        "batch_size": EmbeddingModel.ENCODE_BATCH_SIZE,
        "backend": settings.EMBEDDING_BACKEND,
    }
