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

| **v2.0.0-p1.6** | 2026-09-24 | **模型全部走硅基流动**：`EMBEDDING_BACKEND` / `RERANK_BACKEND` 默认 siliconflow（`bge-large-zh-v1.5` 1024 维 + `bge-reranker-v2-m3`），意图 provider 改为 siliconflow；运行路径不加载本地权重。维度变化后已删 collection 并重灌（24 篇 / 106 片）。`make test` 强制 local。知识库列表每页 10 条，并补 `min-height: 0` 让页面能滚 | `iterations/v2.0.0-p1.6-remote-models.md` |

| **v2.0.0-p1.6b** | 2026-09-24 | **会话持久可见 + 相同问题缓存**：闲置不再把会话从列表拿掉，也不再盖掉旧消息；`MEMORY_MAX_TURNS=5` 只限制模型窗口。Redis db0 精确缓存（最近使用，上限 100），知识库变更清空。完整问句即使出现在已有会话里也会写入；指代短句不写。近义缓存不做。引用显示原始文件名；知识库列表底栏固定分页。问答缓存测试 14/0，module6 130/0 | `iterations/v2.0.0-p1.6b-session-and-cache.md` |

| **v2.0.0-p1.6c** | 2026-09-26 | **流式回答可停止，上滑不再被拽回底部**：生成中按钮改为停止，断开后后端关掉模型流，半截答案不写入记忆。人在底部才自动跟随。token 按动画帧合并写入。流结束补一次解码；消费 `session` 帧。不改成 SSE，也不渲染 Markdown。`vue-tsc` 通过 | `iterations/v2.0.0-p1.6c-streaming.md` |

| **v2.0.0-p1.6d** | 2026-09-28 | 锅圈知识库重灌 + 锅圈 logo 替换 + 知识库列表 307 重定向修复（**本条为事后补记**，当时未入账） | **无迭代文档（欠账，不事后补写）** |
| **v2.0.0-p1.6e** | 2026-10-06 | **修「气泡第一行是空行」**：模型偶尔以 `\n\n` 开头，链路里没有任何一处裁它 —— 前端气泡是 `white-space: pre-wrap` 纯文本容器，前导换行渲染成空首行；首帧整包是 `\n\n` 时更是一个空气泡。两层取证（后端打桩 LLM 实测首帧与落库正文都带脏前缀；渲染层用真实 CSS 数行盒 1 → 3）后修：新增 `_strip_leading_blank()`，让**流式增量 / 缓存回放 / 同步返回 / 落库正文**四个出口口径一致；整包空白不发帧（前端继续显示"正在思考"，不出现空气泡）。module5 新增第 5 组 8 项（55/0），**反向验证**还原修复后 7 条转红。历史脏数据未清（给出 SQL，交人确认后执行） | `iterations/v2.0.0-p1.6e-answer-leading-blank.md` |
| **v2.0.0-p2.11d** | 2026-10-07 | **模拟员工种子（P2-11d）**：新增 `scripts/seed_users.py`（`make seed-users`）—— 3 部门 / 6 职位 / 10 员工（含 1 名已离职，留给管理端做「离职≠删行」的样本），每人一个**随机**临时密码 + `must_change_password=1` + 一条改密历史（所以首次改密不能用同一个）。**默认干跑**（沿用 reindex 的规矩：会写库的命令不该在敲错时直接生效）。**重跑只同步资料，绝不重置密码、绝不改 `status`** —— 员工自己改过的密码不该被脚本打回，管理员停用的员工不该被悄悄恢复（已实测）。凭据**只显示一次**且脚本走 print 不走 logging，实测 4MB 的 app.log 里搜三个临时密码命中 **0** 次；`--credentials-file` 会先判 git 是否跟踪，被跟踪则拒写。⚠️ **修正了已 tag 的迁移 0003**：加一步 `UPDATE user SET employee_no=CONCAT('LEGACY-',id) WHERE employee_no=''`。起因是 11a 留下的「重放需要空表」限制—— 种子数据把 10 个员工灌进去后，往返验收立刻报 `1062 Duplicate entry ''`。**没有绕开，而是把迁移改对**；代价（改了已tag 的迁移、已跑旧版的库需手工补）在迭代文档 §3.1 写明。连带把验收脚本改成**无损往返**（快照/还原工号 + 三张新表数据，否则 downgrade 会 DROP 掉它们，员工的 department_id 变悬空引用且不报错）。module11 141/0、验收 34/0（均连跑两遍），存量 35 文档/90 切片零损失 | `iterations/v2.0.0-p2.11d-seed-users.md` |
| **v2.0.0-p2.11b** | 2026-10-06 | **密码策略（P2-11b）**：新增 `core/password_policy.py` —— **判定规则的唯一出处，且刻意不做任何数据库查询**（要判重就把哈希列表取出来传进去，于是边界与文案都能不起库地钉死）。规则：bcrypt cost 12、有效期 90 天（判据 `now - changed_at > 天数`，**边界正好到期仍算有效**）、最近 5 条不可复用、连错 5 次锁 15 分钟、临时密码一次性生成。三个容易被做错的地方刻意做对了：① **「账号不存在 / 密码错 / 已锁定」对外文案逐字相同**（防用户名枚举），但内部 code 仍区分给日志用；② 四条失败路径都跑一次真实 bcrypt 比对把**耗时**也对齐（实测比值 1.00~1.11，否则响应时间就是枚举器）；③ `password_changed_at` 为 NULL 时**按已过期处理**（fail-closed）。新增 `user_password_history` 表（迁移 0004）与 8 项可配策略。module11 由 73 项扩到 **141 项**（**连跑 20 遍 0 失败**），`compare_metadata` diff=0。**反向验证**：到期判据 `>` 改 `>=` → 边界断言红；删掉耗时对齐 → 耗时比红到 5.7 万倍。⚠️ 顺带抓到一个 15 遍漏 13 遍的 bug：临时密码的强制位只过滤了数字，小写位能抽到 `l`、大写位能抽到 `I`/`O` —— 已改为「所有抽取都从过滤后的池子取」+ 生产代码里加兜底自检 + 断言改为验「候选池」这个不变量而非抽样 | `iterations/v2.0.0-p2.11b-password-policy.md` |
| **v2.0.0-p2.11c** | 2026-10-08 | **账号真相源从 `.env` 迁到 MySQL（P2-11c，P2-11 整格完成）**：种子员工第一次能真的登录 RAG 主应用。⚠️ **偏离设计规格 §8 D11 的字面表述** —— 规格写「网关直连 MySQL」，实际改为**后端出 `/api/v1/internal/auth/{login,logout}`、网关调它**：11b 立的铁律是 `core/password_policy.py` 为判定唯一出处（141 条断言守着：防枚举文案逐字相同、四条失败路径耗时等长、到期判据是 `>` 不是 `>=`），网关是 TypeScript，重写一遍就是两份没测试的副本。**落地前实测 `bcryptjs` 确能验 passlib 哈希**（205ms vs 200ms 比值 1.02）—— 技术上可行，但可行不等于该做。代价：登录多一次内网 HTTP（纯 HTTP 开销实测 0.7~1.1ms；整个登录判定 172~179ms 是 bcrypt cost 12 的成本，判定放在 Python 还是 TS 都一样）+ 多一个必须保护好的内部接口。新增 `core/auth_service.py`（编排登录：判定委托 `policy.verify()`，自己只负责副作用 —— 失败计数 / 锁定 / 解锁 / `last_login_at` / rehash）与 `api/routes/internal.py`（`X-Internal-Token` 用 `hmac.compare_digest` 防时序攻击、`include_in_schema=False` 不进 `/docs`、缺密钥 **503 fail-closed**）。⚠️ **不用网关那套「生产缺 JWT 密钥直接启动失败」** —— 那会连累 `make test` 与 `make api` 裸跑；**也不解决「8000 被映射出去」**，那靠 compose 里 backend 不映射端口（拓扑保证，不靠代码）。**「判定没跑成」与「判定不通过」必须可区分**：`InternalAuthClient.verify()` 返回 `InternalAuthResult | null`，null = 后端不可达/缺密钥/5xx/解析失败（**不是密码错**）；判定不通过也返 HTTP 200，业务成败看 `body.ok`。**对外 code 与后端内部 code 分开**（防枚举第二道）：`not_found`/`wrong_password`/`locked` → 同一个 `INVALID_CREDENTIALS`，只有 `expired` 与 `inactive` 单独文案。⚠️ **回落默认关闭**（`GATEWAY_INTERNAL_FALLBACK=false`，含生产）—— 回落看着「提高可用性」，实际是开了一条绕过 MySQL 的登录通道。⚠️ **Node 全局 `fetch` 会读 `HTTP_PROXY`/`http_proxy` 且不尊重 `NO_PROXY`** → 本机代理下 `fetch failed`/`ECONNREFUSED`；实测（Node 22.22）`undici` 既不能 `require` 也不能 `import 'node:undici'`、`setGlobalDispatcher` 是 `undefined`，**唯一稳定解是 `node:http`**（完全不读代理环境变量）。新增 `gateway/src/auth/internal-auth.client.ts`（8s 超时 + 1MB 响应上限 + 可收窄联合类型 `InternalAuthSuccess \| InternalAuthFailure`，失败态**在类型上就没有** `uid`/`role`）。JWT 载荷改带 `uid`/`role`/`ver`/`src`，⚠️ **`sub` 语义变了**（11c 前是登录名，之后是 `user.id` 的十进制串；旧 token 仍能过校验走 `uid=null` 兼容分支）。⚠️ **修掉三个「以为上线了其实没上线」**：① 升 bcrypt cost 不是换密码，却复用了 `update_password()` → 顺带 `token_version+1` 踢人 + `password_changed_at=now` 续期 90 天，新增 `update_password_hash_only()` 只动一列；② 「改密后旧 token 失效」此前只在管理端改密时生效、**员工自助改密不生效**（后端压根没比对），在 `core/identity.py` 补 `X-Token-Version` 比对（**头缺不拦**：dev 裸跑没这个头，11c 前的旧 token 有最长 12h 窗口，不该在升级瞬间被无收益地踢下线）；③ `create_user` 漏传 `password_changed_at` → 新号一登录就报 `expired`，加 `or now_db()` 兜底 + 断言守着（测试写错，但暴露了真实风险）。⚠️ **「IP 取 XFF 第一段」是路由层的职责**，不是服务层的 —— 第一版把断言下在 `svc.login` 上，越界了。**13d 预留的 `auth.login.success/failure/logout` 三个动作在这一版第一次真被用到**（含 `actor_user_id` 与客户端 IP）。新增 `tests/test_module13_login.py`（**71/0** 连跑两遍，含 2 条反向验证）；module11 141/0、module12_admin 75/0、module12_audit 64/0 全绿，`compare_metadata` diff=0，真链路 curl 四条路径（成功 / 密码错 / 不存在 / 离职）+ 浏览器侧边栏显示 `chen.jie`、提问发送成功、控制台 0 错误。测试库与手工验证数据均已还原（种子 10 人、失败计数全 0、密码还原为随机值、12 条手工登录审计已清） | `iterations/v2.0.0-p2.11c-gateway-mysql-auth.md` |
| **v2.0.0-p2.13d** | 2026-10-07 | **审计日志（P2-13d）**：新增 `audit_log` 表（迁移 `0005`）与 `core/audit_repo.py`（`record`/`list_logs`/`count_logs`/`describe`），**只追加、不可改不可删** —— 三道结构性约束：仓储层**没有** update/delete 入口、**表里没有 `update_time` 也没有 `deleted_at`**（「顺手改一下 / 先软删」在结构上无处落笔）、`assert_detail_is_safe()` 深度遍历拒绝敏感键（password/token/secret…）与 bcrypt 特征值（`$2a$`/`$2b$`/`$2y$`）—— 后一条是因为键名拦得住 `{password:x}` 拦不住 `{note:"密码是 $2b$12$..."}`。**「不可删」的边界写在明处**：应用层之外无强制，任何有 DB 写权限的人都能 `DROP TABLE`；要真不可篡改得另起独立审计库 / WORM 归档，代价见迭代文档 §3.5，本轮明确不做。12 类写操作全部落审计（`user.create`/`profile.update`/`status.change`/`role.change`/`password.reset`/`password.must_change` + 部门职位各三类），`detail` 只记「从什么变成什么」且**无变化不落审计**（一条明细为空的记录会让人以为真改过，几天后没人信这条日志，审计就整体失效）。`Actor` 加 `client_ip`，优先取 `X-Forwarded-For` 第一段（后端在网关后面，`client.host` 全是 127.0.0.1）—— 前提「8000 不对外可达」写在代码注释里而非文档里，因为改代码的人不一定读文档。⚠️ **审计失败 fail-open 不阻断业务**：业务与审计不同事务，改成「审计失败则回滚」等于让只读的历史表变成业务单点（审计表满了就没人能改员工资料）；代价是「操作成功但审计没落库」当场看不出来，只在日志里有一行 error。⚠️ **登录类审计（`auth.login.*`）动作名已预留但没埋点** —— 登录真相源还在 `.env` 的 `GATEWAY_USERS`、网关不查 MySQL，没有 `user_id` 可写，等 11c。前端新增只读「审计日志」页。⚠️ **浏览器交叉验证抓到跨端契约 bug 并加断言钉住**：① 后端统一写 `{"from","to"}`，前端按 `detail.status`/`detail.role` 读 → 明细列整列 `—`（后端全绿、接口 200、页面上像「这条日志没明细」）—— 修法不只是改前端，而是加**契约断言**把 `action → detail 必需键` 钉成表、并**正扫 `AuditView.vue` 源码**确认前端引用的动作全在服务端白名单内；② 13b 的**静默数据污染**：列表接口脱敏 → 编辑弹窗拿 `138****0001` 当当前值 → 管理员不改手机号直接保存把脱敏串写回库（合法字符串，任何校验都拦不住），修法是详情端点给原值（`raw_phone=True`）+ 前端 `openEdit` 先拉详情，列表仍脱敏。测试组顺序也修了一处：手机号回归组原本排在清理组**之后**，而它要读的那行已被清理删掉。新增 `tests/test_module12_audit.py`（**64/0** 连跑两遍，11 组判据偏不变量，含 2 条反向验证：拆 `_audit` → 零留痕、拆 `_FORBIDDEN_KEYS` → 明文真能写进去）。module12 75/0、module11 141/0、`make test` 全绿、`compare_metadata` diff=0、构建通过、浏览器 8 行 + 动作筛选 + 关键词筛选（控制台 0 错误）、XFF 第一段取 IP 实测 `198.51.100.7` | `iterations/v2.0.0-p2.13d-audit-log.md` |
| **v2.0.0-p2.13a** | 2026-10-07 | **管理端脚手架（P2-13a）**：新增独立前端应用 `admin-console/`（Vue3 + TS + Vite，端口 5174 + `strictPort`，`make admin` / `make admin-build`），**不新增后端进程** —— 复用现有 FastAPI 的 `/api/v1/admin/*`（4C4G 预算，PLAN §11.1）。后端新增 `core/identity.py`（`Actor` + `resolve_actor` + `current_actor` / `require_staff` / `require_admin` 三个依赖，dev / gateway 两种信任模式）+ `api/routes/admin.py`（`/me` 与 `/options`），新增配置 `IDENTITY_MODE` / `IDENTITY_DEV_USERNAME`。**它是 12b 后端那一半的提前借用**：管理端每个动作都要知道操作者，否则「hr 不能重置密码」这类规矩一条都落不下去，而缺的就是那种**不报错**的权限漏洞。新增 `tests/test_module12_admin.py`（31/0，连跑两遍），含**反向验证**（把 `require_staff` 换成「谁都放行」→ 普通员工那两条 403 必须转 200）。⚠️ **顺带修掉第四处迁移往返数据损失**：`acceptance_p2_11a.py` 的 downgrade 会把 0003 新增的**列连同数据** DROP 掉，upgrade 回来只有默认值 —— 实测发现种子员工被冲成「人人 role=user / status=active」（管理员变普通员工、离职的人复活），而旧断言只盯着「列在不在 / 行数变没变」，一条都没红。改为往返前后**逐列比对 19 个业务列**的快照。⚠️ 存量数据的损害不可逆，本轮用 `--apply --reset` **重建种子**修复（种子数据，无外部引用） | `iterations/v2.0.0-p2.13a-admin-console.md` |
| **v2.0.0-p2.11a** | 2026-10-06 | **组织与账号表（P2-11a）**：`user` 从 6 列扩到 23 列（工号/邮箱/手机/性别/部门/职位/角色/状态 + 密码策略三项 + 吊销与审计六项，新增 4 个索引与唯一键），**下线 `is_active` 布尔列**（表达不了三态且无人读，权威字段改为 `status`）；新增 `department`（自关联树）与 `position`（职级挂在职位上）两张表；新增 `core/user_repo.py`（只做存储原语，判定规则留给 11b）；迁移 `0003` 可 up/down。module11 **73/0**、验收 **24/0**（均连跑两遍），`compare_metadata` diff=0，存量 35 文档/90 切片/1 会话/12 消息零损失。**反向验证**两条：摘掉 `set_status` 取值校验 → 3 条转红；`schema.py` 列注释改错一字符 → `modify_comment` 精确报错。⚠️ 本轮结束时 `user` 表**零行**、登录仍走 `.env` —— 别把「用户表建好了」理解成「多用户能用」 | `iterations/v2.0.0-p2.11a-user-org-schema.md` |

| **v2.0.0-p2.13bc** | 2026-10-07 | **员工 CRUD + 密码管理（P2-13b + 13c，合并交付——两个任务物理上拆不开，理由见迭代文档 §3.1）**：新增 `core/admin_service.py`（业务编排层，**路由只收参、写操作全部收口在服务层**——13d 审计只需在这一层加一行）+ `api/routes/admin.py` 由 2 条扩到 13 条路由。**13b**：员工 CRUD（新建签发一次性临时密码；停用/复职/离职是状态不是删行；改角色、防「最后一个管理员」自锁；唯一键冲突按键名翻成人话「登录名 xxx 已被使用」）+ 部门/职位维护（防环双层：后端拒绝上级=自己或子孙、前端下拉直接不渲染）。**13c**：重置密码（明文只出现一次，无「指定密码」接口——管理员不该知道员工密码）、强制改密开关（**新增 `repo.set_must_change_password()` 只改一列**——第一版复用 `update_password` 被断言抓到「给旧密码续了 90 天 + 踢人下线」）、到期看板五桶**互斥**（锁定→待改密→已过期→即将到期→长期未登录，前端不重算，测试用 SQL 逐桶对账；只统计在职）。前端补齐三页：员工（筛选+防抖+临时密码弹窗）/ 部门与职位 / 密码看板（卡片即筛选器）。⚠️ **浏览器验收抓到 13a 两个登录闭环 bug 并修掉**：① 登录成功不跳转（`LoginView` 没写 `router.replace`，守卫同步补 `?redirect=`）；② `auth.login()` 里 `fetchProfile()` 写在 `setSession()` 之前 → `/me` 裸请求 401 → 所有人都报「账号已停用或不存在」——两条 curl 各自都是通的，只有浏览器串起来才现形。`test_module12_admin.py` 由 31 扩到 **73/0**（连跑两遍 + 与 module11 故意并行复跑）；收尾断言从「全表=10 行」改为「种子名单逐个还在」（并行写者不再打翻它）。`make test` 纳入 module12。module11 141/0、构建通过、真链路 curl + 浏览器三页截图（控制台 0 错误）、user=10/document=35 零损失 | `iterations/v2.0.0-p2.13bc-employee-crud-and-passwords.md` |

| **v2.0.0-p2.12c** | 2026-10-08 | **会话隔离（P2-12c，③阶段完成）**：「登 A 看 A 的问答、登 B 看 B 的问答」**真的成立**。12b 封的是「冒充」（不能改请求头自称别人），12c 封的才是「看到别人的」。**归属判据下沉到存储层**：`SessionStore` 的 `load`/`save`/`delete`/`exists`/`list_ids` 全部带 `owner_id`，前三个**必填**（漏传立刻 `TypeError`，而不是静默返回全部）—— 放接口层的代价是「每新增一个后端就要在接口层重写一遍归属判定，漏写一处就是一次泄漏」。**新增第6 个契约方法 `is_foreign_to()`**：`load`/`exists`/`list_ids` 都把「不存在」与「不属于我」压成同一个 `None`（**对逻辑层没区别**），但用户拍板越权要报 **403** 而非 404，于是要额外问一次存储层「到底存不存在、是不是别人的」。**`owner_id=None` 的语义是「只认无主会话」不是「不过滤」**（用户拍板）—— 代价是 break-glass 超管（`.env` 的 `admin`）登进去看到空列表，**这是故意的**：一旦存在「能看全部」的取值，任何一次「这里是不是该用 all」的判断失误都不留痕迹。**无主会话（`user_id IS NULL`）认领给第一个打开它的人**，所以查询谓词必须带 `OR user_id IS NULL` —— 这一支是**功能性的**：12c 之前建的会话全是 NULL，若无主会话对所有人不可见，用户第一次打开自己的旧会话时 `load` 返回 `None`，逻辑层会从零重建并**覆盖掉原内容**（那不是隔离，是删数据）。`save()` 三态（`FOR UPDATE` 事务内判归属再写，不存在「写到一半才拒绝」）：库里没有 → 新建；`user_id IS NULL` → 认领；属于别人 → 抛 `SessionOwnershipError`，**一个字都不写**。⚠️ **用异常而不是返回 `False`**：返回 `None` 与「不存在」无法区分（只能二选一地骗客户端：404把「别人的」说成「不存在」，200+空把「越权」说成「没数据」），返回 `False` 极易被 `if not ok: return` 吞掉，异常让越权**必须**被显式处理（漏了变 500，测试立刻红）。⚠️ **`/ask` 与 `/ask/stream` 的越权预检放在调链路之前** —— 否则模型已经完整答完一遍、检索也跑完了才告诉用户这轮问的不该问（白花真金白银的 token 与一次检索）；流式那条必须在**生成器启动前**判，因为 `StreamingResponse` 一旦返回 200，状态码永远改不了。⚠️ **归属刻意不进 `SessionSnapshot`**（内存版存并行字典 `_owners`）—— 快照是业务交换格式，每个调用方都有机会改写它，塞进去等于允许调用方给自己发通行证。⚠️ **`stats()["session_count"]` 保持「全部会话数」**（运维口径，与「谁看得见什么」无关），要看某用户的会话数用 `list_ids(owner_id=...)`。前端**零改动**（已核实：会话不落 localStorage、`sessions.ts` 的 `resetAll()` 早已挂在 `App.vue` 的登出 watch 上）。⚠️ **不需要新迁移**：`session.user_id` 列与 `idx_session_user_active` 索引在 **P0-1b 就已建好**，只是一直为 NULL（当时按「会话属于某个用户」建表，结果认证晚了两格才上线），且起始时 `session`/`chat_message` 均 0 行、无存量迁移负担。**反向验证踩了两个坑并写进文件头**：① 第一版只拆`save` 的越权抛错 → 主测试**全绿**（`save` 是第二道防线，接口层预检先挡住了）→ 改为**两道一起拆**，并断言「其他路由仍绿」那才是「每条路由各自有守卫」的证据；② 桩链写成「一被调用就抛`AssertionError`」→预检被拆后 TestClient 把异常重抛 → **整个测试文件崩掉**（崩 ≠ 红）→ 改为 `_BenignChain`返回 200，稳稳地红。测试适配 5 个文件共 113 处补 `owner_id`（一次性脚本 `scripts/_patch_owner_id.py` 用完即删）。新增 `tests/test_module15_session_isolation.py`（**48/0**，5 组；`write_as()` 刻意走**存储层**而非 `/ask` —— 12c 要验的是「记在谁名下」，而 `/ask` 会把「embedding 维度不匹配」这类环境问题混进失败原因）+ `test_module15_session_isolation_reverse.py`（**23/0**，4 种拆法）+ `acceptance_p2_12c.py`（**真链路 32/0**，两个**平级**员工真登录真提问，刻意不用 admin —— 用 admin 会让「他权限更高所以能看到」成为合理解释，而要证明的恰是**权限再高也看不到别人的问答**）+ **`acceptance_p2_12c_ui.mjs`（真浏览器 28/0，连跑两遍，6 张截图）**：**同一浏览器 context 内不刷新页面换账号**（两个 context 等于两个浏览器，A 的内存与 B 无关，验不到 Pinia 泄漏），只桩掉 `ask/stream` 返回体的 `chunk.content`、**放行真请求**（整个 mock 掉的话后端就不建会话，没东西可验），`session_id` 从**响应首帧**取而非「列表差集」（实测：界面上已有当前会话时前端**会续用**而不是新建，差集法判成「提问失败」，假红一路传染）。⚠️ **浏览器验收第一版自身有两处缺陷**（都已写进迭代文档 §5.5/§5.6）：按可见文字找「登出」按钮（实际是**纯图标 + aria-label**，红的原因看起来像「登出功能坏了」）、`finally` 里收尾三步顺序裸写（崩在第一步 `cleanupSessions` → **`restorePwd` 根本没跑**，两个账号密码留在库里成了探针值）→ 改为收尾各步独立 `try` 且**收尾失败不盖掉主流程已打出的断言**。⚠️ **如实记录一次未复现的漂移**：全量回归两遍 module15 是 48 与 47（统计只数 `[PASS]`，吞了一条 `[FAIL]`），单独跑 6 次 + 复现批次条件 2 次全部 48/0，查库确认 `session`/`chat_message` 残留 0 行 —— **不下结论**，留作待办 | `iterations/v2.0.0-p2.12c-session-isolation.md` |
| **v2.0.0-p2.14df** | 2026-10-07 | **前端入口收敛（14d）+ 管理端下发授权的前端（14f）**：14a 把后端锁住了、14b 加了网关门，但**界面上那些能点的东西还在** —— 14e 实测基线：`chen.jie`（`kb_role=none`）的知识库页有 1 个 file input、10 个含「删除」在内的按钮、35 份文档全可见，**与管理员看到的完全一样**。那不是「权限在生效」，那是「权限在事后惩罚人」：他拖完文件、点完删除，才吃到一个 403。**新增后端 `GET /api/v1/documents/capabilities`**：把 `kb_acl.capabilities()` 的判定**如实**告诉调用者本人，判据全部后端派生、端点内零 if —— 前端**不自己抄一份五档表**（那是 13d 与 14f 在同一个月里踩过两次的「两份清单各自漂」，漂了不报错，表现为「运营有权限却看不到上传按钮」）。**主应用**：上传区/只读说明卡二选一、删除与重试按钮按档位**不渲染**、🔴 **`onDrop` 自己判权限**（拖放挂在整个 section 上，「上传区不渲染」不妨碍「拖到空白处照样上传」—— 本项唯一一个改了界面反而更容易漏的地方，界面验收看不出来，只有监听网络请求才抓得到）、空态文案分两支、store 侧 `?? false` **fail-closed** + 三个写操作各有守卫 + `reset()` 清权限。**管理端**：「知识库写权限」一列徽章 + 行内下拉（选项来自 `/options` 下发的 `short_label`，**由长标签派生**而非另写一份——21 个汉字的长标签会把表格撑到横向溢出、连带把左边几列压成竖排单字）+ 二次确认文案说清「改完他将能干什么」与「知识库是全公司共用的」+ 审计落 `user.kb_role.change`。**顺手修了两个真缺陷**：① 14e 的依赖提取器**会扫注释**（`Depends(require_kb_*)` 出现在 docstring 里被当成真依赖，害得「读路由不该挂守卫」转红）→ 改走 `tokenize` 剥注释并补 4 条提取器自检；② 验收脚本的拖拽对照组**真的上传了 probe.txt** → 加 `route.abort()`，并修掉由此引出的两条恒红/恒真判据。 |
| **v2.0.0-p2.15a** | 2026-10-07 | **Token 额度的聚合与口径（P2-15a）**：用户 2026-10-07 拍板「超出 token 预警，不要做真的阻断或者禁用，只做提醒和看板」，所以本版**先把口径定死**再写代码 —— 口径一旦定错，后面三格（写路径 / 触达 / 越权回归）全部要跟着改。**口径**：按人 × 自然月，来源是 `chat_message` 明细 join `session` 取 `user_id` —— 否掉「直接查 `session` 汇总列」，因为会话被删汇总行一起没了（「他这个月用得少」），而明细是历史真相；计数用 `m.create_time` 而非 `s.create_time`（上月开的会话本月聊 10 次，那 10 次属于本月）；**`cache_read` 单独一列不计入 total**（服务商侧 prompt 缓存，单价不同，混进总额会算错钱，而「算错钱」比「算得粗」严重：它会让「谁超了」这个判断本身失真）；**无额度时 `usage_percent` 返回 `None` 而不是 0.0**（「不管他」与「额度是 0」是两件事）；`0` 而不是 `NULL` 表示「用全局默认」（两边同一套编码，少一个双判据分支 —— 14f 的教训）。**新增** `core/quota_policy.py`（纯函数判定，刻意**没有**「是否允许继续提问」这个函数 —— 一旦有了它，配上前端就会自然长出「禁用」按钮，而那正是用户排除的行为）与 `core/quota_repo.py`（SQL 聚合）+ 迁移 `0007`（`user.token_quota_monthly` BIGINT NOT NULL DEFAULT 0 + 索引，`compare_metadata()` diff=0）。**🔴 抓到三个 bug**（都是代码/口径错，不是断言写错）：① `usage_by_session` 取 cache_read 写 `r2[0][0]` 而 `_query` 返回 dict 列表 → `KeyError: 0`，**这条路径从来没跑通过**；② 🔴 **cache_read 被乘上消息条数**（777 → 2331）—— 「子查询按 `s2.id` 分组再 join」看起来先聚合了，但 **`session.id` 是主键**，按它分组等于没分组，外层 `SUM` 仍在消息粒度累加。否掉 `SUM(DISTINCT)`（两个会话恰好同值时会漏掉一个，而「两人用量相同」很常见），改用 **CTE 先把消息按会话收敛成一行**；③ `requests` 一个字段两个口径（聚合 `COUNT(*)` 给 3、单会话 `role='assistant'` 给 2）→ 统一为 **assistant 消息数**。**🔴 判据「在当前数据下无从验起」比bug 更危险**：② 能活下来是因为库里唯一cache_read 非零的会话**恰好是那条 orphan**（主人已被手工删），而 orphan 按定义不在看板里 → 写成 PASS 是自欺、写成 FAIL 会让人去改**对的东西**。正解是**造临时样本**（会话 + 3 条消息挂在职员工名下，`finally` 删干净），且样本 `create_time` **显式写在本月内**（用 `NOW()` 会导致跨月运行恒红）、断言一律用**增量**（先量基线 —— 直接断言绝对值时该员工自己的真实用量会让 777 变 2331，**探针是对的、断言错了**）。**反向验证整组重写**：上一版写的是「改了值会不会变」，那验的是「那行代码被执行了」，**不是**「那行代码被断言守着」（若断言写成 `requests > 0` 而非等于 assistant 条数，拆掉 role 过滤后它仍绿，而「值会变」抓不到）；改为「拆掉实现 → 受影响的断言必须转红」，5 条扩到 **9 条**。顺带改掉一条**不成立的注释论据**：「`<= 最后一秒` 会漏掉微秒」—— 实测本库所有时间列都是秒级 `datetime`，该论据今天不成立，已换成真实理由（左闭右开让本月与下月首尾相接，将来改 `DATETIME(6)` 时本模块不用动）。**验证** `test_module16_quota.py` 64/0（`--reverse`）/ 55/0，连跑两遍；module11 141/0、module12_admin 79/0、module12_kb_role 47/0、module12_write_acl 177/0；库回基线（`session` 3 / `chat_message` 30 / `document` 35 / 探针残留 0） | `iterations/v2.0.0-p2.15a-token-quota-aggregation.md` |
| **v2.0.0-p2.15b** | 2026-10-08 | **Token 额度的写路径与管理端界面（P2-15b）**：管理员现在能在管理端给每个人设月度 token 额度（行内输入 + 二次确认 + 审计），确认弹窗当面说清「**只做提醒，不做阻断**」—— 用户 2026-10-07 拍板的口径，从判据层（15a）到接口层到界面层全部一致。🔴 **改额度刻意不 bump token_version**（与 role/kb_role/status/password 四条写路径相反）：额度不是权限、不进 JWT、且本项只提醒不阻断 —— 它在生效路径上根本不被读，「+1」是纯粹的成本（管理员改一个数字，员工正在写的对话当场 401，而额度他连界面都看不到）。🔴 **允许改自己的额度**（与 set_kb_role 相反）：额度改小不会锁死任何人，最坏结果是自己看到一条横幅；用一个不成立的危害去禁止一个无害的操作只会逼出荒唐后果。🔴 **布尔在请求体层拦**：第一版把 isinstance(raw,bool) 拦截写在服务层，实测 {quota_monthly: true} 成功写入了 1 —— Pydantic 的 int 字段在反序列化时就把 true 转成 1，根本到不了服务层，服务层那个分支是**永远不执行的死代码**；修法是 TokenQuotaBody 加 field_validator(mode='before')，同时删掉死代码。否掉 StrictInt（连该收的字符串一起拒）与 NonNegativeInt（负数留在服务层才能给「0 表示不限」上下文）。审计记**生效额度**与 default_quota（只记 0 的话三个月后没人知道全局默认当时是多少）、刻意不记 token_version（记一个永不变的值会误导）。🔴 **两个测试坑**：① audit_repo.ACTION_LABELS 白名单没登记新动作名时，_audit 的 fail-open 让业务照常完成而审计静默丢失（漏登记不报错）—— 顺序应当是先登记动作名再写业务；② 测试清理要**按动作删、不按 target_id 删**（本组动了两个人），反向组跑在第 5 组 finally 之后所以每个反向验证**自己收尾**，浏览器脚本落下的审计同样清掉 —— 终检（全库额度 0 + 无审计残留）当场抓到了浏览器脚本的 8 条残留。反向 10 的判据第一版写成自相矛盾的恒假式（a and not a），永远红 —— 修成两半分别成立（行为确实变了 + 原判据会红）。**验证** test_module16_quota.py 87/0（含 reverse）连跑两遍；verify-15b-quota-ui.cjs 15/0 真浏览器；module11/12_admin/12_kb_role/12_write_acl/12_audit 全 0 失败；库回基线（额度 0/审计 0/探针 0） | `iterations/v2.0.0-p2.15b-token-quota-write-path.md` |
| **v2.0.0-p2.15c** | 2026-10-08 | **超额行为档位定死（P2-15c）**：计划书 §15c 的三档（仅提醒/降级/硬阻断）按用户拍板（「不要做真的阻断或者禁用，只做提醒和看板」）收窄为**一档** —— 另两档刻意不存在、连空钩子都不留（空钩子比没有钩子更坏：调用方以为有开关可拨）。`OVER_ACTIONS` 唯一合法值 notify + `validate_over_action()`；🔴 配错值**启动直接失败**（fail-closed）—— 把 block 静默当 notify 的后果是「管理员以为配了阻断，实际什么都没发生」，两种静默都不可接受，宁可起不来。新增结构断言：quota_policy 里不许长出 can_*/allow_*/should_* 形状的函数（有「是否允许继续提问」就会长出禁用按钮 —— 15a 的刻意缺席从此有测试守着）。反向 13 踩一脚：恢复原函数后又调 validate('block') 想验证「会抛」，ValueError 逃出 check() 炸掉整个脚本 —— 抛异常的验证要在 try/except 里做。**验证** 97/0 连跑两遍；notify 正常启动、block 拒启均实测 | `iterations/v2.0.0-p2.15c-over-action.md` |
| **v2.0.0-p2.15de** | 2026-10-08 | **用量看板 + 额度横幅（P2-15d）+ 越权收口（15e）—— P2-15 收官**：管理端新增「用量看板」页（按人 × 本月，输入/输出/缓存命中三数分开，>80% 标黄、>100% 标红，🔴 **底部自带对账行**：全库=按人+未归属的恒等式直接显示 ✓/⚠，漏算自己站出来）；主应用顶部新增提醒横幅（🔴 **文案由后端 banner_text() 给、前端只显示不自己拼**；🔴 **拉不到就静默不显示** —— 横幅是提醒不是关键路径，fail-open 在这里是对的，与鉴权 fail-closed 方向相反；不做轮询）。`GET /admin/usage/board`（require_admin）+ `GET /qa/quota/me`（current_actor）——🔴 **后者响应里没有「还能不能问」字段**（15c 的刻意缺席在端点层同样成立：端点顺手返回 can_ask 的话，前端早晚拿它渲染禁用按钮；要让「不会阻断」稳定成立，每层都不能给那扇门装把手）。15e 三条收口：跨用户改额度 403（网关层真 HTTP 验 FORBIDDEN_PATH）、改动落审计（15b 已验）、用量聚合只统计自己（/quota/me 断言 user_id 过滤 + 无 can_ask 字段）。⚠️ module11 在全量回归时炸出 TypeError（15b 加 UserRecord 字段漏了它的测试夹具，当时 141/0 没跑到那条路径）—— 修夹具后恢复 141/0，记踩坑 95（加字段这种横切变更要全局搜构造点，不是靠跑回归兜底；TypeError 停在夹具构造 = 整组测试消失，比红几条更难被当成回归信号）。**验证** test_module16_quota.py 107/0（含 15 条反向验证）连跑两遍；verify-15d-quota-ui.cjs 12/0 真 DOM 双端；库回基线 | `iterations/v2.0.0-p2.15de-usage-board-banner.md` |
| **v2.0.0-p2.16abc** | 2026-10-08 | **体验打磨三件（P2-16）**：① 主应用左下角用户区可点击 → 个人信息面板（基本资料 + 本月用量 + 历史总用量，数字全后端算好）；🔴 **全局默认额度 0→100,000 tokens/月**（用户拍板给固定用量，推翻 15a 的「故意留 0」；依据实测本月人均 5,368，100K≈18 倍余量）；② 管理端换锅圈食汇 logo（与主应用同源）；③ 管理端 15 处原生 select 全量替换为自定义 `AppSelect` —— 🔴 **箭头贴边与面板遮挡的根因是原生 select 的浏览器默认渲染**（箭头是浏览器画的、面板是浏览器 UI，CSS 都管不了），自绘组件 + padding-right 26px + 面板 fixed 定位在触发框下方 + Teleport 到 body 防滚动裁剪；OrgView 的父部门选项保留「排除自己与子孙」的防环拦截。历史总用量口径=现存明细累计（会话删除会让它变小，面板文案写明）。**验证** verify-16abc.cjs 17/0 真 DOM 双端（含 C3 面板 top≥按钮 bottom 的几何断言）；test_module16_quota.py 107/0 连跑两遍（两条写死全局默认=0 的断言改为与 settings 一致） | `iterations/v2.0.0-p2.16abc-ux-polish.md` |
| **v2.0.0-p2.17** | 2026-10-08 | **管理端组件库 + 列表/分页规范 + 自助改密（P2-17/18，用户反馈五条）**：① 引入 **Element Plus（按需导入）**，15 处自研下拉全部替换为 el-select，筛选区改横向排布（上一版每个下拉独占一行）；② **「列表滚动、分页固定页底」统一规则**（.list-page 套四个列表页，分页永远可见）；③ 员工列表**真分页**（后端 offset + count_users 同条件真总数 —— 原 total 是被 limit 截断的假值）；④ 密码看板新增**「全部人员」视图**（后端 all_people 全部在职 —— 并集方案被实测否掉：9 个在职并集只 6 人，健康的人不在任何桶里）；⑤ 问答端面板**自助修改密码**（验旧密码 + 11b 全套策略 + 过期用户放行 + token_version+1）；🔴 **module16 收尾自动恢复固定密码并打印可登录账号表**（用户要求每轮跑完都有可登录清单）。**验证** verify-17-18.cjs 15/0 真 DOM 双端（含分页在视口内的断言、改密全流程）；test_module16_quota.py 107/0 连跑两遍；库回基线 | `iterations/v2.0.0-p2.17-18.md` |
| **v2.0.0-p2.19** | 2026-10-08 | **UI 反馈第二批（P2-19）**：筛选区改 el-form inline 横排 + placeholder 语义化（部门/角色/状态）；🔴 **色差根因 = CSS 导入顺序**（EP dark 变量覆盖了我们的对齐）→ 调顺序 + fill 色全量对齐 + select/分页/overlay 样式覆盖；🔴 **EP 对 null 显示 placeholder** → 「全部/—」改 '' 哨兵（提交转 null）；密码看板「全部人员」移到第一张卡且默认选中、分页贴页底；改密表单加**动态规则提示**（数字后端下发）。**验证** verify-19.cjs 13/0（含背景色 computed style 断言与规则提示文案断言）；107/0 连跑两遍 | `iterations/v2.0.0-p2.19.md` |｜ **§7 修订（p2.19b）**：审计日志筛选区是漏改的一页（上一轮只改了员工页）—— 补 el-form inline + placeholder（全部动作/全部对象），并全局排查补齐 9 处缺 placeholder 的下拉（含编辑弹窗与行内）；审计页验收 5/0。 ｜ **§7 修订（p2.20）**：个人中心改 WorkBuddy 式底部锚定菜单（菜单层收拢系统设置/文件传输/修改密码，退出登录在底部、外侧入口保留）；改密表单加占位文本 + **新密码实时校验**（逐条显示规则满足情况，判据唯一处仍在后端）；规则提示带示例格式；管理端 favicon 换锅圈 logo。验收 13/0。 ｜ **§7 修订（p2.21）**：个人中心菜单图标换回侧边栏原线性 SVG、宽度调大；修改密码拆成独立弹窗；🔴 **封装通用二次确认组件（两端）**并全员替换 —— 审计发现部门/职位的删除当时没有确认、知识库删除用的是 window.confirm；探针用例验证「确认后真的删掉」（不只测取消不删）；退出登录加二次确认；登录页删 admin/admin123 提示；标题全线改「锅圈RAG 智能问答系统」。验收 21/0。 ｜ **§7 修订二（p2.21c）**：按用户新立的文案规则（提示文案不得出现客户端/服务端/前端/后端等术语，通俗说明白）全局排查并改 8 处：退出确认、部门/职位删除、改状态（去 token_version）、临时密码提示（两处）、管理端登录页脚注、设置页「重启后端」、知识库删除确认；管理端 toast 右上角 → 中间顶部（居中方案从 translateX 改 margin auto，前者实测被页面规则覆盖）。验收 7/0。 
| **v2.0.0-p2.22** | 2026-10-09 | **服务器首部署与联调修复日（计划外运维迭代，总结文档见迭代文件）**：腾讯云轻量 4C4G（Ubuntu 24.04）首次真实部署全链路打通。🔴 **真 bug 三个**：① `frontend/nginx.conf` 删除 `proxy_request_buffering off` —— 请求侧流式转发与网关 Node 流处理存在**随机竞态**，multipart body 约半数丢失（后端收到空表单 → 422 files missing）；与文件名/大小/请求头全部无关，「中文文件名失败」是单样本假相关（踩坑 50 变体），抓包三跳计数才定案；问答流式只依赖响应侧 `proxy_buffering off`，此行删除零损失。② `Dockerfile` 依赖安装结构性修复：国内网络下 pip 串行装 lock 三连败（PyPI 超时/镜像回源截断/JSONDecodeError）→ **torch 独立成层**（装完即缓存，重试成本 45min→分钟级）+ **uv 并行安装**（内置重试）+ 腾讯内网 index + 清华兜底。③ `sessions.ts` 新增 `uuid4Hex()` —— `crypto.randomUUID` 只在安全上下文存在，`http://IP:8080` 验收常态下「新建会话」直接 TypeError，降级 `getRandomValues` 手拼 v4。**环境陷阱五个**：macOS tar 的 AppleDouble `._*.py` 被 alembic 当迁移（打包固化 `COPYFILE_DISABLE=1`）；models 软链接死链（空目录占位）；Docker Hub 不可达（腾讯内网 registry-mirror）；`.env` 里 bcrypt `$` 被 compose 插值吃掉（写 `$$`）；`GATEWAY_ALLOWED_ORIGINS` 不在 example 需追加。**设计行为确认三个**：网关 fail-closed 两连（JWT secret / GATEWAY_USERS）、`role` 与 `kb_role` 独立授权维度（SQL 授权 + 重新登录换 JWT）、检索阈值短路拒答（Q2「年假多少天」未到生成 LLM，Q3 会话改写命中）。新增 `scripts/clean_orphan_chunks.py`（删除与解析并发竞态残留的孤儿切片，干跑默认）；新增 `docs/部署手把手-备案前先跑起来.md`。**检索可观测缺口立项 P2-23（下一迭代，高优先级）** | `iterations/v2.0.0-p2.22-server-deploy-day.md` |
| **v2.0.0-p2.14f0** | 2026-10-10 | **修一个静默失效的鉴权缺陷：`Expect: 100-continue` 让网关完全不注入身份头（14f0 unplanned）**：本项不在计划书里，是做 14f 时的真链路探测撞出来的。根因在 `http-proxy@1.18.1` 的 `web-incoming.js`：`proxyReq.on('socket', function(socket){ if (server && !proxyReq.getHeader('expect')) { server.emit('proxyReq', ...) } })` —— **客户端带 `Expect` 时这个事件根本不触发**，而 12b 把「无条件剥离客户端伪造的身份头」与「注入真实身份」**全都**放在了这个回调里。后果两条：① 真实身份没注入 → 后端在 dev 模式回落 break-glass 超管（`kb_role=none`）→ `require_kb_upload` 判拒 403，gateway 生产模式则是缺 `X-Internal-Auth` 全站 401；② **剥离也没执行** → 客户端自带的 `X-User-Id` 原样穿透到后端，dev 模式下等于伪造身份直接生效，**正是 12b 当年量化过的那条洞换了入口**。⚠️ **为什么能活这么久**：浏览器 `fetch`/`XHR` 默认**不发** `Expect`，而 12b 量化伪造头穿透时用的正是不带 `Expect` 的请求 —— **两个条件从未同时满足过**，而这一格没人测。**修法**：新增 `stampIdentityOnInbound(req)`，把剥离/注入/摘 `Expect` 从 `on.proxyReq` 挪到**转发之前**，改的是**入站** `req.headers`（`setupOutgoing` 第 43 行 `outgoing.headers = extend({}, req.headers)` 整个复制过去 → 效果一致但**不依赖那个可能不触发的事件**），两个 handler 各加一行调用；顺带**摘掉 `Expect`**（逐跳头，RFC 7231 §5.1.1；摘掉之后事件才触发，`fixRequestBody` 与 `Accept-Encoding: identity` 才有地方执行，否则带 `Expect` 的请求会同时失去这两件事）。**新增 `gateway/scripts/probe-expect-header-drop.cjs`**（真实 http-proxy-middleware@3.0.7 + 真实 http 服务，**八场景对比**「旧写法/新写法」×「带/不带 Expect」×「带/不带伪造头」，含 G 行 = **旧写法下伪造身份真的穿透**，下游收到 `440/wu.jing/admin`）+ **`tests/test_module14_expect_header.py`（34/0）三层断言**：真链路（跑脚本读它8 条结论，**不是只看退出码** —— 退出码只说「有失败」不说「哪条失败」）+ 源码结构（注入不许挪回 `on.proxyReq`，用**大括号配对**切回调体而非正则匹配到下一个 `},`）+ 反向验证（`--reverse`，41/0）。⚠️ **node 缺失时必须红不能静默跳过** ——「跑不起来就当过了」是验收脚本里最常见的自欺。**四条反向验证全部转红**：挪回 `on.proxyReq`→5 条 / 删摘 Expect→1 条 / 剥离包进 `if`→1 条 / 剥离改置空串→3 条，每次 `finally` 还原 + 逐字节比对 + 重跑确认全绿。**真链路双路交叉**：curl 上传带 `Expect` 从 **403 `FORBIDDEN_KB_WRITE` → 202 + `doc_id=720`**，带 `Expect` 的 DELETE → 200 `record_removed:true`；浏览器（`gateway/scripts/verify-14f-browser.cjs`，真实登录两个账号 + 页面上下文 `fetch`）四条写全放行到业务层（202/422/404/200，**没有一条 403**）。**审计表佐证**比日志更硬：`actor=chen.jie` 而非 break-glass 的 `admin`。伪造头覆写实测：`-H 'X-User-Id: 440'`（真管理员 id）+ `Expect`，后端收到的仍是 `442 / chen.jie`。顺手改掉 `test_module14_trust_boundary.py` 一条断言（「剥离走的是 `removeHeader`」→ 按**意图**判「让头缺失而非置空串」）—— **它原来验的是 API 名不是意图**，换写法就红。回归module11 141/0、module12_admin 79/0、module12_audit 64/0、module12_write_acl 172/0、module13_login 71/0、module14 103/0、module15 48/0，护栏跑前跑后均 **35/35**，临时回显路由与 `[TEMP-DEBUG]` 日志**已全部撤掉**（两个文件无 diff）。⚠️ **定位过程原样留在迭代文档 §7 当反面教材**：我一路怀疑 multipart（甚至把 `fixRequestBody`/语句顺序/`JwtStrategy`/`decidePath` 逐个排除），而真相是**我自己加的 `Expect` 头**。教训：**加对照实验时一次只改一个变量**，我拿「带 Expect 的multipart」对比「不带 Expect 的 GET」——改了两个变量，结论必然不可靠 | `iterations/v2.0.0-p2.14f0-fix-expect-header-drop.md` |
| **v2.0.0-p2.23** | 2026-10-09 | **检索可观测性：拒答轨迹 + NDJSON 调试事件（P2-23，PLAN §13）**：消除 p2.22 验收发现的观测盲点——检索候选被阈值全过滤时 `重排完成` 日志分数区间退化为 [0,0]，原始 top1 不可见，「拒答差多少分」无从回答。① `core/retriever.py`：`CrossEncoderReranker`/`SiliconFlowReranker` 在阈值过滤**前**记录原始 top1 与被过滤条数，写进 `重排完成` 日志行（`过滤前top1=... 阈值过滤=...`）与实例 `last_trace` 轨迹（候选清单含被过滤者、`passed_threshold` 逐条标记）；`RAGRetriever.get_last_rerank_trace()` 暴露读取口。② `core/rag_chain.py`：query/stream 两处拒答分支新增结构化日志 `拒答 | 原因=检索为空 | 改写后查询=... | top1分数=... | 阈值=...`（取不到时如实写「不可得」，不编 0.0）；`stream()` 加 `debug` 参数，debug 时在 meta 后 chunk 前下发 `retrieval` 调试帧（改写后查询/意图/候选清单含未过阈值者/阈值/过滤前 top1/过滤条数）。③ `api/routes/qa.py`：`AskRequest` 加可选 `debug`，语义「显式 > 默认」（不传 → admin 默认开、其余关），不传 debug 的调用方事件序列逐字节不变；前端因此无需传 debug（角色判定唯一真相源在后端）。④ 前端：`MessageBubble.vue` 「引用资料」旁新增「检索详情」折叠面板（改写查询/意图/阈值/top1 与差值/候选 ✓✗ 清单/汇总行），🔴 拒答气泡（sources 空）同样可见；`types.ts` 加 `RetrievalDebug` 与 `retrieval` 帧类型，`sessions.ts` 捕获该帧。⑤ 新增 `tests/test_module5_retrieval_observability.py` **59/0**（含 `_candidate_brief` 与 `_resolve_chunk_id` 同构断言、全过滤 top1 不丢失、debug=False 零可见变化、拒答场景事件+日志双验）；沙箱回归 module5 全绿；kb-guard-count 35/35/90 三处一致；`npm run build` 通过。**不做的**：调参（等差分数据，PLAN §13 第 3 条）、检索详情持久化（刷新后面板消失是已声明边界）、同步 `/ask` 不加调试事件 | `iterations/v2.0.0-p2.23-retrieval-observability.md` |
| **v2.0.0-p2.14e** | 2026-10-08 | **越权回归 + 🔴 结构断言（14e）**：14a 挂守卫、14b 加网关门，但**两件事都没有回归** —— 14a 的验证是人工枚举四条路由做的，只能证明「今天这四条对了」。本项要堵三个洞：① **将来新增第五条写路由而忘挂守卫** → 矩阵断言照样全绿，而那一条是**裸的**（14.0 原话：「漏一个路由就完全没有防护，而且不会有任何报错提示」）；② **守卫挂上了但拒权路径本身抛异常** → 判定是对的、用户拿到的是 500；③ **后端路由与网关粗筛各写一份、靠人同步**（14b 遗留）。**新增 `tests/test_module12_write_acl.py`（172/0，连跑三遍）+ `tests/test_module12_write_acl_reverse.py`（7 条拆法 / 28 项全成立）**，`make test` 已纳入。矩阵刻意成对**（module15 同一条道理：「`none` 被拒」在守卫没挂时也会绿，所以每组都配一条「有权限的档确实能过这道依赖」）。**第 2 组结构断言是本项存在的核心理由**：扫 `documents.py` 的装饰器块（**不是逐行正则** —— 跨行写装饰器时逐行正则会漏掉半个路由，而漏掉就等于没检查），逐条核对写路由的 `Depends` 里有没有 `require_kb_*`、挂的守卫与语义对不对上、条数与 14a 认定值一致、**读路由不得挂守卫**、守卫必须在 `Depends` 而非函数体（函数体里再判一次就成了两个真相源）。**判据是「从源码发现写路由」，不是「枚举已知路由」** —— 后者对第五条完全隐形。2g 交叉核对网关 `KB_WRITE_ROUTES`（**双向都查 + 条数相等**，任一侧增减都抓）。**第 3 组独立于第1 组**：直接调 `require_kb_*` 依赖函数断「拒权必须 403 而非 500」+ 「文案不含档位名」（防枚举，理由同 11b）。⚠️ **期望矩阵手写不从 `kb_acl` 派生** —— 派生就成「实现等于它自己」，恒真零判别力。真链路不用「上传一个临时文件」验「该放的放」：那会在 `upload/` 与 `document` 留垃圾（14a 已因此栽过两次），改断 `capabilities()` 与 `Actor.can_*` —— 纯函数、零副作用、判别力更强。**反向验证 7 条全部抓到了**：含「加一条没挂守卫的新写路由 → 2a/2b/2c 三条同红」（矩阵断言对它是隐形的）与「拆 `delete` 守卫 → 2c 条数那条**仍绿**，因为它只管「多出来」」。⚠️ **真链路探测顺手修掉一个真 bug**：`core/identity.py` 里「token_version 不匹配」的 `logger.warning` 写在 `raise` 的**下一行** —— Python 先抛异常，那行是**死代码**，于是「不匹配被拒」与「匹配但放行」打的是同一句话。实测实录：`持有=3 库里=3`、请求照常 200，却打了一句「已失效」。**一条与事实相反的日志比没有日志更糟**（排障时会先信它），而且完全静默（功能测试全绿、接口 200、无报错）。修法是warn 挪到 `raise` 之前，并加断言 **2h** 看住（判据是源码里 warn 与 raise 的**行号顺序** —— 「日志内容对不对」跑一百遍也测不出来）。真链路双证（curl + 浏览器）：`chen.jie`(`none`) 四条写**403 `FORBIDDEN_KB_WRITE` ×4**、读 200；`wu.jing`(`superadmin`) 放行到业务层（422/404/200）。两端 `system/health` 均为 `2.0.0-p2.14b`（之前停在 `p2.14a`，是进程启动时读的旧 `.env`，**重启才对齐**）。module11 141/0、module12_admin 79/0、module12_audit 64/0、module14 103/0、module15 48/0。`make kb-guard-count` 跑前跑后均 **35/35**，探针零残留。⚠️ **14d 的基线已量到**（本项不做）：浏览器实测 `chen.jie`(`none`) 知识库页 `file input=1`、含「删除」按钮 10 个、35 份文档全可见 —— 上传区与删除/重试对**所有人完全一样**，正是 14d 要收的。⚠️ **踩了两次「尺子坏了」**：2g 第一版硬编码 `method: 'post'`（网关写的是大写 + `\/` 转义）→ 四条全假红；且 `backend_to_gw` 键写成元组，`for method, pattern in d` 解包出两个字符串、`method[0]` 取到首字母 → 断言名印出「D」而非「DELETE」。两次都像「被测物坏了」而真问题在尺子 —— 修法是解析出四元组再比对 | `iterations/v2.0.0-p2.14e-write-acl-regression.md` |
| **v2.0.0-p2.14b** | 2026-10-08 | **知识库写权限：网关那道门（14b）**：14a 把后端锁住了，但请求**仍然进到后端**才被拒 —— 那一跳的数据库往返、白占的连接、以及「本来进不来却进来了」这件事本身都该消掉。`kb_role` 进 JWT（六档含 `unknown` 降级目标），`decidePath()` 对四条写路径做粗筛。⚠️ **粗筛刻意只判 `none`**（其余全放行去问后端）—— 12b 踩过反面教材：网关比后端严会让后端那条分支**永远走不到**，症状是「只有某个角色能用」。具体代价：`ops` 会被网关放去打后端然后被 `reindex` 拒（误拒一次），而漏放代价是全公司知识库被改。⚠️ **`x-user-kb-role` 只剥不注入**：后端从 MySQL 实时读 `kb_role`（14a 实测改授权立刻生效），再注入一个 JWT 快照就成**第二个真相源** —— 权限系统里那意味着「在某些时刻它就是错的，且不报错」。代价是「降级授权」后网关有最长 12h 滞后（**后端仍拒**，粗筛只是省一次往返）。`decidePath()` 签名从 `role: string` 改成**收整个 user 对象 + method**：传两个字符串的话加第三个维度时少传一个**不会编译报错**，只表现为「那道门忘了看这个字段」。`method` 设为可选（漏传→放行给后端判，粗筛漏了不致命）。**方法必须参与判定** —— `GET` 与 `DELETE` 共用 `/documents/{id}`，只看路径会让 `kb_role=none` 的人**连文档都看不到**，而共用是刻意的业务决策。把 `kbRole` 定成**字面联合**而不是 `string` 是本轮最省事的决定：tsc 编译时**自动抓到三处联合类型漏改**（`BreakglassSuccess` / `LoginResult.user` 等），写成 `string` 这三处全编得过。测试 **47 → 103**（新增第 4b 组，判据全是**不变量**：读接口对所有档放行 / 四条写接口对 `none`·`unknown`·缺失全拒 / 其余四档全放），`--reverse` 107/0。真链路双证：`none` → 网关 **403 `FORBIDDEN_KB_WRITE`** ×4，且**后端日志里那四条完全没有记录**（同一时刻的 `GET /` 有）→ 证明请求没进后端；读接口 200×2；超管经网关 202+200。module11 141/0、module12_admin 79/0、module12_audit 64/0、module14 103/0、module15 48/0。知识库 35 篇 / 35 文件零损失 | `iterations/v2.0.0-p2.14b-gateway-kb-gate.md` |
| **v2.0.0-p2.14a** | 2026-10-08 | **知识库写权限：模型 + 守卫（14a + 14c）**：「谁能改全公司共用的那个知识库」此前**完全没有答案** —— 实测基线：普通员工 `chen.jie` 上传返回 **202 + doc_id=587**、删除返回 **`record_removed:true`**，因为 `documents.py` 的**四条写路由一条身份依赖都没有**（对照：12c 给 `qa.py` 的 7 个路由全挂了）。所以本项不是「加一层校验」而是**「从零补守卫」**，失败模式是漏一条就完全没有防护且不报错。**新增正交维度 `user.kb_role`五档**（迁移 `0006`，默认 `none`）+ **`core/kb_acl.py` 判定唯一处**（纯函数不起库，理由同 `core/password_policy.py`）。⚠️ **刻意不塞进 `user.role`** —— 它三档管「能不能进管理端」（判据 `STAFF_ROLES`/`is_admin()`），加值会让管理端权限**一起漂**（11b踩过的「一处改处处受影响」）；且表达不了 `role=user + kb_role=ops` 这个合法组合。四条写路由逐条挂 `Depends(require_kb_upload/delete)` —— **判定必须在 `Depends` 里而不是函数体里**，因为 14e 的结构断言靠的就是这张列表。**权限判定放后端、网关那道门留给 14b**（D15：网关要比后端**更宽**不能更窄）。`reindex` 单独一档且 `ops` 刻意不给 —— 它是**唯一会让全公司答错**的操作（改切分参数 = 整库重建，`chunk_id` 是派生值）。新增 `core/kb_service.py` 落三条审计（`document.upload`/`delete`/`reparse`，`target_type="document"`），**同名替换掉的那份旧文档也记审计**（用户没点删除却发现文档没了，日志要答得上来）。**break-glass 超管的 `kb_role` 给 `none` 而非 `superadmin`** —— 它不在库里，没人给它授权过；它要做管理端操作靠的是 `role=admin`，不依赖 `kb_role`。真要改知识库该往库里补一行并授权。module11 **141/0**、module12_admin 79/0、module12_audit 64/0、module14 47/0、module15 48/0，`compare_metadata` diff=0（**当场抓到两处列注释漂移**），迁移 up/down 往返干净，真链路双向实测（`none`→403×4 / `superadmin`→202×3 + 200×2 + 审计 5 条）。**反向验证**：拆掉守卫→普通员工立刻能上传（202），而没拆的 delete 仍 403（证明「部分拆」可检出）。⚠️ **修掉一个「判定对了但响应是 500」的 bug**：`kb_acl.describe()` 参数名写成 `kb_role` 而函数体用 `action` → `NameError` → 拒权路径返回 500。教训：**「拒权路径本身要能正常返回 403」是独立于「权限判得对不对」的断言**，14e 必须含。存量知识库 **35 篇 / 35 文件 / 90 切片零损失**，探针已清干净 | `iterations/v2.0.0-p2.14a-kb-write-acl.md` |
| **v2.0.0-p2.12b** | 2026-10-08 | **身份与信任边界（P2-12b，②阶段完成）**：把「谁能声称自己是谁」从「谁先问到就听谁的」改成「必须经过网关、且网关证明过了才算」。**网关侧**：**无条件剥离** 8 个入站身份头（11c 的写法是条件式`if (incoming.user)`，而白名单路径上守卫不跑、`incoming.user` 是 undefined → 客户端伪造头原样穿透），之后才注入真实身份 + `X-Internal-Auth`；`decidePath()` 做路径级粗筛（`/api/v1/admin/*` 要 `admin|hr`，`/api/v1/internal/*` 对**所有身份**报 **404** —— 12b 之前它经网关是**可达的**，唯一防线是共享密钥）。**后端侧**：`verify_gateway_proof()` 用 `hmac.compare_digest`，`gateway` 模式下**验过才认身份头**，缺/错一律 401；探活增`identity` 段（部署当天一眼看出边界有没有生效，**不**回显密钥本身）。⚠️ **实测推翻了规格的一条「选做」**：§5.1.3 把共享密钥列为「选做（纵深）」，依据是「8000 不对外（compose 里backend 不映射端口）」—— 而基线探测实测`curl -H 'X-User-Id: 440' localhost:8000/api/v1/admin/users` **返回 200 + 完整员工名单**（440 是真管理员 `wu.jing` 的 id），因为① 本机 uvicorn 绑 127.0.0.1、**同机任何进程都能连**（「对外」在裸跑形态下不成立）② `.env` 默认 `IDENTITY_MODE=dev`、无头回落到 break-glass 超管。**拓扑是一道依赖部署形态的防线，代码里没有任何东西会察觉它断了** → 征询用户后升为必做。⚠️ **代价写进代码注释而不是文档**：密钥要下发两处（漏配的症状是「登录正常但所有数据接口 401」，所以网关侧生产缺密钥直接启动失败、后端侧探活单独报 `gateway_proof_configured`）、轮换要同步改两处（不是静默降级而是全站 401，**失败得很响是好事**）、**防不了已拿到密钥的人**（密钥进 env 就等于进 `docker inspect`）、**dev 模式刻意不验证明**（否则裸跑时每条 curl 都要先造证明头；它是已知的本机后门，探活的 `trust_enforced` 就是给部署当天看的）。⚠️ **网关的粗筛必须比后端更宽不是更严**：只放 `admin` 会让 hr 在网关被拒而后端那条分支永远走不到，表现为**只有 hr 账号现形**的「他为什么进不去管理端」。⚠️ **剥离清单里有两个「不注入但要剥」的头**：`x-dept-id`（11c的 JWT 还没有 `dept_id`，剥离可提前、注入要等真值）与 `x-role` —— 后者是**规格自己漂了**（持续三个版本：§5.1.2 与 `probe-header-strip.cjs` 写 `X-Role`，11c 实际注入 `X-User-Role`），两个都剥代价为零，而只剥一个就等于给另一个留后门（12d 就会读 role）。⚠️ **修一处「以为修好了其实没修」**：`make test` 第一遍 module12_admin **74/1**（12b 加了证明校验，13a 的旧测试没带 `X-Internal-Auth`），第一反应是把断言改成 `== 401` —— **那是最坏的处理**（那条断言想验的是「合法链路仍然通」，改成 401 就成了恒真的假绿，第 50 条）；改为让 `act_as()` 默认带正确证明（`_AUTO_PROOF` 哨兵）+ 显式传空/错值来验 fail-closed，并补一条前置断言「`.env` 必须配了密钥」（否则「正确证明 → 200」会因为拿不到密钥而假红）。module12 **79/0**。新增 `tests/test_module14_trust_boundary.py`（**47/0**，9组，判据全是「读源码」与不变量 —— 12b 要防的失效**全部不报错**；TS 侧用「Python 等价重实现 + 正扫 .ts 源码」交叉验证，漂移会表现为「逻辑组全绿但源码组红」）+ `tests/test_module14_trust_boundary_reverse.py`（**13 项符合预期 / 0 项不符合**，判据是「拆掉守卫后断言必须转红」；**它自己出错时也必须可辨** —— 崩在import 与断言转红长得不一样，故加一条「主测试没崩」的断言区分）+ `tests/acceptance_p2_12b.py`（**真链路 25/0**，连跑三遍；补 module14 结构上补不了的第三件事：真进程 + 真 token + 真 HTTP）。⚠️ **两条都不进 `make test`，理由不同**：reverse 会临时改生产源码（混进去会让「回归失败」与「反向验证改坏源码」不可分辨），accept 要求两个进程已起着（`make test` 的契约是离线可跑）。⚠️ **三个一次性验证脚本全部删除**（按 11c 惯例不进仓），其中 `verify_p2_12b.sh` 还有个真缺陷：`kill` 8000/3000 时把**调用方 shell 一起 SIGTERM（exit 137）**。`make test` **1265/0** 连跑两遍（=11c 的 1214 + module12 的 +4 + module14 的 47，两条独立路径对上）；gateway `tsc --noEmit` 通过。🔴 **红线未解除**：12c 会话隔离未做，所有人看到的仍是同一份会话与同一个知识库，**账号仍只能本机自用** | `iterations/v2.0.0-p2.12b-trust-boundary.md` |

| **CI/CD 自动化发布** | 2026-10-10 | **部署自动化上线（基础设施，不改变应用版本）**：GitHub Actions self-hosted runner 装在生产服务器，监听 `push main` → 服务器拉取 + 执行 `scripts/deploy.sh`（中间件健康 → alembic 迁移 → 全栈 `--build` → 探活 `http://127.0.0.1:8080/api/health`）；首部署 `ce58a53` 成功，push 即自动部署。详见 `docs/CI-CD自动化发布方案.md` |

> 当前应用版本：`2.0.0-p2.23`
>
> 🚀 **CI/CD 自动部署已上线（2026-10-10）**：`push main` → 服务器 self-hosted runner 自动拉取 + 执行 `scripts/deploy.sh` 重建全栈并探活，无需手动登录服务器。首部署 commit `ce58a53` 验证成功。详见 `docs/CI-CD自动化发布方案.md`。
>
> 📌 **计划修订（2026-10-08，非代码变更）**：用户决策**「所有人共用一个企业知识库，
> 知识库不隔离，只是会话隔离」**。据此 `docs/PLAN-v2.0.0.md` 已改：
> **P2-12d跨部门检索隔离 ❌ 取消**、12a/12e 缩范围、**P2-14 从第五优先级升为唯一待办**
> （读不管了，写成了唯一的知识库防线）、**新增 P2-15 Token 额度与预警**（可并行，无前置依赖）。
> 任务格 **14 → 15**。详见 `PLAN-v2.0.0.md` §8.1与 §8 P2-15。
> 🔴 **红线未解除**：会话隔离已交付，但**任何人仍能上传 / 删除 / 重灌全公司共用的知识库**。
>
> 📌 **用户另两个决策（同日）**：
> ① **P2-15 超额「只提醒，不阻断、不禁用」** → 原设计的三档里后两档**明确不做**（不是默认关掉）；
> ② **P2-15 预警只做管理端看板 + 界面顶部横幅**，邮件/企微/钉钉**明确不做**。
> 📌 **知识库写权限分四类**（用户拍板）：**运营人员/ 测试 / 开发 / 超管**，
> **本项目当前只启用「超管」一档**，其余只建判定逻辑、**不发给任何人**。
> ⚠️ **配套实测发现（比计划书原先假设的更严重）**：普通员工 `chen.jie` 现在就能
> **上传**（202 + `doc_id`）与**删除**（`record_removed:true`）——
> `api/routes/documents.py` 的**四条写路由都没挂任何身份依赖**。
> 所以 P2-14 不是「加一层校验」而是「从零补整个守卫」，
> 而**从零补的失败模式是「漏一个路由就完全没有防护」** → 必须逐条枚举 + 结构断言兜住。
> 详见 `PLAN-v2.0.0.md` §14.0 的枚举表。
>
> 上表是**工程对账**口径（谁在哪个文件里改了什么）。
> 如果是要**向人展示「这个项目怎么一步步完善的」**，读 `docs/RELEASES.md`。

> ⚠️ **`make test` 的总项数不是常量** —— 它取决于当时有没有 worker 在跑。
> module9 第 10 组「真 worker 端到端」在探测不到 worker 时会 `[SKIP]`：
>
> | 条件 | module9 | 合计 |
> |------|---------|------|
> | **2026-10-08 实测（`p2.12c`，无 worker 在应答）** | 167 通过 / 0 失败（第 10 组 SKIP） | **1376** |
> | 有 worker 在应答 | 未实测 | 未实测 |
>
> **1376 是实测数，且两条独立路径对上了**：
> ① 逐模块数 `[PASS]` 行 = **1362**，加 `test_qa_cache.py` 的 **14** 项
>    （它用 `ok` 格式打印，不计入 `[PASS]`，必须单列—— 这条最容易漏）；
> ② 从 12b 的 1265 加「新增 module15 的 48 + module6 的 +42 + module8 的 +21」= **1376**。
> 后两项正是 `store_contract.py` 新增的 17 条归属契约带来的 ——
> **归属断言放进共享契约，任何实现少实现一条语义，module6/7/8 一起红。**
>
> ⚠️ **统计口径本身会骗人，本轮就踩了一次**：`grep -cE "^\s*\[PASS\]"` 在 **BSD grep 下
> `\s` 不匹配**（与坑46 同形），数出来是 **0**。正确写法是 `[[:space:]]`。
> ⚠️ 有 worker 那一行本轮没测—— 按项目铁律，没实测的数字不写具体值，所以**不写「+6」**。
> ⚠️ 旧记录（806 / 812 / 1214 / 1265）是 `p0.4c` / `p2.11c` / `p2.12b` 时期的口径，
> **已被本表取代** —— 留着旧数是因为它同时也是「基线从 719/725 抬到 806/812，差 87 = module10」
> 与「1265 → 1376，差 111 = module15 的 48 + 归属契约的 63」那段推理的依据。
>
> **各模块当前通过数（2026-10-08 `p2.12c` 实测）**：module2 118 / module3 102 / module4 85 /
> module5 55 / **module6 172（12c 前 130，归属契约 +42）** / module7 43 /
> **module8 83（12c 前 62，归属契约 +21）** / module9 167 / module10 87 /
> module11 141 / module12_admin 79 / module12_audit 64 / module13_login 71 /
> module14_trust_boundary 47 / **module15_session_isolation 48（新增）** / qa_cache 14
> （module1 是 smoke script，不打印计数）。
>
> ⚠️ **module12_admin 从 75/74 变成 79 是 12b 的 +4**（不是回归漂移）：
> 12b 给后端加了网关证明校验，而13a 写的旧测试没带`X-Internal-Auth`，
> 于是「gateway 模式下正常链路不受影响」那条红了。修法是让 `act_as()` 默认带
> **正确**的证明，另加两条显式传空/错值的 fail-closed 断言 ——
> **不改成 `== 401`**（那条断言想验的是「合法链路仍然通」，改了就成恒真的假绿）。
> 详见 `iterations/v2.0.0-p2.12b-trust-boundary.md` §4.2。
>
> ⚠️ **module6 +42 / module8 +21 也是加法，不是回归漂移**：
> `tests/store_contract.py` 由「一组断言」拆成「基础 + **归属 17 条**」，
> 而 module6（内存 + Redis）与 module8（MySQL）都跑这份共享契约。
> **这是刻意的** —— 归属语义只写一份，三个实现都跑；
> 哪一步少实现一条（比如 Redis 版 `save()` 的三态漏了 `is not None`）就会一起红。
>
> **p0.4c 本轮口径**：按用户指示**未跑 module9**，逐个跑 module2~8、10 合计
> **666 通过 / 0 失败**（module2 118 / module3 100 / module4 85 / module5 44 /
> module6 127（含新增 14 条）/ module7 43 / module8 62 / module10 87；
> module1 是日志系统演示脚本，无计数汇总行）。
> **666 不与 806/812 基线直接可比**，差额约 146 项即 module9 的贡献。
> 恢复 `make test` 全量口径须补跑 module9，并留意它会全表清空 `document`（见下方遗留项）。

### 2.0.0 整体进度

> ⚠️ **计划书已于 2026-10-06 修订到第 4 版**（第 3 版增补用户权限与管理端：新增 P2-11 / P2-12 / P2-13，
> 并反转第 2 版的决策 D2「网关账号不迁 MySQL」；第 4 版同日第三轮新增 **P2-14 知识库写权限分发**与
> 四阶段验收里程碑，重排 P2 执行顺序）。下表是**第 4 版**口径，
> 修订原因、编号对照与两条记录缺口的对账见 `PLAN-v2.0.0.md` §0.6 / §0.7。
> 第 2 版修订（会话真相源 Redis → MySQL）见 §0.1~§0.5。

| 分组 | 任务 | 状态 |
|------|------|------|
| P0 上线硬前提 | P0-1 业务数据落 MySQL ✅、P0-2 鉴权网关 ✅、P0-3 异步解析 ✅、P0-4 向量元数据对齐 ✅（`p0.4a` 后端 + `p0.4b` 前端 + `p0.4c` 修错位） | ✅ **4 / 4** |
| P1 容器化与部署 | P1-5 Docker 化 ✅（5a + 5b + `p1.5c`）、P1-6 远程模型 ✅（+`p1.6b/c/d/e` 不占格）、P1-7 TLS | 🔄 2 / 3 |
| P2 生产化打磨 | P2-8 配置治理、P2-9 生产构建+备案号、P2-10 观测备份 | ⬜ 0 / 3 |
| **P2 用户体系（第 3 版新增）** | P2-11 用户账号与密码策略 ✅（11a 表 · 11b 密码规则 · 11d 种子员工 · **11c 网关查库登录**）、P2-12 多租户数据隔离 🔄（**12b 身份与信任边界 ✅** / **12c 会话隔离 ← 下一步**）、P2-13 管理端 ✅（13a 脚手架 + 13b 员工 CRUD · 13c 密码管理 · 13d 审计日志） | 🔄 **1 / 3** |
| **P2 知识库写权限（第 4 版新增）** | P2-14 知识库写权限分发（上传 / 删除 / 重灌按 `project` 授权） | ⬜ **0 / 1** |

> **2.0.0 大版本合计：P0-1 ✅、P0-2 ✅、P0-3 ✅（3a 后端 + 3b 前端）、P0-4 ✅（`p0.4a` 后端 + `p0.4b` 前端 + `p0.4c` 修复）、
> P1-5 ✅（5a + 5b + 5c）、P1-6 ✅（模型走硅基流动）、**P2-11 ✅**（11a + 11b + 11c + 11d）、**P2-13 ✅**（13a + 13bc + 13d），
> 其余未开始。完成 9 / 15。**
> ⚠️ **P2-12 是 5 个子项，整格完成才 +1** —— 所以 12c 交付后进度仍是 **9/15**，
> 尽管子版本数已经从 11c 时的 21 涨到 23。按**计划格**计数，不是按子版本计数。
> 📌 **2026-10-08 修订**：知识库决定**不隔离读**（业务需求），**P2-12d 取消**、12a/12e 缩范围、
> **P2-14 升为唯一待办**；**新增 P2-15 Token 额度与预警** → 任务格 **14 → 15**。
>
> 🔴 **红线未解除**：`p2.12c` 会话隔离**已交付**（每个人的问答记录只属于自己），
> 但 **P2-14 知识库写权限还没做** —— 此刻**任何人**都能上传 / 删除 / 重灌全公司共用的知识库。
> 「能分人登录」与「数据分人」之间那道缝还开着，**账号仍然只能本机自用**（计划书 §0.7.2）。
> 在 12e 越权回归全绿之前不要交给第二个人。
>
> **下一步 = ③ 阶段唯一一格 P2-12c（会话隔离）**：`session.user_id` 落库，
> 列表 / 历史 / 删除 / 重命名全部按 uid 过滤。
> **12b 封的是「冒充」，12c 封的是「看到别人的」—— 两者不能互相替代**：
> 12b 做完之后，每个人都能确认「我就是我」，但看到的还是同一份数据。
>
> 🎉 **P0 阶段已全部完成，阶段 tag `v2.0.0-p0.4` 已打**（与本次交付 tag `v2.0.0-p0.4b` 指向同一个 commit）。
> 至此「上线硬前提」四项全部就位：数据落库、鉴权、异步解析、引用可反查可点击。
>
> **执行节奏 = 计划书 §0.7.1 的四阶段里程碑**（每阶段都能亲手验）：
> ① 11a/b/d + 13a/b/c ✅ → ② **11c + 12b ✅（本轮完成）** → **③ 12c ← 下一步**
> → ④ 12a + 12d + 12e → ⑤ P2-14。**「能不能对外开」的判据见 §0.7.2**（②③④ 全绿之前账号只能本机自用）。
> **P1-7 TLS** 等备案放行；**P2-12 数据隔离**可与 TLS 并行。
> ⚠️ **当前仍处于「假隔离」中间态**（多账号能登录、身份不能伪造，但所有人仍看到全部会话与文档），
> 该中间态必须在 11c/12b/12c/12d 的迭代文档里显式标注。
> 🔴 **红线（计划书 §0.7.2）**：`11c` 与 `12e` 之间**不允许出现「已交给第二个人用」的时间窗** ——
> 内部可以分步验，但 12e 越权回归全绿之前账号只能自己在本机用。
> 执行顺序：P1-5a ✅ → P0-1 ✅ → P0-3a ✅ → P0-3b ✅ → P0-4a ✅ → P0-4b ✅ → P0-4c ✅ → P1-5b ✅ → P1-5c ✅ → **P1-6 ✅** → **P1-6b ✅**（会话与精确缓存，不占计划格）→ **P1-6c ✅**（流式停止与滚动跟随，不占计划格）→ **P1-6d ✅**（锅圈知识库重灌，不占计划格）→ **P1-6e ✅**（答案首帧前导空行，不占计划格）→ **P1-7 / ① P2-11a/b/d + P2-13a/b/c ✅ → ② 11c + 12b ✅ → ③ 12c → ④ 12a + 12d + 12e → ⑤ P2-14 / P2-8~P2-10**。
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
| 2026-10-06 | 计划书修订 | **计划书修订到第 3 版**：新增 P2-11 用户账号与密码策略 / P2-12 多租户数据隔离 / P2-13 管理端（admin-console），进度格 10 → 13；**反转第 2 版决策 D2**（网关账号由 `.env` 迁到 MySQL `user` 表，`.env` 只留 1 个 break-glass 超管）；新增决策 D6~D11 待拍板；验收闸门加两条「多账号隔离」硬门槛 | 计划书 §0.6；设计规格 `docs/设计规格-用户权限与管理端.md` |
| 2026-10-06 | 计划书修订 | **计划书修订到第 4 版**（同日第三轮，**按 §12 约定不新开版本号**）：新增 **P2-14 知识库写权限分发**（上传 / 删除 / 重灌索引按 `project` 授权，`reindex` 仅超管），进度格 13 → 14；重排 P2 执行顺序为「① 11a/b/d + 13a/b/c → ② 11c + 12b → ③ 12c → ④ 12a + 12d + 12e → ⑤ P2-14」；新增 **D13**（写权限按 `project` 授，不加全局 `kb_admin` 角色）；验收闸门加第 11 条「写权限生效」；新增 §0.7.1 四阶段验收里程碑 + §0.7.2 上线红线 | 计划书 §0.7；设计规格 §7 / §8.2 |
| 2026-10-08 | 计划书修订 | **计划书修订到第 5 版**（**按 §12 约定不新开版本号**）：⚠️ **实测推翻第 4 版的两处地基** ——① **`document` 四条写路由一条守卫都没有**（`documents.py` 的 upload / upload-batch / reparse / delete 全无 `Depends(current_actor)`），普通员工 `chen.jie` 实测上传返回 **202**、删除返回 **`record_removed:true`**，所以 P2-14 不是「加一层校验」而是**「从零补守卫」**，失败模式是**漏一条路由就完全没有防护且不报错**（P2-14 新增 §14.0 实测基线 + 14e结构断言）② **库里从来没有 `project` 表**（实测只有 10 张表，无 `project` / `user_project`），D13「按 `project` 授`user_project.role`」**作废** ——共用决策之后不存在「多个库」，这列权限无处安放。**新增 D14**（正交维度 `user.kb_role ∈ {none,ops,qa,dev,superadmin}`，默认 `none`，判定唯一处 `core/kb_acl.py`，`reindex` 只给 `superadmin`；**刻意不塞进 `user.role`** —— 它三档管「能不能进管理端」，加值会让管理端权限一起漂）+ **D15**（网关粗筛要比后端更宽不能更窄；`kb_role` 进 JWT，老 token 按 `none` fail-closed）。**新增 P2-15 Token 额度与预警**（进度格 14 → 15）；**§P2-14 整段重写为六格14a~14f**；**验收闸门第 9 条（跨部门检索隔离）作废**、第 11 条按 `kb_role` 五档重写；红线末端从「12d+12e」改为「**P2-14**」 | 计划书 §0.7.5 / §8.1 / §P2-14 / §11.3；设计规格 §7 / §8（待同步） |
| 2026-10-06 | `2.0.0-p1.6e` | **对账发现一条记录欠账**：`v2.0.0-p1.6d`（2026-09-28，锅圈知识库重灌 + logo + 307 修复）**四项记录全缺** —— 无迭代文档、CHANGELOG / RELEASES / 计划书均未入账。本轮只补一行事实，**不事后补写迭代文档**（事后编的取舍记录比缺失更坏）。另：`p1.5c` 记录齐全，仅计划书漏记，已补 | 计划书 §0.6.3 |
| 2026-10-07 | `2.0.0-p2.13d` | **跨端 detail 契约没有任何类型检查（唯一的真 bug）**：后端对状态/角色/改密开关统一写 `{"from","to"}`，前端按 `detail.status` / `detail.role` 读 → `from` 恒为 `undefined`，审计页**明细列整列显示 `—`**。后端全绿、接口 200、前端不报错，页面上完全像「这条日志没有明细」——纯后端断言永远发现不了。修法**不只是改前端**：加契约断言把 `action → detail 必需键` 钉成表、并正扫 `AuditView.vue` 源码确认前端引用的动作全在服务端白名单内（都是字符串，没有类型保护）。断言数量 61 → 64 | p2.13d 文档 §4.1 / §7 |
| 2026-10-07 | `2.0.0-p2.13d` | **测试组顺序写反**：手机号回归组原本排在「清理与零残留」**之后**，而它要读的那行临时员工已被清理删掉。表现不是报错，而是 `repo.get()` 返回 `None` → 断言拿着 `None` 静默判 False，让人去查一个不存在的 bug（第 49 条坑的形状）。改为 10=回归 / 11=清理，并给断言加 `is not None` 保护 | p2.13d 文档 §7 |
| 2026-10-07 | `2.0.0-p2.13d` | **测试自己的残留会让下一轮直接撞唯一键**：上一轮在抛异常前已建了 `m13d_newbie`，收尾清理那组永远跑不到 → 本轮开头 `create_user` 报「登录名 m13d_newbie 已被使用」，而真正原因不是有人抢名字，是上一轮的垃圾。测试开头补「先清本模块残留」（`user` / `user_password_history` / `department` / `position` / `audit_log`） | p2.13d 文档 §7 |
| 2026-10-07 | `2.0.0-p2.13d` | **`Makefile` 少一个 tab**：追加 `test_module12_audit.py` 时那行缩进丢了，`make test` 报权限错（`Permission denied`）。这类错只在真正跑 `make test` 时才现形，`py_compile` 查不出来 | p2.13d 文档 §7 |
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
