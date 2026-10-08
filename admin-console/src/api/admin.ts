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
// 知识库写权限档位（P2-14a / 14f）
// --------------------------------------------------------------------------- //
/**
 * 五档知识库写权限。
 *
 * ⚠️ **刻意不复用 `Role`** —— `role` 管「能不能进管理端」，
 * 而 `kb_role` 管「能不能删全公司共用的那个库」。两者正交：
 * `role=user + kb_role=superadmin` 是合法且常用组合
 * （让一个不碰员工数据的员工去维护知识库）。
 * 把它们塞进同一个类型里会诱使人写成「超管才能管知识库」——
 * 那样就把 D14（按人授权的独立维度）改回按角色授权了。
 *
 * ⚠️ 这里**不维护 `KB_ROLE_LABEL`** —— 标签与每档能力由后端
 * `GET /api/v1/admin/options` 的 `kb_roles` 字典下发（见 `Options`）。
 * 前端自己再写一份标签/能力表，就是 13d 与 14a 各踩过一次的那个坑：
 * **两份清单各自漂，且漂了不报错**（界面显示 A、判定按 B）。
 */
export type KbRole = 'none' | 'ops' | 'qa' | 'dev' | 'superadmin'

/** `/options` 下发的单档描述（`value` 与 `KbRole` 对齐）。 */
export interface KbRoleOption {
  value: KbRole
  /** 长标签（带能力说明）→ 给徽章与确认弹窗。 */
  label: string
  /**
   * 短名（只有档位名）→ 给**宽度受限**的行内下拉。
   *
   * ⚠️ 14f 实测：长标签最长 21 个汉字，放进表格行内下拉会把整张表
   * 撑到横向溢出、连带把左边几列压成竖排单字（部门名变成「总/部/职/能/中/心」）。
   * **没有 `short_label` 的老后端**（升级错配）回落成长标签 ——
   * 宁可挤一点也不要空白下拉。
   */
  short_label?: string
  capabilities: {
    upload: boolean
    delete: boolean
    reindex: boolean
  }
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
  /** P2-14a新增：知识库写权限档位。**与 `role` 正交**，不是它的子集。 */
  kb_role: KbRole
  /**
   * P2-15b：个人月度 token 额度。`0` = 不限（用全局默认）。
   *
   * ⚠️ **与「生效额度」不是一回事**：生效额度 =
   * `token_quota_monthly > 0 ? 它 : options.token_quota.default_monthly`。
   * 界面要显示「生效额度」时必须走那条计算，不能直接显示这个字段 ——
   * 显示 0 而实际生效的是 50000，管理员会以为「他没额度」而去改一个没用的数。
   */
  token_quota_monthly: number
  status: UserStatus
  identity_source: string
  permissions: {
    staff: boolean
    reset_password: boolean
    manage_org: boolean
    /** 知识库三档能力。**判据唯一处是后端 `core/kb_acl.py`**，
     *  这里只转发 —— 前端不自己算「哪个档能干什么」（那是 13d「两份清单
     *  各自漂」的形状，14a 又踩过一次）。 */
    kb_upload: boolean
    kb_delete: boolean
    kb_reindex: boolean
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
  /** P2-14a：知识库写权限档位（与 `role` 正交）。 */
  kb_role: KbRole
  /**
   * P2-15b：个人月度 token 额度。`0` = 不限（用全局默认）。
   *
   * ⚠️ **与「生效额度」不是一回事**：生效额度 =
   * `token_quota_monthly > 0 ? 它 : options.token_quota.default_monthly`。
   * 界面要显示「生效额度」时必须走那条计算，不能直接显示这个字段 ——
   * 显示 0 而实际生效的是 50000，管理员会以为「他没额度」而去改一个没用的数。
   */
  token_quota_monthly: number
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
  /**
   * P2-14f：知识库写权限五档 —— **标签与每档能力全部后端派生**。
   *
   * ⚠️ 刻意**不在前端维护**这张表：能力判定是安全判据，
   * 前端那份只用于「按钮显不显示」，一旦与后端漂了，
   * 症状是「界面给了权限但接口403」或反过来，且没有任何报错。
   * `tests/test_module12_kb_role.py` 第 6 组逐档比对两边的 capabilities。
   */
  kb_roles: KbRoleOption[]
  /**
   * P2-15b：额度相关参数**全部后端下发**，前端不硬编码任何数字。
   *
   * ⚠️ 理由与 `kb_roles` 同源：改了 `.env` 的阈值而界面没变，
   * 管理员会以为「设了没用」。而额度直接决定「谁超了」这个显示，
   * 前端写死一份就是又一份会自己漂的清单。
   */
  token_quota: {
    /** 全局默认月度额度；`0` = 不限。 */
    default_monthly: number
    /** 用到这个百分比开始提醒（`quota_policy.STATUS_WARN`）。 */
    warn_percent: number
    /** 用到这个百分比算「已用完」（`STATUS_OVER`）。 */
    over_percent: number
    /** 结算周期起始日（1 = 自然月）。 */
    period_start_day: number
    /** 档位中文标签由后端 `quota_policy.STATUS_LABELS` 一处给出。 */
    status_labels: Record<'ok' | 'warn' | 'over', string>
    /** 输入下界（0 = 不限）；上界故意不给 —— 给个人类可读上界只会被人当「建议额度」。 */
    min_monthly: number
  }
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

/**
 * 下发 / 收回知识库写权限（P2-14f）。
 *
 * ⚠️ `kb_role` 的类型是 `KbRole` 而**不是 `string`** —— 让 TypeScript
 * 在编译期挡住 `'superadminn'` 这类拼写错误。后端另有严格校验（非规范值 400），
 * 但那是运行时；这里要的是**写错时编辑器就红**。
 *
 * 两条后端规则（前端要读得懂报错文案）：
 *   · 不能改自己的档位往下调（降权会让登录立刻失效，恢复要找别人）；
 *   · 值没变时后端不动库（不会把你白踢下线）。
 */
export function setUserKbRole(id: number, kbRole: KbRole): Promise<UserRow> {
  return patchJson<UserRow>(`/api/v1/admin/users/${id}/kb-role`, { kb_role: kbRole })
}

/**
 * P2-15b：下发月度 token 额度。
 *
 * ⚠️ 类型是 `number`（不是 `0 | 50000 | 100000` 那样的联合类型）：
 * 额度是**连续的数字**而不是档位名，枚举它既写不完也没有正当理由。
 * 判据在后端（负数 400、只有管理员能改），前端只负责
 * 「把明显不合理的挡掉」——而**不要**在前端重复一遍判据。
 *
 * ⚠️ 这条接口**不会让人用不了系统**（只提醒不阻断，用户 2026-10-07 拍板），
 * 所以界面上**不要**写「禁用后他将无法提问」这类话。
 */
export function setUserTokenQuota(id: number, quotaMonthly: number): Promise<UserRow> {
  return patchJson<UserRow>(`/api/v1/admin/users/${id}/token-quota`, {
    quota_monthly: quotaMonthly
  })
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
