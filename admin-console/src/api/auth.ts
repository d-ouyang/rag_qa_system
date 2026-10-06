/**
 * 登录接口 —— 与主前端共用**同一个**网关入口，不另开一条认证链路。
 *
 * 「认证只有一个入口」是刻意守的边界：管理端不自己签发 token，
 * 也不去读 MySQL 的 user 表（那是 P2-11c 之后网关才有的能力）。
 * 它在登录这一步与主应用没有任何区别 —— 区别只发生在登录**之后**：
 * `/api/v1/admin/me` 会告诉它这个人是不是管理员。
 */
import { get, postJson, postJsonNoAuthRedirect } from './http'

export interface LoginResult {
  access_token: string
  token_type: 'Bearer'
  expires_in: number
  user: { username: string }
}

export interface MeResult {
  user: { userId: string; username: string }
}

export function login(username: string, password: string): Promise<LoginResult> {
  return postJsonNoAuthRedirect<LoginResult>('/api/auth/login', { username, password })
}

/** 网关侧只验 token 有效性；**角色判定**要等管理端的 /api/v1/admin/me 才给出。 */
export function fetchMe(): Promise<MeResult> {
  return get<MeResult>('/api/auth/me')
}

export function logout(): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>('/api/auth/logout', {})
}
