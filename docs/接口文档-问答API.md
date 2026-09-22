# 问答 API 接口文档

- 服务：企业级 RAG 智能问答系统（rag-qa-system）
- Base URL：`http://localhost:8000`
- 统一前缀：`/api/v1/qa`
- 交互格式：请求/响应均为 `application/json`（流式接口为 `application/x-ndjson`）
- 在线文档：启动服务后访问 `http://localhost:8000/docs`（Swagger UI）
- 启动命令：`make api`（等价于 `.venv/bin/uvicorn api.main:app --reload --port 8000`）

## 能力说明

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 多轮对话记忆 | `core/memory_manager.py` | 按 `session_id` 隔离历史，保留最近 10 轮（可配 `MEMORY_MAX_TURNS`），闲置 6 小时过期（`MEMORY_SESSION_TTL_SECONDS`） |
| 意图识别 | `core/intent_router.py` | ollama 小模型（默认 qwen2.5:3b）做七类意图分类（六类知识型 + 闲聊），意图驱动回答模板选择；模型不可用时降级为打分制关键词规则 |
| RAG 问答链 | `core/rag_chain.py` | LCEL 串联：问题重写 → 向量召回 → CrossEncoder 精排 → 提示词 → LLM → 字符串解析 |
| 接口层 | `api/routes/qa.py` | 只做协议转换与参数校验，不含业务逻辑 |

## 错误码约定

| 状态码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 业务参数错误（如空问题） |
| 422 | 请求体校验失败（pydantic 自动返回） |
| 502 | 下游不可用（LLM 服务 / 向量库异常），可重试 |

---

## 1. 同步问答

`POST /api/v1/qa/ask`

一次请求拿到完整答案。意图识别为闲聊（chitchat）时不做检索，直接由 LLM 回答。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| question | string | 是 | 用户问题，1~2000 字 |
| session_id | string | 否 | 会话 id。不传则服务端生成并在响应中返回，客户端保存后下次带上即可续聊 |

```bash
curl -X POST http://localhost:8000/api/v1/qa/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是向量数据库？", "session_id": "demo-001"}'
```

### 响应体

| 字段 | 类型 | 说明 |
|---|---|---|
| session_id | string | 会话 id（客户端应保存，续聊时回传） |
| answer | string | 模型生成的回答 |
| intent | string | 细粒度意图：`knowledge_query` / `operation_guide` / `policy_consult` / `comparison_analysis` / `data_statistics` / `troubleshooting` / `chitchat` |
| route | string | 路由结论：`rag_qa`（走了检索）/ `chitchat`（未检索） |
| intent_source | string | 意图来源：`llm`（小模型）/ `rule`（规则兜底） |
| standalone_question | string\|null | 结合历史改写后的独立问题（闲聊时为 null） |
| sources | array | 引用资料列表（见下表），闲聊时为空数组 |
| elapsed_ms | number | 本次问答总耗时（毫秒） |

`sources[]` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| index | int | 资料序号（与 prompt 中【资料N】对应） |
| source | string | 来源文件路径 |
| snippet | string | 片段摘要（前 200 字） |
| rerank_score | number\|null | 重排分数 0~1，越大越相关 |
| vector_similarity | number\|null | 向量余弦相似度 -1~1 |

### 响应示例

```json
{
  "session_id": "demo-002",
  "answer": "根据现有资料无法回答该问题。请提供相关资料后再次提问。",
  "intent": "knowledge_query",
  "route": "rag_qa",
  "intent_source": "llm",
  "standalone_question": "向量数据库和关系型数据库有什么区别？",
  "sources": [],
  "elapsed_ms": 68923.0
}
```

> 上例为第二轮提问「它和关系型数据库有什么区别？」——服务端结合历史把
> 「它」重写为「向量数据库」（standalone_question 字段），说明多轮记忆生效。

---

## 2. 流式问答（NDJSON）

`POST /api/v1/qa/ask/stream`

逐 token 返回答案。请求体与同步问答相同；响应 `Content-Type: application/x-ndjson`，每行一个 JSON 对象：

| 帧类型 | 时机 | 内容 |
|---|---|---|
| `session` | 第 1 帧 | `{"type":"session","session_id":"..."}` |
| `meta` | 第 2 帧 | 意图、溯源资料、重写后的问题（字段同同步响应） |
| `chunk` | 中间 N 帧 | `{"type":"chunk","content":"..."}` 答案文本增量 |
| `done` | 最后 1 帧 | `{"type":"done","elapsed_ms":...}` |
| `error` | 异常时 | `{"type":"error","status":502,"detail":"..."}`（流式无法改状态码，错误以帧下发） |

```bash
curl -N -X POST http://localhost:8000/api/v1/qa/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "你好", "session_id": "demo-003"}'
```

```ndjson
{"type": "session", "session_id": "demo-003"}
{"type": "meta", "intent": "chitchat", "route": "chitchat", "intent_source": "llm", "standalone_question": null, "sources": []}
{"type": "chunk", "content": "你好"}
{"type": "chunk", "content": "！"}
{"type": "done", "elapsed_ms": 4210.5}
```

> 前端用 `fetch` + `ReadableStream` 按行解析即可，无需 EventSource（后者只支持 GET）。

---

## 3. 列出全部会话

`GET /api/v1/qa/sessions`

```bash
curl http://localhost:8000/api/v1/qa/sessions
```

```json
[
  {"session_id": "demo-001", "message_count": 2, "last_active": 1790052155.2},
  {"session_id": "demo-002", "message_count": 4, "last_active": 1790052281.9}
]
```

---

## 4. 查看会话历史

`GET /api/v1/qa/sessions/{session_id}`

```bash
curl http://localhost:8000/api/v1/qa/sessions/demo-002
```

```json
{
  "session_id": "demo-002",
  "message_count": 4,
  "messages": [
    {"role": "user", "content": "什么是向量数据库？"},
    {"role": "assistant", "content": "根据现有资料无法回答该问题。..."}
  ]
}
```

> `message_count`：1 轮问答 = 2 条消息（user + assistant）。

---

## 5. 清空会话记忆

`DELETE /api/v1/qa/sessions/{session_id}`

幂等：会话不存在也返回 200（`cleared: false`）。

```bash
curl -X DELETE http://localhost:8000/api/v1/qa/sessions/demo-002
```

```json
{"session_id": "demo-002", "cleared": true}
```

---

## 6. 链路健康检查

`GET /api/v1/qa/health`

只读状态，不触发真实 LLM 调用，不泄露密钥（只返回 `has_api_key` 布尔值）。

```bash
curl http://localhost:8000/api/v1/qa/health
```

```json
{
  "status": "ok",
  "llm": {"provider": "siliconflow", "model_name": "Qwen/Qwen3-8B", "has_api_key": true, "..." : "..."},
  "retriever": {"top_k": 5, "use_reranker": true, "vector_store_type": "chroma"},
  "memory": {"max_turns": 10, "ttl_seconds": 21600, "session_count": 2},
  "intent_classifier": {"provider": "ollama", "llm_available": true}
}
```

---

## 相关配置项（.env）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| MEMORY_MAX_TURNS | 10 | 单会话最大对话轮数 |
| MEMORY_SESSION_TTL_SECONDS | 21600 | 会话闲置过期时间（秒） |
| INTENT_LLM_PROVIDER | ollama | 意图识别小模型 provider |
| INTENT_LLM_MODEL_NAME | （空=用 OLLAMA_MODEL_NAME） | 意图识别模型 |
| INTENT_LLM_TIMEOUT | 10 | 意图分类超时（秒），超时走规则兜底 |

## 前端接入要点

1. 首次提问不传 `session_id`，从响应中取出后本地保存；
2. 后续提问带上同一 `session_id` 即可实现多轮上下文（「它」「上面说的」会被自动改写为独立问题）；
3. 点「新对话」时调用 `DELETE /sessions/{session_id}` 并清空本地 id；
4. `intent_source=rule` 表示意图识别小模型不可用，回答质量不受影响，只是意图判断精度下降；`intent` 是细粒度标签（用于选回答模板），`route` 才是「有没有走检索」的结论；
5. 回答是否引用资料看 `sources` 是否非空，前端可据此渲染「引用来源」折叠面板。
