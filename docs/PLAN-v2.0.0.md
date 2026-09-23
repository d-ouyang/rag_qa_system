# 2.0.0 迭代计划书

- **版本**：v2.0.0（**进行中**，当前 `2.0.0-p0.2`）
- **计划书版本**：**第 2 版**（2026-09-23 架构调整，第 1 版见 git 历史）
- **对应 tag**：`v2.0.0`（全部完成后打）
- **配套总结**：`docs/SUMMARY-v2.0.0.md`（完成后补）
- **上游基线**：`v1.0.0`（`docs/SUMMARY-v1.0.0.md`）
- **架构参考**：企业 RAG 项目架构设计文档（数据分层 / 队列 / 容器持久化）
- **最后更新**：2026-09-23

---

## 0. 计划修订说明（第 1 版 → 第 2 版）

### 0.1 为什么改

第 1 版把「会话持久化」的落脚点定在 Redis。重新论证后确认这是**数据分层错位**，三条理由：

1. Redis 是内存数据库，实例重启、内存驱逐、容量保护都可能让数据消失。会话与问答记录是**永久业务资产**，不该交给一个随时可能丢数据的组件；
2. 会话在 Redis 后，SQL 能做的事全做不了：按用户查、按时间聚合、用量对账、质检抽样、后续 Agent 审计留痕；
3. 本项目自己实现了 Redis 三级水位预警 + fatal 只读降级 —— 这等于承认「它可能写不进去」。让永久数据依赖它，是自相矛盾。

调整后的分层一句话：**MySQL 存业务真相，Redis 只做队列与短期缓存，Chroma 存向量，原始文件落磁盘 volume，解析交给独立 Worker。**

### 0.2 变了什么

| 项 | 第 1 版 | 第 2 版 |
|---|---|---|
| 会话 / 问答记录 | Redis（HASH + ZSET 索引） | **MySQL** |
| 文档元数据 | 无（从 Chroma 反推） | **MySQL `document` 表** |
| 文档解析 | HTTP 同步做完再返回 | **Redis 队列 + 独立 Worker 异步** |
| 文档状态 | 无 | **MySQL `status` 状态机** |
| Redis 定位 | 会话真相源 + 内存监控 | **队列 broker + 内存水位监控**（缓存类场景挪到 P3，见 §9） |
| 原始文件 | `upload/` 目录 | 不变（容器化后改为 **volume 挂载**） |
| 部署形态 | 服务器上 `docker compose up` | **本地 Docker 全栈开发** + 同一套 compose 作服务器预演 |

### 0.3 没变什么（守住的东西）

- **`SessionStore` 抽象层保留** —— 当初拆这一层，就是为了今天这种「换后端」。**业务侧零改动仍是验收红线**（`core/memory_manager.py` 对外签名、`core/rag_chain.py`、`api/routes/qa.py` 不动）；
- **契约测试体系保留**：同一套断言参数化跑 `Memory` 与 `MySQL` 两个后端；
- **`core/redis_monitor.py` 保留**：Redis 现在只做队列，但队列 broker 一样会被写爆，水位预警与只读降级继续有效；
- P0-2 鉴权网关（已完成）、模型目录、TLS、配置治理、生产构建、观测备份的范围都没变，只是编号顺延。

### 0.4 编号对照（旧 tag 仍可检索）

| 旧编号 | 新编号 | 说明 |
|---|---|---|
| P0-1 会话持久化（→ Redis） | **P0-1 业务数据持久化（→ MySQL）** | 目标存储变更，**返工**；已交付部分并入原迭代文档 §7 |
| P0-2 鉴权网关 | P0-2 | 不变（已完成） |
| — | **P0-3 异步解析链路（队列 + Worker）** | **新增** |
| — | **P0-4 向量元数据对齐 + 知识库重建** | **新增** |
| P1-3 部署三件套 | P1-5 Docker 化（本地全栈 + 服务器预演） | 范围扩大：既是开发环境也是部署预演 |
| P1-4 模型目录处理 | P1-6 | 顺延 |
| P1-5 TLS 证书 | P1-7 | 顺延 |
| P2-6 配置与密钥治理 | P2-8 | 顺延 |
| P2-7 前端生产构建 + 备案号 | P2-9 | 顺延 |
| P2-8 观测与备份 | P2-10 | 顺延 |

已打 tag `v2.0.0-p0.1` / `v2.0.0-p0.2` **保持不动**：改名或重打会让「看到 tag 就知道那个 commit 跑的什么版本」这条约束失效。

### 0.5 版本号怎么走

返工不新开子版本号（见 `docs/CHANGELOG.md` 约定）。所以：

- P0-1 的返工**并入** `docs/iterations/v2.0.0-p0.1-redis-session-store.md` 的 §7，不新开 `p0.1b`；
- `.env` 的 `PROJECT_VERSION` **不回退**，保持 `2.0.0-p0.2`；P0-3 完成时直接升 `2.0.0-p0.3`；
- 阶段 tag `v2.0.0-p0.4` 打在新 P0 四项全绿时 —— 届时「P0-1 完成」的含义是 **MySQL 版跑通**，不再是 Redis 版。

---

## 1. 进度总览

| 任务 | 状态 | 版本 | 迭代文档 |
|------|------|------|----------|
| P0-1 业务数据持久化（MySQL 真相源） | 🔄 **返工中**（Redis 版已作废，见 §6） | `2.0.0-p0.1` | `iterations/v2.0.0-p0.1-redis-session-store.md` |
| P0-2 鉴权网关（NestJS） | ✅ 已完成 | `2.0.0-p0.2` | `iterations/v2.0.0-p0.2-auth-gateway.md` |
| P0-3 异步解析链路（队列 + Worker） | ⬜ 未开始 | — | — |
| P0-4 向量元数据对齐 + 知识库重建 | ⬜ 未开始 | — | — |
| P1-5 Docker 化（本地全栈 + 服务器预演） | 🔄 **5a 中间件已完成**，5b 待 P0 后 | `2.0.0-p1.5a` | `iterations/v2.0.0-p1.5a-middleware-compose.md` |
| P1-6 模型目录处理 | ⬜ 未开始 | — | — |
| P1-7 TLS 证书 | ⬜ 未开始（卡 ICP 备案） | — | — |
| P2-8 配置与密钥治理 | ⬜ 未开始 | — | — |
| P2-9 前端生产构建 + 备案号 | ⬜ 未开始 | — | — |
| P2-10 观测与备份 | ⬜ 未开始 | — | — |

> 状态标记：⬜ 未开始 / 🔄 进行中 / ✅ 已完成。每完成一项就在此表更新状态，
> 并同步「版本」与「迭代文档」两列 —— 这是计划书与 `CHANGELOG.md` 的对账依据。
>
> 大版本合计：**P0-2 完成；P1-5a 完成；P0-1 待返工；其余未开始。**
> P0 阶段 tag `v2.0.0-p0.4` 要等 P0-1 返工 + P0-3 + P0-4 全部做完。
>
> **编号顺序 ≠ 执行顺序**（这是本版计划书的新情况，别被绕进去）：
> P1-5a 是 P0-1 的前置，所以它在 P0-1 之前完成 —— 于是会出现「`p1.5a` 早于 `p0.1b`」的版本号顺序。
> 判读新旧请看 `git log`，不要假设版本号越大越新。

---

## 2. 目标

把 v1.0.0 的「本地可联调系统」升级为「可公开访问的生产服务」，并且**数据分层正确**。

四条主线：**数据落对地方 + 解析异步化 + 容器化部署 + TLS**。

## 3. 范围

- 属于本仓库 `rag-qa-system` 的生产化迭代；
- 不含其他 agent 项目（如「念安」小程序）的后端，那些后续各自立项；
- 本地 LLM（Ollama 9B）仍跑在 Mac，**不**上云服务器；云端只跑嵌入 / 重排小模型 + 业务服务，LLM 走硅基流动 / DeepSeek 云端 API；
- **本阶段开发形态是本地 Docker**（用户已租 4C4G 云服务器 1 年，用于后续线上部署，同一套 compose 先在本地验证）。

---

## 4. 数据分层（本项目最终口径）

> 这张表是后续所有实现的裁决依据。任何「这个数据放哪」的分歧，以本表为准。

| 数据 | 归属 | 生命周期 | 严禁 |
|---|---|---|---|
| 用户 / 文件夹 / 会话 / 消息 / 文档元数据 | **MySQL** | 永久业务资产 | 不放 Redis（会丢）、不放 Chroma（不是 KV） |
| 解析任务（队列消息） | **Redis 队列**（Celery broker，db1） | 消费即删 | **不当可靠任务源**；任务真相是 MySQL `document.status` |
| Redis 内存水位 / 队列积压深度 | Redis（监控读） | 实时 | — |
| chunk 切片正文 + 向量 + metadata(`doc_id`) | **Chroma** | 随文档删除 | 不放原始文件；**不拿它统计业务文档数**（它数的是切片数） |
| 原始 PDF / MD / HTML / TXT | **磁盘 `upload/`**（容器化后 volume） | 随文档删除 | 不进 MySQL 二进制列（只存 `storage_path` 字符串）、不进容器内部 |

**本期不启用的 Redis 场景**（架构文档里有，我们暂不用，理由写清避免后人疑惑）：

| 场景 | 不启用理由 |
|---|---|
| 登录 token 白名单 | 本项目 token 是无状态 JWT（网关签发），天然不需要服务端存储；代价是无法即时吊销，只能等过期（见 §11 D4） |
| 问答字符串缓存（`rag:qa:{project}:hash(q)`） | 收益不确定、且知识库更新必须清对应缓存，否则答旧内容；放 P3 再评估 |
| SSE 流式碎片断点补发 | 只能补「已生成未推送」的碎片，不能恢复模型计算；当前流式未出现明显断点痛点；放 P3 |
| 语义缓存（Chroma `qa_cache`） | 副作用最大：知识库变更不清缓存 = 旧答案幻觉；**默认不做** |

---

## 5. 依赖与执行顺序

```
        ┌─────────────────────────────────────────────┐
        │ P1-5a 中间件 compose（mysql + redis）        │  ← 地基，必须先做
        └───────────────────┬─────────────────────────┘
                            │
                            ▼
        ┌─────────────────────────────────────────────┐
        │ P0-1 业务数据落 MySQL（5 张表 + Store 实现）│
        └───────────────────┬─────────────────────────┘
                            │
                            ▼
        ┌─────────────────────────────────────────────┐
        │ P0-3 异步解析链路（上传即返回 + Celery Worker）│
        └───────────────────┬─────────────────────────┘
                            │
                            ▼
        ┌─────────────────────────────────────────────┐
        │ P0-4 向量元数据对齐 + 知识库重建             │
        └───────────────────┬─────────────────────────┘
                            │  P0 全绿 → tag v2.0.0-p0.4
                            ▼
   P0-2 鉴权网关 ✅ ──→ P1-5b 应用容器化（全栈 compose）
                            │
                            ▼
                    P1-6 模型目录 ──→ P1-7 TLS（等备案放行）
                            │
                            ▼
                  P2-8 / P2-9 / P2-10 生产化打磨
                            │
                            ▼
                  tag v2.0.0 + SUMMARY-v2.0.0.md
```

**关键约束**：

- P0-1 → P0-3 → P0-4 是**串行链**（P0-3 用 P0-1 建的 `document` 表；P0-4 要有 P0-3 的 `doc_id` 才能重建向量库）；
- P1-5a（只起 mysql + redis 两个中间件）必须提到 P0-1 之前 —— 本地没装这两个，不先起容器后面没法开发；
- P1-5b（应用容器化）放在 P0 之后：P0 期间应用要频繁改动，裸跑热重载效率高得多。

---

## 6. P0 — 上线硬前提（不做不能上线）

### P0-1 业务数据持久化：Redis → **MySQL** 🔄 返工中

> **状态：🔄 返工中**。第一轮交付（2026-09-23，`2.0.0-p0.1`）把会话落到了 Redis，**方案已作废**。
> 已交付并**保留**的资产：`SessionStore` 抽象层、契约测试体系、`RedisMemoryMonitor`。
> 已交付**退役**的部分：`RedisSessionStore` 作为会话真相源。
> 变更过程记在 `iterations/v2.0.0-p0.1-redis-session-store.md` §7。

**表结构（Alembic 迁移，一次到位，避免后续加字段再迁移）**：

| 表 | 关键字段 | 本期是否启用 |
|---|---|---|
| `user` | id / username / display_name / password_hash / is_active / create_time | 建表，**暂不使用**（网关账号仍在 `.env`，见 §11 D2） |
| `folder` | id / user_id / name / create_time | 建表，**暂不做 UI** |
| `session` | id / user_id / project_id / folder_id / title / is_pinned / usage_input_token / usage_output_token / last_active_at / is_archived / create_time | ✅ |
| `chat_message` | id / session_id / role / content / usage_input_token / usage_output_token / is_cache_hit / ref_ids(JSON) / create_time | ✅ |
| `document` | id / project_id / file_name / storage_path / file_size / status / chunk_count / fail_reason / attempt_count / upload_time | 建表，**P0-3 启用** |

**改造点**：

- **新增 `MySQLSessionStore`**，实现现有 `SessionStore` 接口（`load` / `save` / `delete` / `exists` / `list_ids` / `purge_expired` / `stats` / `health` / `session_lock` / `write_allowed`）。`save()` 内部把整条快照 **diff 成行级写入**：新增消息 INSERT、截断 DELETE 尾部、编辑重发 UPDATE 并删后续。**接口不变 ⇒ `memory_manager` 一行不改。**
- **会话级锁换实现**：`SELECT ... FOR UPDATE` 包在事务里（不再依赖 Redis Lua 锁）。多 worker 下同样串行，且锁与被保护的数据在同一个事务边界内 —— 比跨进程分布式锁更简单也更可靠。
- **usage 双写**：`session` 表存汇总列（写入时同事务增量更新），`chat_message` 存每轮明细。汇总列是缓存性质、明细是真相，两者可对账。
- **`ref_ids`**：只存 `chunk_id` 列表（JSON 列），**不存切片快照**（理由与代价见 §11 D3）。
- **TTL 语义**：`load` 时 `last_active_at` 超期即视为过期（对外表现等同 v1.0.0 的开新会话），**但不物理删行** —— 永久业务资产不该被 TTL 抹掉。`purge_expired()` 只做归档标记（`is_archived=1`），物理删除留到 P2-10 明确保留策略。
- **`RedisSessionStore` 退役**：从生产路径下线，代码标注 deprecated 暂留（module7 的 Redis 语义验证仍依赖它）；MySQL 版稳定后删除。
- **`MEMORY_BACKEND` 语义变更**：`memory`（本地 / 测试）| `mysql`（生产真相源）。
- **技术选型**：SQLAlchemy 2.0（同步）+ Alembic 迁移 + PyMySQL 驱动。选同步而非 async：`MemoryManager` 本身就是同步的，为了 ORM 去把整条会话链路改成 async 是纯粹的复杂度倒挂。

**验收**：

1. `core/memory_manager.py` 对外签名、`core/rag_chain.py`、`api/routes/qa.py` 一行未改（`git diff --stat` 可证）；
2. 后端重启后：会话列表、历史消息、置顶、标题、用量、每轮 `ref_ids` 全在，且都能 SQL 查到；
3. module6 契约测试以 `[memory, mysql]` 参数化跑**同一套断言**，全绿；
4. 并发 `add_exchange` 到同一会话不丢轮次（`FOR UPDATE` 生效）；
5. 会话闲置超 TTL 后 `load` 返回 None，行仍在库里（`is_archived=1`）。

### P0-2 鉴权网关（NestJS，独立服务） ✅ 已完成

> **已完成（`2.0.0-p0.2`，2026-09-23）**，迭代文档 `iterations/v2.0.0-p0.2-auth-gateway.md`。
>
> 白名单放行 `login` + 网关自身 `/api/health` + 两个上游健康检查（**精确路径**，非前缀通配）；
> 代理注册为 Controller 通配路由 `@All('api/v1/*')` 而非中间件（中间件跑在守卫之前会导致鉴权静默失效）；
> 用户体系用 `.env` 配 bcrypt 哈希，恒定时间比对防用户名枚举；限流防他人白嫖 LLM 额度。
> 冒烟 25 项全绿，浏览器端到端实测通过。

**与架构文档的差异**：架构文档有 `user` 表 + 白名单。本期网关账号仍走 `.env`，`user` 表建好备用。理由见 §11 D2。

### P0-3 异步解析链路：上传即返回 + Worker 消费

- **现状问题**：`api/routes/documents.py` 的上传是**同步**的 —— HTTP 请求里直接 `load_file` → `add_documents`（解析 + 切分 + 嵌入 + 入库）。大文件会把请求挂住几十秒，且无法批量上传。
- **改造后的链路**：
  1. 浏览器提交文件二进制；
  2. 后端生成 **uuid 文件名**落盘（不用原始文件名，防重名覆盖）；
  3. MySQL `document` 写入记录，`status=pending`；
  4. **每个 `doc_id` 推一条独立任务进 Redis 队列，HTTP 立即返回**（不阻塞）；
  5. Worker 消费：`status=parsing` → 读盘 → 解析 → 切分 → 写 Chroma → `status=success` + `chunk_count`；异常则 `status=fail` + `fail_reason`；
  6. 前端**轮询** `GET /api/v1/documents` 读 `status` 显示进度。**网页关闭不影响后台任务**，下次打开从 MySQL 读状态。
- **队列技术选型**：Celery + Redis broker。
  - broker 用 **db1**（`redis://redis:6379/1`），缓存类将来用 db0，逻辑隔离；
  - **不配 result backend** —— 状态真相是 MySQL `document.status`，再存一份 Redis 只会带来一致性麻烦和白耗内存；
  - `--concurrency=1`（解析是 CPU 密集 + 模型内存占用大，并发只会互相抢 CPU）；
  - `--max-tasks-per-child=20`：解析会反复吃内存，定期回收子进程防泄漏；
  - `task_time_limit` 设上限，防坏文件把 worker 卡死。
- **幂等（多 worker 抢同一任务）**：用 **MySQL 原子 UPDATE 抢任务**，不用 Redis 锁 ——
  `UPDATE document SET status='parsing', attempt_count=attempt_count+1 WHERE id=? AND status IN ('pending','fail')`，
  影响行数为 0 说明已被别人拿走，直接返回。抢到后先 `delete_by_doc_id` 再写 Chroma，重复消费也不会重复插切片。
- **失败兜底**：Redis 重启会丢未消费任务 —— 靠「MySQL `status` 为真相 + 手动重解析接口」兜底，不指望 Redis 持久化。

**接口变化**：

| 接口 | 变化 |
|---|---|
| `POST /api/v1/documents/upload` | 改为**立即返回** `{doc_id, status:"pending"}`，不再返回 `chunks_added` |
| `GET /api/v1/documents/` | 改为**读 MySQL**（含 `status` / `chunk_count` / `fail_reason`），支持 `?status=&project_id=` |
| `GET /api/v1/documents/{doc_id}/chunks` | 改为按 `doc_id` 查 Chroma（不再按磁盘路径） |
| `POST /api/v1/documents/{doc_id}/reparse` | **新增**：手动重新触发解析 |
| `GET /api/v1/documents/download?doc_id=` | **新增**：读盘返回二进制（不允许直接暴露磁盘路径） |
| `DELETE /api/v1/documents/{doc_id}` | 改为执行**三件事**（磁盘 + Chroma + MySQL） |
| `GET /api/v1/system/queue` | **新增**：队列积压深度（`LLEN`）+ worker 心跳 |

**前端联动**：知识库管理页的上传改为「提交 → 列表轮询 `status`」，删除/重解析按钮按状态置灰。

**验收**：

1. 上传 50MB 文件，HTTP 响应立即返回（不含解析耗时）；
2. 提交后关闭浏览器，任务照常跑完，重开页面状态正确；
3. 手动 `kill` Worker 后重启，`pending` 任务继续被消费；
4. 人为让同一 `doc_id` 入队两次 → Chroma 不出现重复切片；
5. 上传损坏文件 → `status=fail` + `fail_reason` 可读，前端能重试。

### P0-4 向量元数据对齐 + 知识库重建

- **现状**：Chroma metadata 只有 `source`（磁盘路径），没有 `doc_id`，所以「引用反查」和「按文档删除」都只能靠路径字符串匹配，脆弱且和 MySQL 对不上。
- **改造**：metadata 扩为 `{doc_id, project_id, chunk_index, source, page}`。
- **引用反查链路**（对齐架构文档）：前端点引用 → 拿 `chunk_id` → `GET /api/v1/chunks/{chunk_id}` → Chroma 取切片正文 → `metadata.doc_id` → MySQL 取文件名 / 上传时间 / 归属项目。
- **删除三件事**（缺一即脏数据）：磁盘原文件 → Chroma 全部该 `doc_id` 的切片 → MySQL 记录。
- **知识库重建脚本** `scripts/reindex.py`：扫描 `upload/` 逐个重灌，**复用 P0-3 的同一段解析代码**（不允许出现第二套解析逻辑）。现有向量数据因 metadata 缺 `doc_id` 必须重建一次，这是本次调整的一次性成本。
- **明确不做**：chunk 快照。即「文档删除后，历史回答的引用正文将查不到」，前端降级展示「引用内容已随文档删除」。代价见 §11 D3。

**验收**：

1. 删除文档后，磁盘 / Chroma / MySQL 三处均无残留；
2. 历史回答点击引用 → 能通过 `chunk_id` 拿到切片正文与源文档信息；
3. 重建后同一问题检索到的内容与重建前一致（同问题同答案抽检）；
4. Chroma 里不存在缺 `doc_id` 的切片。

---

## 7. P1 — 容器化与部署

### P1-5 Docker 化（本地全栈 + 服务器预演）

> 拆成两阶段：**5a（仅中间件）必须先于 P0-1**，5b（全栈）放在 P0 之后。

- **5a 中间件 compose**：`mysql:8` + `redis:7-alpine`，`volume` 落 `mysql_data` / `redis_data`；
  Redis 启动参数 `--appendonly yes --maxmemory 128mb --maxmemory-policy noeviction`（队列可以做 AOF 备份，但**不依赖它保任务**）。
- **5b 应用容器化**：
  - `Dockerfile`（后端）：`python:3.11-slim` + 锁版依赖（`requirements.lock.txt`），`uvicorn api.main:app --host 0.0.0.0 --port 8000`；
  - **Worker 复用后端镜像**（同一 Dockerfile，只是 command 换成 `celery -A worker.app worker ...`）—— 省一个镜像层，且解析依赖天然与后端一致；
  - `frontend/Dockerfile`（multi-stage）：`node:20-alpine` 构建 `dist/` → `nginx:alpine` 托管；`nginx.conf` 反代 `/api` 到网关 + **`proxy_buffering off`**（否则 NDJSON 流式会被缓冲）+ gzip + `X-Forwarded-*`；
  - `docker-compose.yml`：`frontend` / `gateway` / `backend` / `worker` / `mysql` / `redis`，`env_file: .env`；
  - `.dockerignore`：排除 `.venv` `node_modules` `vector_db` `upload` `models` `.env` `.git` `*.log`。
- **volume 规划**（容器重建不丢资产）：

| 服务 | 挂载 | 内容 |
|---|---|---|
| mysql | `mysql_data` | 业务元数据（真相源） |
| redis | `redis_data` | 队列 AOF（非强持久） |
| backend + worker | `vector_db` | Chroma 向量（**两者共享**） |
| backend + worker | `upload` | 原始文件（**两者必须看到同一套文件**） |

**验收**：本地 `docker compose up -d` 一键起全栈，IP:端口 全链路自测通过（含流式）。

### P1-6 模型目录处理

- 当前 `models/` 软链到 `~/Downloads/rag_qa_system/models`，Docker 内失效。
- 方案：**服务器放真实目录，compose bind 挂载到 `/app/models`**（镜像小、模型可独立更新）。备选 COPY 进镜像（镜像大但部署简单），否掉理由见该迭代文档。
- **关键优化**：**Worker 只加载嵌入模型，不加载 reranker**（解析链路只需嵌入，rerank 只在问答链路用）。这一条直接省下约 1.1GB 常驻内存，是 4C4G 能不能跑得动的关键。

### P1-7 TLS 证书

- 备案通过后：Cloudflare 免费代理（省心，免证书管理）或 `certbot` 申请 Let's Encrypt 挂 nginx，`80 → 443` 跳转 + 自动续期。
- **验收**：`https://nianan.site` 可访问，浏览器锁标，证书有效。

---

## 8. P2 — 生产化打磨

### P2-8 配置与密钥治理

- `ALLOWED_ORIGINS` 加 `https://nianan.site`（清掉无关 localhost）；
- MySQL 口令、`SECRET_KEY`、硅基流动 key 只走 `env_file`，**绝不进镜像层 / 不进 git**；
- 生产 `.env` 模板整理（`.env.example` 已近似）。

### P2-9 前端生产构建 + 备案号

- 校验 `npm run build` 产物；`VITE_API_BASE` 留空走同源（已支持，零改动）；
- 页脚加备案号组件，链接 `beian.miit.gov.cn`（管局要求）。

### P2-10 观测与备份

- 健康检查扩展：现有 `/api/v1/system/health` + `/api/v1/qa/health`，补 **MySQL 连通性**、**队列积压深度**、**Worker 心跳**；
- 备份：`mysql_data`（`mysqldump`）+ `vector_db` + `upload` + `redis_data`，定期 rsync 到 COS 按量计费，不买预付资源包；
- 明确**数据保留策略**（会话归档行的物理清理周期），承接 P0-1 留下的 `is_archived`。

---

## 9. P3 — 可选增强（2.0.x 再说）

- **P3-11 问答字符串缓存**：Redis `rag:qa:{project_id}:hash(query)`，命中仍写 MySQL 记录并标 `is_cache_hit`；知识库变更时清对应项目缓存。
- **P3-12 SSE 碎片断点补发**：Redis 短 TTL 存已生成未推送碎片，配 `Last-Event-ID` 补发（**只能补发，不能恢复模型计算**）。
- **P3-13 多知识库隔离 UI**：`project_id` 字段已就位，前端加知识库切换。
- **P3-14 会话管理增强**：分页 / 搜索 / 导出（MySQL 到位后这些才有意义）。
- **P3-15 语义缓存**（Chroma 独立 collection）：默认不做，副作用是知识库变更不清缓存会答旧内容。
- **P3-16 Agent 化**：现有双层意图识别做入口粗筛 → 单 Agent ReAct 工具调用 → 主 Agent + 子 Agent 编排 → MCP 扩展外部工具。**Agent 每步思考与工具入参出参落 MySQL（企业审计）**，并设最大循环轮次防死循环。

---

## 10. 验收闸门（2.0.0 完成定义）

1. 后端重启后，会话 / 消息 / 用量 / 引用不丢，且**能 SQL 查到**（P0-1）；
2. 无 token 不能问答（P0-2）✅；
3. 上传文档 HTTP 立即返回，解析由 Worker 完成；关网页任务继续；Redis 重启后可从 MySQL 状态手动重解析（P0-3）；
4. 删除文档后磁盘 / Chroma / MySQL 三处无残留；历史引用凭 `chunk_id` 能反查正文（P0-4）；
5. 本地 `docker compose up -d` 一键起全栈（P1-5）；
6. 服务器上同一套 compose 起得来、流式正常（P1-5 + P1-6）；
7. `https://nianan.site` 可访问、流式正常、页脚有备案号（P1-7 + P2-9）；
8. 打 tag `v2.0.0` + 写 `docs/SUMMARY-v2.0.0.md`。

---

## 11. 风险与注意

### 11.1 4C4G 内存预算（**最大的硬约束，先算清再动手**）

| 服务 | 常驻估算 | 备注 |
|---|---|---|
| MySQL 8 | 350~450 MB | `innodb_buffer_pool_size` 压到 128M |
| Redis | ≤ 128 MB | `maxmemory 128mb` + `noeviction` |
| gateway (Node) | 120~180 MB | |
| nginx | ~20 MB | |
| backend（嵌入 + 重排 + uvicorn） | 1.8~2.2 GB | **reranker-base 是内存大头** |
| worker（仅嵌入 + Celery） | 500~700 MB | `concurrency=1`，**不加载 reranker** |
| OS + Docker 自身 | 300~400 MB | |
| **合计** | **3.4~3.9 GB** | 4G 很紧，必须有对策 |

**对策**（按优先级）：① worker 不加载 reranker；② `--max-tasks-per-child` 定期回收；③ MySQL buffer pool 降 128M；④ Redis 128MB noeviction；⑤ 加 2G swap 兜底；⑥ 若仍不够，reranker 换小模型或延迟加载（问答首请求才加载）。

### 11.2 其他风险

- **队列不可靠**：Redis 重启丢未消费任务。**不修**，靠 MySQL `status` + 重解析接口兜底（修它要引入 Redis Streams + 消费者组，复杂度不划算）；
- **多 Worker 幂等**：所有解析入口必须先做 MySQL 原子抢任务，否则重复插切片；
- **Chroma 无原生 TTL**，且统计的只是切片数、不是业务文档数 —— 业务计数一律以 MySQL 为准；
- **本次调整的一次性成本**：现有向量库 metadata 缺 `doc_id`，必须重建；
- **备案时序**：P1-7 依赖 ICP 备案通过（1~2 周），期间走 IP:端口 自测，勿对外解析域名；
- **密钥**：`.env` 全程不入库、不进镜像；
- **本地 LLM 不上云**：云端 LLM 走 API，无 GPU 机器本地推理太慢。

### 11.3 决策记录

| # | 决策 | 结论 | 理由 / 代价 |
|---|---|---|---|
| D1 | 本地开发形态 | **A：中间件容器化 + 应用裸跑** | 热重载与断点调试不变，同时解决「本机没装 MySQL / Redis」；全栈 compose 只在验收与服务器预演时用 |
| D2 | 网关账号是否迁 MySQL `user` 表 | **本版不迁**，账号仍在 `.env` | 迁了要网关直连 MySQL + 加用户管理 CRUD，本期无多用户需求；代价是账号改动要重启网关（成本可忽略） |
| D3 | 引用是否存 chunk 快照 | **不存**，只存 `chunk_id` | 存快照 = 表爆炸（每条引用一份切片正文）。代价：文档删除后历史引用正文查不到，前端需降级展示 |
| D4 | 是否做 token 白名单（Redis） | **不做** | 无状态 JWT 不需要；代价是无法即时吊销，只能等过期 |
| D5 | 解析队列技术选型 | **Celery + Redis broker** | 重试 / 超时 / 路由 / 观测（Flower）都有现成方案，worker 常驻约 150~200MB 可接受。否掉 Redis Streams 自研（省一个依赖，但要自己写消费循环 / 重试 / 心跳约 200 行）；否掉 RQ / arq（更轻，但重试与观测太薄，后续 Agent 审计链路可能还要再换） |

**D1 结论与落地方式**（这一条会直接影响 `.env` 结构与 compose 写法，先说清）：

- **日常开发**：`docker compose up -d mysql redis` 起中间件，`make api` / `make gateway` / `make frontend` 裸跑；
  应用通过 `localhost:3306` / `localhost:6379` 连容器，热重载与断点调试体验不变；
- **`MYSQL_HOST` / `REDIS_HOST` 必须做成变量**：本地裸跑要 `localhost`，P1-5b 全栈模式下服务间走容器网络得是
  `mysql` / `redis`。用两个变量表达主机名，切模式只改这两个值 ——
  **不要在两处硬编码主机名**，否则切换模式必然漏改一处，且症状是「连不上数据库」这种低信息量报错；
- **全栈 compose 的用途**：本地端到端验收、演示、以及上传服务器前的最后预演。

---

## 12. 版本与文档约定

- 每个大版本：一份 `PLAN-vX.Y.Z.md`（迭代前）+ 一份 `SUMMARY-vX.Y.Z.md`（完成后），与同名 git tag 配对；
- 每个功能迭代完成，做齐 `docs/CHANGELOG.md` 约定的**五件事**（迭代文档 / RELEASES / 流水表 / `PROJECT_VERSION` / 计划书状态）；
- **计划书本身被修订时**：在本文档 §0 追加修订说明 + 更新编号对照表，并在 `CHANGELOG.md` 的「修订」小节记一行 —— 计划书是活文档，但每次改动都要留痕。
