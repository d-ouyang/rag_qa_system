# 前端流式展示消息逻辑总结 + 优化建议 + SSE 适配评估

> 范围：仅限 `rag-qa-system/frontend` 中「用户提问 → 答案逐帧流式展示」这条链路。
> 对照对象：ima 知识库「向日葵的知识库」中的《前端AI大模型SSE流式完整工程方案.md》。
> 结论先行：**本项目没有用 SSE，用的是 NDJSON over `fetch` + `ReadableStream`；这是比 SSE 更合适的选型，应保持。真正该补的是「停止生成」和「滚动意图检测」，而非换成 SSE。**

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 |
|------|------|
| 这里用 SSE 了吗？ | **没有。** 用的是 `application/x-ndjson` + `fetch` + `ReadableStream`（POST，带 `Authorization` 头）。 |
| 用 SSE 是不是更好？ | **不是。** 因为必须 POST 传请求体 + Bearer 鉴权，原生 `EventSource` 既不支持 POST 也不支持自定义头，强行用 SSE 反而要 hack。SSE 文档自己在「选型」一节也承认这个缺陷，并推荐 `fetch + ReadableStream 自定义解析器`——和你现在做的本质一致。 |
| 当前链路哪里能优化？ | ① 缺「停止生成」按钮（Abort 能力已预埋但没接线）；② 自动滚动无条件抢占用户；③ 答案按纯文本渲染（未渲染 Markdown）；④ 逐帧同步追加未做 rAF 批量刷新。 |
| 网关层 SSE 文档提到的坑踩了吗？ | `proxy_buffering off` + `proxy_read_timeout 300s` **已正确配置**（`frontend/nginx.conf`），流式不会被缓冲。 |

---

## 1. 当前流式展示链路

### 1.1 时序

```
用户 Enter
  → ChatInput.vue emit('send', q)
  → sessions.ask(q)                        [stores/sessions.ts]
       ├─ push 用户消息 {role:'user', content}
       ├─ push 空 assistant 消息 {role:'assistant', content:'', streaming:true}
       ├─ 取出 reactive 代理 assistantMsg（关键：必须取数组代理对象，否则不触发响应）
       └─ await askStream(q, sessionId, onFrame)
            → postNdjson POST /api/v1/qa/ask/stream   [api/qa.ts → api/http.ts]
                 ├─ fetch + ReadableStream reader
                 ├─ TextDecoder('utf-8') decode(value, {stream:true}) 逐块解码
                 ├─ 按 '\n' 切分 → JSON.parse 每行 → onFrame(frame)
                 └─ onFrame 按 frame.type 分发：
                      meta   → assistantMsg.intent / .sources / lastMeta
                      chunk  → assistantMsg.content += frame.content
                      done   → assistantMsg.usage / .elapsed_ms
                      error  → content += '⚠️ ...'
            → finally: assistantMsg.streaming=false; streaming=false; fetchSessions()
  → MessageBubble.vue 渲染
       ├─ 首帧前且 content 空 → 显示「正在思考…」占位
       ├─ 有 content 且 streaming → 显示文本 + 闪烁光标
       └─ 内容用 {{ message.content }} 纯文本（white-space: pre-wrap，不渲染 Markdown）
  → ChatView.vue
       watch(messages.length, 末条 content) → nextTick → scrollToBottom() 无条件滚到底
```

### 1.2 帧协议（后端契约，来自 `api/routes/qa.py`）

`Content-Type: application/x-ndjson`，每行一个 JSON 对象，联合类型见 `src/types.ts` 的 `StreamFrame`：

```ts
type StreamFrame =
  | { type: 'session'; session_id: string }
  | { type: 'meta'; intent; route; intent_source; standalone_question?; sources? }
  | { type: 'chunk'; content: string }
  | { type: 'done'; elapsed_ms?; usage?; session_usage? }
  | { type: 'error'; status?; detail: string }
```

相比裸 SSE（`data: {"choices":[{"delta":{"content":"..."}}]}`），NDJSON 每行自带 `type` 字段，天然支持「溯源资料 + 意图 + token 用量」等**结构化富元数据**，不用在字符串里再约定结构。

### 1.3 代码落点

| 职责 | 文件 |
|------|------|
| 流式请求入口（NDJSON） | `src/api/qa.ts` → `askStream` → `src/api/http.ts` → `postNdjson` |
| 帧分发 + 消息状态维护 | `src/stores/sessions.ts` → `ask()` |
| 气泡渲染（纯文本 / 思考占位 / 光标 / 引用 / token 徽章） | `src/components/MessageBubble.vue` |
| 自动滚动 | `src/views/ChatView.vue` |
| 类型定义 | `src/types.ts`（`StreamFrame` / `ChatMessage`） |
| 网关流式透传 | `frontend/nginx.conf`（`proxy_buffering off` + `proxy_read_timeout 300s`） |

---

## 2. 与 ima SSE 方案文档的逐条对照

| SSE 文档机制 | 本项目现状 | 一致性 / 相关性 |
|------|------|------|
| 传输：`text/event-stream` + 原生 `EventSource` | **NDJSON + `fetch` + `ReadableStream`** | 不同，但本项目更优（见第 4 章）。文档自身也推荐 `fetch+ReadableStream`。 |
| 断点续流 `Last-Event-ID` | 无（NDJSON 无 `id` 字段；每次问答是独立 POST，重连会重新生成整段，不划算） | 本项目无需求，无需补。 |
| 多字节防乱码 `TextDecoder({stream:true})` | `postNdjson` 已用 `decoder.decode(value, {stream:true})` | ✅ 一致，已处理 UTF-8 跨分片截断。 |
| TCP 粘包/半包缓冲 | 按 `\n` 切分 + `buffer` 字符串存半行，下一帧补齐 | ✅ NDJSON 按行分隔天然解决，比 SSE 的 `\n\n` 块解析更简单。 |
| `AbortController` 手动停止 | `postNdjson` 支持 `signal`，但 **业务层从不创建/传入 `AbortController`，也无停止按钮** | ❌ **缺口**（见 §3-A）。 |
| MD 双层缓冲 + WebWorker + DOMPurify | **不渲染 Markdown**（纯文本展示），整条链路不适用 | 不适用。但答案目前是裸文本（见 §3-C）。 |
| `requestAnimationFrame` 分片渲染 + `content-visibility` | 逐帧 Vue 响应式更新（纯文本，开销小），未做 rAF 批处理 | 部分覆盖。纯文本场景影响有限（见 §3-D）。 |
| 用户滚动检测（暂停自动滚动） | **无条件 `scrollToBottom`** | ❌ **缺口**（见 §3-B）。 |
| Nginx 延长 `proxy_read_timeout` + 关缓冲 | `nginx.conf` 已配 `proxy_buffering off` + `300s` | ✅ 已踩过坑（见 memory 坑 44），无需补。 |

**关键认知**：你没用 SSE，是**正确判断**。SSE 文档的「网络层」真正推荐的实现就是 `fetch + ReadableStream`（为了 POST 和自定义头），它解析的是 `data:` 前缀的 SSE 文本，而你解析的是 NDJSON 行——工程上等价，NDJSON 还更结构化。该文档里真正对你有借鉴价值的，是 **Abort、滚动检测、rAF 批处理、Markdown 安全渲染** 这几块，而不是「改成 SSE」。

---

## 3. 可优化点 & 建议（按优先级）

### A. 缺少「停止生成」按钮（Abort）— P0，功能缺口，影响最大

- **现状**：`ask()` / `askStream(text, sessionId, onFrame, signal)` 已预留 `signal` 参数，但 `sessions.ask` 调用时**从不传 `signal`**；`ChatInput.vue` 流式期间只把按钮禁用，没有「停止」态。
- **后果**：用户无法中断一个长回答（比如答偏了、想重问）。前端已把能力埋好，只差接线。
- **建议**：
  1. `sessions` store 维护 `let currentAbort: AbortController | null`；`ask()` 开头 `currentAbort = new AbortController()`，传给 `askStream(..., currentAbort.signal)`；`finally` 里 `currentAbort = null`。
  2. `ChatInput.vue`：当 `streaming` 时按钮文案改「停止」，点击 `sessions.stopStream()`（调用 `currentAbort?.abort()`）。
  3. **后端配合验证**：`/ask/stream` 是 FastAPI `StreamingResponse`，前端断开连接时 Starlette 会取消生成器。需确认 generator 在 `disconnect` 时**真的退出**（否则后端仍空转消耗 LLM token）。如有必要在 generator 里加 `yield` 间的取消检查。
- **代码落点**：`stores/sessions.ts`（`ask` + 新增 `stopStream`）、`api/qa.ts`（`askStream` 已支持）、`components/ChatInput.vue`（停止按钮）。

### B. 自动滚动无条件抢占（用户滚动检测）— P1，体验

- **现状**：`ChatView.vue` 的 `watch` 在每次 `content` 变更后 `scrollToBottom()`。
- **现象**：用户上滑去看「引用资料」或历史消息时，被新到的帧硬拽回底部，无法稳定阅读。
- **建议**：复用 SSE 文档做法——
  - 监听滚动容器 `scroll` 事件，用户主动上滑（距底超过阈值）时置 `userScrolled=true`；
  - 3s 内无滚动操作才恢复自动滚动；或用户滚回底部附近才恢复；
  - 仅当「用户在底部」或「非用户滚动」时才 `scrollToBottom()`。
- **代码落点**：`views/ChatView.vue`。

### C. 答案未渲染 Markdown（纯文本）— P1/P2，取决于产品决策

- **现状**：`MessageBubble.vue` 用 `{{ message.content }}` 原样展示，LLM 返回的 `#` 标题、`**加粗**`、代码块、表格、列表全部变成裸文本。
- **权衡**：
  - 引入 `markdown-it`（或 `marked`）渲染 → **必须配 `DOMPurify` 防 XSS**（答案内容含检索片段 + 模型输出，虽可信度较高但安全清洗不能省）；
  - 流式场景要处理「半截断语法」（SSE 文档的双层缓冲、反引号成对计数、长度/超时双降级），否则代码块/表格渲染到一半会样式崩坏；
  - **最简零风险方案**：等 `done` 帧后再整体渲染 Markdown（牺牲流式富文本，但绝无截断错乱）。
- **建议**：先和产品确认是否需要富文本。要的话走「markdown-it + DOMPurify + 流式半截断兜底」；不要的话保持纯文本，本条不动作。这是**产品决策，不是纯技术债**。

### D. 逐帧同步追加未做节流/批量刷新 — P2，性能

- **现状**：每个 `chunk` 帧同步 `assistantMsg.content += frame.content`，触发 Vue 响应式 + `watch` 滚动。高频小帧（接口文档实测 248 帧/9.6s）会多次重渲染。
- **影响**：纯文本开销可控，但长回答 + 高频帧会有布局抖动；且每次追加都触发一次 `scrollToBottom`。
- **建议**：攒帧 + `requestAnimationFrame` 批量 flush——把待追加文本累计到局部变量，rAF 回调里一次性 `content += acc`；`done`/错误前确保 flush。和 §3-B 的滚动逻辑共用「是否自动滚动」判断。
- **代码落点**：`stores/sessions.ts`（`ask` 的 `chunk` 分支）+ `views/ChatView.vue`（滚动）。

### E. 小健壮性 nit：`postNdjson` 结尾未 flush 解码器 — P3

- `postNdjson` 循环退出后没有 `decoder.decode()`（不带 `stream:true`）兜底 flush。尾部若有极端残留多字节会丢。实际因为最后一帧是完整 `done` JSON，不影响，但加一行 `decoder.decode()` 更严谨。极低优先级。

### F. `session` 帧类型未消费 — P3

- `StreamFrame` 定义了 `{type:'session'; session_id}`，但 `sessions.ask` 的 handler 没有对应 case。若后端会下发该帧（用于回填真实 session_id），应消费；若不下发，删掉类型定义避免误导。建议核对 `api/routes/qa.py` 是否 `yield` 了 `session` 帧。

---

## 4. SSE 是否适合当前项目（评估结论）

**结论：不适合切换成 SSE，当前 NDJSON 方案更优，应保持。**

理由：

1. **鉴权**：必须带 `Bearer` token。原生 `EventSource` **不能自定义请求头**，token 只能塞进 URL——而 URL 会进浏览器历史与访问日志，等于泄露凭据（这正是 `http.ts` 注释里写明「不用 EventSource」的原因）。SSE 文档自己在「选型/降级」一节也承认这个缺陷。
2. **请求体**：`/ask/stream` 需要 POST 传 `{question, session_id}`；`EventSource` 只支持 GET。要发 POST 就必须回到 `fetch` + `ReadableStream`——也就是你现在的方案。
3. **结构化帧**：NDJSON 每行自带 `type`，天然支持 meta/sources/usage 等富元数据；SSE 的 `data:` 是纯字符串，要自己再约定结构。**你的方案比裸 SSE 更贴合业务**。
4. **断点续流收益有限**：RAG 问答每轮是「一次性生成」，没有长文档续传需求；即便网络抖动，重连会重新生成整段答案（POST 也无法简单用 `Last-Event-ID` 续），性价比低。
5. **网关无关**：`proxy_buffering off` + `proxy_read_timeout 300s` 对任意长连接流式都生效，不分 SSE / NDJSON。

**所以**：你没用 SSE 是正确判断；建议保持 NDJSON，把精力放在 §3 的 A（停止生成）、B（滚动检测），并按产品决策处理 C（Markdown）上。

---

## 5. 优先行动清单

| 优先级 | 项 | 说明 |
|------|------|------|
| **P0** | 加「停止生成」+ Abort | 能力已预埋，只差接线；同步验证后端断连真退出 |
| **P1** | 自动滚动用户意图检测 | 上滑阅读不被拽回底部 |
| **P1/P2** | Markdown 渲染 + DOMPurify | 产品确认要富文本再做；带流式半截断兜底 |
| **P2** | rAF 批量 flush 帧 | 长回答高频帧的布局抖动 |
| **P3** | 解码器 flush / 消费 `session` 帧 | 健壮性收尾 |

---

*附：本文对照的 ima 文档《前端AI大模型SSE流式完整工程方案.md》取自「向日葵的知识库」（KB `001a98704ec06851`），其核心建议（Abort、滚动检测、rAF 分片、MD 安全渲染、nginx 配置）已在本项目逐项比对，相关项已落到 §3。*
