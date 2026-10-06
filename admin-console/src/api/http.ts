/**
 * 统一 HTTP 客户端 —— 与主前端 `frontend/src/api/http.ts` 同一套写法，只多一件。
 *
 * 三件事是一样的：自动带 token、错误格式归一（网关 `/ 后端` 两种结构）、
 * 401 统一登出。多出来的第四件是 **403：权限不足单独拎出来** ——
 *
 *   主应用里 403 基本不会发生（页面达不到），而管理端恰恰相反：
 *   HR 账号天天会遇到「只能改资料、不能重置密码」。把它和 401 混为一谈，
 *   用户看到的就是「登录状态已失效」这种指错了方向的话。
 *   所以 ApiError 带上 status，由调用方决定展示哪个文案。
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

interface AuthHooks {
  getToken: () => string
  onUnauthorized: (error: ApiError) => void
}

let hooks: AuthHooks = {
  getToken: () => '',
  onUnauthorized: () => {},
}

export function configureAuth(next: Partial<AuthHooks>): void {
  hooks = { ...hooks, ...next }
}

function buildHeaders(json: boolean): HeadersInit {
  const headers: Record<string, string> = {}
  const token = hooks.getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

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
  if (status === 403) {
    return new ApiError(status, '没有权限执行该操作', 'FORBIDDEN', headerRequestId)
  }
  return new ApiError(status, `请求失败（HTTP ${status}）`, '', headerRequestId)
}

/**
 * 统一收尾：非 2xx 一律转成 ApiError，并在 401 时触发登出。
 *
 * `skipAuthRedirect` 给登录接口自己用：账密错误也是 401，但那不该走
 * 「清凭据 + 回登录页」（用户本来就在登录页）。
 */
async function ensureOk(res: Response, skipAuthRedirect = false): Promise<Response> {
  if (res.ok) return res
  const error = await toApiError(res)
  if (res.status === 401 && !skipAuthRedirect) {
    hooks.onUnauthorized(error)
  }
  throw error
}

export async function get<T>(path: string): Promise<T> {
  const res = await ensureOk(await fetch(`${API_BASE}${path}`, { headers: buildHeaders(false) }))
  return (await res.json()) as T
}

/** `body` 可省：像「重置密码」这种无请求体的动作不该被迫传一个 `{}`。 */
export async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: buildHeaders(true),
      body: body === undefined ? undefined : JSON.stringify(body),
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

export async function patchJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, {
      method: 'PATCH',
      headers: buildHeaders(true),
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  )
  return (await res.json()) as T
}

export async function del<T>(path: string): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, { method: 'DELETE', headers: buildHeaders(false) }),
  )
  return (await res.json()) as T
}
