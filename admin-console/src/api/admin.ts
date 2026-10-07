/**
 * 管理端数据接口 —— 全部挂在 `/api/v1/admin/*`（同一个网关、同一个后端进程）。
 *
 * 类型定义刻意与后端 `core/identity.Actor.to_dict()` / `core/user_repo.to_dict()`
 * **逐字段对齐**，不做「前端顺手加个字段」的重映射：偏移一旦发生，
 * 界面上就是「改了没生效」，而排查会先看前端。
 */
import { del, get, patchJson, postJson } from './http'

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

// --------------------------------------------------------------------------- //
// 员工
// --------------------------------------------------------------------------- //
export interface UserListResult {
  items: UserRow[]
  total: number
}

export interface CreateUserPayload {
  username: string
  employee_no: string
  display_name?: string
  email?: string | null
  phone?: string | null
  gender?: string | null
  department_id?: number | null
  position_id?: number | null
  role?: Role
}

export interface CreateUserResult {
  user: UserRow
  /** ⚠️ 只出现这一次：后端不存明文、不再有第二次查看的接口 */
  temporary_password: string
}

export function fetchUsers(params: {
  keyword?: string
  department_id?: number | null
  role?: string | null
  status?: string | null
  include_resigned?: boolean
}): Promise<UserListResult> {
  const query = new URLSearchParams()
  if (params.keyword) query.set('keyword', params.keyword)
  if (params.department_id != null) query.set('department_id', String(params.department_id))
  if (params.role) query.set('role', params.role)
  if (params.status) query.set('status', params.status)
  if (params.include_resigned) query.set('include_resigned', 'true')
  const suffix = query.toString()
  return get<UserListResult>(`/api/v1/admin/users${suffix ? `?${suffix}` : ''}`)
}

/**
 * 单个员工详情。**手机号是原值**（列表接口脱敏）。
 * 编辑弹窗必须用它，否则会把脱敏串当新值写回库 —— 详见 UsersView.openEdit 的注释。
 */
export function fetchUser(id: number): Promise<UserRow> {
  return get<UserRow>(`/api/v1/admin/users/${id}`)
}

export function createUser(payload: CreateUserPayload): Promise<CreateUserResult> {
  return postJson<CreateUserResult>('/api/v1/admin/users', payload)
}

export function updateUser(
  id: number,
  payload: Partial<Pick<UserRow, 'display_name' | 'email' | 'phone' | 'gender' | 'department_id' | 'position_id'>>,
): Promise<UserRow> {
  return patchJson<UserRow>(`/api/v1/admin/users/${id}`, payload)
}

export function setUserStatus(id: number, status: UserStatus): Promise<UserRow> {
  return patchJson<UserRow>(`/api/v1/admin/users/${id}/status`, { status })
}

export function setUserRole(id: number, role: Role): Promise<UserRow> {
  return patchJson<UserRow>(`/api/v1/admin/users/${id}/role`, { role })
}

// --------------------------------------------------------------------------- //
// 密码（P2-13c）
// --------------------------------------------------------------------------- //
export interface ResetPasswordResult {
  user: UserRow
  temporary_password: string
}

/** 重置为一次性临时密码 + 强制改密。明文**只在这一次响应里**。 */
export function resetPassword(id: number): Promise<ResetPasswordResult> {
  return postJson<ResetPasswordResult>(`/api/v1/admin/users/${id}/password/reset`)
}

export function setMustChange(id: number, mustChange: boolean): Promise<UserRow> {
  return patchJson<UserRow>(`/api/v1/admin/users/${id}/password/must-change`, {
    must_change: mustChange,
  })
}

export interface PasswordBoard {
  must_change: BoardItem[]
  expiring_soon: BoardItem[]
  expired: BoardItem[]
  stale_login: BoardItem[]
  locked: BoardItem[]
  counts: {
    must_change: number
    expiring_soon: number
    expired: number
    stale_login: number
    locked: number
  }
  generated_at: string
  policy: { expire_days: number; warn_days: number }
}

export interface BoardItem {
  id: number
  username: string
  display_name: string
  department_id: number | null
  status: UserStatus
  must_change: boolean
  expire_in_days: number | null
  last_login_at: string | null
}

export function fetchPasswordBoard(): Promise<PasswordBoard> {
  return get<PasswordBoard>('/api/v1/admin/password/board')
}

// --------------------------------------------------------------------------- //
// 审计日志（P2-13d）—— 只有读，没有写
// --------------------------------------------------------------------------- //
export interface AuditRow {
  id: number
  actor_user_id: number | null
  actor_username: string
  actor_role: string | null
  action: string
  action_label: string
  target_type: string
  target_id: number | null
  target_label: string | null
  detail: Record<string, unknown> | null
  ip: string | null
  created_at: string
}

export interface AuditListResult {
  items: AuditRow[]
  total: number
  limit: number
  offset: number
  /** 动作候选 —— 来自服务端白名单，前端不自己维护一份（两份一定会漂） */
  actions: { value: string; label: string }[]
  target_types: string[]
}

export function fetchAuditLogs(params: {
  actor?: string | null
  action?: string | null
  target_type?: string | null
  target_id?: number | null
  keyword?: string | null
  limit?: number
  offset?: number
}): Promise<AuditListResult> {
  const query = new URLSearchParams()
  if (params.actor) query.set('actor', params.actor)
  if (params.action) query.set('action', params.action)
  if (params.target_type) query.set('target_type', params.target_type)
  if (params.target_id != null) query.set('target_id', String(params.target_id))
  if (params.keyword) query.set('keyword', params.keyword)
  query.set('limit', String(params.limit ?? 50))
  query.set('offset', String(params.offset ?? 0))
  const suffix = query.toString()
  return get<AuditListResult>(`/api/v1/admin/audit-logs?${suffix}`)
}

// --------------------------------------------------------------------------- //
// 部门与职位
// --------------------------------------------------------------------------- //
export interface DepartmentPayload {
  code: string
  name: string
  parent_id?: number | null
  leader_user_id?: number | null
  sort_order?: number
}

export function createDepartment(payload: DepartmentPayload): Promise<DepartmentRow> {
  return postJson<DepartmentRow>('/api/v1/admin/departments', payload)
}

export function updateDepartment(
  id: number,
  payload: Partial<Pick<DepartmentRow, 'name' | 'parent_id' | 'leader_user_id' | 'sort_order'>>,
): Promise<DepartmentRow> {
  return patchJson<DepartmentRow>(`/api/v1/admin/departments/${id}`, payload)
}

export function deleteDepartment(id: number): Promise<{ ok: boolean }> {
  return del<{ ok: boolean }>(`/api/v1/admin/departments/${id}`)
}

export interface PositionPayload {
  code: string
  name: string
  level?: string | null
  sequence?: Sequence
}

export function createPosition(payload: PositionPayload): Promise<PositionRow> {
  return postJson<PositionRow>('/api/v1/admin/positions', payload)
}

export function updatePosition(
  id: number,
  payload: Partial<Pick<PositionRow, 'code' | 'name' | 'level' | 'sequence'>>,
): Promise<PositionRow> {
  return patchJson<PositionRow>(`/api/v1/admin/positions/${id}`, payload)
}

export function deletePosition(id: number): Promise<{ ok: boolean }> {
  return del<{ ok: boolean }>(`/api/v1/admin/positions/${id}`)
}
