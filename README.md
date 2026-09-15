# rag-qa-system

跟着教学视频从 0 到 1 实现的企业级私有化 RAG 智能问答系统（后端 FastAPI + LangChain LCEL，前端 Streamlit → 后续 Vue 3）。
参考源码：`/Users/ouyangding/Downloads/rag_qa_system`（视频原版，含 7 处必修缺陷，见 `docs/缺陷修复清单.md`）。

---

## 1. 环境方案：uv venv（项目内），不用项目级 conda env

```
conda/uv 提供 Python 版本  →  项目内 .venv 隔离依赖  →  uv pip sync 锁死版本
```

**为什么不在项目下建 conda 环境**：conda env 默认落在 `~/miniconda3/envs/<name>`，虽然可以用 `conda create -p ./.conda` 放进项目，但 IDE 识别差、路径长、容易误删、`.gitignore` 要额外加规则。项目内 `.venv` 是 Python 官方惯例，PyCharm / VSCode 自动识别，删掉即弃。

### 首次搭建

```bash
# 一次性：把 uv 加入 PATH（已装在 ~/.local/bin/uv，当前 shell 没找到它）
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

cd ~/Desktop/AIAgent/rag-qa-system
make setup          # = uv venv --python 3.11 .venv && uv pip sync requirements.lock.txt
```

用 conda 提供 Python 也行（等价，只是慢一些）：

```bash
conda create -n rag-qa python=3.11 -y
conda activate rag-qa
pip install -r requirements.txt
```

### 日常

```bash
source .venv/bin/activate
make api     # FastAPI  http://localhost:8000/docs
make web     # Streamlit http://localhost:8501
```

---

## 2. 本地模型

| 位置 | 模型 | 体积 | 用途 |
|------|------|------|------|
| `models/bge_small_zh`（软链到 Downloads 原项目） | BAAI/bge-small-zh-v1.5 | 92 MB | 中文嵌入，512 维 |
| `models/bge_reranker_base` | BAAI/bge-reranker-base | 1.1 GB | 交叉编码重排 |
| Ollama `qwen3.5:9b` | 通义 3.5 9B | ~6 GB | 本地 LLM（可替代云端） |
| Ollama `bge-m3` | BGE-M3 | 1.16 GB | 嵌入（1024 维，可替代 bge-small-zh） |
| Ollama `nomic-embed-text` | Nomic | ~270 MB | 嵌入（768 维） |

`models/` 是软链（`ln -s`），不复制 1.2 GB。彻底迁移时把原目录 `mv` 过来，重建同名软链或删掉软链即可。

**用本地 Ollama 跑 LLM**：服务已在运行（`:11434`），`.env` 里设

```ini
LLM_PROVIDER=ollama
OLLAMA_MODEL_NAME=qwen3.5:9b
```

24 GB 内存的 M5 跑 9B 没问题，但首 token 延迟明显（含改写 + 生成两次调用，一次问答约 5~15 秒）。
**建议：嵌入和重排固定走本地（快、免费），LLM 开发期走硅基流动（秒回、便宜），演示"纯离线"时再切 Ollama。**

---

## 3. 目录

```
rag-qa-system/
├── config/       settings.py（Pydantic Settings）、logging_config.py
├── core/         document_loader / embedding / vector_store / llm_client
│                 retriever / memory_manager / rag_chain / intent_recognizer
├── api/          main.py + routes（upload / qa / knowledge / system）
├── frontend/     Streamlit（教学阶段用，Vue 上线后废弃）
├── web/          Vue 3 + TS 前端（M3 阶段开始）
├── tests/        test_module1~5，对应视频 5 个模块
├── models/       软链 → 本地模型
├── docs/         缺陷修复清单等
└── Makefile
```

## 4. 端口分配（多项目共用机器时避免冲突）

| 服务 | 端口 |
|------|------|
| FastAPI | 8000 |
| Streamlit | 8501 |
| Vue dev server | 5173 |
| 后续 agent 项目 | 8100 / 5273 |

---

## 5. 跟视频的正确姿势

1. 视频一个模块写完 → 跑 `tests/test_module{N}_*.py` → 通过再进下一个。
2. 改造内容（缺陷修复之外的增强）单独开分支 `feat/xxx`，主干保持与视频一致，方便回看对照。
3. 每完成一个里程碑 commit 一次，message 写清「视频模块 N + 本地改造项」。
4. **不要边看边升级依赖**，依赖版本以本项目 `requirements.txt` 为准。
