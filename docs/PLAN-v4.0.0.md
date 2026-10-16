# PLAN v4.0.0 — 运行时配置服务（业务开关热更新 · 设置页可操作化）

> 2026-10-16 讨论产出（IDENTITY_MODE=dev→gateway 切换完成后顺势提出）。涉及 api/core/frontend/admin-console 与现有「系统设置」页。
> 铁律不变：每任务 = 迭代文档 + tag `4.0.0-p4.N` + RELEASES + CHANGELOG + 版本号（见 §1 新规则）+ 本表更新状态。
> 判新旧看 `git log` 不看编号；开打前 `git tag | grep 'p4\.'` 查占用。
> 优先级：**P4-1 / P4-2 为本轮核心；P4-3 / P4-4 降为低优先级，待 P4-1/P4-2 落地后视需求排期**（仍为 ConfigService 的消费者，依赖 P4-1）。

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

依赖：P4-2 依赖 P4-1（UI 编辑的是 ConfigService）；P4-3 / P4-4 依赖 P4-1（消费者）。P4-1 独立可先做。

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
