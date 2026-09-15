# RAG 智能问答系统 · 从 0 到 1 实施计划

> 基线项目：`/Users/ouyangding/Downloads/rag_qa_system`（企业级私有化 RAG 智能问答系统，2250 行 Python）
> 目标空间：`/Users/ouyangding/Desktop/AIAgent`（后续多个 agent 项目共用）
> 制定日期：2026-09-10

---

## 0. 结论先行

| # | 结论 |
|---|------|
| 1 | 源码能跑通主干，但有 **7 处必修缺陷**，其中 `core/vector_store.py:114` 的笔误是**致命级**（一调用就抛异常）。建议**先建"修复基线"再跟视频**，否则视频进度到模块 3 会卡死。 |
| 2 | 前端改造**推荐 Vue 3 + TypeScript**（复用你锅圈之家 / 念安的现有栈，改造成本最低、见效最快）；若目标是 AI 产品岗面试作品，**第二个项目改用 React + Next.js**，形成双栈对比作品集。 |
| 3 | 共用 conda **可行，但只能共用"Python 版本 + 重型二进制 + 模型缓存"**，依赖必须按项目隔离。绝不能把 langchain 0.3.x（本项目）和 langchain 1.x（你 `langchain` env）装进同一个环境。 |

---

## 1. 现状盘点

### 1.1 项目结构

```
rag_qa_system/
├── config/          配置层  settings.py（Pydantic Settings）+ logging_config.py
├── core/            核心层  8 个模块（见下表）
├── api/             接口层  FastAPI，4 组路由
├── frontend/        Streamlit 前端（1 首页 + 3 页面 + 2 组件）
├── models/          本地模型 bge_small_zh（嵌入）+ bge_reranker_base（重排）
├── vector_db/       Chroma 持久化（chroma.sqlite3 + HNSW 分片）
├── upload/          上传文件落盘
└── tests/           test_module1~5，对应视频 5 个教学模块
```

### 1.2 核心模块职责

| 模块 | 行数 | 职责 | 复用价值 |
|------|------|------|---------|
| `config/settings.py` | 83 | Pydantic Settings 统一配置，自动建目录 | 高 · 后续项目直接抄 |
| `core/document_loader.py` | 109 | 6 种格式 Loader 映射 + 递归切分（500/50，中文分隔符） | 高 |
| `core/embedding.py` | 62 | HuggingFace 嵌入模型**单例** | 高 |
| `core/vector_store.py` | 163 | Chroma/FAISS 双实现统一接口，全局单例 | 中（有 bug，见 1.4） |
| `core/retriever.py` | 161 | 向量检索 + CrossEncoder 重排，延迟加载模型 | 高 |
| `core/llm_client.py` | 74 | LLM 工厂（ollama / openai / siliconflow） | 高 |
| `core/memory_manager.py` | 153 | 多会话隔离 + BufferWindow(k=5) + TTL 清理 | 中（内存态，重启即失） |
| `core/rag_chain.py` | 158 | LCEL 链：历史感知改写 → 检索 → stuff → 生成 | 高 |
| `core/intent_recognizer.py` | 35 | 规则关键词意图识别，**识别后未参与路由** | 低（改造点） |

### 1.3 本机环境事实（已实测）

| 项 | 现状 |
|----|------|
| conda | `/Users/ouyangding/miniconda3`，conda 26.5.3 |
| 已有 env | `rag_qa_system`（Python **3.10.20**，187 个包，1.6G）、`langchain`（Python **3.11.16**，langchain 1.3.18，908M） |
| 关键版本 | langchain 0.3.7 / chromadb 0.5.17 / torch 2.2.2 / fastapi 0.115.0 / streamlit 1.39.0 / **pydantic 2.13.4** |
| 模型 | `models/bge_small_zh`、`models/bge_reranker_base` 均已本地化，无需联网下载 |
| 前端工具链 | Node v22.23.2、pnpm 11.21.0、npm 10.9.8 |
| 磁盘 | 剩余 398 GiB，充足 |

### 1.4 已发现缺陷（按优先级排序，M0 阶段全部修掉）

| # | 位置 | 问题 | 影响 | 修复方案 |
|---|------|------|------|---------|
| **B1** | `core/vector_store.py:114` | `k = k or settings.SEARCH_TOP_KNone`，`SEARCH_TOP_KNone` 是笔误 | **致命**：`similarity_search()` 一调用即抛 AttributeError | 改为 `settings.SEARCH_TOP_K` |
| **B2** | `core/vector_store.py:_delete_by_source_faiss` | `to_keep` 收集的是 doc_id，却传给 `FAISS.from_documents`（要求 Document 列表） | FAISS 模式下删除文档必崩 | 收集 `doc` 对象而非 `doc_id` |
| **B3** | `core/retriever.py:retrieve` | `next(score for doc_, score in ... if doc_ == doc)` 用 Document 内容相等匹配分数 | 相同内容片段分数串位；O(n²) | 用 `id(doc)` 建字典映射 |
| **B4** | `api/routes/system.py:/config` | provider=siliconflow 时回显 `OPENAI_MODEL_NAME` | 设置页显示模型名错误（显示 gpt-3.5-turbo） | 按 provider 三分支返回 |
| **B5** | `config/settings.py:ALLOWED_ORIGINS` | 硬编码只允许 `localhost:8501` | 换 Vue/React（5173/3000）直接 CORS 失败 | 改为 env 可配 + 加白开发端口 |
| **B6** | 项目根 | **无 `.gitignore`**，`.env` 明文含硅基流动 API Key | 一 `git init` 就泄露密钥 | 先建 `.gitignore`，并轮换该 Key |
| **B7** | `api/routes/upload.py` | 用 BackgroundTasks 异步处理，无任务状态查询接口 | 前端无法感知向量化是否完成，用户体验断层 | 新增 `GET /upload/tasks/{id}` 或 SSE 进度推送 |
| **B8** | 依赖 | 环境实际装的是 pydantic **2.13.4**，requirements 未锁定；chromadb 0.5.17 对高版本 pydantic 敏感 | 可能 `import chromadb` 失败 | M0 实测 `import chromadb`，必要时锁 `pydantic<2.10` |

---

## 2. 端到端技术架构

### 2.1 数据流转（对应对话中的流程图）

**① 离线索引链路（Ingest）**

| 步骤 | 实现 | 关键参数 |
|------|------|---------|
| 1 上传校验 | `api/routes/upload.py`，白名单校验 + uuid 前缀重命名 | 7 种扩展名 |
| 2 解析 | `DocumentLoader.LOADER_MAP`：PyMuPDF / Docx2txt / TextLoader / Unstructured | txt 强制 utf-8 |
| 3 切分 | `RecursiveCharacterTextSplitter` | chunk 500 / overlap 50 / 中文分隔符 |
| 4 元数据注入 | source、file_name、file_type、category、upload_time | 溯源与删除都靠它 |
| 5 嵌入 | `HuggingFaceEmbeddings(bge_small_zh)`，单例 | 512 维，normalize，batch 32 |
| 6 落库 | Chroma 持久化到 `vector_db/`（FAISS 可切换） | HNSW 索引 |

**② 在线问答链路（Query）**

| 步骤 | 实现 | 说明 |
|------|------|------|
| 1 意图识别 | `IntentRecognizer`（规则关键词） | 目前**只识别不路由**，是改造点 |
| 2 问题改写 | `create_history_aware_retriever` + 改写 Prompt | 解决"那个便宜？"这类指代 |
| 3 向量召回 | `similarity_search_with_score`，Top-5 | **受 B1 缺陷影响** |
| 4 交叉重排 | `CrossEncoderReranker`（bge_reranker_base），延迟加载 | 首次调用才加载模型 |
| 5 上下文组装 | `create_stuff_documents_chain` + qa_prompt | 溯源片段截断 200 字 |
| 6 生成 | ChatOpenAI 兼容接口（硅基流动 Qwen2.5-7B / Ollama） | temperature 0.1 |
| 7 记忆回写 | `MemoryManager` BufferWindow k=5 | 内存态，重启丢失 |

### 2.2 接口清单（现状）

| 方法 | 路径 | 说明 | 改造建议 |
|------|------|------|---------|
| GET | `/api/v1/system/health` | 健康检查 | 保留 |
| GET | `/api/v1/system/config` | 当前配置 | 修 B4 |
| POST | `/api/v1/upload/file` | 单文件上传 | 增加返回 task_id |
| POST | `/api/v1/upload/batch` | 批量上传 | 保留 |
| POST | `/api/v1/qa/ask` | 问答（同步阻塞） | **新增流式版本** `/qa/ask/stream`（SSE） |
| POST | `/api/v1/qa/clear_memory` | 清除记忆 | 保留 |
| GET | `/api/v1/qa/history` | 历史记录 | 增加分页 |
| GET | `/api/v1/knowledge/stats` | 库统计 | 保留 |
| POST | `/api/v1/knowledge/delete` | 按 source 删除 | 修 B2 |
| POST | `/api/v1/knowledge/clear` | 清空 | 增加二次确认 token |

**缺口**：无文件列表接口（前端做不出"已上传文档表格"）、无任务状态、无鉴权、无流式。

### 2.3 Agent 核心模块（从"链"到"Agent"的演进路径）

当前项目本质是 **Chain（固定管道）**，不是 Agent。演进分三步，可复用你 `/Users/ouyangding/Desktop/langChain` 里已有的 `agent_core.py`、`rag_tools.py`、`complex_agent.py`：

| 阶段 | 形态 | 能力 | 对应里程碑 |
|------|------|------|-----------|
| 现在 | LCEL 单链 | 检索 → 生成，固定路径 | M1 |
| 进阶 | Router Chain | 意图 → 分流到不同检索策略（规则库/FAQ/文档/闲聊） | M4 |
| 目标 | **LangGraph Agent** | 工具调用（知识检索 / 联网搜索 / 计算器 / 文档上传）+ 自检重检索（检索结果不达标自动改写重查）+ 引用校验 | M5 |

LangGraph 状态机建议节点：`intent_router → retrieve → grade → （不达标则 rewrite 回 retrieve）→ generate → cite_check → end`。
你 `面试/langgraph-七种模式学习笔记.md` 里的模式可以直接对号入座。

---

## 3. 前端改造方案（Streamlit → 现代前端）

### 3.1 Streamlit 的硬伤（为什么必须换）

| # | 问题 | 具体表现 |
|---|------|---------|
| 1 | 整页重跑 | 任何交互都重跑整个脚本，长对话性能塌方 |
| 2 | 无真实路由 | 靠文件名 `1_💬_问答.py` 分页面，无法做嵌套路由 / 路由守卫 |
| 3 | 无组件复用 | 只有函数级封装，无 props / 状态提升 / 生命周期 |
| 4 | 流式输出难 | 打字机效果要 hack，SSE 支持弱 |
| 5 | 样式受限 | 无法接入公司设计系统，改 CSS 靠 `unsafe_allow_html` |
| 6 | 服务端状态 | `session_state` 在服务端，多用户并发是隐患 |
| 7 | 前端能力浪费 | 你的 Vue3 / TS / 设计稿还原 / 动效能力全部用不上 |

### 3.2 目标技术栈（Vue 方案）

```
Vite + Vue 3 + TypeScript + Pinia + Vue Router + Element Plus
  + UnoCSS（或 Tailwind）+ Axios + markdown-it + highlight.js
  + EventSource（SSE 流式）+ openapi-typescript（类型自动生成）
  + Vitest + Playwright
```

### 3.3 Vue 3 + TS vs React 对比

| 维度 | Vue 3 + TS | React（+ Next.js） | 对你谁更优 |
|------|-----------|-------------------|-----------|
| 上手成本 | 低，模板语法近 HTML，响应式心智负担小 | 中，需要理解 hooks 心智模型、闭包陷阱 | **Vue** |
| 你的现有经验 | 锅圈之家（Vue3+Vant4+Pinia）、念安（UniApp+Vue3） | 需另起炉灶 | **Vue** |
| 类型推导 | Vue 3.4+ 已很好，`defineProps` 泛型完善 | TS 支持更成熟，泛型组件更自然 | React 略优 |
| AI Chat UI 生态 | Element Plus / Naive UI 够用，AI 组件需自研 | Vercel AI SDK、assistant-ui、shadcn/ui 生态更强 | **React** |
| SSE 流式 | 自己封装 `EventSource` + composable | `ai/react` 的 `useChat` 开箱即用 | **React** |
| SSR / SEO | Nuxt 生态弱于 Next | Next.js App Router 是事实标准 | **React** |
| 岗位需求面 | 国内中后台 / H5 大量 | AI 产品岗、外企、远程岗更偏 React | **React** |
| 构建体积 | 更小（runtime 约 30KB gzip） | 稍大 | Vue |
| 后台管理落地 | Element Plus 表格/上传/表单齐全，最快 | shadcn/ui 或 Ant Design | 平 |
| 迁移成本 | Streamlit 页面→Vue 组件映射直观 | 同 | 平 |

**我的建议（务实版）**：

1. **第一个项目（RAG 问答）用 Vue 3 + TS** —— 目标是把 Streamlit 的 3 个页面 + 2 个组件在最短时间内升级成可用的产品级前端，复用你已有肌肉记忆，风险最低。
2. **第二个 agent 项目用 React + Next.js** —— 补齐 AI 前端生态（`useChat`、Vercel AI SDK、流式开箱即用），同时形成**双栈对比作品**，面试时能讲清"同一后端两种前端实现"的取舍，这比只会一个栈有说服力得多。
3. 两者共用同一套 FastAPI 后端 + 同一份 OpenAPI 生成的 TS 类型，边际成本不高。

如果你只想选一个：**选 React**。理由是 AI 应用前端的复杂度集中在流式渲染、会话状态机、工具调用中间态这些地方，React 生态现成方案最多，能省掉大量自研；而 Vue 的收益（上手快）对你这个经验级别已经不太明显了。

### 3.4 前端目录结构（Vue 方案）

```
web/
├── src/
│   ├── api/              自动生成的 TS 类型 + 请求封装
│   ├── components/
│   │   ├── chat/         MessageList / MessageItem / SourceDrawer / ChatInput
│   │   ├── kb/           UploadDropzone / DocTable / TaskProgress
│   │   └── common/       EmptyState / Loading / ErrorBoundary
│   ├── composables/      useChat（SSE 流式）/ useUpload / useKnowledge
│   ├── stores/           chat.ts / knowledge.ts / system.ts
│   ├── router/
│   ├── views/            ChatView / KnowledgeView / SettingsView
│   └── styles/
├── index.html
├── vite.config.ts        dev proxy: /api → localhost:8000
└── package.json
```

### 3.5 后端需配合的改造（前端改造的前置条件）

| 改造项 | 内容 | 前端收益 |
|--------|------|---------|
| CORS | `ALLOWED_ORIGINS` 加 `http://localhost:5173` | 联调通 |
| 流式接口 | 新增 `POST /qa/ask/stream`（SSE，逐 token 推送，末尾推 sources） | 打字机效果 |
| 文档列表 | 新增 `GET /knowledge/documents`（按 file_name 聚合，返回片段数、上传时间、分类） | 知识库管理页能出表格 |
| 任务状态 | 新增 `GET /upload/tasks/{id}`（pending/processing/done/failed + 进度） | 上传进度条 |
| 统一响应 | `{code, message, data}` 封装 + 全局异常处理器 | 前端错误处理统一 |
| OpenAPI | 用 `openapi-typescript` 从 `/openapi.json` 生成 `types.ts` | 类型零手写、后端改动前端立刻报错 |

### 3.6 前端改造里程碑

| 阶段 | 内容 | 交付物 |
|------|------|--------|
| P0 | Vite 脚手架 + TS + Pinia + Router + Element Plus + 代理配置 | 空壳跑起来，能调通 `/system/health` |
| P1 | 问答页：会话列表、消息流、来源抽屉、清空记忆 | 替代 `1_💬_问答.py` |
| P2 | 流式改造：SSE + 打字机 + 中断生成 | 体验跃迁 |
| P3 | 知识库页：拖拽上传、进度条、文档表格、删除、清空 | 替代 `2_📁_知识库管理.py` |
| P4 | 设置页 + 暗色主题 + 响应式 + 错误边界 | 替代 `3_⚙️_系统设置.py` |
| P5 | 构建产物由 FastAPI/Nginx 托管，Streamlit 正式下线 | 单端口部署 |

---

## 4. 多 agent 项目共用 conda 环境的方案

### 4.1 可行性结论

**可行，但"共用"必须拆成三件事分别看待**：

| 层级 | 能否共用 | 做法 |
|------|---------|------|
| conda 底座（conda 自身、Python 解释器版本） | ✅ 应该共用 | 一个 miniconda3 管所有 Python 版本 |
| 重型二进制（torch / faiss / onnxruntime） | ✅ 建议共用 | 由 conda 提供，避免每个项目重复几百 MB |
| **Python 依赖（langchain / fastapi / chromadb）** | ❌ **绝不能共用** | 每项目独立环境 |
| 模型权重（bge / reranker / LLM） | ✅ 必须共用 | 统一缓存目录，软链或环境变量指向 |
| 向量库数据 | ⚠️ 按项目隔离 | 每项目独立 `vector_db/` |

### 4.2 推荐的三层策略

```
L0  conda 底座          ~/miniconda3            —— 只装 conda + Python 3.10/3.11 + 重型二进制
L1  每项目隔离环境      项目内 .venv（uv 创建）   —— 纯 Python 依赖，秒级重建，可删可弃
L2  共享资源            ~/ai-shared/            —— HF 模型缓存、torch 缓存、公共数据集
```

**为什么推荐"conda 管 Python + uv 管依赖"而不是"每项目一个 conda env"**：

| 方案 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| A. 所有项目塞进一个 conda env | 省事、省磁盘 | **版本地狱**：langchain 0.3.x 与 1.x 无法共存，一个项目升级全崩 | ❌ 不推荐 |
| B. 每项目一个 conda env | 隔离彻底、IDE 友好 | 每 env 1~2GB，创建慢（conda 解依赖分钟级） | 项目数 ≤ 3 时可用 |
| C. **conda 管 Python + 项目内 venv（uv）** | 隔离彻底 + 创建秒级 + 磁盘友好 + lock 文件可复现 | 需多学一个工具（uv，10 分钟） | ✅ **推荐** |

### 4.3 硬性注意事项（踩坑清单）

1. **绝不混装 langchain 大版本**。`rag_qa_system` env 是 langchain **0.3.7**（`create_retrieval_chain` / LCEL 时代），`langchain` env 是 **1.3.18**（API 已重构，部分能力迁到 `langchain-classic`）。两者装进同一 env，项目直接 import 失败。这是你当前最大的一颗雷。
2. **不要污染 base 环境**。base 只放 conda 自身，任何项目依赖都不装进 base。
3. **conda 与 pip 的分工与顺序**。conda 装编译型库（torch、faiss、opencv），pip 装纯 Python 包；**先 conda 后 pip**，之后再也不要 `conda install`（会覆盖 pip 装的版本，破坏依赖树）。
4. **Python 版本统一踩在 3.10 或 3.11**。3.13 上 torch / chromadb / sentence-transformers 普遍缺 wheel，会退回源码编译，装到怀疑人生。你现有两个 env 正好是 3.10 和 3.11，保持即可，不要升。
5. **锁定 pydantic**。环境里已是 pydantic 2.13.4，而 chromadb 0.5.17 发布时对应的是 2.9 时代。M0 必须实测 `import chromadb`，若报错则 `pydantic<2.10` 写进 lock。
6. **锁文件是生命线**。每项目必须有 `requirements.lock.txt`（`pip freeze`）或 `uv.lock`，并在 README 写死"用 lock 装，不要用 requirements.txt 装"。
7. **共享模型缓存，别重复下载**。设 `HF_HOME=~/ai-shared/hf-cache`、`TORCH_HOME=~/ai-shared/torch`，或直接把 `models/` 做成共享目录，`settings.MODELS_DIR` 指向它。bge 模型一份约 400MB，三个项目各下一份纯属浪费。
8. **解释器指向要显式**。PyCharm / VSCode 每个项目手动指定 `项目/.venv/bin/python` 或 `~/miniconda3/envs/xxx/bin/python`，不要依赖"自动发现"，否则会出现"命令行能跑、IDE 报错"。
9. **密钥不进仓库、不共享**。每项目独立 `.env`，`.gitignore` 必含 `.env`；你 Downloads 这份 `.env` 里的硅基流动 Key 已经明文暴露，**建议直接轮换**。
10. **定期清理**。`conda clean -a` + 删除废弃 env，conda 的 pkgs 缓存很容易涨到 10GB+。

### 4.4 标准操作命令（复制即用）

```bash
# —— 方案 C：conda 提供 Python，uv 管依赖 ——

# 1. 建一个只带 Python 的轻环境（一次即可）
conda create -n agent-base python=3.11 -y

# 2. 项目内建 venv 并安装（秒级）
cd /Users/ouyangding/Desktop/AIAgent/rag-qa-system
~/miniconda3/envs/agent-base/bin/python -m venv .venv
source .venv/bin/activate
pip install uv
uv pip install -r requirements.txt
uv pip freeze > requirements.lock.txt   # 锁死版本

# 3. 共享模型缓存（写入 ~/.zshrc）
export HF_HOME=~/ai-shared/hf-cache
export TORCH_HOME=~/ai-shared/torch

# 4. 日常
conda activate agent-base && source .venv/bin/activate   # 或封装成 make dev
make api        # uvicorn api.main:app --reload --port 8000
make web        # pnpm --dir web dev
```

```bash
# —— 方案 B：每项目一个 conda env（项目数少时更简单）——
conda create -n rag-qa python=3.10 -y
conda activate rag-qa
conda install -c pytorch faiss-cpu -y      # 先 conda：编译型
pip install -r requirements.txt             # 后 pip：纯 Python
conda env export --no-builds > environment.yml
```

### 4.5 多项目目录约定

```
~/Desktop/AIAgent/
├── .workbuddy/memory/          项目记忆（已有）
├── _shared/                    共享资产：prompts/、eval 集、公共工具库
├── rag-qa-system/              项目 1：RAG 问答（本计划）
│   ├── .venv/                  uv venv（gitignore）
│   ├── web/                    Vue3 前端
│   ├── core/ api/ config/
│   └── Makefile
├── agent-project-2/            项目 2：React + Next.js 的 Agent
└── README.md                   总索引：每个项目的 env 名、端口、启动方式
```

**端口分配表（提前规划，避免冲突）**：

| 项目 | 后端 | 前端 |
|------|------|------|
| rag-qa-system | 8000 | 5173 |
| agent-project-2 | 8100 | 5273 |
| 后续项目 | 82xx | 53xx |

---

## 5. 总体里程碑（与视频模块对齐）

视频的 5 个教学模块可从 `tests/test_module1~5` 反推：模块 1 配置 → 模块 2 文档加载 → 模块 3 向量库 → 模块 4 LLM → 模块 5 RAG 链。

| 里程碑 | 内容 | 对应视频 | 验收 |
|--------|------|---------|------|
| **M0 环境与基线** | 建 `.gitignore`、轮换 Key、修 B1~B6、跑通 `import chromadb`、备份现有 `vector_db/` | 课前 | 5 个 test 全绿 |
| **M1 跟课实现** | 按视频逐模块自己写一遍（模块 1→5），每模块跑对应 test | 模块 1-5 | 每个模块独立可跑 |
| **M2 后端增强** | 流式 SSE、文档列表、任务状态、统一响应、CORS、OpenAPI | 课后加餐 | curl 验证 + `/docs` 完整 |
| **M3 前端改造** | P0~P5（见 3.6） | 并行 | 浏览器全流程走通 |
| **M4 Agent 化** | 意图路由、工具化、LangGraph 状态机、引用校验 | 进阶 | 多轮 + 工具调用可演示 |
| **M5 工程化部署** | Docker Compose、CI、日志与监控、压测 | 收尾 | 一条命令启动 |

### 建议的跟课节奏

- **每看完一个模块，先跑通 test，再做我的改造**，不要边看边改（改了就跟不上视频）。
- 改造部分单独开分支（`feat/vue-frontend`、`feat/agent`），主干保持与视频一致，方便回看对照。
- 每完成一个里程碑 `git commit` 一次，commit message 写清"视频模块 N + 本地改造项"。

---

## 6. 验收标准（curl + 浏览器双路，符合你的习惯）

**后端 curl 清单**

```bash
curl -s localhost:8000/api/v1/system/health
curl -s localhost:8000/api/v1/knowledge/stats
curl -s -F "file=@tests/sample_docs/test.docx" -F "category=test" localhost:8000/api/v1/upload/file
curl -s -X POST localhost:8000/api/v1/qa/ask -H "Content-Type: application/json" \
     -d '{"question":"测试文档讲了什么","session_id":"curl-test"}'
curl -N -X POST localhost:8000/api/v1/qa/ask/stream -H "Content-Type: application/json" \
     -d '{"question":"流式测试"}'          # M2 后应逐 token 返回
```

**前端浏览器清单**

1. 上传 docx → 进度条走完 → 文档表格出现该行（P3）
2. 提问 → 打字机逐字输出 → 可中断（P2）
3. 点击引用角标 → 抽屉展示文件名 + 页码 + 原文片段（P1）
4. 追问"那第二个呢？" → 模型能正确指代（历史感知生效，M1）
5. 切换会话 → 历史隔离；刷新页面 → 会话列表保留（P1）
6. 移动端视口 → 布局不塌（P4）

---

## 7. 需要你拍板的 3 个决策点

| # | 决策 | 选项 A | 选项 B | 我的倾向 |
|---|------|--------|--------|---------|
| 1 | 前端栈 | Vue 3 + TS（快、复用经验） | React + Next.js（生态强、面试牌面） | 项目一 Vue，项目二 React；只选一个则 **React** |
| 2 | 环境方案 | conda 管 Python + uv 管依赖（推荐） | 每项目一个 conda env（简单但重） | **A**，项目数超 3 个时优势明显 |
| 3 | 项目位置 | 把 Downloads 的项目复制/移动到 `AIAgent/rag-qa-system` 后开发 | 直接在 Downloads 原位开发 | **A**，统一到 AIAgent 便于多项目管理（1.2G 的模型目录建议移动后用软链共享） |

确认后我从 **M0 环境与基线** 开始执行。
