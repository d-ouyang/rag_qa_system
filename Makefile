# 环境方案：uv 管理 Python 与依赖（conda 仅作为可选后备）
# 首次：make setup    之后：make dev（一键起三端）/ make test
# 全部命令见 `make help`。

PYTHON_VERSION ?= 3.11
UV ?= uv
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help setup venv sync lock api web frontend gateway dev stop test memory releases clean ollama

help:
	@echo "—— 一次性 ——"
	@echo "make setup     创建 .venv 并安装依赖"
	@echo "make dev       一键启动三件套：后端 8000 + 网关 3000 + 前端 5173（Ctrl-C 全部停止）"
	@echo "make stop      按端口停掉三件套（8000 / 3000 / 5173）"
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

ollama:
	@curl -s http://localhost:11434/api/tags | head -c 200; echo

clean:
	rm -rf $(VENV) __pycache__ */__pycache__
