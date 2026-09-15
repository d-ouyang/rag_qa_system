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

from langchain_huggingface import HuggingFaceEmbeddings

from config.settings import settings

logger = logging.getLogger(__name__)


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
    model: HuggingFaceEmbeddings | None = None   # 真正的嵌入模型对象
    loaded_from: str = ""                        # 加载来源："local" 或 "remote"
    model_path: str = ""                         # 实际用于加载的路径 / 仓库名

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
        """执行真正的加载流程：先本地，本地不可用再走远程。"""

        # settings.EMBEDDING_MODEL_NAME 既可能是本地目录名，也可能是 HF 仓库名
        model_name = settings.EMBEDDING_MODEL_NAME
        # 本地目录名：把仓库名里的 / 换成 _（如 Qwen/Qwen3-8B → Qwen_Qwen3-8B），
        # 否则 "/" 会被当成两级目录，拼出来的路径就错了
        local_dir = settings.MODELS_DIR / model_name.replace("/", "_")
        # 远程仓库名：查表得到；查不到就原样交给 HuggingFace（可能失败，但日志里能看清用了什么名字）
        remote_name = self.HF_REPO_MAP.get(model_name, model_name)

        # 重置为「未加载」状态：加载成功后再逐项赋值
        self.model = None
        self.loaded_from = ""
        self.model_path = ""

        # ---------- 第一级：本地目录 ----------
        if local_dir.is_dir():
            try:
                logger.info("尝试从本地加载嵌入模型：%s", local_dir)
                self.model = self._build(str(local_dir))
                self.loaded_from = "local"
                self.model_path = str(local_dir)
            except Exception as e:
                # 本地目录可能只有残缺文件（比如下载中断），
                # 这时不应该直接失败，而要给远程一次机会
                logger.warning("本地模型加载失败，将回退到远程：%s | 错误：%s", local_dir, e)
                self.model = None
        else:
            logger.info("本地未找到模型目录（%s），直接走远程加载", local_dir)

        # ---------- 第二级：HuggingFace 远程 ----------
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

        # 加载成功后记录一行「事实日志」，后面排查问题（比如怀疑模型没生效）时直接看这行
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

    def get_model(self) -> HuggingFaceEmbeddings:
        """返回 LangChain Embeddings 实例（可直接作为 Chroma / FAISS 的 embedding_function）。"""
        if self.model is None:
            # 正常路径不会走到这里（_initialize 加载失败会直接抛异常），兜底避免返回 None
            raise RuntimeError("嵌入模型尚未加载完成")
        return self.model

    def get_dimension(self) -> int | None:
        """
        返回向量维度（bge-small-zh 为 512）。

        取不到时返回 None 而不是抛异常：维度只是展示/校验用的元信息，
        不应该因为它拿不到就让整个向量化流程失败。
        """
        if self.model is None:
            return None
        # langchain_huggingface 把 SentenceTransformer 存在 _client（不同版本可能是 client）
        client = getattr(self.model, "_client", None) or getattr(self.model, "client", None)
        if client is None:
            return None
        try:
            return int(client.get_sentence_embedding_dimension())
        except Exception:
            logger.debug("无法获取向量维度（不影响主流程）", exc_info=True)
            return None


# --------------------------------------------------------------------------- #
# 模块级便捷入口
# --------------------------------------------------------------------------- #
def get_embedding_model() -> HuggingFaceEmbeddings:
    """获取全局唯一的嵌入模型实例（业务代码统一从这里拿）。"""
    return EmbeddingModel().get_model()


def get_embedding_model_info() -> dict[str, Any]:
    """返回嵌入模型的加载信息，供 /system/config、知识库状态页之类的展示接口使用。"""
    instance = EmbeddingModel()
    return {
        "model_name": settings.EMBEDDING_MODEL_NAME,
        "loaded_from": instance.loaded_from,       # local / remote
        "model_path": instance.model_path,         # 实际加载路径
        "device": settings.EMBEDDING_DEVICE,
        "dimension": instance.get_dimension(),
        "normalize_embeddings": True,
        "batch_size": EmbeddingModel.ENCODE_BATCH_SIZE,
    }
