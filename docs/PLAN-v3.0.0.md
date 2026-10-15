# PLAN v3.0.0 — 额度拦截 · 下载收敛 · 检索混合化 · 大文件与进度

> 2026-10-11 六问讨论产出。涉及 rag（api/core/frontend/worker）与 admin（admin-console）两侧代码。
> 铁律不变：每任务 = 迭代文档 + tag `3.0.0-pN.M[a]` + RELEASES + CHANGELOG + `.env` PROJECT_VERSION + 本表更新状态。
> 判新旧看 `git log` 不看编号；开打前 `git tag | grep 'p3\.'` 查占用。

---

## 1. 问题裁决（六问结论速览）

| # | 问题 | 裁决 |
|---|------|------|
| Q1 | token 用量是统计而来还是专门存储？ | **专门存储**。`chat_message.usage_input_token/usage_output_token` 两列在消息写入时落库（源头是 LLM 返回的 usage，`stream_usage=True`），session 表另有四列汇总。不是事后推算。**无需再专门存储**；仅补一个月度快照表做归档审计（P3-6，可选） |
| Q2 | 额度拦截语义 | **预检 + 宽限单**：提问前查当月已用，`已用 >= 限量` → 429 拦截；`已用 < 限量` → 本轮放行照常成功，用后超限，下一问被拦。**否决「成功但提示失败」**（流式已发出内容无法撤回，内部状态与用户感知不一致） |
| Q3 | 月额度分配是否完善 | **端到端完整**（输入框 @change → PATCH `/api/v1/admin/users/{id}/token-quota` → `user.token_quota_monthly` → 下次查询即生效）。截图中 `0 + 100,000（全局默认）` 是正常态：0 = 用全局默认。无需重做；P3-1 落地后同步更新弹窗文案（删「只提醒不阻断」） |
| Q4 | 下载按钮隐藏 & 与 COS 关系 | **与 COS 无关**。下载 = FileResponse 流式读服务器磁盘 `upload/`，COS 只用于整站备份（`scripts/backup_to_cos.sh`）。隐藏不省 COS 流量，省的是**服务器带宽**（轻量机峰值带宽会挤占问答流式）。做法：ACL + 配置开关（P3-2） |
| Q5 | BM25 + RRF 混合检索 | **值得做，但先建评测集**。当前零 BM25/RRF 残留；chunk 正文完整存 Chroma，可直接做 BM25 语料。顺序：P3-3 评测集 → P3-4 混合检索，用 recall@20 / MRR@10 增益决定是否默认开启 |
| Q6 | 大文件 100~300M | **50→150M 现有流程小改可扛；300M 扛不住**（nginx 120m 先 413，FastAPI 整读内存，worker 全量切片驻留，540s 超时）。本期做到 150M + 流式写盘 + 切片进度条（P3-5）；300M+ 分片上传列为「暂不做」 |

---

## 2. 范围与进度总览（唯一权威进度）

| 任务 | 内容 | 涉及端 | 状态 | tag |
|------|------|--------|------|-----|
| P3-1 | 额度拦截：block 档 + 提问预检 + 宽限单 + 429 错误体 + 前端拦截气泡 | api/core/frontend/admin-console | ✅ 完成（2026-10-11） | `v3.0.0-p3.1` |
| P3-2 | 下载收敛：download 路由加 ACL + 配置开关 + 前端按 capabilities 显隐 | api/core/frontend | ✅ 完成（2026-10-11） | `v3.0.0-p3.2` |
| P3-3 | 检索评测集 + 评测脚本（golden set + recall/MRR/拒答正确率） | scripts/data | ✅ 完成（2026-10-12） | `v3.0.0-p3.3` |
| P3-4 | BM25 + RRF 混合检索（双路召回 → RRF 融合 → reranker 裁决不变） | core/api | ✅ 完成（2026-10-12，**A/B 不达标 → `RETRIEVAL_HYBRID` 保持 false**） | `v3.0.0-p3.4` |
| P3-5 | 上传大小定版 50M：前端 size 预检（✅ 2026-10-11 已落地，见 §3 P3-5）；超大文件本期不做 | frontend | ✅ 完成 | （随首个 3.0.0 tag 带出） |
| P3-6 | 月度用量快照表（归档审计，不改判定路径） | core/worker/alembic | ✅ 完成（2026-10-12） | `v3.0.0-p3.6` |
| P4-1 | site-mood：念安站心情收集（**计划外插入模块**）——公开提交口（per-IP 限流 3/min + 邮箱冷却 600s）+ 独立 token 查看口 + 独立表 site_mood_submission，与 RAG 业务零关联 | api/core/gateway/alembic + nianan-site 仓库 | ✅ 完成（2026-10-14；p4.2 修订：库内去重，同邮箱永久只算一条） | `v3.1.0-p4.1`/`v3.1.0-p4.2` |
| P3-7a | 检索质量路线图 §6.1 A：Enrichment 标题上下文补全（`CHUNK_MODE=title` + header_path 入库 + 读取侧原文契约）——**A/B 不达标 → 默认保持 fixed**（负结论记档） | core/config/scripts/tests | ✅ 完成（2026-10-14） | `v3.1.0-p3.7a` |
| P3-7b | 检索质量路线图 §6.2 B：PDF 表格走 MinerU（batch 流客户端 + 路由降级 + md/HTML 表格行组原子；真实链路实测 7s 通过） | core/config/.env/tests | ✅ 完成（2026-10-14） | `v3.1.0-p3.7b` |
| P3-7d | 检索质量路线图 §6.4 D：Query 增强——单轮口语化 LLM 改写（复用主答模型，新增 `QUERY_REWRITE` 开关）+ 可选 HyDE（`QUERY_HYDE` 开关，假设答案 embedding 做向量召回，BM25 仍用真实问句）；多轮指代改写此前已在 `rag_chain` 落地，本任务补单轮+HyDE。**默认双关，由 P3-3 评测集说话** | core/config/scripts/tests | ✅ 完成（2026-10-15） | `v3.1.0-p3.7d` |

依赖：P3-4 依赖 P3-3（没有评测基线不许开混合检索）；P3-1 与 P3-2/P3-5 可并行。
P4-1 独立于 P3 全部任务（无依赖；上线走同一套 develop→main 部署）。

---

## 3. 任务详情

### P3-1 额度拦截（block 档 + 宽限单语义）

**现状事实**
- `/ask`、`/ask/stream` 全程无额度判定（`api/routes/qa.py:260-304`）。
- `core/quota_policy.py:95-111` 刻意只有 `notify` 一档，无 `can_ask`；`OVER_ACTIONS = ("notify",)`，配错值启动即失败（`api/main.py:94`）。
- 用量判定唯一出处 `core/quota_policy.py`（纯函数）+ `core/quota_repo.py:234-275`（CTE 按 session 收敛，cache_read 不计总额）。
- 管理端确认弹窗与横幅文案明说「只提醒不阻断」（`quota_policy.py:252-257`、`admin-console/src/views/UsersView.vue:400-431`）。

**方案（裁决 Q2）**

1. `core/quota_policy.py`：
   - 新增纯函数 `can_ask(used: int, quota: int|None) -> tuple[bool, str]`：`quota=None` 或 `used < quota` → 放行；`used >= quota` → 拦截。不做单轮 token 预估（复杂度不值，宽限单语义天然覆盖「最后一次用超」的场景：提问时未超就放行）。
   - `OVER_ACTIONS` 扩展为 `("notify", "block")`，`validate_over_action` 同步扩展；`.env` `TOKEN_QUOTA_OVER_ACTION=block`。
   - ⚠️ 旧测试 `tests/test_module16_quota.py` 中「OVER_ACTIONS 只有 notify」「横幅文案说不会限制」的断言要随决策更新（决策变更，非回归），迭代文档 §3 必须记录推翻 P2-15「只提醒不阻断」的原因。
2. 预检点：`api/routes/qa.py` 的 `/ask` 与 `/ask/stream`，在越权预检之后、进入 RAG 链**之前**：
   - `OVER_ACTION=block` 且 `can_ask` 为 False → `raise HTTPException(429)`，错误体：
     ```json
     {"detail": {"code": "quota_exceeded", "used": 100231, "quota": 100000, "period": "2026-10", "message": "本月 token 额度已用完，请联系管理员调整。"}}
     ```
   - 流式接口在返回 StreamingResponse 之前判定（此时 200 头未发出，429 可正常返回）；判定通过后本轮不再复查（宽限单）。
   - `OVER_ACTION=notify` 保持现行为（横幅提醒，不拦）。
   - 并发竞态接受：同用户并发提问可能双双通过预检、实际略超限量——内部系统，写进迭代文档风险节即可。
3. 前端 `frontend/`：HTTP 429 + `quota_exceeded` 特化处理——拦截气泡展示「额度已用完（已用 X / 限量 Y）」，与普通错误区分；`/quota/me` 横幅在 over 档增加「继续提问将被拒绝」提示。
4. admin-console：`UsersView.vue` 确认弹窗文案改为「超出额度后该员工将无法继续提问」；`quota_policy.py:252-257` 横幅文案按档位切换。

**改动文件**：`core/quota_policy.py`、`api/routes/qa.py`、`frontend/src/stores/sessions.ts`（错误帧处理）、`frontend/` 拦截气泡组件、`admin-console/src/views/UsersView.vue`、`.env`/`.env.example`（OVER_ACTION、可能加 `QUOTA_BLOCK_HINT`）、`tests/test_module16_quota.py`。

**验收**
- 用量达到限量后 `/ask`、`/ask/stream` 均 429 且错误体含 code/used/quota；前端出现专用拦截气泡。
- 用量 99999/100000 时提问成功，回复正常计费后下一问被拦（宽限单）。
- `TOKEN_QUOTA_OVER_ACTION=notify` 时行为与现版本完全一致（回归）。
- 验收脚本连跑两遍通过；admin 改额度后不用重新登录即生效（不 bump token_version 的既有决策不变）。

---

### P3-2 下载收敛（ACL + 开关 + 显隐）

**现状事实**
- 下载链路：`frontend/src/views/KnowledgeView.vue:347` 按钮 → `GET /api/v1/documents/download?doc_id=`（`api/routes/documents.py:390-417`）→ `FileResponse` 读磁盘 `upload/`。
- **该路由无 kb ACL Depends**：网关 JWT 之后任意登录用户可下载任意文档（upload/reparse/delete 都有 ACL，唯独 download 没有）。
- 与 COS 无关：COS 仅备份（`scripts/backup_to_cos.sh`）；下载消耗服务器带宽。

**方案**
1. 后端：download 路由加 ACL（复用 `core/kb_acl.py`，判据 `can_qa` 及以上可下载；`kb_role=none` 403）。与现有「只读角色不能下载」的直觉对齐。
2. 配置开关：`DOC_DOWNLOAD_ENABLED: bool = True`（`config/settings.py` + `.env`）；为 false 时路由 404，前端按钮不渲染。
3. capabilities：`GET /documents/capabilities` 返回体增加 `can_download`（= 开关 && kb ACL），前端 `KnowledgeView.vue` 按 capabilities 渲染下载按钮（对齐 P2-14 前端判据惯例）。
4. 带宽说明写入迭代文档：隐藏的真实收益是保问答流式带宽，不是省 COS。

**改动文件**：`api/routes/documents.py`、`core/kb_acl.py`（如需新谓词）、`config/settings.py`、`.env`/`.env.example`、`api/routes/documents.py`（capabilities）、`frontend/src/views/KnowledgeView.vue`、`frontend/src/types.ts`。

**验收**：`none` 角色 403；开关关闭后按钮消失且直连接口 404；qa/dev/superadmin 正常下载；中文文件名 RFC 5987 不回归。

---

### P3-3 检索评测集 + 评测脚本（BM25 的前置，也是 P2-23 的验收工具）

**现状事实**：全项目无任何 golden set / recall / MRR / 拒答评测；现有 35 文档 **90 片**（⚠️ 原写 69 片是早期快照，2026-10-12 实测更正），人工标注量可承受。

> **✅ 2026-10-12 完成（tag `v3.0.0-p3.3`）**：`data/eval/golden.jsonl` 40 条已标注并逐条反查校验；
> `scripts/eval_retrieval.py` + `make eval-retrieval` 已就位；**基线已落盘**
> `data/eval/results/20261012-001334-baseline.json`：
> recall@5 **0.8944** / recall@10 **0.9389** / recall@20 **0.9389** / mrr@10 **0.8889** / 拒答 **10/10**。
> 详情见 `docs/iterations/v3.0.0-p3.3-eval-set.md`。

**方案**
1. 评测集 `data/eval/golden.jsonl`，40 行，每行格式：
   ```json
   {"id": "q001", "question": "试用期离职需要提前几天通知？", "relevant_chunk_ids": ["21:0"], "should_answer": true}
   ```
   构成（执行时按此配比直接标注，不再确认）：
   - 30 条库内问题：10 条专有名词/制度名精确查询、8 条编号/条款类（如「26号文」「住所承诺」）、7 条口语化改写（答案在库里但问法不同）、5 条跨文档综合；
   - 10 条库外问题（`relevant_chunk_ids: []`、`should_answer: false`）：天气、闲聊、库外领域各若干。
   - chunk_id 用真实库里的 `"<doc_id>:<chunk_index>"`，标注时用 `GET /api/v1/chunks/{chunk_id}` 反查确认内容相关。
2. 评测脚本 `scripts/eval_retrieval.py`：
   - 读取 golden → 逐条调 `RAGRetriever.retrieve()`（复用生产配置，top_k 与 candidate_k 取 .env 实际值）→ 计算 `recall@5 / recall@10 / recall@20`、`MRR@10`、库外 10 条的拒答正确率（阈值下零召回即正确）；
   - CLI：`python scripts/eval_retrieval.py --golden data/eval/golden.jsonl --tag baseline`，结果写 `data/eval/results/<timestamp>-<tag>.json` 并终端打印对比表；`--compare <file>` 与历史结果并排对比；
   - Makefile 加目标 `eval-retrieval`。
3. **先跑基线**（纯向量 + reranker 现状）落盘为 P3-4 对照，数字记入 P3-3 迭代文档。

**改动文件**：新增 `scripts/eval_retrieval.py`、`data/eval/golden.jsonl`；不改生产代码。

**验收**：`make eval-retrieval` 一键跑完出数；基线 JSON 落盘并记入迭代文档。

---

### P3-4 BM25 + RRF 混合检索（开工即执行，无需二次确认）

**现状事实**：零 BM25/RRF 残留；chunk 正文完整存 Chroma documents 字段；metadata 有 `doc_id/project_id/chunk_index/chunk_id/source/file_name/file_type/page`；MySQL 无 chunk 正文表；backend 单进程（Dockerfile 禁多 worker）→ 进程内 BM25 索引可行。

**开工前准备清单（全部就绪才开始写代码，中途不停下来问）**
1. 依赖：`requirements.in` 加 `rank-bm25` 与 `jieba`，`uv pip compile` 重新生成 lock 与 requirements.txt（照现有流程，不走裸 pip install）。
2. 语料探底（一次性）：`python -c` 脚本从 Chroma `get()` 拉全量 chunk，确认全部切片有非空正文、chunk_id 无重复；结果记入迭代文档（这就是 BM25 建索引的输入，运行时同样以此为准）。
   > **✅ 2026-10-12 已做（P3-3 期间顺手完成）**：**90 chunks / 35 files / doc_id 588~622**，正文全部非空、chunk_id 无重复。⚠️ 原写「69 片」是早期快照。
3. 评测基线：P3-3 的 baseline JSON 已存在（没有就先跑 P3-3——这是硬前置，不是可选项）。
   > **✅ 已有**：`data/eval/results/20261012-001334-baseline.json`（2026-10-12）。
   > ⚠️ 基线里 `recall@10 == recall@20 == 0.9389`，**瓶颈在排序不在召回面** ——
   > 而 BM25 的卖点是扩召回面。开工前先想清楚拿什么换 recall@20 的 +5pp。
4. 配置占位：`.env` / `.env.example` 追加 `RETRIEVAL_HYBRID=false`、`RRF_K=60`（默认关，评测达标后才翻 true）。
5. 分支：在 develop 分支开工（见 §4.1 分支策略）。

**实现步骤（按序）**
1. 新增 `core/bm25_index.py`：进程内单例（模块级 + 锁），惰性构建——首次 `search()` 时从 Chroma 拉全量 chunk（jieba `lcut` 分词）建 `BM25Okapi` 索引；对外只有两个方法：`search(query, k) -> list[tuple[str, float]]`（返回 chunk_id + bm25 分）和 `invalidate()`（置脏标记，下次检索前全量重建；69 片毫秒级，不做增量）。
2. 一致性钩子：`core/parsing.py` 的 `delete_by_doc_id` 与 `add_documents` 成功后调 `bm25_index.invalidate()`；`scripts/rechunk_kb.py`、`scripts/clean_orphan_chunks.py`、`scripts/reset_kb.py` 收尾处同样加。多进程注意：worker 不做检索，不建索引；backend 单进程无并发问题。
3. 检索融合（`core/retriever.py` `retrieve()`）：`settings.RETRIEVAL_HYBRID=true` 时——向量召回 candidate_k（20）+ BM25 召回 candidate_k → RRF 融合：`score(d) = Σ 1/(RRF_K + rank_i(d))`，两路名次合成 → 取 RRF top candidate_k → 送现有 reranker（**bge-reranker-v2-m3 与 0.1 阈值裁决完全不变**，BM25 只扩召回面）；hybrid 关闭时走原路径，一行不多。
4. 可观测：检索日志与 NDJSON `retrieval` 调试帧新增 `bm25_hits`（BM25 命中数）与 `rrf_top`（融合后 top1 chunk_id）字段；`frontend/src/types.ts` 补类型、`MessageBubble.vue` 检索详情面板透传展示（P2-23 面板已有骨架）。
5. 拒答不变：rerank + 阈值过滤后为空 → 原拒答话术，阈值不因 hybrid 调整（先看评测数据再说）。

**收益判定（达标才默认开启，负结论同样记档）**
`make eval-retrieval` 跑 A/B（baseline vs hybrid），满足**全部三条**才把 `.env` 的 `RETRIEVAL_HYBRID` 翻 true：
- `recall@20` 提升 ≥ 5pp；
- `MRR@10` 提升 ≥ 0.05；
- 拒答正确率不降（10/10 保持）。
不达标：开关保留默认 false，迭代文档记录负结论与数字，**不为了做功能而开**。

> **✅ 2026-10-12 完成（tag `v3.0.0-p3.4`）——判定：不达标，开关保持 false。**
> A/B 实测：recall@20 +0.0（❌）/ mrr@10 +0.0（❌）/ 拒答 10/10（✅）。
> 但 recall@5 **0.8944 → 0.9389（+4.45pp）**、hit@5 → 1.0、基线唯一失手的编号题被 BM25 救回 ——
> recall@20 判据是在基线出来前写的，基线在 k=20 已摸天花板（hit@20=1.0），该口径结构性不可能 +5pp。
> 详见 `docs/iterations/v3.0.0-p3.4-hybrid-retrieval.md` §5.3。
>
> **⚠️ 2026-10-12 决策变更：用户拍板翻 true**（判据口径失效 ≠ 功能无效；真实语料扩大对 BM25 是利好）。
> 观察指标与关回条件见该文档 §7 修订 1。**服务器 `/opt/qa/.env` 需同步改 `RETRIEVAL_HYBRID=true`**（.env 不入 git）。

**改动文件**：新增 `core/bm25_index.py`；`core/retriever.py`、`core/parsing.py`、`core/rag_chain.py`（debug 帧扩字段）、`config/settings.py`、`.env`/`.env.example`、`requirements.*`、`scripts/rechunk_kb.py`、`scripts/clean_orphan_chunks.py`、`scripts/reset_kb.py`、`frontend/src/types.ts`、`frontend/src/components/MessageBubble.vue`、`Makefile`。

**验收**
- `RETRIEVAL_HYBRID=false` 全量回归零变化（`make test-sandbox` + `make kb-guard` 三道检查）；
- true 时评测 A/B 数落盘、前端检索详情面板能看到 bm25_hits/rrf_top；
- 上传同名文件覆盖、删除文档、rechunk 后 BM25 无脏命中（旧 chunk_id 查不到）；
- NDJSON retrieval 帧新字段契约测试进 `tests/test_module5_retrieval_observability.py`；验收脚本连跑两遍过。

---

### P3-5 上传大小定版 50M（2026-10-11 用户拍板：不做超大文件）

**裁决**：单文件上限维持 50M（`DOC_UPLOAD_MAX_BYTES=52428800`），100M~300M 方案全部搁置，等真实需求出现再立项。

**现状确认（Q1）**
- 前端：只有文件数守卫（`BATCH_MAX_FILES=50`），**没有大小校验**——50MB 只是 `KnowledgeView.vue:305` 的提示文案，超限文件会白传一趟到后端才 413。
- 后端：有拦截。`config/settings.py:263` 唯一权威定义 → `api/routes/documents.py` `_accept_one_upload` 校验 `len(content) > max_bytes` 返回 413。注意校验发生在 `await file.read()` 整读内存之后，50M 量级可接受，不必改。
- nginx：`client_max_body_size 120m`，50M 上传无压力，不动。

**已落地（2026-10-11）**：`frontend/src/stores/documents.ts` upload() 增加单文件 size 预检（`MAX_FILE_BYTES = 50*1024*1024`，与后端同名口径注释互相指向），超限 toast 列出文件名（>3 份折叠显示），vue-tsc 通过。后端与 nginx 均不动。

**明确不做**：150M/300M 上限放开、流式写盘、分片上传、断点续传、切片进度（parsed_chunks）——挂账，触发条件：出现真实的 >50M 文档需求。

---

### P3-6 月度用量快照表（可选，低成本版）

**动机**：拦截（P3-1）上线后月用量成为「执法依据」；当前明细在 `chat_message`，未来清理/归档聊天记录会丢历史用量。
**方案**：新表 `quota_usage_monthly(user_id, period, input_tokens, output_tokens, requests, updated_at)`；Celery beat 每日低峰聚合 upsert（只增不删，当天数据覆盖写）；**判定路径不改**（预检仍走实时 CTE，数据量小）。归档对账用它。
**验收**：连跑两日快照数值与 `quota_repo.usage_by_user` 对账一致；chat_message 删除部分记录后快照不回退。

> **✅ 2026-10-12 完成（tag `v3.0.0-p3.6`）**：迁移 0009 已应用；beat 每天 04:10（避开 03:20 备份）；
> 数值来源 = `usage_by_user()` 本身（写入瞬间逐位相等，对账是恒等式）；
> 写入路径**不带账期参数**（历史账期无写入路径，快照不回退由签名保证）。
> 真库实测连跑两遍幂等 + 对账 ✅。详见 `docs/iterations/v3.0.0-p3.6-quota-snapshot.md`。

---

## 4. 顺序建议

1. **P3-1**（最急：服务器已公开上线，额度是当前唯一想收口的风控）
2. **P3-2**（小，顺手）——与 P3-1 可并行
3. **P3-3**（评测集）→ **P3-4**（混合检索，等评测说话）
4. ~~P3-5~~（✅ 2026-10-11 已落地前端预检，其余搁置）
5. **P3-6**（可选，最后）

### 4.1 分支与部署策略（2026-10-11 定）

- **开发在 develop 分支**：日常提交推 `develop`；main 只进合并。`.github/workflows/deploy.yml` 触发条件是 `push: branches: [main]`，**推 develop 和打 tag 都不会触发部署**——develop 随便推，线上无感。
- **tag 不会触发部署**：本仓库部署只认 main 分支 push（`.github/workflows/deploy.yml:5-10`，tag 触发是注释里的备选方案）。一口气合多个 tag/多个 commit 到 main = **只触发一次部署**（一次 push 一个 workflow run），服务器端是 `git fetch + reset --hard origin/main` + deploy.sh，只看 origin/main 最新态，与 tag 无关。
- **合并节奏**：develop → main 用一次 merge（或 squash）+ 一次 push，即一次部署。避免「合并后再单独 push main」多次触发；即便真的连续 push main，concurrency group `deploy-qa`（`cancel-in-progress: false`）会串行排队不打架，只是多跑几轮。
- **tag 照打不误**：铁律五件事的 tag（`3.0.0-pN.M`）在 develop 或合并后打都行，不影响部署；tag 仅作为版本锚点与回滚参照。
- **注意**：合并进 main 的是「累计多个任务的成果」，部署是整包上线——所以 develop 上每个任务验收（`make test-sandbox` + `make kb-guard`）必须做完再合，不能指望线上发现问题再回滚。

## 5. 风险与明确不做

- P3-1 推翻 P2-15「只提醒不阻断」的刻意决策——迭代文档 §3 必须写清推翻理由（对外服务 + 成本可控性），旧测试更新属决策变更非回归。
- P3-1 并发竞态接受（同用户并发可略超限量）。
- P3-4 收益不达标则不默认开启——不为了做功能而做。
- P3-5 不解决 300M+：分片上传/断点续传/流式解析本期不做。
- 全程不动：chunk_id 契约 `"<doc_id>:<chunk_index>"`、鉴权 fail-closed 结构、真相源分工、`set_token_quota` 不 bump token_version、cache_read 不计总额。

---

## 6. 检索质量优化路线图（A/B 已交付：v3.1.0-p3.7a / p3.7b / p3.7d）

> 2026-10-14 讨论产出（对照腾讯 RAG 架构图与切分最佳实践）。
> ~~本文是规划不是迭代~~ **2026-10-14 起按 P3-7 系列开工**（进度登记见 §2 总览表）：
> A（Enrichment）已随 `v3.1.0-p3.7a` 落地，**A/B 判定不达标 → `CHUNK_MODE` 默认保持 `fixed`**，
> 结论与数字见 `docs/iterations/v3.1.0-p3.7a-title-enrichment.md`；B（MinerU）已随 `v3.1.0-p3.7b` 落地（真实链路实测通过）；golden 表格类题待首个表格 PDF 入库时补（见 p3.7b 迭代文档 §6）。
> D（Query 增强）已随 `v3.1.0-p3.7d` 落地：单轮口语化 LLM 改写 + 可选 HyDE（详见 §6.4）。**多轮指代改写（standalone_question）此前已在 `core/rag_chain.py` 落地，不属于 p3.7d 新增**——p3.7d 补的是「单轮也过一次改写」+「HyDE」两块，且默认关闭、由 eval 说话。
> 落地节奏建议按 A→E 优先级串行；**A 与 B 高度耦合**（标题切分 + 结构化解析），可合并为同一 MINOR 一次交付。
> 所有「改切分参数」的改动都触发铁律：**整库重建 + `make kb-guard` 三道检查 + 重建后 `make eval-retrieval` 复核**（P3-3 的 golden 40 条正是为此准备的）。
> 版本归属：P3 系列已收官于 `v3.0.0-p3.6`，且 `v3.1.0-p4.1` 已插入；本路线图建议并入**下一个 MINOR（暂记 `v3.1.0` 的 P3-7 系列）**，开工时再在进度总览表登记。

### 6.0 路线图优先级（结论先行）

| 序 | 阶段 | 一句话 | 收益确定性 | 改动量 |
|----|------|--------|-----------|--------|
| A | **Enrichment（标题上下文补全）** | chunk 入库前补 `header_path` 拼到正文再 embedding | 高（标题密集文档直接涨召回） | 中（loader + 入库改写 + 重建） |
| B | **PDF 表格走 MinerU** | 结构化解析保表格/合并单元格，整表原子 chunk | 高（当前纯文本抽取必丢结构） | 中（新增 MinerU 客户端 + 路由） |
| C | **QA 对入库** | 问答对跳过切分直接建索引，高置信召回 | 中（取决于是否有 QA 语料） | 小（新增入库类型） |
| D | **Query 增强（改写/扩展）** | 检索前 LLM 改写口语化/指代问题 | 中（多轮指代改写已落地；单轮+HyDE 随 `v3.1.0-p3.7d` 进库，**默认关闭**待 eval 说话） | 小（retriever 前置一步） |
| E | **意图路由** | 多知识库/多业务域才需要 | 低（当前单管线无需求） | 占位设计，不实现 |

---

### 6.1 A — Enrichment：标题上下文补全（优先级最高）

**现状事实**
- `core/document_loader.py:81` 用 `RecursiveCharacterTextSplitter`（`CHUNK_SIZE=500 / OVERLAP=50`），分隔符优先 `\n\n`/`\n`——**不是无脑 500 字硬切**，但完全不感知标题层级。
- 写入 Chroma 的 chunk metadata（`core/parsing.py:202-205`）只有 `doc_id/project_id/chunk_index/chunk_id/source/file_name/file_type/page`，**无章节路径**；embedding 输入是裸 chunk 正文。
- 结果：用户问「XX 章节讲了什么」「XX 的步骤是什么」时，chunk 里若只写了「额定 220V±10%」而无「电气规格」语境，纯语义匹配偏弱。

**方案**
1. 新增 `CHUNK_MODE`（`.env`）：`fixed`（默认，= 现状 500/50）| `title`（标题感知）。`fixed` 路径一行不多，保证回归零变化。
2. `title` 模式：
   - markdown 走 `MarkdownHeaderTextSplitter` 按标题树切；超长小节（> CHUNK_SIZE）二次用 `RecursiveCharacterTextSplitter` 切；过短小节向相邻合并（`chunk_size` 的 0.3 倍阈值）。
   - PDF/Word 的标题层级依赖 B 阶段的结构化解析输出（markdown/HTML），本阶段先做 md 闭环，PDF 走 B 落地后自动接通。
3. **Enrichment 写入**：chunk 入库前把 `header_path`（如 `产品手册 > 安装 > 3.2 电压要求`）拼到正文前生成 `enriched_content` 再 embedding；元数据新增 `header_path` 字段。**`content` 保留原文**（展示/引用反查 `GET /chunks/{chunk_id}` 仍返回裸文本，不污染用户看到的引用）；embedding 用 `enriched_content`。
4. ⚠️ 切分参数变了 = 整库重建（铁律）。重建走既有 `scripts/rechunk_kb.py` + `make kb-guard`，重建后必须 `make eval-retrieval` 复核。
5. **A/B 决策**：用 P3-3 golden set 跑 `fixed` vs `title-enriched`，`recall@5` / `mrr@10` 提升且拒答不降才把 `CHUNK_MODE` 翻 `title`（默认仍可保持 `fixed`，功能进库择机启用——对齐 P3-4 的负结论同样记档原则）。

**改动文件**：`core/document_loader.py`、`core/parsing.py`（写 `header_path` + `enriched_content`）、`core/vector_store.py`（元数据/字段落库）、`config/settings.py`、`.env`/`.env.example`、`scripts/rechunk_kb.py`、`scripts/eval_retrieval.py`（加 A/B 维度）、`tests/`（chunk_id 契约回归）。

**验收**
- `CHUNK_MODE=fixed` 全量回归零变化（`make test-sandbox` + `make kb-guard`）。
- `title` 模式下 golden 集 `recall@5`/`mrr@10` 有可量化提升；引用反查仍返回原始正文（不返回 enriched 拼接串）。
- 重建后 `upload/ 行数 = document 行数 = Chroma 切片数` 三处一致（kb-guard 三道检查）。

---

### 6.2 B — PDF 表格走 MinerU（保结构 + 合并单元格）

**现状事实**
- PDF 走 `PyMuPDFLoader`（`document_loader.py:53`），抽的是**纯文本流**：表格按行线性化、列对齐丢失、单元格合并（rowspan/colspan）信息直接消失，跨页表格断裂。这是纯文本抽取的通病，无参数可救。
- 桌面 langChain 项目已接 MinerU API（`v4 precise + v1 lightweight`），能输出保留表格结构与跨页拼接的 markdown/HTML；rag 项目尚未接入（key 在桌面 `langChain/.env` 的 `MINERU_API_KEY`，rag 侧需独立配置）。

**方案**
1. 新增 `core/mineru_client.py`：封装 MinerU 上传/轮询/取结果（对齐桌面项目已验证的调用方式）；`.env` 加 `MINERU_API_KEY` + `MINERU_API_URL`（默认走 `v4 precise`，大文档回退 `v1 lightweight`）。
2. `core/parsing.py` 入口加 PDF 路由：含表格/版式复杂的 PDF 走 MinerU 拿 markdown → 进 A 阶段的 `title` md 切分（结构化标题天然可用）；纯文本、无表格的小 PDF 仍走 PyMuPDF（更快、省额度）。路由判据先用「强制开关 + 文件大小阈值」，后续可加「MinerU 是否可用」的健康探活。
3. **表格原子性**：MinerU 输出的 markdown table / 带 rowspan 的 HTML，在切分环节识别为**整体一个 chunk，绝不从中间切断**；合并单元格由 MinerU 版面模型还原为完整网格（或展开成「每行带合并值」），embedding 用 markdown table 文本即可。
4. eval 集补 3~5 条表格类问题（跨页表、合并单元格表各若干），作为 B 的验收基线。

**改动文件**：新增 `core/mineru_client.py`；`core/parsing.py`（PDF 路由）、`core/document_loader.py`（md 切分已存在，复用）、`config/settings.py`、`.env`/`.env.example`、eval golden 扩充。

**验收**
- 含跨页表格 / 合并单元格的 PDF 入库后，问「表格里 XX 行的 YY 列是多少」能命中且返回**完整表**（非截断行）。
- `make eval-retrieval` 表格类新题命中率达标；重建后 kb-guard 三道检查过。
- MinerU 不可用（key 缺失/超时）时**优雅降级**回 PyMuPDF，不阻塞上传。

---

### 6.3 C — QA 对入库（跳过切分直接建索引）

**现状事实**：所有文档统一走切分；无「问答对」这一高价值形态（架构图里的 QA/FAQ 知识类型）。

**方案**
1. 新增入库类型 `qa_pair`：`POST /documents`（或独立 `POST /documents/qa-pair`）+ 前端上传时可选「问答对」模式；支持单条填写（Q/A 两框）或 Excel 两列（问题/答案）批量。
2. 每条 QA 对 = **一个 chunk，不切分**：`content` = 答案 A；`enriched_content` = `问题：Q\n答案：A`（可能再拼标题）；metadata 加 `doc_type=qa_pair`。
3. 检索命中 QA 对时，可作为**高置信度上下文**直接喂给 LLM（或高亮「官方问答」标记）；拒答阈值对 QA 对适当放宽（答案明确，召回即有用）。
4. eval 集补 QA 类问题验证召回。

**改动文件**：`api/routes/documents.py`（新入库入口）、`core/parsing.py`（qa_pair 分支，跳过 splitter）、`frontend/`（问答对上传 UI，可后置）、eval 扩充。

**验收**：QA 对原问句检索命中率显著高于把同样内容当普通文档切分；引用反查返回原问答对。

---

### 6.4 D — Query 增强（改写 / 扩展）

**现状事实（2026-10-15 落地后）**
- 多轮对话的指代 / 口语化改写（standalone_question）**此前已在 `core/rag_chain.py` 落地**（`_contextualize_q_prompt` + `_rewrite_question`，随 `_PIPELINE_EXECUTOR` 与意图/检索并行，延迟被隐藏）；这已经覆盖了「多轮指代」这一类。
- 单轮口语化改写 + HyDE 此前**没有**，即 golden 集里 7 条纯口语改写题（q019–q025）单轮问时仍直接拿原句检索，召回偏弱。
- 因此本任务（p3.7d）补的是「单轮也过一次改写」+「HyDE」两块，且严格默认关闭，由 P3-3 评测集说话。

**方案（已随 `v3.1.0-p3.7d` 落地）**
1. `config/settings.py` 新增两个开关，均默认 `False`：
   - `QUERY_REWRITE`：单轮口语化改写，复用主答模型（`get_llm_client()`），不另起模型。改写沿用多轮那套 `_contextualize_q_prompt`，输出做 sanitize 兜底（空输出回退原句）。
   - `QUERY_HYDE`：在改写基础上生成一段「假设答案」文本（`_HYDE_SYSTEM_PROMPT`），用该文本的 embedding 做**向量召回**；BM25 仍用真实问句关键词（`retriever.retrieve(vector_query=...)` 只替换向量 embedding 文本，不污染关键词匹配）。单独开 HyDE 会自动先走一次改写。
2. 触发条件：`should_rewrite = bool(chat_history) or QUERY_REWRITE or QUERY_HYDE`。无历史且双关关 → 行为与旧版字节级一致（零变化）。改写与意图/检索在 `_PIPELINE_EXECUTOR` 并行，改写延迟被隐藏。
3. A/B 用 `scripts/eval_retrieval.py --category paraphrase --enhance`（+`--hyde`）显式开启受控变量；评测集 7 条口语题命中情况见 `docs/iterations/v3.1.0-p3.7d-query-enhance.md`。

**改动文件**：`config/settings.py`（双开关）、`core/rag_chain.py`（`rewrite_query` / `generate_hypo_answer` 纯函数 + `_prepare_parallel` 改写/HyDE 分支 + debug 帧记 `vector_query`）、`core/retriever.py`（`retrieve(vector_query=)` 透传）、`scripts/eval_retrieval.py`（`--enhance`/`--hyde`）、`tests/test_module23_query_enhance.py`（14 项，无基础设施依赖）。

**验收（铁律）**：开关关时 `make test-sandbox` + `make kb-guard` 零变化；`tests/test_module23_query_enhance.py` 三分支（无增强/无历史、QUERY_REWRITE 开、QUERY_HYDE 开）全绿；A/B 数字决定是否翻默认（见迭代文档 §5）。

---

### 6.5 E — 意图路由（占位设计，本期不实现）

**现状事实**：单一问答管线，无路由/意图识别（架构图里的「意图识别 / 路由」「话题切换」）。

**方案（仅设计，不编码）**
- 当系统接入**多个知识库 / 多业务域**后再做：轻量意图分类（规则 + LLM）路由到不同 Chroma collection / prompt / 拒答策略。
- 当前单管线、单知识库，无需求；列为「明确不做（本期）」，触发条件：出现多知识库隔离或业务分流需求。

---

### 6.6 路线图与既有任务的依赖关系

- A 依赖 B 才能覆盖 PDF 标题层级；但 A 的 md 闭环可独立先上，PDF 在 B 落地后自动接通——**建议 A+B 合并同一 MINOR 一次交付**，避免两次整库重建。
- A/B/C 全部改了 chunk 形态 → 每次上线都要 `make eval-retrieval` 复核 golden 40 条（P3-3 的投入此刻开始持续回报）。
- D 独立于切分改动，可随时单上；E 本期不排期。
- 全程不碰：chunk_id 契约、鉴权 fail-closed、真相源分工、`RETRIEVAL_HYBRID` 现有开关语义（A/B 只改 chunk 内容，不撼动 P3-4 的向量+BM25+RRF+rerank 检索架构）。
