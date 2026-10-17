# PLAN v4.0.0 — 运行时配置服务（业务开关热更新 · 设置页可操作化）

> 2026-10-16 讨论产出（IDENTITY_MODE=dev→gateway 切换完成后顺势提出）。涉及 api/core/frontend/admin-console 与现有「系统设置」页。
> 铁律不变：每任务 = 迭代文档 + tag `4.0.0-p4.N` + RELEASES + CHANGELOG + 版本号（见 §1 新规则）+ 本表更新状态。
> 判新旧看 `git log` 不看编号；开打前 `git tag | grep 'p4\.'` 查占用。
> 优先级：**P4-1 / P4-2 / P4-5 / P4-7 已收官；当前核心 = P4-6（Groundedness 三件套）；P4-9 / P4-10（查询分解 / 滚动摘要）为下一批候选**；P4-8 为持续运营项；P4-3 / P4-4 仍为低优先级待排期。

---

## 1. 设计裁决（结论速览）

| # | 问题 | 裁决 |
|---|------|------|
| Q1 | 版本号如何随代码同步（.env 不进 git，UI 老版本号） | **入库真相源**：新增 `config/version.py`（入库）放 `PROJECT_VERSION`；settings 以它为默认，`.env` 仅作本地覆盖（删行即跟随代码）。服务器 `.env` 删掉 `PROJECT_VERSION` 一行即可自动跟随部署代码/tag。详见 §1.1 |
| Q2 | 模型切换粒度 | **全局唯一 + 白名单**（superadmin 维护可选列表并指定生效模型，用户无感）。per-user 切换写入「遗留触发条件」，现在不做 |
| Q3 | 会话三层存储落地节奏 | **先 L1 进程内缓存**（TTL + LRU 容量上限，4C4G 内存紧），L2 Redis 视收益放后续；MySQL 真相源/行锁/owner 校验不动 |
| Q4 | 配置分类 | ① 重启必需（连接串/密钥/端口/CORS，留 `.env` + UI「⚠️ 需重启」徽标）｜② 热纯开关｜③ 热参数｜④ 热+副作用（CHUNK_MODE / 嵌入模型 = 必须重建索引，superadmin + 二次确认） |
| Q5 | 权限 | v1 全部配置变更 **superadmin** 专属，admin 只读展示 |

### §1.1 版本号同步方案（解决 Q1）

**问题根因**：`PROJECT_VERSION` 写在 `.env`，而 `.env` 被 gitignore，`deploy.sh` 的 `git reset --hard` + `git clean -fd` 不会带它到服务器；服务器 `.env` 长期停留旧值（当前 `3.0.0-p3.6`），导致 UI/health 显示老版本。

**方案**：把版本真相源从「gitignore 的 .env」移到「入库的代码文件」。

1. 新增 **`config/version.py`**（入库，随代码/tag 同步）：
   ```python
   PROJECT_NAME = "企业级RAG智能问答系统"
   PROJECT_VERSION = "4.0.0-p4.1"   # 每次迭代在此 bump，铁律五件事之一
   ```
2. `config/settings.py` 改为以它为默认，并加兜底校验（防 `.env` 写了空串覆盖成空）：
   ```python
   from config import version as _ver
   PROJECT_NAME: str = _ver.PROJECT_NAME
   PROJECT_VERSION: str = _ver.PROJECT_VERSION
   @field_validator("PROJECT_VERSION")
   @classmethod
   def _fallback_version(cls, v):
       return v or _ver.PROJECT_VERSION
   ```
3. `.env.example`：保留 `PROJECT_VERSION` 作为「可选本地覆盖」示例（本地开发可写 `dev`），与 `config/version.py` 同步 bump 仅供文档对齐。
4. **服务器 `/opt/qa/.env`：删除 `PROJECT_VERSION` 那一行**（否则它会覆盖成旧值）。删除后 recreate，health 自动显示当前部署代码的版本。
5. 迭代 chore 调整：版本号 bump 落在 `config/version.py`（入库）+ `.env.example`；`.env`（gitignore）不再是版本真相源，仅本地覆盖用。

> 这样 `make deploy`（git push main → 服务器 `git reset --hard origin/main`）会把新 `config/version.py` 一起拉下来，版本号天然随代码/tag 同步，无需再手工改服务器 `.env`。
> 可选增强：加 `make bump-version V=4.0.0-p4.2` 一次性改两处（仅提效，非必须）。

---

## 2. 范围与进度总览（唯一权威进度）

| 任务 | 内容 | 涉及端 | 状态 | tag | 优先级 |
|------|------|--------|------|-----|--------|
| P4-1 | ConfigService 底座：MySQL `system_config` 真相源 + 进程内缓存 + TTL 兜底；`config/version.py` 版本同步；试点迁移 `QUERY_REWRITE` | api/core/alembic | ✅ 完成（2026-10-16） | `v4.0.0-p4.1` | **高** |
| P4-2 | 设置页改造：现有只读「系统设置」页升级为分层可操作面板（热项可编辑 + 确认按钮 + 需重启徽标）+ 后端 admin/config 路由 | api/core/frontend | ✅ 完成（2026-10-16，8 标志全链路热化 + superadmin 门禁 + 审计） | `v4.0.0-p4.2` | **高** |
| P4-3 | 模型切换：白名单 + 全局生效模型（qa_cache key 带模型名；嵌入模型保持只读+重建任务） | api/core/frontend | 🔲 未开始 | `4.0.0-p4.3` | 低（P4-1/2 后） |
| P4-4 | 会话三层存储 L1：进程内缓存层（TTL+LRU），读 L1→L2→L3、写 L3 先行，ACL 不过缓存 | api/core | 🔲 未开始 | `4.0.0-p4.4` | 低（P4-1/2 后） |
| P4-5 | **问答流程回溯（Q&A Trace）**：检索轨迹按请求采集 + 随轮落库 `turn_meta.qa_trace` + 历史接口回吐 + 前端「检索详情」改读持久化（**一期**）；跨会话分析表 + 溯源可视化页（**二期**） | api/core/frontend/admin-console | ✅ **全部完成**（一期 2026-10-16 四终端状态落库；二期 `v4.0.0-p4.5b`：qa_trace 分析表 + /admin/qa-traces + 管理端溯源页 + QaFlowMachine 收口） | `v4.0.0-p4.5` / `v4.0.0-p4.5b` | **高（当前）** |
| P4-6 | **答案充分性校验（Groundedness）三件套**：① 答案后置校验（小模型判定关键论断能否在上下文找到依据，不能 → 改写为「现有制度未查到」或挂标）② 低置信徽标（top1 处于低置信带时答案挂警示）③ 答案溯源标注（「依据：考勤管理制度 第七条」，点击跳转溯源页候选原文） | api/core/frontend | 🔲 未开始（**P4-7 扫档数据是其立项铁证**：库内最低 top1=0.2569 与库外最高 0.3232 犬牙交错，阈值无解） | `4.0.0-p4.6` | **高（当前）** |
| P4-7 | **幻觉治理一期**：QA 提示词硬化（正面清单式禁编造）+ 阈值扫档 0.1/0.3/0.5/0.7 出「拒答正确率 vs 命中率」曲线报告 + 评测集加幻觉负样本（q041 病假）+ 溯源页候选原文查看（`GET /admin/qa-traces/chunks`，人工校验落点） | api/core/scripts/admin-console | ✅ 完成（2026-10-16，扫档裁决：阈值维持 0.1——0.3 档纯亏，根治在 P4-6） | `v4.0.0-p4.7` | 高 |
| P4-8 | **知识库运营：制度缺口反馈**（持续运营项，非代码）：溯源页拒答/未命中 → 归档制度缺口清单 → 反馈 HR 补文档（首个缺口：病假扣薪规定缺失） → 补文档后重灌知识库 + 重建评测标注 | 运营流程 | 🚧 流程已定义，持续执行 | — | 中 |
| P4-9 | **查询分解（复合问题）**：改写环节升级为「复合检测 + 多子查询输出」；子查询并行检索、候选池按 chunk_id 去重合并；生成侧按子问题组织答案，某子问题资料未覆盖须明说 | api/core | 🔲 未开始（2026-10-17 立项：实测复合问句重排分稀释——「怎么请事假？」top1=0.5967/5 通过 vs 复合问 0.3488/仅 1 通过） | `4.0.0-p4.9` | **高** |
| P4-10 | **上下文滚动摘要**：`get_recent_messages` 从「窗口截断」改为「窗口 + 滚动摘要」——超窗口的对话由 LLM 压缩成 summary 随上下文带下去；解决长会话（>5 轮）上下文失忆 | api/core | 🔲 未开始（2026-10-17 立项：用户要求长会话跨轮记忆；现状=全量在库但窗口外 LLM 不可见） | `4.0.0-p4.10` | **高** |

依赖：P4-2 依赖 P4-1（UI 编辑的是 ConfigService）；P4-3 / P4-4 依赖 P4-1（消费者）。P4-1 独立可先做。
P4-5 独立（只动检索轨迹 + 落库 + 前端展示），不依赖其它任务，可即做。

---

## 3. 任务详情

### P4-1 ConfigService 底座（版本同步 + 热开关基础设施）

**现状事实**
- 所有开关在 pydantic-settings，来自 compose `env_file` 注入，**进程启动期冻结**；改任何开关要 `docker compose up -d --force-recreate`。
- 无运行时配置源；MySQL 已有用户/会话/文档元数据，但**无 `system_config` 表**。
- 验证端点 `/api/v1/system/health` 的 `identity` 段已能报 `mode` / `trust_enforced`（信任边界已生效，2026-10-16 切换完成）。

**方案**
1. **MySQL 表 `system_config`**：`key`(PK, varchar) / `value`(text，JSON 序列化) / `value_type`(enum: bool|int|str|enum) / `description` / `updated_at` / `updated_by`。alembic 新迁移。
2. **`core/config_service.py`**：
   - 启动加载全表到进程内 `dict`；`get_flag(key, default)`、`set_flag(key, value, actor)`。
   - `set_flag` 写 MySQL + 审计（actor/时间）；**P1 先用 60s TTL 回源兜底**保证一致，**P2 再加 Redis `PUBLISH config:changed` 订阅刷新**（多副本即时失效）。
   - `set_flag` 做**类型校验**，拒绝非法值（bool 字符串、超 enum 范围等）。
3. **版本同步**：按 §1.1 落地 `config/version.py` + settings 兜底；服务器 `.env` 删 `PROJECT_VERSION`。
4. **试点迁移**：`core/rag_chain.py` 把 `settings.QUERY_REWRITE` 改为 `config_service.get_flag("query_rewrite", False)`；验证「不 recreate 即热生效」。
5. **边界**：基础设施类（DB/Redis/Chroma 连接、密钥、端口、`BIND_ADDR`、CORS）继续留 `.env`，ConfigService 只接**业务开关/参数**。

**改动文件**：`alembic/versions/*`（新表）、`config/version.py`（新）、`config/settings.py`、`core/config_service.py`（新）、`core/rag_chain.py`（试点）、`.env.example`。

**验收**
- 起 MySQL 后 ConfigService 能从表加载；`set_flag` 后进程内 dict **立即变（无重启）**；60s 内 TTL 回源值与表一致。
- `QUERY_REWRITE` 不在表内时默认 `False`；经 ConfigService 改 `true` 后，下一次提问走改写（**无 recreate**）。
- 非法类型 `set_flag` 被拒（返回 4xx）。
- 服务器删 `.env` 的 `PROJECT_VERSION` 后，`/system/health` 显示代码入库版本（不再是 `3.0.0-p3.6`）。

---

### P4-2 设置页改造（只读页 → 分层可操作面板）

**现状事实**
- `qa.nianan.site` 现有「系统设置」页为**只读展示**，数据来自 pydantic settings，页脚明写「只读…修改后需重启生效」。
- 卡片：项目信息 / 大语言模型 / 检索与重排 / 向量库与嵌入 / 文档切分 / 会话记忆 / 意图识别。

**方案（卡片映射）**

| 卡片 | v1 处理 |
|------|---------|
| 项目信息 | 保持只读（版本/地址属重启必需） |
| 大语言模型 | 模型下拉 + temperature/max_tokens **可编辑（热）**；Provider / API 地址保持只读 |
| 检索与重排 | top_k、重排开关 / 模型 / 分数阈值 **可编辑（热）**；重排热开启需**懒加载** cross-encoder |
| 向量库与嵌入 | **全部只读**；嵌入模型标「更改 = 重建索引」禁止随手拨 |
| 文档切分 | 可编辑，确认后**触发异步重建索引任务**（superadmin + 二次确认） |
| 会话记忆 | 轮数 / 过期时间 **可编辑（热）** |
| 意图识别 | 模型 / 超时 **可编辑（热）**；本地模型（qwen3.5:9b）热开关需懒加载 |

- 通用：热项加「**确认修改**」按钮 → `PATCH /api/v1/admin/config`（superadmin 门禁，复用 `core/kb_acl`）→ 写 ConfigService + 广播；重启必需项加「⚠️ 需重启」徽标；页脚按条目改写语义。
- 后端 `api/routes/admin/config.py`：`GET` 返回所有可编辑项（含分类 / 权限 / 需重启标记 / 当前值），`PATCH` 校验 + 写 ConfigService + 审计。

**改动文件**：`frontend` 设置页组件、`api/routes/admin/config.py`（新）、`core/config_service.py`、`core/kb_acl.py`（superadmin 判据复用）。

**验收**
- superadmin 在设置页拨 `QUERY_REWRITE` → **立即生效**，其他登录用户下次请求即见。
- admin 角色看不到编辑控件（只读）。
- 重启必需项显示「需重启」徽标且 `PATCH` 被拒；热项 `PATCH` 后 health/接口确认值已变。

---

### P4-3 模型切换（低优先级）

- **全局唯一模型 + 白名单**：superadmin 维护可选模型列表（LLM_PROVIDER / 模型名 / 温度区间），指定当前生效模型；普通用户无感。
- **切换语义**：下一请求生效；进行中请求用旧模型跑完；历史不迁移（历史是文本，不依赖模型）。
- **硬约束**：`qa_cache` 缓存 key **必须带模型名**（或切换时 bump 命名空间），否则换模型后命中旧模型答案。
- 嵌入模型不在此列（换向量空间 = 重建索引，保持只读 + superadmin + 重建任务，见 P4-2 卡片映射）。
- **per-user 切换列入「遗留触发条件」**：触发条件 = 出现「不同角色/个体确需不同模型」的真实需求；届时 `user` 表加 `default_model` 列、读取优先级 用户 > 全局、superadmin 限定可选范围。现在不做。

---

### P4-4 会话三层存储 L1（低优先级）

- **读序 L1（进程内）→ L2（Redis）→ L3（MySQL）**（不是 Redis 先：进程内比 Redis 还快一跳）。
- **写 L3 先行**：事务 + `SELECT...FOR UPDATE` 行锁 + `owner_id` 校验留 MySQL 层；缓存只做读优化，写成功后再更新 L1（多副本时广播失效）。
- **现状核对**：Redis 当前未按 sessionId 存会话（仅解析队列 db1 + 问答缓存）；`MEMORY_BACKEND=memory|mysql` 是二选一非分层；mysql 模式无进程内缓存，每次会话读都打 MySQL。
- **P4-4 只做 L1**：进程内缓存层（TTL + LRU 容量上限，4C4G 内存紧），开关接入 ConfigService；**ACL/owner 校验永不过缓存**。
- L2 Redis 视实际收益放后续（backend 重启暖启动 / 多副本），与 ConfigService **共用同一套 Redis pub/sub**。
- MySQL 永不关，开关只剩 L1/L2 两个，语义干净。

---

### P4-5 问答流程回溯（Q&A Trace）

> 立项动机：反复要回答「这一轮为什么这么答 / 为什么拒答」。p2.23 交付的「检索详情」面板**只在当轮流式过程中可见**，刷新即消失 —— 当时刻意没做持久化（v2.0.0-p2.23 §1/§6 明写"待有真实需求再立项"）。本任务就是那个真实需求：把每轮的完整链路决策 + 检索细节**落库**，可回看、可分析。

**现状事实**
- `chat_message.turn_meta`（JSON 列）已随轮持久化 `sources / intent / route / standalone_question / usage / elapsed_ms / ts / cache_hit`，**但没有候选级检索细节**（重排分/阈值/top1/filtered_count/hybrid 轨迹）。
- 检索轨迹挂在重排器实例的 `last_trace` / 检索器的 `last_hybrid_trace`（P2-23「最近一次」口径，并发会互相覆盖）。
- 一次问答的终端状态有四种（隐式分支，无正式状态机）：`cache_hit` / `chitchat` / `zero_recall` / `rag_qa`。

**方案（一期：落库 + 历史回看）**
1. **轨迹按请求采集**：`RAGRetriever.retrieve()` 加**可选出参** `trace_out`（不改返回值、不加位置参数 → 测试注入的打桩重排器 `rerank(query, documents, top_k=None)` 零影响）；`_rank()` 在 `rerank()` 一返回就取走 `last_trace`（把「最近一次」窗口从整个请求缩到本次调用）。`_prepare_parallel` 生成一份 `trace` 传给检索，收进 `prepared["retrieval_trace"]`（重新检索时换新的一份）。
2. **组装 + 落库**：新增 `_build_qa_trace()`（复用 `_retrieval_debug_event` 产出检索段，保证「流式看到的 = 落库存的」）/ `_cache_qa_trace()`（缓存命中轮）；`query()` / `stream()` / `_finish_cached()` 把 `qa_trace` 写进 `exchange_meta` → 随 `turn_meta` 落 `chat_message`。**无需 alembic 迁移**（JSON 列）。
3. **接口回吐**：`GET /qa/sessions/{id}` 给 assistant 消息补 `qa_trace`（老轮次为 None，前端据此不展示）。
4. **前端**：`MessageBubble` 的「检索详情」面板数据源改为 `qa_trace ?? retrieval_debug`（落库优先、流式回退）—— **刷新 / 历史回看不再丢面板**。

**方案（二期：跨会话分析 + 可视化溯源页）**（本次不做，另行排期）
- 新增 `qa_trace` 表（alembic，按 decision/intent/日期建索引）+ `GET /admin/qa-traces`（superadmin/ops，`decision` / `min_top1_gap` / `date` 过滤）。
- 前端「问答溯源」页：会话时间线高亮每轮**实际走过的路径** + 完整状态机参照图。
- 抽 `QaFlowMachine`（`QaFlowState` 枚举）把 `implied` 分支收口成可测决策点。

**改动文件（一期）**：`core/retriever.py`、`core/rag_chain.py`、`api/routes/qa.py`、`frontend/src/{types.ts,components/MessageBubble.vue}`、`tests/test_module26_qa_trace.py`（新）、`scripts/test_sandbox.sh`。

**验收（一期）**
- 四个终端状态（cache_hit / chitchat / zero_recall / rag_qa）都产出 `qa_trace` 并随轮落库（`memory.get_exchange_meta` 可读）。
- `qa_trace` 的检索段与流式 `retrieval` 帧**同源同值**（同构断言守，防两处漂移）。
- 刷新 / 重进会话后「检索详情」面板仍可展开（后端 `GET /qa/sessions/{id}` 回吐 `qa_trace`）。
- 不传 `trace_out` 的调用（含测试打桩重排器）行为逐字节不变。
- `tests/test_module26_qa_trace.py` 全绿；`make test-sandbox` 连跑两遍仅基线红。

**已知边界**：轨迹仍是「重排器实例级即时取走」而非真·按请求隔离（彻底隔离要改 `rerank()` 返回值，会打挂 module20 打桩器，收益与破坏面不成比例）；`turn_meta` 因多存候选清单会变大（单轮约 1~3KB，可接受）。

---

### P4-6 答案充分性校验（Groundedness 三件套）

> 立项动机（P4-7 扫档铁证）：q041「请病假扣工资吗」top1=0.3232 与库内真命中最低分 0.2569 犬牙交错——**任何阈值都无法同时放行真命中、拦住擦边无答案**。病假案例中模型用常识编造扣薪规则（库内根本无此规定），引用面板却挂着文档出处，「看起来有据、实际无据」是最危险的幻觉形态。

**三件套（一期范围）**
1. **答案后置校验**：答案生成后，用小模型（或主模型低成本调用）判定「答案的每个关键论断能否在本次上下文中找到依据」；判定不通过 → 改写为「根据现有制度未查到相关规定」或保留答案但挂警示。接入点在 rag_chain 生成之后、落库之前；判定结果进 `qa_trace`（新增 `grounded` / `unsupported_claims` 字段）。
2. **低置信徽标**：top1 处于低置信带（扫档数据：0.25~0.35 犬牙交错区）时，前端答案卡挂「低置信」徽标；阈值维持 0.1 不动，徽标只做提示不改行为（热开关控制显隐）。
3. **答案溯源标注**：要求模型输出时标注依据（资料中的文件 + 条款号），前端渲染为「依据：考勤管理制度 第七条」，点击跳转溯源页对应候选（P4-7 已备好候选原文按需查询 `/admin/qa-traces/chunks`）。

**验收**：q041 类问题（库内无答案）要么拒答要么挂标，不再出现无据编造；评测集负样本的「应拒答拒答率」不降；库内题命中率不降（校验只动展示层与改写，不动检索）。

### P4-7 幻觉治理一期（本期完成）

1. **提示词硬化**（`_QA_SYSTEM_PROMPT`）：「不要编造」升级为正面清单——资料没有明确规定的制度/规则类问题必须回答「根据现有制度未查到相关规定」，禁止以常识补充（实测病假案例中旧措辞被模型先验击穿）。
2. **阈值扫档**：新 `scripts/threshold_sweep.py`——以阈值 0（关过滤）跑一遍评测集、记录每题 top-5 分数，离线重算任意阈值的全部指标（重排分数降序，数学等价）。产出 0.1/0.3/0.5/0.7 四档「拒答正确率 vs 命中率」曲线报告（暗色 HTML，`data/eval/results/*threshold-sweep*.html`）。
   **裁决：维持 0.1**。库内最低 top1=0.2569（q020）与库外最高 0.3232（q041）犬牙交错：0.3 档命中率 -10pp + 误拒 +3 且 q041 照样漏；0.5 档拒答全对但误拒 7/30。阈值这条路到头了，P4-6 才是出路。
3. **评测集负样本**：golden.jsonl 40 → 41 条，新增 q041「请病假扣工资吗」（幻觉回归锚，库内无病假扣薪规定），module19 配比断言同步。
4. **溯源页候选原文查看**：`GET /admin/qa-traces/chunks?ids=`（staff + view_qa_traces 门禁，单次 ≤20 个，按需现查不落库）+ 溯源详情候选行「原文」展开（两层缓存：traceId → chunkId → 正文）。人工校验三步法（搜关键词 / 看分数 / 对原文）全部落在一个页面内完成。

### P4-8 知识库运营：制度缺口反馈（持续运营项）

- **流程**：溯源页发现「未查到相关规定」类回答或误拒 → 记入制度缺口清单（现阶段手记，P4-6 落地后可从 qa_trace 自动聚合）→ 反馈 HR 补文档 → 文档入库重灌 → 评测集补该主题的正样本。
- **首个缺口**：病假扣薪规定缺失（q041）。
- **不做什么**：不为「让某个问题能答」临时塞文档——缺口清单按主题归并、批量补。

### P4-9 查询分解（复合问题）

> 立项动机（2026-10-17 实测）：复合问句「怎么请事假？事假扣不扣钱？」作为单查询检索，重排分被稀释——单独问 top1=0.5967、5 个切片通过；复合问 top1=0.3488、仅 1 个通过，事假流程切片没进上下文，答案缺流程。改写模块现在只做指代消解，不做拆分。

**方案**：① 改写提示词升级：先判是否复合问题（≥2 个可独立回答的子问题），是则输出子查询列表（换行分隔），否则原句；② `_prepare_parallel` 按子查询并行检索，候选池按 `chunk_id` 去重合并（同切片多路命中保留最高分）；③ 生成侧 prompt 要求分子问题组织答案，某子问题资料未覆盖须明说（复用 P4-7 硬化的拒答措辞）；④ 评测集加复合问题样本（如 q042「怎么请事假？事假扣不扣钱？」双子问题标注）。

**明确不做**：不上 ReAct/Agent 循环——复合问题是「多个独立子问题」，一次分解 + 并行检索 + 一次生成即可；ReAct 留给真正的多步推理任务（对比/汇总类），届时单独立项。

**验收**：复合样本两个子问题都有对应切片进上下文；单问句行为不回退（子查询=1 时与现状等价）；每轮 LLM 调用次数不超过现状 +1。

### P4-10 上下文滚动摘要

> 立项动机（2026-10-17）：用户要求长会话跨轮记忆。现状：MySQL 全量消息持久化（`_trim` 不删消息），`MEMORY_MAX_TURNS=5` 只切「喂 LLM/改写」的窗口（`get_recent_messages`）——**超窗口内容 LLM 不可见**；`MEMORY_SESSION_TTL_SECONDS=6h` 不活跃过期。

**方案**：① `get_recent_messages` 改为「窗口内原文 + 窗口外滚动摘要」：窗口外部分由 LLM 压缩成一段 summary（增量式：新轮次发生后只摘要「上次摘要之后」的部分，避免每轮全量重摘）；② 摘要存 `session` 表新列（迁移 0013）或独立 `session_summary` 表，随会话读写；③ 摘要失败降级为纯窗口（fail-open，与 qa_trace 镜像同姿态）；④ TTL 策略：默认 6h 不变，设置项可调大（运营向）；「永远不忘」= TTL 调大 + 摘要兜底窗口外。

**明确不做**：不做跨会话的用户级长期记忆/画像（那是另一件事，等 P4-10 验证摘要质量后再评估）。

**验收**：>5 轮的会话中，第 6 轮提问引用第 1 轮内容时 LLM 能依据摘要正确响应；摘要调用失败时行为回退现状；每轮摘要 token 增量 ≤ 新增对话的 30%。
