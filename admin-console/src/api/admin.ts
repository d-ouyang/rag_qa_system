/**
 * 管理端数据接口 —— 全部挂在 `/api/v1/admin/*`（同一个网关、同一个后端进程）。
 *
 * 类型定义刻意与后端 `core/identity.Actor.to_dict()` / `core/user_repo.to_dict()`
 * **逐字段对齐**，不做「前端顺手加个字段」的重映射：偏移一旦发生，
 * 界面上就是「改了没生效」，而排查会先看前端。
 */
import { get } from './http'

// --------------------------------------------------------------------------- //
// 角色与状态
// --------------------------------------------------------------------------- //
export type Role = 'admin' | 'hr' | 'user'
export type UserStatus = 'active' | 'disabled' | 'resigned'
export type Sequence = 'tech' | 'management' | 'function'

export const ROLE_LABEL: Record<Role, string> = {
  admin: '系统管理员',
  hr: '人事',
  user: '普通员工',
}

export const STATUS_LABEL: Record<UserStatus, string> = {
  active: '在职',
  disabled: '停用',
  resigned: '离职',
}

export const SEQUENCE_LABEL: Record<Sequence, string> = {
  tech: '技术',
  management: '管理',
  function: '职能',
}

// --------------------------------------------------------------------------- //
// 数据结构
// --------------------------------------------------------------------------- //
export interface PasswordState {
  must_change: boolean
  expire_in_days: number | null
  expired: boolean
  warn: boolean
}

export interface ActorProfile {
  id: number | null
  username: string
  display_name: string
  role: Role
  status: UserStatus
  identity_source: string
  permissions: {
    staff: boolean
    reset_password: boolean
    manage_org: boolean
  }
  password: PasswordState
}

export interface UserRow {
  id: number
  username: string
  employee_no: string
  display_name: string
  email: string | null
  phone: string | null
  gender: string | null
  department_id: number | null
  position_id: number | null
  role: Role
  status: UserStatus
  must_change_password: boolean
  token_version: number
  failed_login_count: number
  locked_until: string | null
  last_login_at: string | null
  create_time: string
  update_time: string
}

export interface DepartmentRow {
  id: number
  code: string
  name: string
  parent_id: number | null
  leader_user_id: number | null
  sort_order: number
  create_time: string
  children?: DepartmentRow[]
}

export interface PositionRow {
  id: number
  code: string
  name: string
  level: string | null
  sequence: Sequence
  create_time: string
}

export interface Options {
  departments: DepartmentRow[]
  department_tree: DepartmentRow[]
  positions: PositionRow[]
  roles: Role[]
  statuses: UserStatus[]
  sequences: Sequence[]
  password_policy: {
    min_length: number
    expire_days: number
    warn_days: number
    history_keep: number
  }
  server_time: string
}

// --------------------------------------------------------------------------- //
// 当前操作者
// --------------------------------------------------------------------------- //
export function fetchProfile(): Promise<ActorProfile> {
  return get<ActorProfile>('/api/v1/admin/me')
}

export function fetchOptions(): Promise<Options> {
  return get<Options>('/api/v1/admin/options')
}
