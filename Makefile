# 环境方案：uv 管理 Python 与依赖（conda 仅作为可选后备）
# 首次：make setup    之后：make dev（一键起三端）/ make test
# 全部命令见 `make help`。

PYTHON_VERSION ?= 3.11
UV ?= uv
VENV := .venv
PY := $(VENV)/bin/python
# 用变量包一层是为了让沙箱/窄 PATH 环境能通过 `make infra DOCKER=/usr/local/bin/docker` 覆盖
DOCKER ?= docker

.PHONY: help setup venv sync lock api web frontend gateway dev stop test memory releases clean ollama \
        infra infra-check infra-logs infra-stop infra-down \
        db-upgrade db-current db-downgrade db-revision db-sql

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
	@echo "—— 数据库迁移（Alembic，P0-1）——"
	@echo "make db-upgrade    把库升到最新结构（alembic upgrade head）"
	@echo "make db-current    当前库到哪个迁移版本了"
	@echo "make db-downgrade  回退一个版本（少用；本地重建用 infra-down -v 更快）"
	@echo "make db-revision   新建一个空迁移（要传 M=\"说明\"，如 make db-revision M=\"add xxx\"）"
	@echo "make db-sql        只打印 DDL 不执行（给 DBA 审批用）"
	@echo ""
	@echo "—— 单独启动（想在各自终端看日志时用）——"
	@echo "make api       启动 FastAPI (8000)"
	@echo "make gateway   启动 NestJS 鉴权网关 (3000，首次需 cd gateway && npm install)"
	@echo "make frontend  启动 Vue3 前端 Vite 开发服务器 (5173)"
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
	$(PY) tests/test_module1_config.py
	$(PY) tests/test_module2_document_loader.py
	$(PY) tests/test_module3_vectorstore.py
	$(PY) tests/test_module4_llm_and_retriever.py
	$(PY) tests/test_module5_rag_chain_api.py
	$(PY) tests/test_module6_session_store.py
	$(PY) tests/test_module7_redis_over_tcp.py
	$(PY) tests/test_module8_mysql_session_store.py

ollama:
	@curl -s http://localhost:11434/api/tags | head -c 200; echo

# ---------- 本地中间件（MySQL + Redis，P1-5a）----------
# 决策 D1：中间件容器化、应用裸跑。应用通过 127.0.0.1 连这两个端口，
# 因此容器端口只绑回环地址（见 docker-compose.yml 的注释）。
infra:
	$(DOCKER) compose up -d mysql redis
	@echo ""
	@echo "中间件已启动。自检：make infra-check"

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
	@echo "容器已移除，volume（mysql_data / redis_data）保留，业务数据未丢。"
	@echo "如需彻底清空：$(DOCKER) compose down -v   ⚠️ 会删除全部业务数据"

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
