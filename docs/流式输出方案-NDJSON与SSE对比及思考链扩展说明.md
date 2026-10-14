# 流式输出方案总结：NDJSON vs SSE，以及思考过程 / Agent 步骤展示的扩展路径

> 2026-10-09 · 纯梳理文档，不含任何代码改动。
> 依据当前实现：后端 `api/routes/qa.py` + `core/rag_chain.py` + `core/llm_client.py`，前端 `frontend/src/api/http.ts` + `types.ts`。

---

## 一、现状方案是什么

你用的不是浏览器 SSE（EventSource），而是 **NDJSON 流**（Newline-Delimited JSON）：

| 层 | 实现 |
|---|---|
| 后端 | `StreamingResponse`，`media_type="application/x-ndjson"`，每行一个 JSON 对象（`qa.py:358`） |
| 帧协议 | `session` → `meta`（意图/溯源/重写后问题）→ `chunk`×N（正文增量）→ `done`（耗时/token）；出错发 `error` 帧 |
| 前端 | 普通 `fetch` POST + `res.body.getReader()`，buffer 攒字节、按 `\n` 切行、逐行 `JSON.parse` 后回调（`http.ts` 的 `postNdjson`） |
| 链路 | `rag_chain.stream()` 产 dict 事件 → `event_generator()` 逐个 `json.dumps + "\n"` |

本质上它**就是**「HTTP 分块传输 + 每行一个 JSON」，和 SSE 一样是服务端单向流，只是没有套 SSE 那层 `data:`/`event:` 的文本协议外壳。

## 二、和传统 SSE 的区别

| 维度 | SSE（EventSource） | NDJSON（现状） |
|---|---|---|
| 请求方式 | **只支持 GET**，无法携带请求体 | 任意 method，**POST + JSON body** |
| 请求头 | EventSource **没法加自定义头**（Authorization 带不上） | 正常带 `Authorization: Bearer` |
| Token 传递 | 只能塞 URL → 进浏览器历史/访问日志 = 凭据泄露 | 走 header，安全 |
| 断线重连 | 浏览器**自动重连** + `Last-Event-ID` 续传 | 无自动重连，要自己做（现状是一次性流，断了就 error 收尾） |
| 解析 | 浏览器代解析 `data:` 块 | 自己做 buffer→行→JSON（`postNdjson` 约 40 行） |
| 多事件类型 | 原生 `event:` 字段 | 用 JSON 里的 `type` 字段表达（等价） |
| 代理/缓冲 | 同样必须 `proxy_buffering off`（已设 `X-Accel-Buffering: no`） | 同左 |
| Content-Type | `text/event-stream` | `application/x-ndjson` |

**为什么选 NDJSON（`qa.py:298` 和 `http.ts:239` 的注释就是裁决依据）：**

1. 问答是 POST——要发 `question` + `session_id`，EventSource 天生做不了；
2. 全链路带 JWT 鉴权（网关 fail-closed），EventSource 带不了 Authorization 头，token 塞 URL 是安全事故；
3. 项目里其他接口全是 fetch 封装，`postNdjson` 复用同一套鉴权/错误归一/401 登出钩子，不引入第二套客户端；
4. 帧本身是强类型 JSON（`StreamFrame` 联合类型），TS 侧类型收口比 SSE 的字符串事件干净。

代价（已接受）：没有浏览器级自动重连和 Last-Event-ID；断线靠前端手动重试整轮问答。对内部问答场景，一轮重发的成本可忽略，这个取舍是划算的。

## 三、思考模式为什么吐「很多空串」

根因在模型侧，不在流协议侧：

1. **思考 token 不走 `content` 字段。** Qwen3 系（硅基流动）/ qwen3.5:9b（Ollama）默认开 thinking，思考内容放在独立的 `reasoning_content`（OpenAI 兼容协议）或 `reasoning`（Ollama）字段里。流式循环只读 `chunk.content`（`rag_chain.py:551`），思考阶段 content 一直是空 → 前端收到大量空 chunk，气泡空闪几十秒。
2. **`_stream_text` 只过滤不转发。** 空文本被 `if text` 挡住不发帧（只顺带收 usage），所以用户看到的是「长时间无输出」+ 偶发空串，而不是思考内容。
3. **当前方案的处理 = 干脆关掉思考。** 实测裁决（`llm_client.py:216-230`）：
   - 硅基流动只认**顶层** `enable_thinking=false`；vLLM 风格的 `chat_template_kwargs.enable_thinking` 会被忽略（思考照常、content 长时间为空——当初空闪的根因），且只对 qwen3 系列传，非混合模型收未知参数会 400；
   - Ollama 分支用 `ChatOllama(reasoning=False)`（`OLLAMA_REASONING`），关掉后实测提速约 7 倍。

所以「空串」不是 bug，是协议里思考 token 的正常形态没被消费；关思考只是把它藏起来了。

## 四、要做更复杂 Agent：展示思考过程 / 工具调用 / 每步结果，怎么扩展

好消息：**NDJSON 帧协议天然适合这件事**——加新帧类型即可，`StreamFrame` 是联合类型，`event_generator()` 对事件 dict 透传，前端 `onFrame` 按 `type` 分流。SSE 做同样的事反而要自己拼 `event:` 块。

### 路线 A：只展示思考过程（改动最小）

- 后端 `llm_client` 打开思考开关（硅基流动 `enable_thinking=true` / Ollama `reasoning=true`）；
- `rag_chain` 的流式循环里，除 `chunk.content` 外再读 `chunk.additional_kwargs.get("reasoning_content")`（OpenAI 兼容）或 `additional_kwargs.get("reasoning")`（langchain-ollama），有的话 yield 一个 `{"type": "thinking", "content": ...}` 事件；
- 前端 `StreamFrame` 加 thinking 帧、`ChatMessage` 加 `thinking` 字段，UI 上做可折叠「思考中…」面板（ChatGPT/DeepSeek 式）。
- ⚠️ 三个坑提前记：① 思考 token 会计入 output tokens（额度口径要确认）；② 思考内容**不能**写进会话记忆和精确问答缓存的正文，否则污染下轮上下文与缓存命中；③ `_stream_cached` 路径没有思考可放，回放缓存时 thinking 帧直接跳过。

### 路线 B：完整 Agent（工具调用 + 分步展示）

当前 `rag_chain` 是固定 LCEL 管线（检索→重排→生成），没有工具循环。要做多步 Agent 需要：

- 引擎换成 **LangGraph**（或手写事件循环），用 `astream_events` / `stream_mode="messages"|"updates"` 拿每一步的事件流；
- 帧协议扩展建议：

```jsonc
{"type": "step_start", "step": "retrieve", "label": "检索知识库"}
{"type": "step_end",   "step": "retrieve", "summary": "命中 4 条"}
{"type": "tool_call",  "name": "rag_retrieve", "args": {...}}
{"type": "tool_result","name": "rag_retrieve", "result_preview": "..."}
{"type": "thinking",   "content": "..."}          // 模型思考增量
{"type": "chunk",      "content": "..."}          // 最终答案增量（协议不变）
{"type": "done",       ...}                        // 协议不变
```

- 前端改动集中在三处：`types.ts` 扩联合类型、sessions store 的帧分流逻辑、消息气泡里加「过程时间线」组件；`postNdjson` 解析层**完全不用动**——这就是当初选 NDJSON 的红利。
- ⚠️ 网关侧不用改协议，但 NestJS 代理继续保持「不缓冲、透传流」（现状已满足）；错误帧语义不变。

### 建议节奏

路线 A 是路线 B 的子集且不动管线结构，先做 A 验证「思考帧 → 前端折叠面板」这条链，再评估是否值得为工具循环引入 LangGraph。两步都只扩展 `type` 字段，老帧（session/meta/chunk/done/error）契约不变，历史会话不受影响。
