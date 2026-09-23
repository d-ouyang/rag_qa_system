# 项目阶段性总结 · v1.0.0（第一阶段）

- **版本号**：v1.0.0（对应 git tag `v1.0.0`）
- **完成日期**：2026-09-23
- **阶段定位**：教学视频 M1~M5 全部落地 + 类 WorkBuddy 双栏 Vue3 工作台前端打通，形成可本地联调的完整 RAG 问答系统。

## 一、本阶段交付内容

- **后端 FastAPI（`api/`）**：问答 `/api/v1/qa`、知识库文档管理 `/api/v1/documents`、系统配置 `/api/v1/system`，NDJSON 流式逐字回答。
- **核心链路（`core/`）**：`document_loader`（多格式解析切分）、`embedding`（bge-small-zh）、`vector_store`（Chroma/FAISS 双实现，默认 Chroma 落盘）、`llm_client`（ollama / siliconflow / openai 三 provider）、`retriever`、`rag_chain`、`intent_router`、`memory_manager`。
- **前端（`frontend/`，Vue3 + TS + Pinia + Vite）**：左侧边栏（项目信息 / 会话列表 / 入口）+ 右侧 ChatView / KnowledgeView / SettingsView；开发期 Vite 代理 `/api → 127.0.0.1:8000`。
- **配置（`config/settings.py`）**：Pydantic Settings 读 `.env`，含向量库 / 检索 / 重排 / 记忆 / 意图识别全量开关。
- **测试（`tests/test_module1~5_*.py`）**：对应视频五个模块，全绿。

## 二、关键架构事实（上线前必须知道）

1. **会话 / 置顶 / 用量都在内存**：`MemoryManager` 是纯进程内 dict（`_sessions` / `_session_meta` / `_usage`），6 小时 TTL 惰性回收，**重启即丢**。代码注释已预留换 Redis 的替换点（接口不变，只换内部实现）。
2. **真正落盘的只有两处**：Chroma 向量库（`vector_db/chroma.sqlite3` + 集合目录，存切片全文 / 页码 / source）和 `upload/`（原始上传文件）。`data/` 当前为空、未使用；`app.log` 仅日志。
3. **前端无浏览器存储**：Pinia 四 store（sessions / ui / documents / settings）全内存态，无 `localStorage` / `IndexedDB`；`API_BASE` 默认空 → 请求走同源相对路径 `/api`，生产 Nginx 同域托管 + 反代零改动。
4. **无应用数据库、无鉴权**：当前任何人可上传 / 问答，公开前必须补。

## 三、上线就绪度与阻塞项

- **服务器**：腾讯云轻量 4C4G（上海，公网 `42.192.111.82`），已续费 1 年。
- **域名**：`nianan.site` 已购、实名审核中、ICP 备案进行中（主体选安徽，个人备案）。
- **待办（即 2.0.0 迭代）**：
  1. `MemoryManager` → Redis 持久化
  2. NestJS 鉴权网关（JWT + 全局 Guard + `http-proxy-middleware` 转发）
  3. 部署三件套（`Dockerfile`×2 / `nginx.conf` / `docker-compose.yml` / `.dockerignore`）
  4. TLS（certbot / Cloudflare）
  5. 备案通过后放开 80 / 443
- 详细迭代计划见 `docs/实施计划-RAG-Agent项目.md` 与 2.0.0 TODO。

## 四、如何查看历史版本总结

每个大版本对应一份 `docs/SUMMARY-vX.Y.Z.md`，与同名 git tag 配对，按版本号检索即可。
