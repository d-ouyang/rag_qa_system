# 2.0.0 迭代计划书

- **版本**：v2.0.0（**进行中**，当前 `2.0.0-p0.2`）
- **对应 tag**：`v2.0.0`（完成后打）
- **配套总结**：`docs/SUMMARY-v2.0.0.md`（完成后补）
- **上游基线**：`v1.0.0`（`docs/SUMMARY-v1.0.0.md`）
- **最后更新**：2026-09-23

## 进度总览

| 任务 | 状态 | 版本 | 迭代文档 |
|------|------|------|----------|
| P0-1 会话持久化：MemoryManager → Redis | ✅ 已完成 | `2.0.0-p0.1` | `iterations/v2.0.0-p0.1-redis-session-store.md` |
| P0-2 鉴权网关（NestJS） | ✅ 已完成 | `2.0.0-p0.2` | `iterations/v2.0.0-p0.2-auth-gateway.md` |
| P1-3 部署三件套 | ⬜ 未开始 | — | — |
| P1-4 模型目录处理 | ⬜ 未开始 | — | — |
| P1-5 TLS 证书 | ⬜ 未开始（卡 ICP 备案） | — | — |
| P2-6 配置与密钥治理 | ⬜ 未开始 | — | — |
| P2-7 前端生产构建 + 备案号 | ⬜ 未开始 | — | — |
| P2-8 观测与备份 | ⬜ 未开始 | — | — |

> 状态标记约定：⬜ 未开始 / 🔄 进行中 / ✅ 已完成。每完成一项就在此表更新状态，
> 并同步「版本」与「迭代文档」两列 —— 这是计划书与 `CHANGELOG.md` 的对账依据。

---

## 一、目标

把 v1.0.0 的「本地可联调系统」升级为「可公开访问的生产服务」。

一句话概括四大改造：**会话持久化 + 鉴权网关 + 容器化部署 + TLS**。

## 二、范围

- 属于本仓库 `rag-qa-system` 的生产化迭代。
- 不含其他 agent 项目（如「念安」小程序）的后端，那些后续各自立项。
- 本地 LLM（Ollama 9B）仍跑在 Mac，**不**上云服务器；云端只跑嵌入 / 重排小模型 + 业务服务，LLM 走硅基流动/DeepSeek 云端 API。

## 三、依赖与执行顺序

```
P0-1 Redis 会话持久化  ─┐
                       ├─→ P1-3 部署三件套 ─→ P1-4 模型目录处理 ─→ P1-5 TLS ─→ 备案放行后上线
P0-2 NestJS 鉴权网关   ─┘              ↑ 含 P0-2 网关 + P2-7 前端登录态
```

- P0-1 与 P0-2 可并行开发。
- P1 全部依赖 P0 完成。
- P1-5（TLS）卡在 ICP 备案通过，备案期间可用「IP:端口」全程自测。

---

## P0 — 上线硬前提（不做不能上线）

### P0-1 会话持久化：MemoryManager → Redis

> **状态：✅ 已完成（`2.0.0-p0.1`，2026-09-23）**
> 迭代文档：`iterations/v2.0.0-p0.1-redis-session-store.md`
>
> 落地与计划的差异（都是增强，非偏离）：
> - 抽出 `SessionStore` 抽象层，`MEMORY_BACKEND=memory|redis` 二选一；Memory 后端保留，本地不起 Redis 也能跑测试。
> - 除计划要求的「内存预警」外，补了**容量治理闭环**：`write_allowed()` 写入前主动拒绝 → OOM 立刻只读降级（丢记忆但不 502）→ 水位回落自动解除。
> - 新增 `GET /api/v1/system/memory` 暴露 `redis_memory.level` 供监控抓取。
> - 验收：module5（44 项，接口零改动迁移）与 module6（107 项，双后端同套语义断言）全绿。

- **现状**：`core/memory_manager.py` 是进程内 dict（`_sessions` / `_session_meta` / `_usage` / `_exchange_meta`），6h TTL 惰性回收，重启即丢。
- **改造点**：内部实现从内存 dict 换成 Redis 客户端；对外接口（`get_messages` / `add_exchange` / `update_session_meta` / `clear_session` / `list_sessions` / `truncate_session` / `*_usage` 等）**保持不变**，调用方零改动。
- **配置新增**：
  - `REDIS_URL`（如 `redis://redis:6379/0`）
  - `MEMORY_BACKEND=redis|memory`（dev 继续用内存，prod 走 Redis，避免本地必须起 Redis 才能跑测试）
- **数据结构**：每会话一个 key `rag:session:{id}`，用 hash 存 `messages` / `exchange_meta` / `session_meta` / `usage`；整体 TTL = 现有 `MEMORY_SESSION_TTL_SECONDS`（默认 6h），续聊即刷新 TTL，语义与现 TTL 对齐。
- **依赖**：`requirements` 增加 `redis>=5`；compose 起 `redis:7-alpine`（`--appendonly yes --maxmemory 128mb --maxmemory-policy noeviction`）。
- **验收**：后端重启后旧会话 / 置顶 / 标题 / 用量仍在；并发请求不串会话；TTL 过期行为与 v1.0.0 一致。
- **依赖**：无（可独立开工）。

### P0-2 鉴权网关（NestJS，独立服务）

> **状态：✅ 已完成（`2.0.0-p0.2`，2026-09-23）**
> 迭代文档：`iterations/v2.0.0-p0.2-auth-gateway.md`
>
> 落地说明：
> - 白名单放行 `login` + 网关自身 `/api/health` + 两个上游健康检查（`/api/v1/system/health`、`/api/v1/qa/health`，**精确路径**，非前缀通配）。
> - 代理注册为 Controller 通配路由 `@All('api/v1/*')` 而非中间件 —— 中间件跑在守卫之前会导致鉴权静默失效。
> - 用户体系用 `.env` 配 bcrypt 哈希（`npm run hash` 生成），未引入 SQLite；恒定时间比对（用户不存在时也跑 bcrypt）防用户名枚举。
> - 验收：冒烟 25 项全绿（含无限 token 401、伪造 token 401、流式被挡在 401、限流 429、白名单精确性反向断言）；浏览器端到端实测通过。

- **范围**：只做「无状态校验 + 转发」，不写 RAG 业务。
- **技术栈**：`@nestjs/core` + `@nestjs/jwt` + `passport-jwt` + `@nestjs/throttler` + `http-proxy-middleware` + `class-validator`。
- **路由设计**：
  - `POST /api/auth/login`：校验账号口令，签发 JWT。
  - 全局 `JwtAuthGuard`，`@Public()` 装饰器白名单放行 `login` 与两个健康检查（`/api/v1/system/health`、`/api/v1/qa/health`）。
  - 校验通过后 `http-proxy-middleware` 把 `/api/v1/*` 转发到 `backend:8000`。**直接 pipe 不缓冲**，保证 NDJSON 逐字效果。
- **用户体系（初期）**：单 / 少数用户即可；账号存 SQLite 或首条由 `.env` 初始管理员口令生成，不引入重型用户模块。
- **限流**：`@nestjs/throttler` 按用户 / IP 限制，防他人白嫖硅基流动额度。
- **前端联动（必做）**：当前前端无登录态，需加极简登录页 + token 存储（**这是不用 localStorage 的例外场景**：Pinia + localStorage 存 token，区别于会话数据），请求统一带 `Authorization: Bearer`。
- **验收**：无 token 调 `/api/v1/qa/*` 返回 401；login 后全链路通；超限被限流。
- **依赖**：无（可与 P0-1 并行）。

---

## P1 — 部署与运维

### P1-3 部署三件套

- `Dockerfile`（后端）：`python:3.11-slim`，装锁版依赖（`requirements.lock.txt`），`uvicorn api.main:app --host 0.0.0.0 --port 8000`。
- `frontend/Dockerfile`（multi-stage）：`node:20-alpine` 构建 `dist/` → `nginx:alpine` 托管。
- `frontend/nginx.conf`：托管 `dist/` + 反代 `/api` 到网关 `:3000` + 流式 `proxy_buffering off` + gzip + `X-Forwarded-*` 头。
- `docker-compose.yml`：服务 `frontend`(nginx 入口) / `gateway`(NestJS) / `backend`(FastAPI) / `redis`；卷 `vector_db` `upload` `redis_data`；`env_file: .env`。
- `.dockerignore`：排除 `.venv` `node_modules` `vector_db` `upload` `models` `.env` `.git` `*.log`。
- **验收**：服务器 `docker compose up -d` 后，IP:端口 自测全通。

### P1-4 模型目录处理

- 当前 `models/` 是软链到 `~/Downloads/rag_qa_system/models`，Docker 内失效。
- 二选一：① 服务器放真实目录，compose `bind` 挂载到 `/app/models`；② COPY 进镜像（镜像变大但部署简单）。推荐 ①（镜像小、模型可独立更新）。

### P1-5 TLS 证书

- 备案通过后：Cloudflare 免费代理（最省心，免证书管理）或 `certbot` 申请 Let's Encrypt 挂给 nginx，`80 → 443` 跳转，自动续期。
- **验收**：`https://nianan.site` 可访问，浏览器锁标，证书有效。

---

## P2 — 生产化打磨

### P2-6 配置与密钥治理

- `ALLOWED_ORIGINS` 加 `https://nianan.site`（删掉无关 localhost 或仅留测试）。
- 生产 `.env` 模板（`.env.example` 已近似）；密钥只走 `env_file`，绝不进镜像层 / 不提交 git。

### P2-7 前端生产构建 + 备案号

- 校验 `npm run build` 产物；`VITE_API_BASE` 留空走同源（已支持，零改动）。
- 页脚加备案号组件，链接 `beian.miit.gov.cn`（管局要求）。

### P2-8 观测与备份

- 复用已有健康检查 `/api/v1/system/health` + `/api/v1/qa/health` 接探活 / 监控。
- 卷备份：定期备份 `vector_db` / `upload` / `redis_data`（可 rsync 到 COS 按量，不买预付资源包）。

---

## P3 — 可选增强（2.0.x 再说）

- **P3-9 多知识库隔离**：`vector_store` 已预留 `persist_dir` 多库能力，前端加知识库切换。
- **P3-10 会话管理增强**：分页 / 搜索 / 导出。

---

## 四、验收闸门（2.0.0 完成定义）

1. 后端重启后，旧会话 / 置顶 / 用量不丢（P0-1）。
2. 无 token 不能问答（P0-2）。
3. 四容器 `docker compose up -d` 一键起（P1-3）。
4. `https://nianan.site` 可访问、流式正常、页脚有备案号（P1-5 + P2-7）。
5. 打 tag `v2.0.0` + 写 `docs/SUMMARY-v2.0.0.md`。

## 五、风险与注意

- **备案时序**：P1-5 依赖 ICP 备案通过；备案 1~2 周，期间走 IP:端口 自测，勿对外解析域名。
- **内存**：服务器 4C4G，RAG 栈（嵌入 + 重排）常驻约 1.5~2G，留给其他服务约 2G；Redis 限 128MB。
- **密钥**：`.env` 含硅基流动 / OpenAI key，全程不入库、不进镜像。
- **本地 LLM 不上云**：云端 LLM 走 API，避免无 GPU 机器推理过慢。

## 六、版本与文档约定

- 每个大版本：一份 `PLAN-vX.Y.Z.md`（迭代前）+ 一份 `SUMMARY-vX.Y.Z.md`（完成后）。
- 与同名 git tag 配对，按版本号检索。
