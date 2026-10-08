<script setup lang="ts">
/**
 * 员工列表与维护（P2-13b）。
 *
 * --------------------------------------------------------------------------
 * 三个刻意的设计
 * --------------------------------------------------------------------------
 * 1. **离职是「状态」，不是「删除」。** 行操作里有「停用 / 复职 / 离职」，
 *    没有「删除」。离职的人名下还有会话与文档，删行会让它们全部变成悬空引用。
 *    （后端也确实没有 delete 接口 —— 前端不给这个入口是对齐，不是偷懒。）
 *
 * 2. **危险操作都要二次确认。** 停用、离职、改角色会立刻让对方手上的 token
 *    失效（`token_version + 1`）。这不是「改个字段」，是「把人踢下线」，
 *    所以确认框里写清后果，而不是只写「确定吗」。
 *
 * 3. **权限不足的按钮根本不出现。** 人事账号看不到「重置密码」——
 *    但**真正的防线在后端**（绕过界面直接调接口会 403）。
 *    这里只是别让人去点一个必然失败、且失败原因看不懂的按钮。
 */
import { computed, onMounted, ref } from 'vue'
import ModalDialog from '@/components/ModalDialog.vue'
import ToastStack from '@/components/ToastStack.vue'
import { formatDateTime, newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import { ROLE_LABEL, STATUS_LABEL, type KbRole, type KbRoleOption, type Role, type UserRow, type UserStatus } from '@/api/admin'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useConfirmStore } from '@/stores/confirm'

const auth = useAuthStore()
const confirm = useConfirmStore()

// ---------- 数据 ----------
const rows = ref<UserRow[]>([])
// P2-17：真分页（后端 offset/limit + 同条件 total）。
const PAGE_SIZE = 20
const page = ref(1)
const total = ref(0)
const departments = ref<api.DepartmentRow[]>([])
const positions = ref<api.PositionRow[]>([])
/**
 * P2-14f：五档知识库写权限的**标签与能力**，由后端 `/options` 下发。
 * ⚠️ 前端不自己维护这份表 —— 判定唯一处是后端 `core/kb_acl.py`，
 * 前端那份只用于「下拉里显示什么」。两份清单漂了不报错（13d/14a 各踩过一次）。
 */
const kbRoleOptions = ref<KbRoleOption[]>([])
/**
 * P2-15b：额度相关参数**全部后端下发**，前端不硬编码任何数字。
 *
 * ⚠️ 理由与 `kbRoleOptions` 同源：改了 `.env` 的阈值而界面没变，
 * 管理员会以为「设了没用」。
 * ⚠️ 取不到时给 `null` 而不是默认对象 —— 后端没下发就说明版本不匹配，
 *    那时界面**不该假装知道阈值**（fail-closed 的同一种思路，只是这里只影响显示）。
 */
const quotaParams = ref<api.Options['token_quota'] | null>(null)
const loading = ref(false)
const toasts = ref<ToastItem[]>([])

const keyword = ref('')
const filterDept = ref<number | ''>('')
const filterRole = ref<Role | ''>('')
const filterStatus = ref<UserStatus | ''>('')
const includeResigned = ref(false)

function toast(kind: ToastItem['kind'], text: string) {
  const item = newToast(kind, text)
  toasts.value.push(item)
  setTimeout(() => dismiss(item.id), kind === 'ok' ? 2600 : 4200)
}
function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

function deptName(id: number | null): string {
  if (id == null) return '—'
  return departments.value.find((d) => d.id === id)?.name ?? `#${id}`
}
function positionName(id: number | null): string {
  if (id == null) return '—'
  const p = positions.value.find((x) => x.id === id)
  return p ? (p.level ? `${p.name}（${p.level}）` : p.name) : `#${id}`
}

async function loadOptions() {
  const opt = await api.fetchOptions()
  departments.value = opt.departments
  positions.value = opt.positions
  // P2-14f：五档标签与每档能力**全部后端派生**，前端只存不用来算权限。
  // 少了这一行的话下拉会是空的 —— 而空下拉不报错，只是「授权不了」。
  kbRoleOptions.value = opt.kb_roles ?? []
  quotaParams.value = opt.token_quota ?? null
}

/** 筛选条件变化 → 回第 1 页再查（否则停在第 5 页改筛选会看到空页）。 */
/** el-pagination 翻页。 */
function onPage(n: number) {
  page.value = n
  void load()
}

function resetAndLoad() {
  page.value = 1
  void load()
}

async function load() {
  loading.value = true
  try {
    const res = await api.fetchUsers({
      keyword: keyword.value.trim() || undefined,
      department_id: filterDept.value === '' ? null : filterDept.value,
      role: filterRole.value || null,
      status: filterStatus.value || null,
      include_resigned: includeResigned.value,
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    rows.value = res.items
    total.value = res.total
  } catch (e) {
    toast('error', e instanceof ApiError ? e.message : '加载员工列表失败')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadOptions()
  await load()
})

let searchTimer: number | undefined
function onSearchInput() {
  window.clearTimeout(searchTimer)
  // 300ms 防抖：每敲一个字都发一次请求，会把数据库按成筛子
  searchTimer = window.setTimeout(() => void load(), 300)
}

// ---------- 新建 / 编辑 ----------
const editOpen = ref(false)
const editing = ref<UserRow | null>(null)
const form = ref({
  username: '',
  employee_no: '',
  display_name: '',
  email: '',
  phone: '',
  gender: '',
  department_id: '' as number | '' | null,
  position_id: '' as number | '' | null,
  role: 'user' as Role,
})
const formError = ref('')
const submitting = ref(false)

function openCreate() {
  editing.value = null
  form.value = {
    username: '', employee_no: '', display_name: '', email: '', phone: '',
    gender: '', department_id: '', position_id: '', role: 'user',
  }
  formError.value = ''
  editOpen.value = true
}

/**
 * 打开编辑弹窗。**先拉一次详情**再填表，不直接用列表里的那一行。
 *
 * 为什么：列表接口的手机号是脱敏的（`138****0001`）。若直接把它填进表单，
 * 管理员什么都不改点个保存，这个脱敏串就会被当成新值写回库 ——
 * 数据被悄悄污染，且当场不报错（它是个合法字符串）。
 * 后端 `GET /users/{id}` 对 staff 返回原值，所以这里拿得到真手机号。
 */
async function openEdit(row: UserRow) {
  let detail = row
  try {
    detail = await api.fetchUser(row.id)
  } catch (e) {
    toast('warn', e instanceof ApiError ? e.message : '取员工详情失败，将用列表里的信息')
  }
  editing.value = detail
  form.value = {
    username: detail.username,
    employee_no: detail.employee_no,
    display_name: detail.display_name,
    email: detail.email ?? '',
    phone: detail.phone ?? '',
    gender: detail.gender ?? '',
    department_id: detail.department_id ?? '',
    position_id: detail.position_id ?? '',
    role: detail.role,
  }
  formError.value = ''
  editOpen.value = true
}

async function submitForm() {
  submitting.value = true
  formError.value = ''
  try {
    if (editing.value) {
      const updated = await api.updateUser(editing.value.id, {
        display_name: form.value.display_name,
        email: form.value.email || null,
        phone: form.value.phone || null,
        gender: form.value.gender || null,
        department_id: form.value.department_id === '' ? null : form.value.department_id,
        position_id: form.value.position_id === '' ? null : form.value.position_id,
      })
      toast('ok', `${updated.display_name} 的资料已更新`)
    } else {
      const created = await api.createUser({
        username: form.value.username,
        employee_no: form.value.employee_no,
        display_name: form.value.display_name,
        email: form.value.email || null,
        phone: form.value.phone || null,
        gender: form.value.gender || null,
        department_id: form.value.department_id === '' ? null : form.value.department_id,
        position_id: form.value.position_id === '' ? null : form.value.position_id,
        role: form.value.role,
      })
      // 临时密码**只在这里出现一次**，所以必须立刻弹给它看，且不能随手关掉
      tempPassword.value = created.temporary_password
      tempPasswordFor.value = created.user.display_name || created.user.username
      tempPasswordCopied.value = false
      tempOpen.value = true
      toast('ok', `已创建 ${created.user.display_name}`)
    }
    editOpen.value = false
    await load()
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : '保存失败'
  } finally {
    submitting.value = false
  }
}

// ---------- 一次性临时密码 ----------
const tempOpen = ref(false)
const tempPassword = ref('')
const tempPasswordFor = ref('')
const tempPasswordCopied = ref(false)

async function copyTemp() {
  try {
    await navigator.clipboard.writeText(tempPassword.value)
    tempPasswordCopied.value = true
  } catch {
    // 剪贴板在无 HTTPS / 无权限时不可用 —— 这时必须让用户能手工选中
    tempPasswordCopied.value = false
    toast('warn', '浏览器拒绝了剪贴板访问，请手动选中复制')
  }
}

// ---------- 状态与角色 ----------
/**
 * P2-21：确认统一走全局 `confirm` store（这是**适配层**，保持原有
 * `ask(文案, 动作)` 调用点不动 —— 5 处调用点的文案都是「标题？+\n\n+ 说明」
 * 的格式，第一段当标题、其余当正文）。
 *
 * 🔴 这样做的理由：这几个操作（重置密码/改状态/改角色/改档位/改额度）
 *    的确认文案都是逐字打磨过的（写明后果），迁移时**不能动文案**；
 *    抽通用组件只该换「弹窗长什么样」，不该顺手改「说了什么」。
 *    第三参 danger 由调用点显式传（不靠文案里有没有「删除」来猜）。
 */
async function ask(text: string, action: () => Promise<void>, danger = false) {
  const [title, ...rest] = text.split('\n\n')
  const ok = await confirm.ask({
    title,
    text: rest.join('\n\n').trim(),
    danger,
    confirmText: danger ? '确认' : '确认',
  })
  if (ok) await action()
}

function askReset(row: UserRow) {
  ask(
    `重置「${row.display_name}」的密码？\n\n` +
      `会生成一个一次性临时密码，他下次登录必须改掉；他手上的凭证也会立刻失效。`,
    async () => {
      try {
        const res = await api.resetPassword(row.id)
        tempPasswordFor.value = row.display_name
        tempPassword.value = res.temporary_password
        tempPasswordCopied.value = false
        tempOpen.value = true
        await load()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '重置失败')
      }
    },
    true,
  )
}

function changeStatus(row: UserRow, status: UserStatus) {
  const label = STATUS_LABEL[status]
  ask(
    `把「${row.display_name}」改为${label}？\n\n` +
      `他手上的登录凭证会立刻失效（token_version + 1），需要重新登录。`,
    async () => {
      try {
        await api.setUserStatus(row.id, status)
        toast('ok', `${row.display_name} 已改为${label}`)
        await load()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '操作失败')
        // 同 changeRole：失败后必须让这一行按库里的真实值重绘，
        // 否则下拉停在「看起来改成功了」的值上，而库里还是旧值。
        await load()
      }
    },
  )
}

function changeRole(row: UserRow, role: Role) {
  ask(
    `把「${row.display_name}」的系统角色改为${ROLE_LABEL[role]}？\n\n` +
      `这会影响他能做什么（人事只能改资料，管理员能重置密码与改角色）；` +
      `他的登录凭证也会立刻失效。`,
    async () => {
      try {
        await api.setUserRole(row.id, role)
        toast('ok', `${row.display_name} 的角色已改为${ROLE_LABEL[role]}`)
        await load()
      } catch (e) {
        // ⚠️ **必须重新渲染这一行**，否则下拉会停在一个「看起来改成功了」的
        // 值上：`:value` 是单向绑定，请求失败时数据没变、界面却已经变了，
        // 而库里还是旧值 —— 管理员会以为改过了（这个坑下面两个 change* 一样）。
        toast('error', e instanceof ApiError ? e.message : '操作失败')
        await load()
      }
    },
  )
}

/**
 * P2-14f：下发 / 收回知识库写权限。
 *
 * 确认文案刻意把「这一档具体能干什么」写出来 —— 那是 `/options` 下发的
 * capabilities，**不是前端写死的**。管理员最需要知道的是
 * 「给他超管档意味着他能删掉全公司共用的那个库里的任何东西」，
 * 而这句话不该由前端硬编码（档位定义会变，见 kbRoleOptions 的注释）。
 */
function changeKbRole(row: UserRow, kbRole: KbRole) {
  const opt = kbRoleOptions.value.find((o) => o.value === kbRole)
  const caps: string[] = []
  if (opt?.capabilities.upload) caps.push('上传文档')
  if (opt?.capabilities.delete) caps.push('删除文档')
  if (opt?.capabilities.reindex) caps.push('重建全部索引')
  const can = caps.length ? caps.join('、') : '只能查看，不能改'
  // ⚠️ 收回比给出更需要说清后果：他不会收到通知，被收的人也不会知道。
  const losing = caps.length === 0
  ask(
    `把「${row.display_name}」的知识库写权限改为「${opt?.label ?? kbRole}」？\n\n` +
      `改完他将${can}。\n` +
      (losing
        ? `\n⚠️ 他现在可能正在依赖这个权限操作，被收回后不会有任何提示。\n`
        : `\n⚠️ 知识库是全公司共用的 —— 这个权限的范围等于整库，不只是他自己的文件。\n`) +
      `\n他的登录凭证会立刻失效，需要重新登录。`,
    async () => {
      try {
        await api.setUserKbRole(row.id, kbRole)
        toast('ok', `${row.display_name} 的知识库写权限已改为「${opt?.label ?? kbRole}」`)
        await load()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '操作失败')
        await load()
      }
    },
  )
}

/**
 * 额度单元格里显示什么。
 *
 * ⚠️ 刻意**不显示使用率**：15d 才有用量数据，现在这一列只回答「他的上限是多少」。
 *   提前塞一个「用量 0%」进去会让人以为「额度是按用量算的」，
 *   而它此刻还只是个人设置。
 */
function quotaText(row: UserRow): string {
  const own = row.token_quota_monthly ?? 0
  if (own > 0) return own.toLocaleString('zh-CN')
  const def = quotaParams.value?.default_monthly ?? 0
  if (def > 0) return `${def.toLocaleString('zh-CN')}（全局默认）`
  return '不限'
}

/**
 * P2-15b：下发 / 收回月度额度。
 *
 * 确认文案刻意写明三件事：
 * ① **只提醒不阻断** —— 这是用户 2026-10-07 拍板的，管理员必须知道
 * 「设了额度他不会用不了」，否则会以为这是个开关；
 * ② 「0 = 不限，用全局默认」；
 * ③ 「不会被踢下线」—— 与权限变更那条相反，容易让人以为要重新登录。
 */
function changeQuota(row: UserRow, raw: string) {
  const n = Number(raw)
  if (!Number.isFinite(n) || n < 0 || !Number.isInteger(n)) {
    toast('error', '额度必须是不小于 0 的整数（0 表示不限）')
    return
  }
  const own = row.token_quota_monthly ?? 0
  if (n === own) {
    // 值没变就别发请求：后端是幂等的（不落审计），
    // 而这里省掉一次「改了但其实没改」的提示更清楚。
    return
  }
  const target = n > 0 ? `${n.toLocaleString('zh-CN')} tokens / 月` : '不限（用全局默认）'
  const was = own > 0 ? `${own.toLocaleString('zh-CN')} tokens / 月` : '不限（用全局默认）'
  ask(
    `把「${row.display_name}」的月度额度从「${was}」改为「${target}」？\n\n` +
      `⚠️ 本项**只做提醒，不做阻断**：设了额度之后，` +
      `他用得接近上限时会在页面顶部看到一条提醒横幅，` +
      `但**他仍然可以继续提问，不会被禁用**。\n\n` +
      `他不会因此被强制退出登录。`,
    async () => {
      try {
        await api.setUserTokenQuota(row.id, n)
        toast('ok', `${row.display_name} 的月度额度已改为「${target}」`)
        await load()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '操作失败')
        await load()
      }
    },
  )
}

/**
 * P2-18：AppSelect 的选项表。空值选项用 `null` 当 value（「全部/—」）——
 * 用空字符串会与真实的空串枚举值冲突，用 null 语义干净。
 */
const deptOptions = computed(() => [
  // ⚠️ 「全部」用 '' 哨兵而不是 null：Element Plus 对 null 一律显示 placeholder
  //   （实测：filterDept=null 时界面显示 "Select" 而不是「全部部门」）。
  //   load 里把 '' 转回 null 再发请求。
  { value: '', label: '全部部门' },
  ...departments.value.map((d) => ({ value: d.id, label: d.name })),
])
const roleOptions = computed(() => [
  { value: '', label: '全部角色' },
  ...Object.entries(ROLE_LABEL).map(([key, label]) => ({ value: key, label })),
])
const statusOptions = computed(() => [
  { value: '', label: '全部状态' },
  ...Object.entries(STATUS_LABEL).map(([key, label]) => ({ value: key, label })),
])
const posOptions = computed(() => [
  { value: '', label: '—' },
  ...positions.value.map((x) => ({
    value: x.id,
    label: x.level ? `${x.name}（${x.level}）` : x.name,
  })),
])
const deptFormOptions = computed(() => [
  // ⚠️ null 在 EP 里显示 placeholder 而不是 label —— 「不分配」也用 '' 哨兵，
  //   提交时（submitForm）把 '' 转回 null 落库。
  { value: '', label: '—' },
  ...departments.value.map((d) => ({ value: d.id, label: d.name })),
])
const statusInlineOptions = [
  { value: 'active', label: '在职' },
  { value: 'disabled', label: '停用' },
  { value: 'resigned', label: '离职' },
]
const roleInlineOptions = [
  { value: 'user', label: '普通员工' },
  { value: 'hr', label: '人事' },
  { value: 'admin', label: '系统管理员' },
]
const formRoleOptions = roleInlineOptions

const canReset = computed(() => auth.profile?.permissions.reset_password === true)
const isSelf = (id: number) => auth.profile?.id === id

/**
 * 档位 → 中文标签。**查后端下发的表**，不在前端硬编码。
 * 取不到时回落到档位原值而不是「—」：新加一档时旧版本前端会显示 `new_role`，
 * 那一眼就能看出「后端加了档而我前端还没跟上」，比显示 `—` 可诊断得多。
 */
function kbRoleLabel(v: KbRole | string | null | undefined): string {
  if (!v) return '—'
  return kbRoleOptions.value.find((o) => o.value === v)?.label ?? String(v)
}
</script>

<template>
  <div class="list-page">
    <div class="head">
      <div>
        <h1 class="page-title">员工</h1>
        <p class="page-sub">共 {{ total }} 人 · 离职走「停用/离职」，不删行</p>
      </div>
      <button class="btn btn-primary" @click="openCreate">＋ 新建员工</button>
    </div>

    <el-form :inline="true" class="card filters filters-form">
      <el-input v-model="keyword" placeholder="搜索姓名 / 登录名 / 工号 / 邮箱" clearable @input="onSearchInput" />
      <el-select
                v-model="filterDept" placeholder="部门"
                @change="resetAndLoad"
    >
      <el-option
        v-for="o in deptOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
      <el-select
                v-model="filterRole" placeholder="角色"
                @change="resetAndLoad"
    >
      <el-option
        v-for="o in roleOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
      <el-select
                v-model="filterStatus" placeholder="状态"
                @change="resetAndLoad"
    >
      <el-option
        v-for="o in statusOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
      <el-form-item>
        <label class="check">
          <input v-model="includeResigned" type="checkbox" @change="resetAndLoad" />
          <span>含离职</span>
        </label>
      </el-form-item>
      <el-form-item>
        <button class="btn btn-ghost" :disabled="loading" @click="resetAndLoad">刷新</button>
      </el-form-item>
    </el-form>

    <div class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>姓名 / 登录名</th>
            <th>工号</th>
            <th>部门</th>
            <th>职位</th>
            <th>角色</th>
            <th>
              知识库写权限
              <em class="th-hint" title="知识库是全公司共用的一个库（业务决策，不按人隔离读）。这一列决定谁能上传 / 删除 / 重建索引 —— 范围等于整库，不是他自己的文件。">?</em>
            </th>
            <th>
              月度额度
              <em class="th-hint" title="每月可用的 token 上限。0 = 不限（用全局默认）。⚠️ 只做提醒、不做阻断：接近上限时他会在页面顶部看到提醒，但仍可继续提问。">?</em>
            </th>
            <th>状态</th>
            <th>最近登录</th>
            <th class="ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>
              <div class="who">
                <strong>{{ row.display_name }}</strong>
                <span class="muted mono">{{ row.username }}</span>
              </div>
            </td>
            <td class="mono">{{ row.employee_no }}</td>
            <td>{{ deptName(row.department_id) }}</td>
            <td>{{ positionName(row.position_id) }}</td>
            <td>
              <span class="badge" :class="`badge-${row.role}`">{{ ROLE_LABEL[row.role] }}</span>
            </td>
            <td>
              <span class="badge" :class="`badge-kb-${row.kb_role}`">{{ kbRoleLabel(row.kb_role) }}</span>
            </td>
            <td>
              <!--
                P2-15b：月度额度，行内 number 输入。
                ⚠️ 显示**个人值**而不是「生效额度」：
                   下拉/输入框里改的是个人覆盖值，把生效值放进去会让
                   「全局默认 50000 的人」输入框里也显示 50000，
                   他一确认就把全局默认**固化成个人值** ——
                   以后改全局默认对他就不再生效，而没有人知道为什么。
                   所以下拉只放个人值（0 = 不限/用全局默认），
                   生效值显示在旁边的灰字里。
              -->
              <span v-if="!isSelf(row.id)" class="quota-edit">
                <input
                  class="input inline quota-input"
                  type="number"
                  min="0"
                  step="1"
                  :value="row.token_quota_monthly ?? 0"
                  :title="`个人值。0 = 不限（当前全局默认 ${quotaParams?.default_monthly ?? 0}）`"
                  @change="changeQuota(row, ($event.target as HTMLInputElement).value)"
                  @focus="($event.target as HTMLInputElement).select()"
                />
                <span class="muted quota-eff">{{ quotaText(row) }}</span>
              </span>
              <span v-else class="mono">{{ quotaText(row) }}</span>
            </td>
            <td>
              <span class="badge" :class="`badge-${row.status}`">{{ STATUS_LABEL[row.status] }}</span>
              <span v-if="row.must_change_password" class="badge badge-danger ml">待改密</span>
            </td>
            <td class="muted">{{ formatDateTime(row.last_login_at) }}</td>
            <td class="ops">
              <button class="btn btn-sm" @click="openEdit(row)">编辑</button>
              <button
                v-if="canReset && !isSelf(row.id) && row.status === 'active'"
                class="btn btn-sm"
                @click="askReset(row)"
              >
                重置密码
              </button>
              <el-select
                v-if="!isSelf(row.id)"
                class="inline"
                :model-value="row.status" placeholder="状态"
                @change="(v: string | number | null | undefined) => changeStatus(row, v as UserStatus)"
    >
      <el-option
        v-for="o in statusInlineOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
              <el-select
                v-if="!isSelf(row.id)"
                class="inline"
                :model-value="row.role" placeholder="角色"
                @change="(v: string | number | null | undefined) => changeRole(row, v as Role)"
    >
      <el-option
        v-for="o in roleInlineOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
              <!--
                P2-14f：知识库写权限下拉。
                ⚠️ **option 全部来自后端 `/options` 的 `kb_roles`** ——
                刻意不接受自由输入（没有那个输入框），因为后端写路径只认规范值
                （`kb_acl.is_canonical_kb_role`），自由输入必然 400。
                而标签与每档能力也来自后端，前端不维护第二份（见 kbRoleOptions 注释）。

                ⚠️ **自己那一行不显示下拉**，但仍显示档位标签（上面那个 badge）。
                后端也拒「把自己降档」（降权后恢复要找别人），但 UI 直接不给入口
                更好：让人先撞一次403 才知道规矩，体验差。
              -->
              <el-select
                v-if="!isSelf(row.id) && kbRoleOptions.length"
                class="inline"
                :model-value="row.kb_role" placeholder="权限"
                @change="(v: string | number | null | undefined) => changeKbRole(row, v as KbRole)"
    >
      <el-option
        v-for="o in kbRoleOptions.map((o) => ({ value: o.value, label: o.short_label ?? o.label }))"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
              <span v-if="isSelf(row.id)" class="muted self">（自己）</span>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="10" class="empty">没有符合条件的员工</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <el-pagination
        layout="total, prev, pager, next"
        :total="total"
        :page-size="PAGE_SIZE"
        :current-page="page"
        @current-change="onPage"
      />
    </div>

    <!-- 新建 / 编辑 -->
    <ModalDialog
      :open="editOpen"
      :title="editing ? `编辑 · ${editing.display_name}` : '新建员工'"
      :width="480"
      @close="editOpen = false"
    >
      <label class="field">
        <span>登录名 <em v-if="!editing">*</em></span>
        <input
          v-model="form.username"
          class="input"
          :disabled="!!editing"
          placeholder="建议用工号或姓名拼音"
        />
        <span v-if="editing" class="hint">登录名建号后不可改（它是账号的唯一标识）</span>
      </label>
      <label class="field">
        <span>工号 <em v-if="!editing">*</em></span>
        <input v-model="form.employee_no" class="input" :disabled="!!editing" placeholder="G0011" />
      </label>
      <label class="field">
        <span>姓名</span>
        <input v-model="form.display_name" class="input" />
      </label>
      <div class="two">
        <label class="field">
          <span>邮箱</span>
          <input v-model="form.email" class="input" placeholder="name@example.com" />
        </label>
        <label class="field">
          <span>手机号</span>
          <input v-model="form.phone" class="input" />
        </label>
      </div>
      <div class="two">
        <label class="field">
          <span>部门</span>
          <el-select
                v-model="form.department_id" placeholder="部门"
    >
      <el-option
        v-for="o in deptFormOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
        </label>
        <label class="field">
          <span>职位</span>
          <el-select
                v-model="form.position_id" placeholder="职位"
    >
      <el-option
        v-for="o in posOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
        </label>
      </div>
      <label v-if="!editing" class="field">
        <span>系统角色</span>
        <el-select
                v-model="form.role" placeholder="角色"
    >
      <el-option
        v-for="o in formRoleOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
        <span class="hint">建号后会签发一个一次性临时密码，他首次登录必须改掉</span>
      </label>
      <!--
        P2-14f：知识库写权限**只读展示**，不在这里给下拉。
        刻意只有一个变更入口（表格行内那个下拉）：两处都能改的话，
        管理员会问「这两个有什么区别」，而答案只是「没区别」——
        多一个入口就多一处要同步维护、要测、要防漂移的地方。
        另外它是**独立维度**，跟建号时的初始 role 无关，新建的人默认 `none`。
      -->
      <div v-if="editing" class="field">
        <span>知识库写权限</span>
        <div>
          <span class="badge" :class="`badge-kb-${editing.kb_role}`">
            {{ kbRoleLabel(editing.kb_role) }}
          </span>
          <span class="hint" style="margin-left: 8px">
            要改请用列表里那一行的下拉（改动会立刻让对方重新登录）
          </span>
        </div>
      </div>

      <p v-if="formError" class="form-error">{{ formError }}</p>

      <template #footer>
        <button class="btn" @click="editOpen = false">取消</button>
        <button class="btn btn-primary" :disabled="submitting" @click="submitForm">
          {{ submitting ? '保存中…' : '保存' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 一次性临时密码：只在创建/重置后出现一次 -->
    <ModalDialog :open="tempOpen" title="临时密码（只显示这一次）" :width="460" @close="tempOpen = false">
      <p class="temp-lead">{{ tempPasswordFor }} 的临时密码：</p>
      <div class="temp-box mono" @click="copyTemp">{{ tempPassword }}</div>
      <p class="hint-block">
        ⚠️ 关掉这个窗口之后就再也看不到了（后端只存哈希，没有第二次查看的接口）。
        请立刻发给本人；他首次登录会被强制改掉。丢了只能再重置一次。
      </p>
      <template #footer>
        <button class="btn" @click="copyTemp">{{ tempPasswordCopied ? '已复制' : '复制' }}</button>
        <button class="btn btn-primary" @click="tempOpen = false">我已记下，关闭</button>
      </template>
    </ModalDialog>

    <!-- 二次确认 -->


    <ToastStack :items="toasts" @dismiss="dismiss" />
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.filters {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.search {
  flex: 1 1 240px;
  width: auto;
}
.filters .el-select {
  width: 160px;
  flex: 0 0 auto;
}
.el-select.inline {
  width: 132px;
}
.check {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
  font-size: 13px;
  white-space: nowrap;
}
.table-wrap {
  overflow: auto;
}
.who {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}
.who .muted {
  font-size: 12px;
}
.ops {
  white-space: nowrap;
}
.ops .btn,
.ops .select {
  margin-right: 6px;
}
.select.inline {
  width: auto;
  padding: 3px 6px;
  font-size: 13px;
  /* P2-14f：知识库档位的 label 最长是「超级管理员（可上传 / 删除 / 重灌索引）」，
     直接放进下拉会把整张表撑到横向溢出、把左边几列压成竖排单字。
     → 下拉**限宽并省略**，而完整文案在两处都能看到：
       ① 那一行的档位徽章（badge 本身不截断，它在自己的列里）；
       ② 确认弹窗（changeKbRole 里明确写出「改完他将能做什么」）。
     这是刻意的取舍：下拉只承担「选一个档位」，不承担「解释这个档位」。 */
  max-width: 168px;
  text-overflow: ellipsis;
}
.self {
  font-size: 12px;
}
.ml {
  margin-left: 6px;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.field em {
  color: var(--danger);
  font-style: normal;
}
.form-error {
  color: var(--danger);
  background: var(--danger-soft);
  border: 1px solid rgba(240, 97, 109, 0.3);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  font-size: 13px;
}
.temp-lead {
  color: var(--text-2);
  margin-bottom: 8px;
}
.temp-box {
  background: var(--bg-app);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  font-size: 17px;
  letter-spacing: 1px;
  text-align: center;
  cursor: copy;
  user-select: all;
}
.hint-block {
  margin-top: 12px;
  color: var(--warn);
  font-size: 12.5px;
  line-height: 1.6;
}
.confirm-text {
  white-space: pre-line;
  line-height: 1.7;
}
</style>
