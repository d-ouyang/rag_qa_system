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
| P3-6 | 月度用量快照表（归档审计，不改判定路径） | core/worker/alembic | ☐ 可选 | 3.0.0-p3.6 |

依赖：P3-4 依赖 P3-3（没有评测基线不许开混合检索）；P3-1 与 P3-2/P3-5 可并行。

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
