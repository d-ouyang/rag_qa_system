/**
 * 鉴权接口（经 NestJS 网关）。
 *
 * 注意登录路径是 `/api/auth/login` 而不是 `/api/v1/...`：
 * 网关只把 `/api/v1/*` 转发给后端，鉴权接口是网关自己的能力，不经过后端。
 * 前端因此不需要知道后端在哪 —— 它只跟一个地址（网关）打交道。
 */
import { get, postJson, postJsonNoAuthRedirect } from './http'

export interface LoginResult {
  access_token: string
  token_type: 'Bearer'
  /** token 有效期（秒） */
  expires_in: number
  user: { username: string }
}

export interface MeResult {
  user: { userId: string; username: string }
}

/** 登录换 token。401 由调用方处理（展示「用户名或密码错误」）。 */
export function login(username: string, password: string): Promise<LoginResult> {
  return postJsonNoAuthRedirect<LoginResult>('/api/auth/login', { username, password })
}

/** 校验当前 token 是否仍有效；无效会抛 401（已登录用户刷新页面时用）。 */
export function fetchMe(): Promise<MeResult> {
  return get<MeResult>('/api/auth/me')
}

/**
 * 登出。
 *
 * JWT 无状态，服务端没有会话可销毁，这里调用只是让「登出」在日志里留痕、
 * 并为将来加 token 黑名单预留接口。前端无论如何都要清本地凭据，
 * 所以这里失败也不该阻塞登出（调用方 catch 掉即可）。
 */
export function logout(): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>('/api/auth/logout', {})
}
