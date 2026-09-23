from pathlib import Path
from typing import ClassVar, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # 项目信息
    PROJECT_NAME : str = "rag-qa-system"
    PROJECT_VERSION : str = "1.0.0"
    BASE_DIR : Path = Path(__file__).parent.parent.resolve()

    # 数据目录
    DATA_DIR : Path = BASE_DIR / "data"
    VECTOR_DB_DIR : Path = BASE_DIR / "vector_db"
    MODELS_DIR : Path = BASE_DIR / "models"
    UPLOAD_DIR : Path = BASE_DIR / "upload"

    # 向量数据库的类型
    # 向量库配置
    VECTOR_STORE_TYPE : Literal["faiss", "chroma"] = "chroma"
    EMBEDDING_MODEL_NAME : str = "bge_small_zh"
    EMBEDDING_DEVICE : str = "cpu"      # 可以配置cuda
    CHUNK_SIZE : int = 500
    CHUNK_OVERLAP : int = 50

    # 检索配置
    SEARCH_TOP_K : int = 5
    USE_RERANKER : bool = True
    RERANKER_MODEL_NAME : str = "bge_reranker_base"
    # 重排前的候选池倍数：先按 SEARCH_TOP_K × N 从向量库召回候选，再精排到 SEARCH_TOP_K。
    # 若只召回 TOP_K 就直接重排，等于「把同样的 K 条换个顺序」，召回率没有任何提升。
    RERANK_CANDIDATE_MULTIPLIER : int = 4
    # 重排分数阈值：低于该值的候选直接丢弃（None 表示不启用过滤）。
    # 注意 bge-reranker 输出的是 sigmoid 后的 0~1 相关性分数，与向量距离不是一个量纲。
    # 实测分布：相关文档 ≈0.99，无关文档 ≈0.0003，0.1 是保守安全的分界线。
    # 不过滤的恶果：无关文档混进上下文，模型拒答但 sources 照带 ——
    # 前端出现「无法回答 + 引用资料 1 条」的矛盾展示（实测复现）。
    RERANK_SCORE_THRESHOLD : float | None = 0.1

     # LLM配置
    LLM_PROVIDER : Literal["openai", "siliconflow", "ollama"] = "siliconflow"

    # LLM 通用生成参数（三个 provider 共用，构造客户端时统一传入）
    LLM_TEMPERATURE : float = 0.1    # RAG 问答追求稳定可复现，温度不宜高
    LLM_MAX_TOKENS : int = 2048      # 单次回答的最大 token 数
    LLM_TIMEOUT : int = 60           # 单次请求超时（秒）；不设的话网络异常会把请求永久挂住
    LLM_MAX_RETRIES : int = 2        # 失败自动重试次数

    # ollama配置
    OLLAMA_BASE_URL : str = "http://localhost:11434"
    # 默认用小模型：9B 本地单轮 30~80s，交互不可接受；3B 约 1/3 耗时，中文能力够 RAG 问答用
    OLLAMA_MODEL_NAME : str = "qwen2.5:3b"
    # 是否让支持 thinking 的模型输出思考链（如 qwen3.5）。默认关闭，原因有二：
    # ① 思考 token 不进正文，stream 会连吐几百个空 chunk，前端看不到逐字效果；
    # ② RAG 问答已有检索结果兜底，不需要模型自己 long-CoT，关掉能显著提速。
    OLLAMA_REASONING : bool = False

    # OpenAI配置
    OPENAI_API_KEY : str = ""
    OPENAI_BASE_URL : str = "https://api.openai.com/v1"
    OPENAI_MODEL_NAME : str = "gpt-3.5-turbo"

    # 硅基流动（OpenAI 兼容）
    SILICONFLOW_API_KEY : str = ""
    SILICONFLOW_BASE_URL : str = "https://api.siliconflow.cn/v1"
    SILICONFLOW_MODEL_NAME : str = "Qwen/Qwen3-8B"
    # 是否让 Qwen3 系列输出思考链（reasoning_content）。
    # 必须默认关闭：思考 token 不作为 content 流出（LangChain 放进 additional_kwargs），
    # 流式问答会长时间没有任何 chunk —— 前端气泡空闪几十秒，实测 TTFT 60s+。
    # 且 RAG 问答有检索结果兜底，不需要模型 long-CoT（与 OLLAMA_REASONING 同理）。
    SILICONFLOW_ENABLE_THINKING : bool = False

    # 会话记忆配置（core/memory_manager.py 消费）
    # 单个会话保留的最大对话轮数：超出后从最早的开始裁剪。
    # 1 轮 = 1 条用户消息 + 1 条 AI 消息（即 2 条 message）。
    MEMORY_MAX_TURNS : int = 10
    # 会话闲置多少秒后视为过期，被清理线程/惰性检查回收（防内存无限增长）
    MEMORY_SESSION_TTL_SECONDS : int = 6 * 3600
    # 会话存储后端：memory（进程内 dict，本地开发/测试用）| redis（生产，重启不丢会话）
    # 见 core/session_store.py 的 build_session_store()
    MEMORY_BACKEND : Literal["memory", "redis"] = "memory"
    # 单个会话序列化后的体积上限（字节）。超过则在写入前强制多裁几轮，
    # 防止「用户贴超长文本」把单会话撑到几 MB，拖慢每次读改写。
    MEMORY_MAX_SESSION_BYTES : int = 256 * 1024
    # Redis 不可用/写失败时是否降级为「不写记忆但问答照常返回」。
    # True：可用性优先（问答不因记忆失败而 502）；False：严格模式，直接抛错。
    MEMORY_DEGRADE_ON_ERROR : bool = True

    # Redis 配置（MEMORY_BACKEND=redis 时生效）
    REDIS_URL : str = "redis://localhost:6379/0"
    # 所有本应用 key 统一前缀，便于 SCAN / 统计 / 避免与其他业务撞 key
    REDIS_KEY_PREFIX : str = "rag"
    # 连接池上限。按「每请求最多占用 1 条连接、瞬时并发 QPS」估算，
    # 20 条足够支撑单机数百 QPS；设太大反而会把 Redis 的 maxclients 吃满。
    REDIS_MAX_CONNECTIONS : int = 20
    # 单次命令超时（秒）。必须设：Redis 挂起时不设超时会把请求线程永久挂死。
    REDIS_SOCKET_TIMEOUT : float = 2.0
    # 建连超时（秒），比 socket 超时更短，避免启动期长时间卡住
    REDIS_CONNECT_TIMEOUT : float = 2.0
    # 会话级分布式锁：多 worker（uvicorn --workers / 多容器）下保证同一会话串行
    REDIS_LOCK_ENABLED : bool = True
    REDIS_LOCK_TTL_MS : int = 5000      # 锁自动过期（防持锁进程崩了死锁）
    REDIS_LOCK_WAIT_MS : int = 2000     # 拿不到锁的最长等待（超时降级为无锁执行 + WARN）

    # Redis 内存预警（core/redis_monitor.py 消费）
    # 目的：Redis 是共享资源，本应用写爆它会连带把同机其他服务一起拖垮。
    # 因此按 maxmemory 水位分三级告警，并对 fatal 级做「只读保护」。
    REDIS_MEMORY_MONITOR_ENABLED : bool = True
    REDIS_MEMORY_CHECK_INTERVAL_SECONDS : int = 60   # 后台巡检间隔
    REDIS_MEMORY_WARN_RATIO : float = 0.70           # ≥70% 水位 → WARN 日志
    REDIS_MEMORY_CRITICAL_RATIO : float = 0.85       # ≥85% → ERROR + 主动清理过期会话
    REDIS_MEMORY_FATAL_RATIO : float = 0.95          # ≥95% → CRITICAL + 只读保护
    # Redis 未设置 maxmemory（=0，无上限）时，用这个假定容量估算水位比例
    REDIS_MEMORY_ASSUMED_MAX_MB : int = 128
    # fatal 水位下是否开启只读保护：新会话不再写记忆，但问答本身照常返回。
    # 宁可丢「多轮上下文」也不让服务整体 503 —— 可用性优先。
    REDIS_MEMORY_FATAL_READONLY : bool = True

    # 意图识别配置（core/intent_router.py 消费）
    # 分类任务只需输出一个词，用本地小模型足够且零成本；
    # 模型加载/调用失败时自动降级为本地规则映射，不影响主链路。
    INTENT_LLM_PROVIDER : Literal["openai", "siliconflow", "ollama"] = "ollama"
    # 意图识别用的小模型名；留空则回退用 OLLAMA_MODEL_NAME
    INTENT_LLM_MODEL_NAME : str = ""
    INTENT_LLM_TIMEOUT : int = 10        # 分类必须快，超时直接走规则兜底

    # 服务配置
    API_HOST : str = "localhost"
    API_PORT : int = 8000
    STREAMLIT_PORT : int = 8501

    # 日志配置
    LOG_LEVEL : str = "INFO"
    LOG_FILE : Path = BASE_DIR / "app.log"

    # 安全配置
    ALLOWED_ORIGINS : list[str] = ["http://localhost:5173","http://localhost:8501","http://localhost:3000"]
    SECRET_KEY : str = ""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env", env_file_encoding="utf-8", extra="ignore",
    )

# 实例化全局配置
settings = Settings()   

# 确保必要目录存在
for dir_path in [settings.DATA_DIR, settings.VECTOR_DB_DIR, settings.MODELS_DIR, settings.UPLOAD_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)