from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import quote_plus

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.version import PROJECT_NAME as _VERSION_NAME
from config.version import PROJECT_VERSION as _VERSION_STRING

class Settings(BaseSettings):
    # 项目信息
    # ⚠️ 版本号真相源是**入库的 config/version.py**（随代码/tag 同步，PLAN-v4.0.0 §1.1）：
    #    `.env` 的 PROJECT_VERSION 只是「本地开发覆盖」，服务器 .env 删掉该行即跟随代码。
    #    别再手动改服务器 .env 的版本行——那是 2026-10-16 之前「UI 显示老版本」的根因。
    PROJECT_NAME : str = _VERSION_NAME
    PROJECT_VERSION : str = _VERSION_STRING
    BASE_DIR : Path = Path(__file__).parent.parent.resolve()

    @field_validator("PROJECT_VERSION")
    @classmethod
    def _version_not_empty(cls, v: str) -> str:
        """`.env` 写了空串（`PROJECT_VERSION=` 留空）时兜底回代码入库版本。

        没有这道校验，一次手滑的空值会让 UI 设置页与 /system/health 的版本号
        显示成空字符串——排查时第一眼会以为是部署没生效，方向直接跑偏。
        """
        return v or _VERSION_STRING

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
    # 嵌入从哪来（v2.0.0-p1.6）：
    #   siliconflow = 调硅基流动的嵌入接口，进程不加载本地权重（运行路径用这个）
    #   local       = 读 models/ 目录里的 HuggingFace 权重（仅 make test 回归用）
    # 切换嵌入模型会改变向量维度，已有切片必须重灌，不能混在同一个 collection 里。
    EMBEDDING_BACKEND : Literal["siliconflow", "local"] = "siliconflow"
    SILICONFLOW_EMBEDDING_MODEL : str = "BAAI/bge-large-zh-v1.5"
    CHUNK_SIZE : int = 500
    CHUNK_OVERLAP : int = 50
    # 切分模式（v3.1.0 P3-7a，PLAN-v3.0.0.md §6.1）：
    #   fixed = 现状 RecursiveCharacterTextSplitter（500/50），一行行为不变；
    #   title = md 按标题树切分（MarkdownHeaderTextSplitter），超长小节二次切、
    #           过短小节向相邻合并，入库前把 header_path 拼到正文前做 Enrichment。
    # ⚠️ 切默认值 = 整库重建（铁律）：切分参数变了 chunk_index→正文对应关系全漂。
    #    是否把默认翻成 title 由 golden 评测说话（recall/MRR 提升且拒答不降），不由「做了就开」决定。
    CHUNK_MODE : Literal["fixed", "title"] = "fixed"

    # ------------------------------------------------------------------ #
    # PDF 结构化解析走 MinerU（v3.1.0 P3-7b，PLAN-v3.0.0.md §6.2）
    # ------------------------------------------------------------------ #
    # 定位：PyMuPDF 抽的是纯文本流，表格按行线性化、合并单元格信息直接消失，
    # 这是纯文本抽取的通病，无参数可救。MinerU 版面模型能输出保留表格结构与
    # 跨页拼接的 markdown —— 但它按次计费且依赖外部服务，所以只做「路由增强」，
    # 失败一律降级回 PyMuPDF，绝不让上传链路的可用性绑定在一个外部 API 上。
    # 总开关：false = PDF 一律 PyMuPDF（行为与 P3-7b 之前一致）。
    # ⚠️ 与 RETRIEVAL_HYBRID 同一个坑：真实开关在 `.env`，本地已设 true（按次计费），
    #    判断线上开没开要读 `.env`，别拿代码默认值下结论。
    MINERU_ENABLED : bool = False
    MINERU_API_KEY : str = ""
    MINERU_API_BASE : str = "https://mineru.net/api/v4"
    # 路由判据之一：超过此大小的 PDF 直接 PyMuPDF（大文件解析慢、耗额度多）。
    # 默认 20MB；MinerU 自身上限 200MB，这里取更保守值——大 PDF 通常是扫描件，
    # pipeline 模型收益有限，真要精析可调大。
    MINERU_MAX_BYTES : int = 20 * 1024 * 1024
    # 解析模型：pipeline（默认，均衡）/ vlm（识别更强、更贵）
    MINERU_MODEL_VERSION : Literal["pipeline", "vlm"] = "pipeline"
    MINERU_POLL_INTERVAL : int = 3       # 轮询间隔（秒）
    MINERU_POLL_TIMEOUT : int = 300      # 轮询硬上限（秒），超时降级 PyMuPDF

    # 检索配置
    SEARCH_TOP_K : int = 5
    USE_RERANKER : bool = True
    RERANKER_MODEL_NAME : str = "bge_reranker_base"
    # 重排从哪来。siliconflow = POST /rerank，不加载本地 CrossEncoder（约 1.1GB）。
    # local 只留给 make test。分数仍是「越大越相关」，阈值沿用 RERANK_SCORE_THRESHOLD。
    RERANK_BACKEND : Literal["siliconflow", "local"] = "siliconflow"
    SILICONFLOW_RERANK_MODEL : str = "BAAI/bge-reranker-v2-m3"
    # 重排前的候选池倍数：先按 SEARCH_TOP_K × N 从向量库召回候选，再精排到 SEARCH_TOP_K。
    # 若只召回 TOP_K 就直接重排，等于「把同样的 K 条换个顺序」，召回率没有任何提升。
    RERANK_CANDIDATE_MULTIPLIER : int = 4
    # 重排分数阈值：低于该值的候选直接丢弃（None 表示不启用过滤）。
    # 注意 bge-reranker 输出的是 sigmoid 后的 0~1 相关性分数，与向量距离不是一个量纲。
    # 实测分布：相关文档 ≈0.99，无关文档 ≈0.0003，0.1 是保守安全的分界线。
    # 不过滤的恶果：无关文档混进上下文，模型拒答但 sources 照带 ——
    # 前端出现「无法回答 + 引用资料 1 条」的矛盾展示（实测复现）。
    RERANK_SCORE_THRESHOLD : float | None = 0.1
    # ↑ P4-7（2026-10-16）扫档结论：0.3/0.5/0.7 三档实测（41 题，含 q041 幻觉回归锚）
    #   库内最低 top1=0.2569 与库外最高 top1=0.3232 犬牙交错——不存在能分开两类的阈值：
    #   0.3 档 = 命中率 -10pp + 误拒 +3，而 q041（0.3232）照样漏；0.5 档 = 拒答全对但误拒 23%。
    #   → 维持 0.1，根治在 P4-6 答案充分性校验。曲线报告：data/eval/results/*threshold-sweep*.html
    # ------------------------------------------------------------------ #
    # 混合检索（v3.0.0 P3-4）：向量召回 + BM25 关键词召回 → RRF 融合 → 原 reranker
    # ------------------------------------------------------------------ #
    # ⚠️ 默认值保持 False，**不要改这个默认值**：回归必须可复现
    #    （scripts/test_sandbox.sh 强制 RETRIEVAL_HYBRID=false；module20 有断言盯住默认值，
    #     翻 true 会让回归红）。**真实开关在 `.env`**（.env 覆盖代码默认值）——
    #     判断线上到底开没开，读 `.env`，别读这里。
    #
    # 📌 决策沿革（2026-10-12 两次定论，别只读首轮结论就下判断）：
    #   · 首轮 A/B：recall@20 判据不达标 → 负结论记档，默认保持 false。
    #   · 决策变更（同日）：判据口径失效 ≠ 功能无效 —— 基线在 k=20 已摸天花板
    #     （hit@20=1.0），recall@20 结构性不可能达标；而 recall@5 有 +4.45pp 实证增益、
    #     唯一失手的编号题被 BM25 第一名救回 → 用户拍板 **`.env` 翻 true**，已生效。
    #     观察 / 关回指标 = `bm25_only`；完整论证见迭代文档 §7 修订 1，PLAN §3 P3-4 同注。
    #    混合检索只扩召回面，**不动 reranker 与 0.1 阈值** —— 开与不开，
    #    拒答口径与引用质量都应是同一套。
    # ⚠️ 运维：服务器 `/opt/qa/.env` 需同步改 true（.env 不入 git），改完重启后端。
    RETRIEVAL_HYBRID : bool = False
    # RRF 融合常数 k：`score(d) = Σ 1/(k + rank_i(d))`，逐路累加。
    # k 越大，名次差异被压得越平（第 1 名与第 10 名的贡献差越小）；
    # 60 是原始论文（Cormack 2009）的经验值，也是各家的默认值 —— 不调它，
    # 免得把「融合公式」和「调优」两件事混在一起，A/B 时说不清是谁的功劳。
    RRF_K : int = 60

    # ------------------------------------------------------------------ #
    # 检索前 Query 增强（v3.1.0-p3.7d，检索质量路线图 §6.4 D）
    # ------------------------------------------------------------------ #
    # 单轮改写：把口语化 / 不规范问法规整成检索友好句（复用主答模型，不另起模型）。
    # ⚠️ 默认关闭：收益由 P3-3 评测集说话，未达标前保持关；开关关时零行为变化
    #    （与 RETRIEVAL_HYBRID 同哲学——「做了就该开」在这里不成立）。
    #    多轮对话里的指代改写（standalone_question）与此开关无关，始终生效。
    QUERY_REWRITE : bool = False
    # HyDE（Hypothetical Document Embeddings）：改写后再生成一段「假设答案」，
    # 用该假设答案的 embedding 去做向量召回（BM25 仍用真实问句关键词，不污染关键词匹配）。
    # 仅当 QUERY_REWRITE 或本开关打开时才执行改写（HyDE 需要一条干净问句去展开）；
    # 单独开 HyDE 会自动先走一次改写。开关关时零行为变化。
    QUERY_HYDE : bool = False

    # ------------------------------------------------------------------ #
    # 答案依据校验（v4.0.0 P4-6，PLAN-v4.0.0.md）
    # ------------------------------------------------------------------ #
    # 生成之后、落库之前，用同模型做一次「答案论断 vs 检索资料」的依据比对
    # （q041 病假幻觉的根治方案：阈值挡不住「擦边相关但无答案」，
    #  只有校验答案本身能不能在上下文里找到依据才挡得住）。
    # 热开关 groundedness_check 在 config_flags 注册；判定失败 fail-open
    # （grounded=None，不挡回答）。低置信带常量在 core/groundedness.py。
    GROUNDEDNESS_ENABLED : bool = True

    # ------------------------------------------------------------------ #
    # 会话滚动摘要（v4.0.0 P4-10）
    # ------------------------------------------------------------------ #
    # 窗口（MEMORY_MAX_TURNS）之外的对话由模型增量压成一段摘要，
    # 读的时候前置进上下文 —— 「用户看得见的历史」模型也能看见。
    # 摘要失败 fail-open（保留旧摘要，不影响问答）。热开关 session_summary。
    SESSION_SUMMARY_ENABLED : bool = True

    # ------------------------------------------------------------------ #
    # 运行时配置服务（v4.0.0 P4-1，PLAN-v4.0.0.md）
    # ------------------------------------------------------------------ #
    # 定位：把「业务开关 / 参数」从进程启动期冻结的 `.env` 解耦到**热更新**数据源，
    #       改开关不再需要 recreate 容器（基础设施配置仍留 .env，那是应得的重启代价）。
    # 真相源 = MySQL `system_config` 表（迁移 0011 / core/schema.py）；
    # 读取 = core/config_service.py 的进程内缓存 + TTL 回源，请求链路零 DB 开销。
    # 与 Redis 的分工不变：Redis 在 P2 只做变更广播 + 热缓存，**不存真相**。
    #
    # 后端选择（与 MEMORY_BACKEND 同哲学）：
    #   memory = 进程内 dict。本地开发 / 测试用（重启即忘，无需 MySQL）；
    #   mysql  = system_config 表。**生产用**，改一次全站生效、重启不丢。
    # ⚠️ 默认 memory 是刻意的：让所有离线测试（module23/24）不依赖 MySQL 也能跑。
    #    生产要在 .env 显式配 mysql——「以为配了热更新、其实存在内存里」比
    #    「重启才生效」更难排查（后者至少每次重启行为一致）。
    CONFIG_BACKEND : Literal["memory", "mysql"] = "memory"
    # 配置缓存 TTL（秒）。P1 靠它兜底最终一致（别的实例/脚本直改库后，
    # 最多这么久全站可见）；P2 加 Redis pub/sub 即时失效后它仍是第二道保险。
    CONFIG_TTL_SECONDS : int = 60

    # ------------------------------------------------------------------ #
    # 月度用量快照（P3-6，归档审计；判定路径不读它）
    # ------------------------------------------------------------------ #
    # beat 由 worker 进程内嵌（`celery worker -B`），单实例部署的最低成本形态。
    USAGE_SNAPSHOT_ENABLED : bool = True
    # 每日低峰时点。避开 03:20 的 COS 备份（P2-10b）：两个定时任务挤同一分钟
    # 会互相争 IO / 连接，排障时也分不清日志是谁的。
    USAGE_SNAPSHOT_HOUR : int = 4
    USAGE_SNAPSHOT_MINUTE : int = 10

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
    # 送进模型和问题重写的最近轮数。1 轮 = 1 问 + 1 答。
    # 只限制模型看见的窗口，不删库里的历史。左侧列表始终能打开全部会话。
    MEMORY_MAX_TURNS : int = 5
    # 仍记录 last_active，供「多久没打开 / 没再问」使用。
    # 不再用来把会话从列表里藏掉，也不再用来清空记忆。
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

    # ---------- 身份与信任边界（P2-13a 落地管理端时的第一次借用，见设计规格 §5.1.3 的 A 方案）----------
    # 后端只认「由网关注入的身份头」，自己不验 JWT（认证只有一个入口）。
    # 代价：8000 一旦对外可达，任何人都能靠伪造 X-User-Id 冒充任意用户。
    # 所以 compose 里 backend 不映射端口，这一侧的防线在**网络拓扑**，不在代码。
    #
    #   dev     本地裸跑 / 测试：身份头缺失时回落到 DEV_USERNAME，库里查不到则
    #           视作 `.env` 的 break-glass 超管（role=admin）。好处是 `make api`
    #           起来就能用，代价是有后门 —— 所以它不该出现在任何非本机环境。
    #   gateway 生产：缺头 / 查不到 / 非在职 → 一律 401（fail-closed）。
    IDENTITY_MODE : Literal["dev", "gateway"] = "dev"
    # dev 模式的回落登录名（应与 `.env` 里 GATEWAY_USERS 的账号一致）
    IDENTITY_DEV_USERNAME : str = "admin"

    # ---------- 内部接口共享密钥（P2-11c）----------
    # `/api/v1/internal/*` 下是「替网关做登录判定」的接口：它能验证密码、
    # 能改 failed_login_count、能写 last_login_at、能落审计。拿到它 ≈ 半个登录系统。
    #
    # 为什么不用「内网可达就行」：容器网络里同网段**任意服务**都能连到 8000，
    # 将来多起一个容器（监控、日志、二次开发）就是一条绕过网关的路径。
    # 密钥是第二道防线 —— 第一道是拓扑（compose 里 backend 不映射端口）。
    #
    # ⚠️ 缺失的处理与网关那侧**刻意不同**：网关是「生产缺 JWT 密钥直接启动失败」，
    # 这里却是「运行期返回 503 拒绝一切请求」（见 api/routes/internal.py）。
    #
    # 理由：网关那个决定是对的，但搬过来会连累两个场景 ——
    #   ① `make test` 与 module1 的配置断言（要不起一个 FastAPI 进程就要有密钥）；
    #   ② `make api` 裸跑（本机开发，没人想先配一个 48 字节随机串）。
    # 而「拒绝一切请求」已经拿到了与「启动失败」**等价的安全结果**：
    # 配错密钥的表现是登录全挂（立刻发现），不是「悄悄放行」。
    # 真要更早发现，`docker-compose` 里给 backend 显式注入该变量即可
    # （compose 的 `environment` 缺项会在容器启动时一眼看到）。
    #
    # 生成：python -c "import secrets; print(secrets.token_urlsafe(48))"
    INTERNAL_SHARED_SECRET : str = ""

    # ---------- 共享办公网关的可信凭证（多服务共享网关，additive 信任）----------
    # 背景：RAG 与 office 助手等同公司不同服务共用一套 API 网关（见 office 仓
    # 《共享网关迁移设计》，认证放 B：自建共享网关）。该网关转发到 RAG 后端时，
    # 用【本密钥】作 `X-Internal-Auth` 网关证明，后端即信任其注入的身份头。
    #
    # 为什么单独一把、不复用 INTERNAL_SHARED_SECRET：两者是**可独立轮换**的信任关系 ——
    # RAG 自己的网关与共享办公网关各持一把，轮换/吊销其一不影响另一个（复用一把则
    # 「改一处要同步三处」，正是本文件顶部反复警告的故障模式）。
    # 留空 = 不信任共享办公网关（老路不受影响，fail-closed）。仅在 gateway 模式生效。
    # 生成同 INTERNAL_SHARED_SECRET。
    OFFICE_GATEWAY_SHARED_SECRET : str = ""

    # ---------- 密码策略（P2-11b，账号体系）----------
    # 这一组是「判定规则」的唯一出处，core/password_policy.py 只从这里取值，
    # 任何地方都不许再写死 12 / 90 / 5 / 15 这几个数字 ——
    # 「一处配三处用」的日子过不了两次需求就会漂。
    #
    # 哈希算法固定 bcrypt（与网关 bcryptjs 兼容，同一格式 `$2b$`），
    # 刻意不用 MD5/SHA/可逆加密，也不换 argon2 —— 换算法会让现存哈希全部作废，
    # 而账号体系本期刚起步，此时换是最便宜的时机，将来就不便宜了。
    BCRYPT_COST : int = 12
    # 最小长度。长度比复杂度规则有效得多（见设计规格 §4）。
    PASSWORD_MIN_LENGTH : int = 10
    # 有效期（天）。判据是 `now - password_changed_at > 天数`，所以「正好到期的
    # 那一刻」仍算有效，差一秒才算过期 —— 边界写进测试，避免有人改口径。
    PASSWORD_EXPIRE_DAYS : int = 90
    # 剩余天数 ≤ 该值时登录后返回提醒横幅（P2-11c 用）。
    PASSWORD_EXPIRE_WARN_DAYS : int = 7
    # 最近 N 条历史不可复用。⚠️ 每条都要跑一次 bcrypt 比对：**本机实测 cost 12
    # 单次约 165ms**（M 系列；别的机器会不同，这个数只说明量级），所以 N 调大是线性
    # 变慢；5 条约 0.85s，只发生在「改密」这一次动作上，不在登录路径上，可接受。
    PASSWORD_HISTORY_KEEP : int = 5
    # 连续登录失败达该次数 → 锁定。
    PASSWORD_MAX_FAILURES : int = 5
    # 锁定时长（分钟）。到期自动解锁，不需人工介入。
    PASSWORD_LOCK_MINUTES : int = 15

    # Redis 配置（队列 broker + 短期缓存；**不承载会话真相**）
    # 相同问题缓存（core/qa_cache.py）。只缓存规范化后逐字相同的问题，
    # 不缓存「办理居住证」这类改写过的近义句。Redis 丢了只是多调一次模型。
    QA_CACHE_ENABLED : bool = True
    # 池子上限。按最近使用淘汰：命中或新写入把这条放到最前，超限删最久没被用到的。
    QA_CACHE_MAX_ENTRIES : int = 100

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
    # P3-2：是否开放「下载知识库原始文件」。
    #   true  → 路由按 kb ACL 判定（只读档 none 仍然 403）；
    #   false → 路由直接 404（**功能不存在**，不是「你没权限」），
    #           前端按钮也不渲染。
    #
    # 为什么关掉要 404 而不是 403：403 的语义是「有这东西，不给你」，
    # 它会诱导人去找管理员要权限，而管理员那边根本没有开关 ——
    # 那是把一次部署决策变成一个永远解决不了的工单。
    #
    # 关掉的**真实收益**是保问答流式的带宽（轻量机峰值带宽会被下载挤占），
    # 不是省对象存储流量 —— COS 只用于整站备份，下载读的是本机磁盘。
    DOC_DOWNLOAD_ENABLED : bool = True
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

    # ---------- Token 额度（P2-15）----------
    # 每人每月的 token 额度。**NULL/0 = 不限**（见 core/quota_policy.py）。
    #
    # ⚠️ 放在 .env 而不放库里的理由与 `MEMORY_BACKEND` 同源：
    # 改一次全站生效，不必写迁移。代价是**改额度要重启后端**（可接受：
    # 额度是「月」级参数，不是「随时」级）。
    #
    # ⚠️ 取值 0 而不是「写一个默认数字」：额度这件事没有普适的默认值 ——
    # 一份 20 页 PDF 的问答和一句「你好」的 token 差两个数量级，
    # 拍一个数字出来只会让人以为「这个数有依据」。
    # 0 = 不限，于是「不设额度」不需要解释为什么是 0，
    # 而「设成 500 万」需要解释为什么是 500 万。
    TOKEN_QUOTA_DEFAULT_MONTHLY : int = 100000  # 2026-10-08 用户拍板给固定默认（原 0=不限，见 .env 注释）
    # 用量达到额度百分之几时开始提示（80 = 80%）。
    # ⚠️ **不阻断**（用户 2026-10-07 拍板：「不要做真的阻断或者禁用，
    # 只做提醒和看板」）—— 所以这两个阈值只驱动横幅与看板配色。
    TOKEN_QUOTA_WARN_PERCENT : int = 80
    # 「已超额」的判定百分比（100 = 用满即算超）。
    TOKEN_QUOTA_OVER_PERCENT : int = 100
    # 超额行为档位（P3-1 起两档）：
    #   notify = 只提醒不阻断（2026-10-07 拍板，横幅 + 看板标红，提问照常）；
    #   block  = 本月已用 >= 限量时，/ask 与 /ask/stream 预检返回 429
    #            （2026-10-11 拍板，系统已对外上线；判据见 quota_policy.can_ask）。
    # 合法值清单在 core/quota_policy.OVER_ACTIONS（判定唯一处）。
    # ⚠️ 配错值会让**启动直接失败**（见下方 validate_over_action）——
    #   宁可起不来，也不要「以为配了阻断、其实什么都没发生」。
    # ⚠️ 默认值仍是 notify：**升级不改行为**，要拦得显式配 block。
    TOKEN_QUOTA_OVER_ACTION  : str = "notify"
    # 看板与聚合的默认统计自然月起点偏移（0 = 当月 1 号）。
    # 留成配置是为了将来要「按结算周期」时不用改代码。
    TOKEN_QUOTA_PERIOD_START_DAY : int = 1
    # 问 worker「你还活着吗」的等待时长（秒）。
    # Celery 的 inspect().ping() 会**等满这个时长**才返回（它不知道有几个 worker 会应答，
    # 只能等窗口关掉），所以这个值直接等于接口的最坏耗时。
    # 5 秒会让监控接口慢到没人愿意接；1 秒在本机/内网内足够让健康的 worker 应答
    # （实测单机应答 < 100ms）。worker 挤在一台 4C4G 上，1 秒是合理的分界。
    QUEUE_WORKER_PING_TIMEOUT_SECONDS : float = 1.0

    # ---------- 念安站心情收集（site-mood，独立于 RAG 业务）----------
    # 定位与边界见 api/routes/mood.py 模块注释。数据落在 site_mood_submission
    # 表（core/schema.py / 迁移 0010），与 RAG 的用户/会话/文档零关联。
    # submit 的 per-IP 固定窗口限流：每 IP 每分钟最多几次提交
    # （含被去重拒绝的尝试 —— 限流在最外层，重复点击也在消耗额度）。
    MOOD_RATE_LIMIT_PER_MINUTE : int = 3
    # 邮箱冷却去重窗口（秒）：同一邮箱（大小写不敏感）在该窗口内第二次提交按重复拒绝。
    # 是「冷却」不是「永久封死」：过期后允许再次提交（比如更新心情）。
    # 判定放 Redis（SET NX EX）而不是 MySQL 唯一约束，就是为了改窗口不用改表。
    MOOD_EMAIL_COOLDOWN_SECONDS : int = 600
    # admin 查看口令。⚠️ 为空 = admin 接口一律 503（fail-closed，与内部接口
    # INTERNAL_SHARED_SECRET 的运行期处理一致——配错的表现是「看不了」，
    # 不是「悄悄放行」）。生成：python -c "import secrets; print(secrets.token_urlsafe(32))"
    # 刻意不复用 JWT / INTERNAL_SHARED_SECRET：这个功能独立，密钥互不牵连。
    MOOD_ADMIN_TOKEN : str = ""

    # 意图识别。默认走硅基流动，失败再降级成本地规则，不依赖本机 Ollama。
    INTENT_LLM_PROVIDER : Literal["openai", "siliconflow", "ollama"] = "siliconflow"
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
    ALLOWED_ORIGINS : list[str] = [
        "http://localhost:5173",
        "http://localhost:8501",
        "http://localhost:3000",
        # 管理端（P2-13a，admin-console，端口固定 5174）。
        # 正常链路走 Vite proxy（同源、不产生跨域），留白名单是给「不经代理直连调试」留的门。
        "http://localhost:5174",
    ]
    SECRET_KEY : str = ""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env", env_file_encoding="utf-8", extra="ignore",
    )

# 实例化全局配置
settings = Settings()   

# 确保必要目录存在
for dir_path in [settings.DATA_DIR, settings.VECTOR_DB_DIR, settings.MODELS_DIR, settings.UPLOAD_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)