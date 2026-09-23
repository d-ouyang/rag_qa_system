/**
 * 统一 HTTP 客户端：薄封装 fetch，集中处理三件事。
 *
 * 1. **自动带 token**：所有请求统一注入 `Authorization: Bearer <token>`。
 *    散落在各处手动加头是必然出错的（漏一个接口就是 401，而且只有那条链路挂）。
 * 2. **错误格式归一**：后端（FastAPI）返 `{"detail": "..."}`，网关返
 *    `{"error": {"code", "message", "requestId"}}`。业务层只 catch 一种 ApiError，
 *    不用关心错误是谁产生的。
 * 3. **401 统一登出**：token 过期时清本地凭据并通知 UI 回登录页。
 *    放在这里而不是每个调用点，是因为「token 失效」可能发生在任意请求上。
 *
 * 为什么不在这里 import auth store：
 * 会造成 http ↔ store 的循环依赖（store 的 action 又要调 http）。
 * 改用「注册回调」的方式把 token 读写和登出动作注入进来，依赖方向单向清晰。
 */

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** 统一错误对象：status 是 HTTP 码，code 是后端/网关给的稳定错误标识。 */
export class ApiError extends Error {
  status: number
  /** 机器可判定的错误码，如 TOKEN_EXPIRED / TOO_MANY_REQUESTS / UPSTREAM_UNAVAILABLE */
  code: string
  /** 网关生成的请求 id，报障时带上它就能在日志里定位这一次请求 */
  requestId?: string

  constructor(status: number, message: string, code = '', requestId?: string) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

// --------------------------------------------------------------------------- //
// 鉴权钩子（由 auth store 在初始化时注册，避免循环依赖）
// --------------------------------------------------------------------------- //
interface AuthHooks {
  /** 取当前 token（无则返回空串） */
  getToken: () => string
  /** 收到 401 时调用（清凭据 + 回登录页） */
  onUnauthorized: (error: ApiError) => void
}

let hooks: AuthHooks = {
  getToken: () => '',
  onUnauthorized: () => {},
}

export function configureAuth(next: Partial<AuthHooks>): void {
  hooks = { ...hooks, ...next }
}

/** 拼装请求头：有 token 就带上；body 是 JSON 时才声明 Content-Type。 */
function buildHeaders(json: boolean): HeadersInit {
  const headers: Record<string, string> = {}
  const token = hooks.getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

/**
 * 把响应体解析成 ApiError。
 *
 * 兼容两种错误格式（这正是「统一底层报错」在客户端的落点）：
 *   · 网关： { error: { code, message, detail, requestId } }
 *   · 后端： { detail: "..." }（FastAPI 默认）/ { detail: {...} }
 * 兜底：非 JSON 响应（如 Nginx 的 502 HTML 页）给一句人能看懂的话。
 */
async function toApiError(res: Response): Promise<ApiError> {
  const status = res.status
  const headerRequestId = res.headers.get('X-Request-Id') ?? undefined
  try {
    const body = (await res.json()) as {
      error?: { code?: string; message?: string; requestId?: string }
      detail?: unknown
    }
    if (body.error && typeof body.error === 'object') {
      return new ApiError(
        status,
        body.error.message || `请求失败（HTTP ${status}）`,
        body.error.code ?? '',
        body.error.requestId ?? headerRequestId,
      )
    }
    if (typeof body.detail === 'string') return new ApiError(status, body.detail, '', headerRequestId)
    if (body.detail != null) return new ApiError(status, JSON.stringify(body.detail), '', headerRequestId)
  } catch {
    /* 响应体不是 JSON：走下面的兜底文案 */
  }
  if (status === 502 || status === 503 || status === 504) {
    return new ApiError(
      status,
      '无法连接到问答服务，请确认网关与后端已启动',
      'UPSTREAM_UNAVAILABLE',
      headerRequestId,
    )
  }
  return new ApiError(status, `请求失败（HTTP ${status}）`, '', headerRequestId)
}

/**
 * 统一收尾：非 2xx 一律转成 ApiError，并在 401 时触发登出。
 *
 * `skipAuthRedirect` 用于登录接口本身：账号密码错也是 401，但那种情况
 * 不该走「清凭据 + 跳登录页」的流程（用户本来就在登录页），
 * 而是把「用户名或密码错误」显示在表单上。
 */
async function ensureOk(res: Response, skipAuthRedirect = false): Promise<Response> {
  if (res.ok) return res
  const error = await toApiError(res)
  if (res.status === 401 && !skipAuthRedirect) {
    hooks.onUnauthorized(error)
  }
  throw error
}

// --------------------------------------------------------------------------- //
// 通用请求方法
// --------------------------------------------------------------------------- //
export async function get<T>(path: string): Promise<T> {
  const res = await ensureOk(await fetch(`${API_BASE}${path}`, { headers: buildHeaders(false) }))
  return (await res.json()) as T
}

export async function del<T>(path: string): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, { method: 'DELETE', headers: buildHeaders(false) }),
  )
  return (await res.json()) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: buildHeaders(true),
      body: JSON.stringify(body),
    }),
  )
  return (await res.json()) as T
}

/** 登录专用：401 不触发登出流程，交给调用方在表单上展示错误。 */
export async function postJsonNoAuthRedirect<T>(path: string, body: unknown): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: buildHeaders(true),
      body: JSON.stringify(body),
    }),
    true,
  )
  return (await res.json()) as T
}

export async function patchJson<T>(path: string, body: unknown): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, {
      method: 'PATCH',
      headers: buildHeaders(true),
      body: JSON.stringify(body),
    }),
  )
  return (await res.json()) as T
}

export async function uploadFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  // 注意：FormData 不能手动设 Content-Type（boundary 由浏览器生成），
  // 所以这里走 buildHeaders(false)
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, { method: 'POST', headers: buildHeaders(false), body: form }),
  )
  return (await res.json()) as T
}

/**
 * NDJSON 流式请求：POST + ReadableStream 逐行解析，每解析出一行回调一次。
 *
 * 后端 ask/stream 返回 application/x-ndjson（每行一个 JSON 对象），
 * 不用 EventSource 的原因：它只支持 GET、无法携带请求体，
 * 更要紧的是**没法带 Authorization 头**（token 只能塞进 URL，
 * 而 URL 会进浏览器历史和访问日志，等于泄露凭据）。
 */
export async function postNdjson(
  path: string,
  body: unknown,
  onLine: (frame: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: buildHeaders(true),
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    const error = await toApiError(res)
    if (res.status === 401) hooks.onUnauthorized(error)
    throw error
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  // 流式分块不保证按「行」到达：先攒进 buffer，遇到换行才算一条完整 JSON
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, idx).trim()
      buffer = buffer.slice(idx + 1)
      if (!line) continue
      let frame: unknown
      try {
        frame = JSON.parse(line)
      } catch {
        // 单行解析失败跳过（理论上不会发生，防御半行截断）
        continue
      }
      try {
        onLine(frame)
      } catch (e) {
        // 回调异常不中断整个流，但必须打日志，不能静默吞掉
        console.error('NDJSON 帧处理失败', e, frame)
      }
    }
  }
  const tail = buffer.trim()
  if (tail) {
    try {
      onLine(JSON.parse(tail))
    } catch {
      /* 忽略结尾残片 */
    }
  }
}
