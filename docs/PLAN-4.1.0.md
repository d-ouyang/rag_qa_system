# PLAN — rag-qa-system v4.1.0（Harness 契约对齐）

> 立项 2026-11-15 · 主线参考：工作区根《迭代计划-harness工程化-office2.2.0-rag4.1.0.md》§3
> 定位：RAG 是**带治理的固定管线**——E 层（Agent Loop / durable execution）不做；
> 力气花在：工具契约化、计量外移、事件流统一、评测门禁。
> 分支：`feature/4.1.0-contract` · 版本号 `4.1.0-pN.M`（老 tag 永不重打）

## Phase ①：检索工具契约化（p1.x）

| # | 步骤 | 状态 |
|---|---|---|
| ①1a | ToolSpec 契约 + RagToolRegistry + 集中批注（含 chunk_rebuild 高危标记） | ✅ **p1.0** |
| ①1b | 调用点收编：retriever/rag_chain/bm25/embedding/mineru/parsing 的直调改为 registry 取用 | ⬜ p1.1 |
| ①1c | 判据验收：检索链路无绕过 registry 的直调（测试断言/静态检查） | ⬜ p1.1 |
| ①2 | 检索结果回灌规范：截断+结构化统一；上下文注入长度上限可配 | ⬜ p1.2 |

## Phase ②：计量外移 + 降级链（p2.x）

| # | 步骤 | 状态 |
|---|---|---|
| ②1 | quota 三件套判定逻辑外移 NestJS 网关（FastAPI 只留查余额/接 429） | ⬜ |
| ②2 | LLM/embedding 收口 + **硅基流动 502 → 重试 + 本地 bge-m3 降级**（Ollama 本地 → DeepSeek API 链路） | ⬜ |
| ②3 | usage 归因 tenant/user/run（对齐 office B2） | ⬜ |

## Phase ③：事件流统一（p3.x）

| # | 步骤 | 状态 |
|---|---|---|
| ③1 | qa_trace + audit 按 Event 契约合成单一 append-only 流 + query_id 回放接口 | ⬜ |

## Phase ④：评测闭环（p4.x）

| # | 步骤 | 状态 |
|---|---|---|
| ④1 | groundedness + RAGAS + QA 直击集 → golden set；**切分/Prompt/模型改动必须过回归门禁**（接入 CI） | ⬜ |
| ④2 | session_summary / qa_cache / memory_manager 纳入统一 context budget（对齐 office A4 Policy） | ⬜ |

## 明确不做

Temporal / K8s / Langfuse 全家桶（4C4G 单机）；durable execution；重写为 dsh 插件。
协议压在 OpenAI-compatible 上，不绑厂商 SDK。
