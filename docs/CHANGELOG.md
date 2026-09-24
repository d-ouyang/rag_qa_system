# 变更日志（CHANGELOG）

> **项目铁律：每完成一块功能，就落一份迭代文档 + 一个版本号。**
> 版本号不是装饰，是「项目走到哪了」的唯一可检索标识：
> 看到 `2.0.0-p0.2` 就知道已经做完 2.0.0 计划里的 P0-1、P0-2 两项。

---

## 一、版本号规则

```
        2.0.0        -        p0.2
        └─┬─┘                └─┬─┘
     大版本（对应 docs/PLAN-v2.0.0.md 一份计划书）
                    子版本（对应计划书里的任务编号 P0-2）
```

| 位置 | 值 | 说明 |
|------|-----|------|
| `docs/PLAN-vX.Y.Z.md` | 迭代前写 | 这一版要做什么、验收标准是什么 |
| `docs/SUMMARY-vX.Y.Z.md` | 大版本完成后写 | 这一版最终交付了什么（阶段总结） |
| `docs/iterations/vX.Y.Z-pN.M-<slug>.md` | **每个功能完成就写** | 单次功能迭代的变更记录（**对内**：含「否掉的方案与代价」） |
| `docs/RELEASES.md` | **每个功能完成就补** | 对外变更叙事（**对外**：新增什么能力 / 修了什么 / 优化了什么） |
| `.env` / `.env.example` 的 `PROJECT_VERSION` | 跟随最后一次子版本 | 应用运行时版本，前端侧边栏与 `/api/v1/system/health` 都能看到 |
| git tag | 大版本完成 / 阶段里程碑 | 见下方说明 |

**tag 规则**：

| 时机 | tag 名 | 例 |
|------|--------|-----|
| 大版本全部完成（计划书所有任务） | `vX.Y.Z` | `v2.0.0` |
| **每个可独立验收的交付**（阶段、或阶段里的半项） | `vX.Y.Z-pN.M[a]` | `v2.0.0-p0.2`、`v2.0.0-p1.5a` |
| 同一任务的**第二轮返工** | 加字母后缀；**老 tag 永不重打** | `v2.0.0-p0.1b` |

> **2026-09-23 二次修订**（用户要求「每步都留可检索的落点」）：
> 原规则「单个功能迭代不打 tag」作废。现在只要一块功能**能独立验收**，就打 tag。
> 半项用字母后缀区分（如 P1-5 拆成 5a / 5b → `p1.5a` / `p1.5b`）。
>
> **返工 tag**：同一任务重做时，用字母后缀（`p0.1b` / `p0.1c`），
> **绝不删除或重打老 tag** —— tag 一旦重打，「看到 tag 就知道那个 commit 跑的什么版本」这条约束就失效了。
>
> ⚠️ **tag 的时间先后 ≠ 版本号大小**。执行顺序按依赖走，会出现「`p1.5a` 早于 `p0.1b`」这种情况
> （P1-5a 是 P0-1 的前置）。判读新旧请看 `git log`。

> 阶段 tag 解决的是「想回去看某个阶段刚完成时的代码」这个需求 —— 没有它，
> 只能在 commit 历史里翻。**tag 名与 `.env` 的 `PROJECT_VERSION` 保持一致**，
> 这样看到 tag 就知道那个 commit 上是哪个版本在跑。

**新增一次功能迭代时必须做的五件事**（漏一件就算没完成）：

1. 新增 `docs/iterations/vX.Y.Z-pN.M-<slug>.md`（模板见文末）；
2. 在 `docs/RELEASES.md` 追加这一版的「新增 / 修复 / 优化 / 验证」段落；
3. 在本文件「二、版本流水」表格追加一行；
4. 更新 `.env` 与 `.env.example` 的 `PROJECT_VERSION`；
5. 在 `docs/PLAN-vX.Y.Z.md` 对应任务上标注状态。

> 第 2 条为什么和第 1 条分开：两份文档**读者不同**。
> 迭代文档是写给「三个月后回来改代码的自己」，所以最重的是「当时还考虑过什么、否掉了什么」；
> RELEASES 是写给「不了解这个项目的人」，所以要讲清「这一版解决了什么问题」，
> 不能出现只有内行才懂的术语。合成一份的结果通常是两边都不好用。

---

## 二、版本流水

| 版本 | 日期 | 内容摘要 | 迭代文档 |
|------|------|----------|----------|
| v1.0.0 | 2026-09-22 | 本地可联调系统：模块 1~5（配置/文档加载/向量库/LLM+检索/记忆+意图+RAG链+API）+ Vue 3 前端 | `SUMMARY-v1.0.0.md` |
| **v2.0.0-p0.1** | 2026-09-23 | 会话持久化：内存 dict → 可插拔 SessionStore（Memory / Redis）+ Redis 内存预警与只读降级 | `iterations/v2.0.0-p0.1-redis-session-store.md` |
| **v2.0.0-p0.2** | 2026-09-23 | 鉴权网关：NestJS JWT 登录 + 全局守卫 + 限流 + 反代（NDJSON 透传）+ 统一错误体；前端登录页与 token 联动 | `iterations/v2.0.0-p0.2-auth-gateway.md` |
| **v2.0.0-p1.5a** | 2026-09-23 | 本地中间件容器化：MySQL + Redis 编排（命名卷持久化 + 端口只绑回环 + 健康检查）、14 项自检脚本、Makefile `infra*` 目标；**应用代码零改动** | `iterations/v2.0.0-p1.5a-middleware-compose.md` |
| **v2.0.0-p0.1b** | 2026-09-23 | **P0-1 返工交付**：会话 / 消息 / 用量 / 引用落 MySQL（Alembic `0001` 五张表 + `MySQLSessionStore` + `SELECT ... FOR UPDATE` + 软 TTL 归档）；抽共享存储契约、新增 module8（62 项）；`MEMORY_BACKEND` 改 `memory\|mysql`，退役取值**抛错而不静默回退**；**业务侧仍零改动** | `iterations/v2.0.0-p0.1-redis-session-store.md` §7.2 |
| **v2.0.0-p0.3a** | 2026-09-23 | **P0-3 后端交付**：上传改**异步**（202 + `pending` + 入队，不再返回 `chunks_added`）；Celery + Redis db1 消费；`document` 状态机 + **原子 UPDATE 抢任务**（16 线程只 1 个成功）；迁移 `0002` 加 `parse_started_at` 回收**僵尸 parsing**；切片 metadata 补 `doc_id`/`project_id`/`chunk_index`（P0-4 前置）；`documents` 路由 7 接口重写 + 新增 `GET /api/v1/system/queue`；module9（166 项）+ 验收脚本（5 条标准全绿）；**前端未改（p0.3b 必修）**、问答链路零改动 | `iterations/v2.0.0-p0.3a-async-parsing.md` |
| **v2.0.0-p0.3b** | 2026-09-23 | **P0-3 前端交付**（P0-3 完成）：知识库页从「等它做完」改成「看着它做」——`stores/documents.ts` 里做状态轮询（递归 `setTimeout` + 三条自我限制 + 失败退避），切走页面/关掉视图也照常收到完成提示；终态提示只发给「进过 `watched` 的文档」（避免一开页糊十几条 toast）；四按钮按 status 置灰**且说明原因**；失败原因整行展示 + 「重试」闭环；新增链路告警（有文档在排队但无 Worker，带可执行下一步）；下载改 `fetch`+blob（`<a href>` 带不上 token）；新增 `make accept-ui` 浏览器验收（56 项 / 0 失败，连跑两遍）；**后端一行未改**、`make test` 仍 725 项全绿 | `iterations/v2.0.0-p0.3b-frontend-polling.md` |

| **v2.0.0-p0.4a** | 2026-09-23 | **P0-4 后端交付**：切片身份证 `chunk_id = "<doc_id>:<chunk_index>"`（`build/parse_chunk_id` 契约 + 写入侧 metadata + 读出侧 `_resolve_chunk_id` 三级取值）；反查接口 `GET /api/v1/chunks/{chunk_id}`（400 / 404 两种文案 / 200+`document_exists=false` 四种分工）；孤儿切片 `list/purge_orphan_chunks`；`scripts/reindex.py` 三步重建（补登记 → 幂等重灌 → 清孤儿，**默认干跑** + worker/后端两道闸门）；`chat_message.ref_ids` 首次真正有值；`document_repo.list_all()`；module10（87 项）+ 验收脚本（4 条标准全绿）；**前端未改（p0.4b 必修）** | `iterations/v2.0.0-p0.4a-chunk-refs.md` |

| **v2.0.0-p0.4b** | 2026-09-24 | **P0-4 前端交付（P0-4 完成，P0 四项全绿）**：`types.ts` 加 `SourceItem.chunk_id` + `ChunkDetail`；新增 `api/chunks.ts`（`getChunk` + `isValidChunkId`，**不用 `encodeURIComponent`** 以免冒号被代理二次编码）；`api/qa.ts` 的内联 sources 类型换成 `SourceItem`（消除第二份定义）；`MessageBubble.vue` 引用条可点化 + 展开区（全文/元信息/加载中/错误文案）+ 按 chunk_id 缓存（**失败也缓存**）+ 样式；新增 `tests/acceptance_p0_4b_ui.mjs`（18 项，连跑两遍）；`make accept-ui-p04b`。**后端一行未改** | `iterations/v2.0.0-p0.4b-chunk-ref-ui.md` |

| **v2.0.0-p0.4c** | 2026-09-24 | **修 `p0.1` 期潜伏至今的轮元数据错位**（用户真实使用中暴露）：`add_exchange()` 里 `_normalize_meta()` 从「append 消息**之后**」移到**之前** —— 原位置让每轮凭空多补一个空占位，再被 `_trim()` 防御分支从头部砍掉，净效果是**每写一轮就挤掉最老一轮的 meta**（连写 5 轮实测 `[i3, {}, i4, {}, i5]`，前两轮蒸发）。表现为「只有第一次提问有引用，刷新后后面几轮全空」。新增 module6 第 3B 组（14 条，memory/redis 各 7 条，**写死轮号**并复刻线上 `metas[i//2]` 回填逻辑）+ **反向验证**（还原 bug → 8 条红，证明断言有效）+ `scripts/e2e_p0_4c_meta.py`（连问 3 轮 → 刷新 → 每轮引用都在）。顺带修 `tests/test_module5_rag_chain_api.py` 第 1 组**不传 store 导致连真实业务库**（`.env` 切 mysql 后 2 条假失败，且 `cleanup_expired()` 会归档真实会话），改为显式注入 `MemorySessionStore`。**不写存量数据迁移脚本**（会话可重建，用户已确认） | `iterations/v2.0.0-p0.4c-meta-alignment.md` |

| **v2.0.0-p1.5b** | 2026-09-24 | **P1-5b 应用容器化（✅ 已联调通过）**：新增 `Dockerfile`（python:3.11-slim + `requirements.lock.txt`）、`frontend/Dockerfile`（node:20-alpine → nginx:alpine）、`frontend/nginx.conf`（`/api` 反代 + **`proxy_buffering off`** + SPA 回落）、`gateway/Dockerfile`、三个 `.dockerignore`；compose 追加 `backend`/`worker`/`gateway`/`frontend` **全部挂 `profiles: [full]`**（否则 `make infra` 会与裸跑的 8000/3000/5173 撞端口）；靠 compose `environment` **覆盖** `env_file` 实现「同一份 .env 两种模式共存」（`MYSQL_HOST=mysql`、两个 Redis URL、`GATEWAY_BACKEND_URL=http://backend:8000`、`GATEWAY_TRUST_PROXY=true`）；worker **复用后端镜像** + `USE_RERANKER=false`（省 1.1G，解析不用重排）；`vector_db`/`upload`/`models` 用 **bind mount**（复用宿主机已有数据，命名卷会让容器里知识库是空的）；backend **不映射 8000**（网关要求内网可达）；worker **不配 healthcheck**（`celery inspect ping` 会误判健康 worker）；`extra_hosts: host.docker.internal:host-gateway`（容器连宿主机服务）；新增 `make stack-up/ps/logs/down/rebuild`。**应用代码一行未改**。2026-09-24 全栈联调通过（六容器/登录/同步+流式问答/上传解析/引用反查/删除闭环实测），联调修复三处见下方「修订」表 | `iterations/v2.0.0-p1.5b-app-containers.md` |

| **v2.0.0-p1.5c** | 2026-09-24 | **联调后五项优化**：复制成功 toast；Token 明细只在徽章 hover，浮层改 `position:fixed` 并按视口朝上/朝下；消息区 `scrollbar-gutter: stable`；Chroma 改 server（`HttpClient`，数据在 `./chroma_data`，`make infra` 含 chroma，自检 17 项）；`POST /documents/upload/batch` + 前端多选/选文件夹，nginx `client_max_body_size 120m`。上传后不重启即可检索（实测命中新 doc）。module3 102/0、module9 173/0 | `iterations/v2.0.0-p1.5c-ux-chroma-batch.md` |

> 当前应用版本：`2.0.0-p1.5c`
>
> 上表是**工程对账**口径（谁在哪个文件里改了什么）。
> 如果是要**向人展示「这个项目怎么一步步完善的」**，读 `docs/RELEASES.md`。

> ⚠️ **`make test` 的总项数不是常量** —— 它取决于当时有没有 worker 在跑。
> module9 第 10 组「真 worker 端到端」在探测不到 worker 时会 `[SKIP]`：
>
> | 条件 | module9 | 合计 |
> |------|---------|------|
> | 无 worker 在应答 | 160 通过 / 0 失败（第 10 组 SKIP） | 806 |
> | 有 worker 在应答 | 166 通过 / 0 失败 | 812 |
>
> 差额正好 6 项 = 第 10 组的 6 条断言。**看到 806 不要当成回归**，先确认 `make worker` 起着。
> 两个数都是 0 失败，所以「全绿」这条结论不受影响。
> （p0.4a 起基线从 719/725 抬到 806/812，差 87 项 = 新增的 module10，正好对得上。）
>
> **p0.4c 本轮口径**：按用户指示**未跑 module9**，逐个跑 module2~8、10 合计
> **666 通过 / 0 失败**（module2 118 / module3 100 / module4 85 / module5 44 /
> module6 127（含新增 14 条）/ module7 43 / module8 62 / module10 87；
> module1 是日志系统演示脚本，无计数汇总行）。
> **666 不与 806/812 基线直接可比**，差额约 146 项即 module9 的贡献。
> 恢复 `make test` 全量口径须补跑 module9，并留意它会全表清空 `document`（见下方遗留项）。

### 2.0.0 整体进度

> ⚠️ **计划书已于 2026-09-23 修订到第 2 版**（数据分层调整：会话真相源 Redis → MySQL，
> 新增异步解析链路与向量元数据对齐，编号顺延）。下表是**第 2 版**口径，
> 修订原因与编号对照见 `PLAN-v2.0.0.md` §0。

| 分组 | 任务 | 状态 |
|------|------|------|
| P0 上线硬前提 | P0-1 业务数据落 MySQL ✅、P0-2 鉴权网关 ✅、P0-3 异步解析 ✅、P0-4 向量元数据对齐 ✅（`p0.4a` 后端 + `p0.4b` 前端 + `p0.4c` 修错位） | ✅ **4 / 4** |
| P1 容器化与部署 | P1-5 Docker 化 ✅（5a + 5b，已联调）、P1-6 模型目录、P1-7 TLS | 🔄 1 / 3 |
| P2 生产化打磨 | P2-8 配置治理、P2-9 生产构建+备案号、P2-10 观测备份 | ⬜ 0 / 3 |

> **2.0.0 大版本合计：P0-1 ✅、P0-2 ✅、P0-3 ✅（3a 后端 + 3b 前端）、P0-4 ✅（`p0.4a` 后端 + `p0.4b` 前端 + `p0.4c` 修复）、
> P1-5 ✅（5a + 5b，已联调），其余未开始。完成 6 / 10。**
>
> 🎉 **P0 阶段已全部完成，阶段 tag `v2.0.0-p0.4` 已打**（与本次交付 tag `v2.0.0-p0.4b` 指向同一个 commit）。
> 至此「上线硬前提」四项全部就位：数据落库、鉴权、异步解析、引用可反查可点击。
>
> **下一步：P1-6 模型目录**（本机 `models` 是软链，联调中已验证容器内可正常跟随；
> 上服务器前换真实目录），之后 P1-7 TLS。
> 执行顺序：P1-5a ✅ → P0-1 ✅ → P0-3a ✅ → P0-3b ✅ → P0-4a ✅ → P0-4b ✅ → P0-4c ✅ → P1-5b ✅（已联调）→ **P1-5c ✅** → **P1-6** → P1-7。
>
> ✅ **`v2.0.0-p1.5b` 已于 2026-09-24 全栈联调通过**，阶段 tag `v2.0.0-p1.5` 已补打。
> 联调抓到并修复 3 个真问题（网关生产配置缺口、compose 插值吃掉 bcrypt 哈希、
> lock 文件在 Linux 下补拉整套 CUDA），详见下方「修订」表与 p1.5b 迭代文档 §7。

### 修订（同版本内的返工，不新开子版本号）

子版本号对应计划书里的任务编号（P0-1 → `p0.1`），所以**修 bug / 补漏不新开 `p0.x`**，
而是记在对应迭代文档末尾的「修订记录」一节里。这样版本号始终等于「计划书里做完了几项」，
不会因为返工而漂移。
**返工时在 git tag 上加字母后缀**（`p0.1b` / `p0.1c`），老 tag 永不重打。

| 日期 | 版本 | 修订 | 详见 |
|------|------|------|------|
| 2026-09-23 | `2.0.0-p0.1` | 新增 `tests/test_module7_redis_over_tcp.py`（走真 socket 验「重启不丢数据」）；`inspect()` 在不可达时补 `thresholds`/`advice` | p0.1 文档 §7 |
| 2026-09-23 | `2.0.0-p0.2` | 补上白名单放行两个上游健康检查（计划书要求、首轮实现漏掉）；冒烟 22 → 25 项 | p0.2 文档 §7 |
| 2026-09-23 | `2.0.0-p0.1` | **方案变更（架构级）**：会话真相源 Redis → **MySQL**；`RedisSessionStore` 从生产路径退役，`SessionStore` 抽象层与其契约测试保留；P0 新增 2 项、P1/P2 编号顺延 | p0.1 文档 §7.1；计划书 §0 |
| 2026-09-23 | `2.0.0-p0.1b` | **返工交付**：新增 `core/db.py`、`core/schema.py`、`core/mysql_store.py`、`alembic/`（`0001` 建 5 张表）；新增 `tests/store_contract.py`（三后端共享契约）与 module8（62 项）；`MEMORY_BACKEND` 改 `memory\|mysql` 且退役/未知取值抛错；module7 的工厂装配断言改为显式注入 | p0.1 文档 §7.2 |
| 2026-09-23 | `2.0.0-p0.1b` | **发现既有缺陷（本轮刻意未修，已于 `p0.4c` 修复）**：`add_exchange` 里 `_normalize_meta()` 的调用位置导致轮元数据整体错位一格（第 1 轮的 meta 被顶掉、末尾多一个空 dict）。已确认 memory 后端同样存在，与本次返工无关；为保住「业务侧零改动」这条验收证据不修，修法已在文档中写明 | p0.1 文档 §6.1 第 1 条 |
| 2026-09-23 | `2.0.0-p0.3a` | **验收脚本第 4 条重写**：首轮把「对已 `success` 的文档重复入队」当幂等验证，但 `reset_for_reparse` 按设计**拒绝 `success`**，两条消息都被挡掉 —— 「没重复切片」是因为压根没跑，证不出幂等。改为用**新**文档在 worker 抢到前连推两条，并加断 `attempt_count == 1` | p0.3a 文档 §7；仅验收脚本，`core/` 无改动 |
| 2026-09-23 | `2.0.0-p0.3a` | **修正一处失效指引**：`core/document_repo.py` 注释里指向的 `core/document_service.py` **并不存在**，实际编排在 `api/routes/documents.py` 的 `_purge_document` | p0.3a 文档 §7；仅注释 |
| 2026-09-24 | `2.0.0-p0.4b` | **验收脚本的两个假信号（本轮唯一返工）**：① 第 7 组「文档已删除」首轮**假失败** —— 它挑的是一条**已被展开过**的引用，结果已进组件内缓存、再点不发请求，于是「假装 404」的桩永远拦不到，那一组实际在验缓存而不是验错误分支。改为用 `usedIdx` 显式挑未展开过的引用。② `page.route` 的 glob `**/api/v1/chunks/**` 对**带冒号**的 URL 不保险，换成 predicate 函数（`url.pathname.startsWith`）。③ 侧边栏选择器 `.app-sidebar` → `.sidebar`（写错的表现是「登录失败」，易往鉴权方向查错） | p0.4b 文档 §3.7 / §7 |
| 2026-09-23 | `2.0.0-p0.4a` | **`response_model` 静默丢字段（本版最贵的一行）**：`_extract_sources()` 加了 `chunk_id`，但 `api/routes/qa.py` 的 `SourceItem` 未声明它 → pydantic 默认 `extra='ignore'`，问答接口与会话历史返回的 `sources` 里**根本没有 chunk_id**，且不报任何错、测试全绿（测的是内部函数，没测接口出参）。修法：`SourceItem` 加 `chunk_id: str \| None`；并在 module10 加「产出的键 ⊆ 模型字段」通用守卫（不绑定字段名）。否掉的方案：给模型加 `extra="forbid"` —— 同样能暴露问题，但暴露方式是**线上 500** 而不是测试红，对这种无害 drift 处罚过重 | p0.4a 文档 §3.10 / §7 |
| 2026-09-23 | `2.0.0-p0.4a` | **回归测试会清空真实业务表**：`tests/test_module10_chunk_refs.py` 照抄 module9 的 `sa_delete(document_table)`（不带 `WHERE`），跑一次就把开发机上真实文档记录抹掉，留下「查得到正文、溯不到源」的幽灵引用且无报错提示。已按 `file_name LIKE 'm10_<uuid>%'` 收窄，结尾「按 `storage_path` 删文件」那段（遍历 `repo.list_all()` 全表）同样加了前缀过滤。**module9 仍是全表清空**（`total_documents == 1` 等断言依赖空表），列为遗留项；当前应对：跑完 `make test` 执行 `make reindex-apply` 即可恢复演示数据 | p0.4a 文档 §3.11 / §6 第 2 条 |
| 2026-09-23 | `2.0.0-p0.4a` | **`scripts/reindex.py` 加第二道闸门**：验收时发现「多进程同时改写同一 Chroma `persist_directory`」会让 HNSW 索引不一致（查询返回 `documents=None` → langchain 构造 `Document` 直接 `ValidationError` → **问答接口 500**；或 hnswlib 抛 `ef or M is too small`）。后端在线时 `--apply` 拒绝执行，退出码 2。另注：**「重置单例」不等于「重启进程」** —— chromadb 同进程按 (目录, collection) 共享集合实例，`reset_vector_store_manager()` 之后拿回的还是同一个对象，实测照样崩 | p0.4a 文档 §3.6 / §3.7 |
| 2026-09-23 | `2.0.0-p0.4a` | **验收脚本的「重建前后一致」改为多重集比较**：首轮夹具用「同一句话重复 40 遍」凑长度，切出的多片正文完全相同 → 重排分并列 → 两次检索顺序互换被误判成漂移。夹具每条加序号，断言改 `Counter` 比较。**并列是合法的，漂移才是 bug**，断言要能区分这两件事 | p0.4a 文档 §3.12 |
| 2026-09-23 | `2.0.0-p0.3b` | **交付前自查**：`syncNow()` 开头调 `stopPolling()` 会把 `polling` 置 `false`，下一句 `ensurePolling()` 又置回 `true` → 界面「正在自动刷新」小圆点以轮询周期闪烁。拆出 `clearTimer()`（只清定时器句柄），`stopPolling()` = `clearTimer()` + 灭灯 | p0.3b 文档 §3.3 / §7 |
| 2026-09-24 | `2.0.0-p0.4c` | **单元测试会连真实业务库（本轮连带修复）**：`tests/test_module5_rag_chain_api.py` 第 1 组的 `MemoryManager(...)` **不传 store** → 按 `.env` 的 `MEMORY_BACKEND` 建。本次排障把 `.env` 从 `memory` 改成 `mysql` 后，这组单元测试就連上真实业务库：`session_count() == 2` 被库里既有会话顶翻（2 条假失败），且 `cleanup_expired()` 会把真实会话一并**归档**。判据：`MEMORY_BACKEND=memory` 重跑 → 44/0 全绿，据此确认与本次代码改动无关。修法：显式注入 `MemorySessionStore` 并同步下传 TTL。**单元测试既不该碰业务库，也不该随 `.env` 漂移** | p0.4c 文档 §3.5 |
| 2026-09-23 | `2.0.0-p0.3b` | **验收脚本自己会骗人（两处）**：① 首轮按**文案**去重记 toast，而同一文档重复失败的两次文案**一模一样**，第二次被吞 → 「重试真的又跑了一遍」假失败；改为按 DOM 元素记账（`WeakSet`）。② 第二轮点开抽屉后立刻 `count('.chunk-card')`，而抽屉正在异步取片段 → 数到 0（首轮是**假通过**）；改为 `tryWait(n >= chunkN)`。教训：**一次通过不算通过**，本轮验收连跑两遍才算数 | p0.3b 文档 §3.13 / §3.14 / §7；仅验收脚本，`frontend/src` 无改动 |
| 2026-09-24 | `2.0.0-p1.5b` | **全栈联调抓到并修复 3 个真问题**：① **网关生产配置缺口** —— compose 里网关 `NODE_ENV=production` + `env_file: .env`（根目录），但根 `.env` 缺 `GATEWAY_JWT_SECRET`/`GATEWAY_USERS`（`gateway/.env` 不进运行镜像）→ 启动即崩，已补根 `.env` 与 `.env.example`；② **compose 对 `env_file` 的 `$` 插值吃掉 bcrypt 哈希**（`$2a$10$V7V4...` 的 `$V7V4...` 被当变量替换成空 —— 静默损坏，登录永远 401 且日志无配置错误）→ 根 `.env` 改 `$$` 双写转义；③ **macOS 编译的 `requirements.lock.txt` 在 Linux 构建时补解析整套 CUDA**（pip 补拉 `nvidia-*` 数 GB）→ `Dockerfile` 先显式装 CPU 版 torch 再装 lock（后端镜像 3.07GB）。修复后六容器/登录/同步+流式问答/上传解析/引用反查/删除闭环全部实测通过 | p1.5b 文档 §5.2 / §7 |

---

## 三、迭代文档模板

```markdown
# vX.Y.Z-pN.M <一句话标题>

- 版本 / 日期 / 对应计划书任务
- 上游基线（上一版是什么）

## 1. 范围
这一版做了什么、**明确不做什么**

## 2. 变更清单
按文件列出新增/修改，一句话说明这个文件为什么被动

## 3. 关键设计决策
为什么这么选，**否掉的方案是什么、否掉的代价是什么**
（这部分最值钱：三个月后回看代码能看懂"为什么"，但看不懂"当时还考虑过什么"）

## 4. 配置与接口变化
新增/变更的 env、HTTP 接口、数据结构

## 5. 验证证据
跑过什么、结果如何（命令 + 实际输出摘要，别只写"已验证"）

## 6. 已知边界与遗留项
现在不做的、已知不完善的、踩过的坑
```
