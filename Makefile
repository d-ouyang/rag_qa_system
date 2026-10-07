# 环境方案：uv 管理 Python 与依赖（conda 仅作为可选后备）
# 首次：make setup    之后：make dev（一键起三端）/ make test
# 全部命令见 `make help`。

PYTHON_VERSION ?= 3.11
UV ?= uv
VENV := .venv
PY := $(VENV)/bin/python
# 前端验收脚本要跑 node；窄 PATH 环境（沙箱）可用 `make accept-ui NODE=/abs/path/node` 覆盖
NODE ?= node
# 用变量包一层是为了让沙箱/窄 PATH 环境能通过 `make infra DOCKER=/usr/local/bin/docker` 覆盖
DOCKER ?= docker
# 全栈容器对外端口（与 docker-compose.yml 的默认值保持一致；改 .env 后这里也要改）
FRONTEND_PORT ?= 8080
GATEWAY_PORT ?= 3000

.PHONY: help setup venv sync lock api web frontend gateway admin admin-build dev stop test memory releases clean ollama \
        infra infra-check infra-logs infra-stop infra-down \
        stack-up stack-ps stack-logs stack-down stack-rebuild \
        db-upgrade db-current db-downgrade db-revision db-sql \
        worker accept accept-ui reindex reindex-apply accept-p04a accept-ui-p04b \
        smoke-session accept-12b-reverse

help:
	@echo "—— 一次性 ——"
	@echo "make setup     创建 .venv 并安装依赖"
	@echo "make dev       一键启动三件套：后端 8000 + 网关 3000 + 前端 5173（Ctrl-C 全部停止）"
	@echo "make stop      按端口停掉三件套（8000 / 3000 / 5173）"
	@echo ""
	@echo "—— 本地中间件（MySQL + Redis，Docker）——"
	@echo "make infra         起容器（mysql 3306 + redis 6379，仅绑 127.0.0.1）"
	@echo "make infra-check   看容器状态 + 两库连通性自检"
	@echo "make infra-logs    跟踪容器日志"
	@echo "make infra-stop    停容器（保留数据）"
	@echo "make infra-down    移除容器与网络（保留 volume，数据不丢）"
	@echo ""
	@echo "—— 全栈容器（P1-5b）——"
	@echo "make stack-up      起全栈（中间件 + backend/worker/gateway/frontend，会先构建镜像）"
	@echo "make stack-ps      看全栈容器状态"
	@echo "make stack-logs    跟踪全栈日志"
	@echo "make stack-down    只停四个应用容器（中间件继续跑）"
	@echo "make stack-rebuild 重建单个服务的镜像（make stack-rebuild s=backend）"
	@echo ""
	@echo "—— 数据库迁移（Alembic，P0-1）——"
	@echo "make db-upgrade    把库升到最新结构（alembic upgrade head）"
	@echo "make db-current    当前库到哪个迁移版本了"
	@echo "make db-downgrade  回退一个版本（少用；本地重建用 infra-down -v 更快）"
	@echo "make db-revision   新建一个空迁移（要传 M=\"说明\"，如 make db-revision M=\"add xxx\"）"
	@echo "make db-sql        只打印 DDL 不执行（给 DBA 审批用）"
	@echo ""
	@echo "—— 异步解析（P0-3a）——"
	@echo "make worker        启动解析 Worker（Celery，消费 Redis db1 的 rag.parse 队列）"
	@echo "                   上传接口只把任务投进队列，解析在这里真正发生；"
	@echo "                   不起它，文档会一直停在 pending"
	@echo "                   池按平台自动选（macOS solo / Linux prefork），见 worker/app.py"
	@echo "make accept        跑**验收**脚本（P0-3a 异步解析链路；不打桩；自己起/杀 worker，需先停掉别的 worker）"
	@echo "                   与 make test 的区别：test 是回归（打桩、离线可跑），accept 是真链路"
	@echo "make accept-p04a   跑**验收**脚本（P0-4a 引用反查 + 知识库重建；真跑一次重建脚本）"
	@echo "                   与 make accept 的两处前提不同：要求**没有** worker 在跑、会改动 vector_db/"
	@echo ""
	@echo "—— 前端验收（P0-3b / P0-4b）——"
	@echo "make accept-ui     跑**浏览器验收**（Playwright 驱动真 Chromium：登录 → 上传 → 轮询状态 →"
	@echo "                   终态提示 → 失败重试 → 片段抽屉 → 下载 → 删除）"
	@echo "                   前置：make infra + 四端全在（api / gateway / frontend / worker）"
	@echo "                   与 make accept 的区别：accept 走 HTTP，accept-ui 走真浏览器（验 DOM 行为）"
	@echo "make accept-ui-p04b 跑**浏览器验收**（P0-4b：登录 → 提问 → 点引用 → 展开切片全文 →"
	@echo "                   收起 / 只展开一条 / 不重复请求 / 后端说查不到时展示后端文案）"
	@echo "                   前置：make infra + 三端在跑（api / gateway / frontend）+ 知识库有已解析文档"
	@echo ""
	@echo "—— 知识库重建（P0-4a）——"
	@echo "make reindex       干跑：报告「要补登记哪些文件 / 要重灌几篇 / 有多少孤儿切片」"
	@echo "                   一句话都不改数据。这个脚本会重灌整库，所以默认是干跑"
	@echo "make reindex-apply 真执行（补登记 → 逐文档重灌 → 清掉缺 doc_id 的遗留切片）"
	@echo "                   前置：先停掉 make worker（并发重灌会让切片翻倍）；需 MySQL + 向量库"
	@echo ""
	@echo "—— 会话冒烟 ——"
	@echo "make smoke-session 用知识库跑一轮真问答，验多轮上下文 / 引用反查 / 落库一致 / 无孤儿。"
	@echo "                   不打桩，需 make infra + make api；复核已有会话传 SESSION_ID=<id>"
	@echo ""
	@echo "—— 单独启动（想在各自终端看日志时用）——"
	@echo "make api       启动 FastAPI (8000)"
	@echo "make gateway   启动 NestJS 鉴权网关 (3000，首次需 cd gateway && npm install)"
	@echo "make frontend  启动 Vue3 前端 Vite 开发服务器 (5173)"
	@echo "make admin     启动**管理端** Vite 开发服务器 (5174，独立应用；需网关在跑)"
	@echo "make web       启动 Streamlit (8501，旧版界面)"
	@echo ""
	@echo "—— 排障与文档 ——"
	@echo "make memory    查看会话存储后端与 Redis 内存水位"
	@echo "make releases  在浏览器打开版本日志页 docs/releases.html（静态文件，不需要起服务）"
	@echo "make ollama    确认本地 Ollama 服务可用"
	@echo "make lock      重新解析锁文件"

setup: venv sync

venv:
	$(UV) venv --python $(PYTHON_VERSION) $(VENV)

sync:
	$(UV) pip sync requirements.lock.txt

lock:
	$(UV) pip compile requirements.in -o requirements.lock.txt --python-version $(PYTHON_VERSION)

api:
	$(VENV)/bin/uvicorn api.main:app --reload --port 8000

web:
	$(VENV)/bin/streamlit run frontend/app.py --server.port 8501

frontend:
	cd frontend && npm run dev

gateway:
	cd gateway && npm run start:dev

# ---------- 管理端（P2-13a）----------
# 独立前端应用 admin-console/，**不新增后端进程** —— 接口复用现有 FastAPI 的
# /api/v1/admin/*（4C4G 内存预算，见 PLAN §11.1）。
# 端口固定 127.0.0.1:5174 + strictPort（5173 是主应用）：被占用直接报错，
# 而不是悄悄换端口让人打开一个长得差不多的页面。
# ⚠️ 前置：中间件与网关在跑 —— make infra + make api + make gateway。
admin:
	cd admin-console && npm run dev

admin-build:
	cd admin-console && npm run build

# 一键起三件套。各自的日志会混在同一终端里 —— 想看清爽的分栏日志就开三个终端分别 make api / gateway / frontend。
# 这里显式记录三个 PID 再 kill，而不是用 `kill 0`（后者会连当前 shell 的进程组一起打掉）。
dev:
	@echo "启动：后端 8000 / 网关 3000 / 前端 5173    停止：Ctrl-C"
	@echo "浏览器打开 http://localhost:5173  （默认账号 admin / admin123）"
	@echo ""
	@$(VENV)/bin/uvicorn api.main:app --reload --port 8000 & \
	 UV=$$!; \
	 ( cd gateway && npm run start:dev ) & GW=$$!; \
	 ( cd frontend && npm run dev ) & FE=$$!; \
	 trap "kill $$UV $$GW $$FE 2>/dev/null; echo ''; echo '三件套已停止'" INT TERM; \
	 wait

# 按端口停。比 `pkill -f uvicorn` 精准：不会误伤其他项目的同名进程。
stop:
	@for p in 8000 3000 5173; do \
	  pids=$$(lsof -ti :$$p 2>/dev/null); \
	  if [ -n "$$pids" ]; then kill $$pids 2>/dev/null && echo "已停止端口 $$p"; \
	  else echo "端口 $$p 本来就没在跑"; fi; \
	done

# docs/releases.html 是纯静态单文件（零依赖），不需要任何服务，直接开就行。
releases:
	@if [ "$$(uname)" = "Darwin" ]; then open docs/releases.html; \
	 else echo "请手动用浏览器打开：$(PWD)/docs/releases.html"; fi

memory:
	@$(PY) -c "import json;from core.memory_manager import get_memory_manager;print(json.dumps(get_memory_manager().memory_report(), ensure_ascii=False, indent=2))"

test:
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module1_config.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module2_document_loader.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module3_vectorstore.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module4_llm_and_retriever.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module5_rag_chain_api.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module6_session_store.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_qa_cache.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module7_redis_over_tcp.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module8_mysql_session_store.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module9_async_pipeline.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module10_chunk_refs.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module11_account.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module12_admin.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module12_audit.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module13_login.py
	EMBEDDING_BACKEND=local RERANK_BACKEND=local $(PY) tests/test_module14_trust_boundary.py

# ---------- P2-12b 反向验证（**不进 make test**）----------
# 它会临时改生产源码（core/identity.py + 两个 .ts）再跑主测试看断言转红，
# 跑完还原。放进 `make test` 意味着每次回归都在动源码 ——
# 主测试一旦失败，你分不清是「回归真失败」还是「反向验证把自己改坏了」。
# 反过来也有一层理由：反向验证的价值在于「拆掉之后必须红」，
# 而 make test 的契约是「全绿」，把两件事塞进同一条命令会让判据含混。
accept-12b-reverse:
	@echo "  注意：会临时改 core/identity.py 与 gateway/src 下两个 .ts，跑完自动还原"
	@$(PY) tests/test_module14_trust_boundary_reverse.py

# ---------- P2-12b 真链路验收（**也不进 make test**）----------
# 两条不进的理由不同，别混：
#   · accept-12b-reverse —— 它**改源码**，混进去会让「回归失败」不可分辨。
#   · accept-12b         —— 它要求后端+网关两个进程**已经起着**（真 HTTP、真 token），
#     而 make test 的契约是「离线可跑、必须永远全绿」。
# 为什么它不能省：module14 是 Python 脚本，对网关那一半（TypeScript）只能
# ① 在 Python 里等价重实现（验规则对不对）② 正扫 .ts 源码（验那份代码写着这些规则）。
# 两条都不等于「运行时真的这么跑」—— 而本仓库目前**没有 tsc 测试环节**。
# ⚠️ 它会临时改 wu.jing / chen.jie 的密码（要拿真 token 只能真登录），
#    收尾换成新随机临时密码 + 强制改密。别在这两个账号正被使用时报。
accept-12b:
	@$(PY) tests/acceptance_p2_12b.py

# ---------- 异步解析 Worker（P0-3a）----------
# 池、并发、超时、投递语义**全部在 worker/app.py 里按 settings 配置**，
# 所以这里不传 --concurrency / --pool / --max-tasks-per-child ——
# 同一个参数在两处（.env 与 Makefile）配，就必然有一次只改了一边。
# 唯一留在命令行的是日志级别（它属于「这一次怎么跑」，不是应用配置）。
worker:
	$(VENV)/bin/celery -A worker.app worker --loglevel=info

# ---------- 验收（与 make test 是两件事，别混）----------
# make test 是**回归**：队列 / worker / 向量库都打桩或换临时目录，快、离线可跑、必须永远全绿。
# make accept 是**验收**：一处都不打桩 —— 真 MySQL + 真 Redis db1 + 真 worker + 真向量库，
#                        全部走 HTTP，按计划书的验收标准逐条实测。
# ⚠️ 它自己管 worker 的生命周期（起 → kill → 重启 → 停），
#    所以**要求跑之前没有别的 worker 在跑**，否则会误杀。
# 当前脚本对应 docs/PLAN-v2.0.0.md 的 P0-3a（版本号见文件名）。
accept:
	$(PY) tests/acceptance_p0_3a.py

# ---------- 引用反查 + 知识库重建验收（P0-4a）----------
# 与 make accept 同属「不打桩」的验收，区别在两条前提：
#   · 要求**没有** worker 在跑（重建脚本会拒绝并发执行 —— 并发重灌会让切片翻倍）
#   · 会真跑一次 scripts/reindex.py（subprocess），因此会改动 vector_db/
# 这是 P0-4 的一次性成本：遗留切片会被清掉、upload/ 下无记录的文件会被补登记。
# ⚠️ 前置：make infra。别在正跑着 make worker 的终端里跑这个。
accept-p04a:
	$(PY) tests/acceptance_p0_4a.py

# ---------- 模拟员工种子（P2-11d）----------
# 干跑是默认：写的是「账号 + 密码」，参数敲错不该直接生效（沿用 reindex 的规矩）。
# 干跑只打印计划，一个字都不写；--apply 才落库，且**可反复跑**（重跑只同步资料，
# 不动密码、不动 status —— 管理员改过的密码不该被脚本打回临时密码）。
# 临时密码只显示一次，且脚本走 print 不走 logging，所以不会进 app.log。
seed-users:
	$(PY) scripts/seed_users.py

seed-users-apply:
	$(PY) scripts/seed_users.py --apply

# 迁移往返验收（P2-11a）----------
# 与上面几个 accept 的区别：它验的是**迁移本身能不能安全来回**，不是业务功能。
# 做法是真的 downgrade 到 0002 再 upgrade 回 head，中间核对
#   · 表增删是否符合预期（department/position 建了又删、is_active 下线又回归）
#   · **非空表**上重新 upgrade 会不会报错（employee_no 的默认值就在这里被验证）
#   · 往返之后 compare_metadata 仍然为 0（结构零漂移的往返版）
#   · 存量业务数据一行不少（35 文档 / 90 切片 / 会话 / 消息）
# ⚠️ 它会改**数据库结构**（虽然最终回到 head）。跑之前不要有别的进程在改库。
accept-p211a:
	$(PY) tests/acceptance_p2_11a.py

# ---------- 前端验收（P0-3b）----------
# 与 make accept 一样是「不打桩」的验收，区别在**它驱动真浏览器**：
# 真 NestJS 网关登录（也就真鉴权）、真上传、真 Celery Worker 解析、在真 Chromium 里读 DOM。
# 之所以必须用浏览器：本次改造的验收对象（状态列真的在轮询、切走页面后提示还在、
# 按钮按状态置灰、失败原因可读）全是 DOM 行为，HTTP 断言里看不见。
# 只有两处桩，且都是**前端渲染分支**（脚本头注释写明了是哪两处、为什么）：
# /api/v1/system/queue 的 worker 存活、文档列表里注入一条 pending 记录。
# ⚠️ 前置：make infra + 四端全在（make api / gateway / frontend / worker）。
# playwright 装在全球 node_modules 下，而 ESM 的 import 不认 NODE_PATH
# （脚本内部用 createRequire 借道 CJS 才拿得到），所以这里显式把路径传进去。
accept-ui:
	NODE_PATH="$$(npm root -g)" $(NODE) tests/acceptance_p0_3b_ui.mjs

# ---------- 前端验收（P0-4b 引用可点击）----------
# 与 accept-ui 同一套打法（真浏览器读 DOM），但不需要 worker：
# 它问一个真问题，然后验「点引用 → 展开切片全文」这条交互链。
# 只有一处桩：把反查请求改成 404，验「后端说查不到时前端展示后端那句人话」。
# 为什么必须用桩：文档真的删掉之后这条引用就检索不到了，场景无法用真链路复现。
# ⚠️ 前置：make infra + 三端在跑（make api / gateway / frontend），
#    且知识库里至少有一份已解析成功的文档（否则问不出引用）。
accept-ui-p04b:
	NODE_PATH="$$(npm root -g)" $(NODE) tests/acceptance_p0_4b_ui.mjs

# ---------- 知识库重建（P0-4a）----------
# 干跑是**默认**：这个脚本会重灌整库并删除遗留切片，不该在敲错命令时就直接动数据。
# 所以拆成两个目标，reindex 永远安全，reindex-apply 才是要动手的那个。
# ⚠️ 执行前请先停掉 make worker —— 见 scripts/reindex.py 文件头「为什么拒绝并发执行」。
reindex:
	$(PY) scripts/reindex.py

reindex-apply:
	$(PY) scripts/reindex.py --apply

# ---------- 会话冒烟（真链路，P0-4a 之后）----------
# 与上面两个「重灌知识库」不同，这里不碰知识库：它**用**知识库跑一轮完整的三段会话，
# 验四件事 —— 多轮上下文真的带进去了 / 每条引用都能反查到原文 /
# session 与 chat_message 落库条数对得上 / 没有孤儿切片。
# 不打桩：真后端（直连 8000，不经网关）+ 真 Chroma + 真 MySQL + 真模型。
# 问的三个问题都能在 docs/guoquan-kb/ 里查到答案 —— 问不到是知识库的锅，
# 脚本会变成「假的通过」，所以别随手改问题。
# ⚠️ 前置：make infra + make api。
# 复核已有会话（不新增提问）：make smoke-session SESSION_ID=<id>
smoke-session:
	$(PY) scripts/smoke_session.py $(if $(SESSION_ID),--session-id $(SESSION_ID),)

ollama:
	@curl -s http://localhost:11434/api/tags | head -c 200; echo

# ---------- 本地中间件（MySQL + Redis + Chroma，P1-5a / p1.5c）----------
# 决策 D1：中间件容器化、应用裸跑。应用通过 127.0.0.1 连这些端口，
# 因此容器端口只绑回环地址（见 docker-compose.yml 的注释）。
# p1.5c 起 chroma 进默认 profile：backend / worker 经 HttpClient 共享服务端索引。
infra:
	$(DOCKER) compose up -d mysql redis chroma
	@echo ""
	@echo "中间件已启动（mysql + redis + chroma）。自检：make infra-check"

infra-check:
	@bash scripts/infra-check.sh "$(DOCKER)"

infra-logs:
	$(DOCKER) compose logs -f --tail=50

# 停容器，保留容器与数据（再 start 即可恢复）
infra-stop:
	$(DOCKER) compose stop

# 移除容器与网络；volume 保留，数据不丢
infra-down:
	$(DOCKER) compose down
	@echo ""
	@echo "容器已移除，volume（mysql_data / redis_data）与 bind mount（vector_db）保留，业务数据未丢。"
	@echo "如需彻底清空：$(DOCKER) compose down -v   ⚠️ 会删除全部业务数据"

# ---------- 全栈容器（P1-5b）----------
# 应用服务挂在 `full` profile 下（原因见 docker-compose.yml 头部注释）：
# 不加 --profile 的命令看到的只有中间件，与 P1-5a 时期行为一致。
#
# ⚠️ 起全栈前先停掉宿主机上裸跑的 backend(8000) / gateway(3000) / frontend(5173)，
# 否则端口冲突会让容器反复重启（frontend 用的是 8080，不冲突，但 gateway 的 3000 会）。
stack-up:
	$(DOCKER) compose --profile full up -d --build
	@echo ""
	@echo "全栈已启动。前端入口：http://127.0.0.1:$(FRONTEND_PORT)  网关：http://127.0.0.1:$(GATEWAY_PORT)"
	@echo "看状态：make stack-ps      看日志：make stack-logs"

stack-ps:
	$(DOCKER) compose --profile full ps

stack-logs:
	$(DOCKER) compose --profile full logs -f --tail=80

# 只停应用服务，中间件继续跑（日常开发还要用）
stack-down:
	$(DOCKER) compose --profile full stop backend worker gateway frontend
	@echo ""
	@echo "应用容器已停，中间件仍在跑（要一起停：make infra-stop）。"

# 重建某个服务的镜像（改了 Dockerfile 或依赖之后）
# 例：make stack-rebuild s=backend
stack-rebuild:
	@test -n "$(s)" || (echo "用法：make stack-rebuild s=backend|worker|gateway|frontend" && exit 1)
	$(DOCKER) compose --profile full up -d --build $(s)

# ---------- 数据库迁移（Alembic，P0-1）----------
# 连接串不在这里传 —— alembic/env.py 从 config/settings.py（即 .env）读取，
# 避免密码在 alembic.ini 里出现第二份。
ALEMBIC := $(VENV)/bin/alembic

db-upgrade:
	$(ALEMBIC) upgrade head

db-current:
	@$(ALEMBIC) current 2>&1 | grep -v "^INFO" | tail -3

db-downgrade:
	$(ALEMBIC) downgrade -1

# 用法：make db-revision M="add xxx column"
db-revision:
	@if [ -z "$(M)" ]; then echo "❌ 必须传说明：make db-revision M=\"add xxx\""; exit 1; fi
	$(ALEMBIC) revision -m "$(M)"

# 离线模式：只把 DDL 打到 stdout，不连库。生产库账号通常没有 DDL 权限，
# 走这条路可以把变更交给 DBA 审。
db-sql:
	$(ALEMBIC) upgrade head --sql

clean:
	rm -rf $(VENV) __pycache__ */__pycache__
