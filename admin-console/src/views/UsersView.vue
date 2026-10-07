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
import { ROLE_LABEL, STATUS_LABEL, type Role, type UserRow, type UserStatus } from '@/api/admin'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// ---------- 数据 ----------
const rows = ref<UserRow[]>([])
const departments = ref<api.DepartmentRow[]>([])
const positions = ref<api.PositionRow[]>([])
const loading = ref(false)
const toasts = ref<ToastItem[]>([])

const keyword = ref('')
const filterDept = ref<number | null>(null)
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
}

async function load() {
  loading.value = true
  try {
    const res = await api.fetchUsers({
      keyword: keyword.value.trim() || undefined,
      department_id: filterDept.value,
      role: filterRole.value || null,
      status: filterStatus.value || null,
      include_resigned: includeResigned.value,
    })
    rows.value = res.items
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
  department_id: null as number | null,
  position_id: null as number | null,
  role: 'user' as Role,
})
const formError = ref('')
const submitting = ref(false)

function openCreate() {
  editing.value = null
  form.value = {
    username: '', employee_no: '', display_name: '', email: '', phone: '',
    gender: '', department_id: null, position_id: null, role: 'user',
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
    department_id: detail.department_id,
    position_id: detail.position_id,
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
        department_id: form.value.department_id,
        position_id: form.value.position_id,
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
        department_id: form.value.department_id,
        position_id: form.value.position_id,
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
const confirmOpen = ref(false)
const confirmText = ref('')
const confirmAction = ref<(() => Promise<void>) | null>(null)

function ask(text: string, action: () => Promise<void>) {
  confirmText.value = text
  confirmAction.value = action
  confirmOpen.value = true
}

async function runConfirm() {
  const action = confirmAction.value
  confirmOpen.value = false
  confirmAction.value = null
  if (action) await action()
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
        toast('error', e instanceof ApiError ? e.message : '操作失败')
      }
    },
  )
}

const canReset = computed(() => auth.profile?.permissions.reset_password === true)
const isSelf = (id: number) => auth.profile?.id === id
</script>

<template>
  <div>
    <div class="head">
      <div>
        <h1 class="page-title">员工</h1>
        <p class="page-sub">共 {{ rows.length }} 人 · 离职走「停用/离职」，不删行</p>
      </div>
      <button class="btn btn-primary" @click="openCreate">＋ 新建员工</button>
    </div>

    <div class="card filters">
      <input
        v-model="keyword"
        class="input search"
        placeholder="搜索姓名 / 登录名 / 工号 / 邮箱"
        @input="onSearchInput"
      />
      <select v-model="filterDept" class="select" @change="load">
        <option :value="null">全部部门</option>
        <option v-for="d in departments" :key="d.id" :value="d.id">{{ d.name }}</option>
      </select>
      <select v-model="filterRole" class="select" @change="load">
        <option value="">全部角色</option>
        <option v-for="(label, key) in ROLE_LABEL" :key="key" :value="key">{{ label }}</option>
      </select>
      <select v-model="filterStatus" class="select" @change="load">
        <option value="">全部状态</option>
        <option v-for="(label, key) in STATUS_LABEL" :key="key" :value="key">{{ label }}</option>
      </select>
      <label class="check">
        <input v-model="includeResigned" type="checkbox" @change="load" />
        <span>含离职</span>
      </label>
      <button class="btn btn-ghost" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>姓名 / 登录名</th>
            <th>工号</th>
            <th>部门</th>
            <th>职位</th>
            <th>角色</th>
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
              <select
                v-if="!isSelf(row.id)"
                class="select inline"
                :value="row.status"
                @change="changeStatus(row, ($event.target as HTMLSelectElement).value as UserStatus)"
              >
                <option value="active">在职</option>
                <option value="disabled">停用</option>
                <option value="resigned">离职</option>
              </select>
              <select
                v-if="!isSelf(row.id)"
                class="select inline"
                :value="row.role"
                @change="changeRole(row, ($event.target as HTMLSelectElement).value as Role)"
              >
                <option value="user">普通员工</option>
                <option value="hr">人事</option>
                <option value="admin">系统管理员</option>
              </select>
              <span v-if="isSelf(row.id)" class="muted self">（自己）</span>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="8" class="empty">没有符合条件的员工</td>
          </tr>
        </tbody>
      </table>
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
          <select v-model="form.department_id" class="select">
            <option :value="null">—</option>
            <option v-for="d in departments" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
        </label>
        <label class="field">
          <span>职位</span>
          <select v-model="form.position_id" class="select">
            <option :value="null">—</option>
            <option v-for="p in positions" :key="p.id" :value="p.id">
              {{ p.level ? `${p.name}（${p.level}）` : p.name }}
            </option>
          </select>
        </label>
      </div>
      <label v-if="!editing" class="field">
        <span>系统角色</span>
        <select v-model="form.role" class="select">
          <option value="user">普通员工</option>
          <option value="hr">人事</option>
          <option value="admin">系统管理员</option>
        </select>
        <span class="hint">建号后会签发一个一次性临时密码，他首次登录必须改掉</span>
      </label>

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
    <ModalDialog :open="confirmOpen" title="确认操作" :width="420" @close="confirmOpen = false">
      <p class="confirm-text">{{ confirmText }}</p>
      <template #footer>
        <button class="btn" @click="confirmOpen = false">取消</button>
        <button class="btn btn-danger" @click="runConfirm">确认</button>
      </template>
    </ModalDialog>

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
.filters .select {
  width: auto;
  min-width: 120px;
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
