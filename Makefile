# 环境方案：uv 管理 Python 与依赖（conda 仅作为可选后备）
# 首次：make setup    之后：make api / make web / make test

PYTHON_VERSION ?= 3.11
UV ?= uv
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help setup venv sync lock api web test clean ollama

help:
	@echo "make setup   创建 .venv 并安装依赖"
	@echo "make api     启动 FastAPI (8000)"
	@echo "make web     启动 Streamlit (8501)"
	@echo "make lock    重新解析锁文件"
	@echo "make ollama  确认本地 Ollama 服务可用"

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

test:
	$(PY) tests/test_module1_config.py
	$(PY) tests/test_module2_document_loader.py
	$(PY) tests/test_module3_vectorstore.py
	$(PY) tests/test_module4_llm_and_retriever.py

ollama:
	@curl -s http://localhost:11434/api/tags | head -c 200; echo

clean:
	rm -rf $(VENV) __pycache__ */__pycache__
