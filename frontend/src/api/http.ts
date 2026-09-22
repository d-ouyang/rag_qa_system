/**
 * 统一 HTTP 客户端：薄封装 fetch，集中处理「后端错误格式 → 前端异常」的转换。
 *
 * 后端所有错误统一返回 {"detail": "..."}（FastAPI 默认），
 * 这里转成带 message 的 Error，业务层只 catch 一种异常。
 */

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (body.detail != null) return JSON.stringify(body.detail)
  } catch {
    /* 响应体不是 JSON 时走兜底文案 */
  }
  return `请求失败（HTTP ${res.status}）`
}

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

export async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: 'DELETE' })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

export async function patchJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

export async function uploadFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', body: form })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

/**
 * NDJSON 流式请求：POST + ReadableStream 逐行解析，每解析出一行回调一次。
 *
 * 后端 ask/stream 返回 application/x-ndjson（每行一个 JSON 对象），
 * 不用 EventSource 的原因：它只支持 GET、无法携带请求体。
 */
export async function postNdjson(
  path: string,
  body: unknown,
  onLine: (frame: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) throw new ApiError(res.status, await parseError(res))

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
