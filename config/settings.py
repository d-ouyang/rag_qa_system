from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import quote_plus

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
    # Chroma 连接模式（v2.0.0-p1.5c 新增）：
    #   CHROMA_HOST 为空  → 嵌入式（写入 VECTOR_DB_DIR 本地目录，单进程索引，测试/兜底用）
    #   CHROMA_HOST 非空  → server 模式（HttpClient 连 chroma 容器，backend 与 worker
    #                       共享服务端索引，worker 写入后后端**无需重启**即可检索到）
    # 裸跑连宿主机的端口映射（127.0.0.1:8001），容器内连服务名（chroma:8000）。
    CHROMA_HOST : str = ""
    CHROMA_PORT : int = 8001
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
    # 会话存储后端：memory（进程内 dict，本地开发/测试用）| mysql（生产真相源）
    # 见 core/session_store.py 的 build_session_store()
    # ⚠️ 2026-09-23 修订：原取值 memory|redis **作废**。会话/消息是永久业务资产，
    #    不该存在「重启 / 驱逐 / 容量保护就丢」的内存库里。Redis 退为「队列 broker + 短期缓存」，
    #    不再承载任何真相源数据。分层依据见 docs/PLAN-v2.0.0.md §4，变更过程见 p0.1 文档 §7.1。
    MEMORY_BACKEND : Literal["memory", "mysql"] = "memory"
    # 单个会话序列化后的体积上限（字节）。超过则在写入前强制多裁几轮，
    # 防止「用户贴超长文本」把单会话撑到几 MB，拖慢每次读改写。
    MEMORY_MAX_SESSION_BYTES : int = 256 * 1024
    # Redis 不可用/写失败时是否降级为「不写记忆但问答照常返回」。
    # True：可用性优先（问答不因记忆失败而 502）；False：严格模式，直接抛错。
    MEMORY_DEGRADE_ON_ERROR : bool = True

    # ---------- MySQL（业务数据真相源，v2.0.0 P0-1 新增）----------
    # 定位：user / folder / session / chat_message / document 五张表的**唯一真相源**。
    # 与 Redis 严格分工：Redis 只做「解析任务队列 + 短期缓存」，存不住的东西一律不放它。
    #
    # ⚠️ HOST 必须是环境变量，不许在代码里写死：
    #    本地开发（D1 决策）= 中间件容器化 + 应用裸跑 → 连 localhost（容器端口绑在 127.0.0.1）
    #    全栈容器化（P1-5b）    = 服务间走容器网络   → HOST 改成 mysql
    #    写死任一侧的代价是「切模式必漏改一处」，而症状是「连不上数据库」这种零信息量报错。
    MYSQL_HOST : str = "localhost"
    MYSQL_PORT : int = 3306
    MYSQL_DATABASE : str = "rag_qa"
    MYSQL_USER : str = "rag"
    MYSQL_PASSWORD : str = ""
    MYSQL_CHARSET : str = "utf8mb4"

    # 连接池（SQLAlchemy QueuePool）。估算口径与 REDIS_MAX_CONNECTIONS 相同：
    # 「每请求最多占 1 条连接 × 瞬时并发」。设太大反而会打满服务端 max_connections（本项目设 100）。
    MYSQL_POOL_SIZE : int = 5
    MYSQL_MAX_OVERFLOW : int = 10
    # 借出连接前先 ping。必须开：MySQL 8 默认 wait_timeout=8h，空闲连接会被服务端单方面掐掉，
    # 应用侧连接池却以为它还活着 —— 下一次查询直接 Lost connection（且只在低谷后突发流量时出现，极难定位）。
    MYSQL_POOL_PRE_PING : bool = True
    # 主动回收连接的时长，取得比 wait_timeout 短，把「服务端先掐」变成「客户端先换」。
    MYSQL_POOL_RECYCLE_SECONDS : int = 3600
    # 建连超时（秒）。不设的话 MySQL 不可达时请求线程会长时间挂住。
    MYSQL_CONNECT_TIMEOUT : int = 5
    # 调试用：把每条 SQL 打到日志。生产必须关，否则单次问答能刷出上百行日志。
    MYSQL_ECHO_SQL : bool = False
    # Alembic 迁移脚本目录（alembic/env.py 消费）
    ALEMBIC_DIR : Path = BASE_DIR / "alembic"

    @property
    def MYSQL_URL(self) -> str:
        """
        组装 SQLAlchemy 连接串。

        为什么用 quote_plus 而不是 f-string 直拼：密码里一旦出现 @ : / ? # 这类 URL
        保留字符，手拼出来的串会被解析器切错（`p@ss` 会被当成 user=p / host=ss），
        报错信息还完全指不到密码上 —— 排查方向会跑偏几十分钟。
        """
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            f"?charset={self.MYSQL_CHARSET}"
        )

    # Redis 配置（队列 broker + 短期缓存；**不承载会话真相**）
    REDIS_URL : str = "redis://localhost:6379/0"
    # 文档解析任务队列所在库（P0-3 启用）。与缓存分库，便于单独看积压 / 单独清理。
    REDIS_QUEUE_URL : str = "redis://localhost:6379/1"
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
    # ⚠️ **deprecated**：仅 RedisSessionStore（已退出生产路径）还读这三项。
    #    生产路径的会话级锁改由 MySQL `SELECT ... FOR UPDATE` 承担 ——
    #    锁与被保护的数据落在同一个事务边界内，比跨进程分布式锁更简单也更可靠（见 P0-1）。
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

    # ---------- 文档解析异步链路（v2.0.0 P0-3 新增）----------
    # 定位：把「解析 → 切分 → 嵌入 → 写 Chroma」从 HTTP 请求里挪到独立进程。
    # 状态真相源是 MySQL `document.status`，队列（Redis db1）只负责投递与消费。
    #
    # 单文件上传大小上限。校验发生在落盘之前，避免超大文件把磁盘占满。
    DOC_UPLOAD_MAX_BYTES : int = 50 * 1024 * 1024
    # 解析任务队列名。显式命名而不吃 Celery 默认的 "celery"：
    # 同一个 Redis 里将来可能还有别的队列（如 P3 的摘要任务），默认名会让 LLEN 观测串味。
    TASK_QUEUE_NAME : str = "rag.parse"
    # 硬超时（秒）：到点直接 SIGKILL，防坏文件把 worker 永久卡死。
    TASK_TIME_LIMIT_SECONDS : int = 600
    # 软超时（秒）：比硬超时早一步抛 SoftTimeLimitExceeded，
    # 让任务有机会把 document.status 写成 fail 而不是留下一个 parsing 僵尸。
    TASK_SOFT_TIME_LIMIT_SECONDS : int = 540
    # 「parsing 状态」视为孤儿任务的判定时长（秒）。
    # 存在的理由：worker 被 kill 时任务停在 parsing，而抢任务的条件是
    # status IN ('pending','fail') —— 不设这个超时，那条记录就永远捡不回来了。
    # 取值要显著大于单次解析的最长耗时（含模型冷启动），否则会误抢正在跑的任务。
    TASK_STALE_PARSING_SECONDS : int = 900
    # worker 并发数。解析是 CPU 密集 + 嵌入模型吃内存，并发只会互相抢资源。
    WORKER_CONCURRENCY : int = 1
    # 每个子进程处理多少个任务后回收重建。解析会反复吃内存（模型 + 文档对象），
    # 定期回收是防泄漏的最省事手段。
    # ⚠️ 仅 prefork 池有效。macOS 上 worker 跑 solo 池（理由见 worker/app.py），
    #    本地开发**没有**这道保险，跑完记得关。
    WORKER_MAX_TASKS_PER_CHILD : int = 20
    # 问 worker「你还活着吗」的等待时长（秒）。
    # Celery 的 inspect().ping() 会**等满这个时长**才返回（它不知道有几个 worker 会应答，
    # 只能等窗口关掉），所以这个值直接等于接口的最坏耗时。
    # 5 秒会让监控接口慢到没人愿意接；1 秒在本机/内网内足够让健康的 worker 应答
    # （实测单机应答 < 100ms）。worker 挤在一台 4C4G 上，1 秒是合理的分界。
    QUEUE_WORKER_PING_TIMEOUT_SECONDS : float = 1.0

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