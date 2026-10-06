<script setup lang="ts">
/**
 * 密码到期看板（P2-13c）。
 *
 * --------------------------------------------------------------------------
 * 这个页面要解决的问题
 * --------------------------------------------------------------------------
 * 「谁的密码该处理了」这件事，在员工表里是看不出来的 —— 要拿每个人的
 * `password_changed_at` 跟当前时间比一遍才知道。散在表格里就是
 * 「管理员每天自己心算一遍」，所以单开一个看板页。
 *
 * --------------------------------------------------------------------------
 * 三个刻意的设计
 * --------------------------------------------------------------------------
 * 1. **五个桶互不包含，一人只出现一次。** 这是后端 `PasswordBoard` 的
 *    契约（判定顺序：锁定 → 待改密 → 已过期 → 将到期 → 长期未登录）。
 *    前端不再自己算一遍 —— 一旦两边算法分叉，看板加总对不上人数，
 *    而「数字对不上」这种 bug 是最难发现的那种。
 *
 * 2. **卡片即筛选器。** 五个数字摆成一行是给人扫读的，但点进去要能落到
 *    具体名单上，否则「有 7 个待改密」这个信息无法变成动作。
 *
 * 3. **重置密码的按钮对人事不可见。** 与员工页一致：真正的防线在后端
 *    （403），这里只是别让人去点一个必然失败、且原因看不懂的按钮。
 *    页面顶部会把「你为什么没有这个按钮」写在明处，而不是静默隐藏。
 *
 * --------------------------------------------------------------------------
 * 一处必须写下来的代价
 * --------------------------------------------------------------------------
 * 重置密码**不会解除锁定**：锁定是 15 分钟的暴力破解节流，与密码本身无关。
 * 所以「已锁定」这一桶里的人，重置完仍然要等锁定到期（或让人等一会儿）。
 * 不做「顺手解锁」是有意的 —— 否则攻击者只要能让管理员重置一次就绕过了节流。
 */
import { computed, onMounted, ref } from 'vue'
import ModalDialog from '@/components/ModalDialog.vue'
import ToastStack from '@/components/ToastStack.vue'
import { formatDateTime, newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import type { BoardItem, PasswordBoard } from '@/api/admin'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

type BucketKey = 'must_change' | 'expired' | 'expiring_soon' | 'stale_login' | 'locked'

interface BucketMeta {
  key: BucketKey
  label: string
  tone: 'danger' | 'warn' | 'muted'
  desc: string
}

const BUCKETS: BucketMeta[] = [
  {
    key: 'must_change',
    label: '待改密',
    tone: 'danger',
    desc: '拿着一次性临时密码还没改掉，登录会被拦下来',
  },
  { key: 'expired', label: '已过期', tone: 'danger', desc: '超过有效期没换密码' },
  { key: 'expiring_soon', label: '即将到期', tone: 'warn', desc: '快到有效期了，先提醒本人' },
  {
    key: 'stale_login',
    label: '长期未登录',
    tone: 'warn',
    desc: '密码没到期，但人 90 天没来过 —— 这是「账号该回收」的信号',
  },
  {
    key: 'locked',
    label: '已锁定',
    tone: 'muted',
    desc: '连续输错被节流，锁定到期自动解除（重置密码不解锁）',
  },
]

// ---------- 数据 ----------
const board = ref<PasswordBoard | null>(null)
const departments = ref<api.DepartmentRow[]>([])
const loading = ref(false)
const toasts = ref<ToastItem[]>([])
const activeKey = ref<BucketKey>('must_change')

function toast(kind: ToastItem['kind'], text: string) {
  const item = newToast(kind, text)
  toasts.value.push(item)
  setTimeout(() => dismiss(item.id), kind === 'ok' ? 2600 : 4200)
}
function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

const cards = computed(() => {
  const b = board.value
  return BUCKETS.map((meta) => ({
    ...meta,
    count: b ? b.counts[meta.key] : 0,
    items: b ? b[meta.key] : [],
  }))
})

const activeMeta = computed(() => BUCKETS.find((m) => m.key === activeKey.value) ?? BUCKETS[0])
const activeItems = computed(() => {
  const card = cards.value.find((c) => c.key === activeKey.value)
  return card ? card.items : []
})

const canReset = computed(() => auth.profile?.permissions.reset_password === true)

function deptName(id: number | null): string {
  if (id == null) return '—'
  return departments.value.find((d) => d.id === id)?.name ?? `#${id}`
}

/** 到期天数：后端给的是「还剩几天」，负数=已过期。这里只负责说人话。 */
function expireText(days: number | null): string {
  if (days == null) return '—'
  if (days < 0) return `已过期 ${Math.abs(days)} 天`
  if (days === 0) return '今天到期'
  return `${days} 天后到期`
}

async function load() {
  loading.value = true
  try {
    board.value = await api.fetchPasswordBoard()
    // 默认落在「最急且非空」的那一桶：打开就看到空表会被当成坏了
    const first = cards.value.find((c) => c.count > 0)
    if (first) activeKey.value = first.key
  } catch (e) {
    toast('error', e instanceof ApiError ? e.message : '加载密码看板失败')
  } finally {
    loading.value = false
  }
}

async function loadOptions() {
  const opt = await api.fetchOptions()
  departments.value = opt.departments
}

onMounted(async () => {
  await loadOptions()
  await load()
})

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
    tempPasswordCopied.value = false
    toast('warn', '浏览器拒绝了剪贴板访问，请手动选中复制')
  }
}

// ---------- 二次确认 ----------
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

async function reload() {
  await load()
}

// ---------- 行操作 ----------
function doReset(item: BoardItem) {
  if (auth.profile?.id === item.id) {
    toast('warn', '重置自己的密码请去主应用的「我的账号」')
    return
  }
  ask(
    `重置「${item.display_name}」的密码？\n\n` +
      `会签发一个一次性临时密码，他下次登录必须改掉；他手上的凭证会立刻失效。` +
      (activeKey.value === 'locked' ? `\n\n注意：重置不会解除锁定，锁定到期后他才能登录。` : ''),
    async () => {
      try {
        const res = await api.resetPassword(item.id)
        tempPasswordFor.value = item.display_name
        tempPassword.value = res.temporary_password
        tempPasswordCopied.value = false
        tempOpen.value = true
        await reload()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '重置失败')
      }
    },
  )
}

function doToggleMustChange(item: BoardItem) {
  const next = !item.must_change
  ask(
    next
      ? `要求「${item.display_name}」下次登录必须改密？\n\n` +
          `不换密码，只是给他加一道关卡。适合怀疑密码外泄、又不想直接停用的情况。`
      : `取消「${item.display_name}」的强制改密？\n\n` +
          `他可以继续使用现在的密码，直到它自然到期。`,
    async () => {
      try {
        await api.setMustChange(item.id, next)
        toast('ok', next ? `${item.display_name} 已被要求改密` : `已取消 ${item.display_name} 的改密要求`)
        await reload()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '操作失败')
      }
    },
  )
}

function doDisable(item: BoardItem) {
  ask(
    `停用「${item.display_name}」？\n\n` +
      `他立刻登不进来，手上的凭证也会失效。停用可以再改回在职，离职才是不再回来的那一步。`,
    async () => {
      try {
        await api.setUserStatus(item.id, 'disabled')
        toast('ok', `${item.display_name} 已停用`)
        await reload()
      } catch (e) {
        toast('error', e instanceof ApiError ? e.message : '操作失败')
      }
    },
  )
}
</script>

<template>
  <div>
    <div class="head">
      <div>
        <h1 class="page-title">密码管理</h1>
        <p class="page-sub">
          <template v-if="board">
            有效期 {{ board.policy.expire_days }} 天 · 剩 {{ board.policy.warn_days }} 天起提醒
            · 统计于 {{ formatDateTime(board.generated_at) }}
          </template>
          <template v-else>统计口径：只在职员工</template>
        </p>
      </div>
      <button class="btn" :disabled="loading" @click="reload">{{ loading ? '刷新中…' : '刷新' }}</button>
    </div>

    <div v-if="!canReset" class="card tip">
      当前账号（{{ auth.profile?.role === 'hr' ? '人事' : auth.profile?.role }}）没有重置密码的权限 ——
      下面的「重置」按钮不会出现。改资料、停用、离职照常可用。
    </div>

    <div class="cards">
      <button
        v-for="card in cards"
        :key="card.key"
        class="card stat"
        :class="[`tone-${card.tone}`, { on: card.key === activeKey }]"
        @click="activeKey = card.key"
      >
        <span class="num">{{ card.count }}</span>
        <span class="name">{{ card.label }}</span>
      </button>
    </div>

    <div class="card table-wrap">
      <div class="bucket-head">
        <div>
          <strong>{{ activeMeta.label }}</strong>
          <span class="muted desc">{{ activeMeta.desc }}</span>
        </div>
        <span class="muted">{{ activeItems.length }} 人</span>
      </div>
      <table class="grid">
        <thead>
          <tr>
            <th>姓名 / 登录名</th>
            <th>部门</th>
            <th>密码到期</th>
            <th>最近登录</th>
            <th class="ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in activeItems" :key="item.id">
            <td>
              <div class="who">
                <strong>{{ item.display_name }}</strong>
                <span class="muted mono">{{ item.username }}</span>
              </div>
            </td>
            <td>{{ deptName(item.department_id) }}</td>
            <td>
              <span :class="{ danger: (item.expire_in_days ?? 99) < 0 }">
                {{ expireText(item.expire_in_days) }}
              </span>
            </td>
            <td class="muted">{{ formatDateTime(item.last_login_at) }}</td>
            <td class="ops">
              <button
                v-if="canReset && auth.profile?.id !== item.id"
                class="btn btn-sm"
                @click="doReset(item)"
              >
                重置密码
              </button>
              <button
                v-if="canReset"
                class="btn btn-sm"
                @click="doToggleMustChange(item)"
              >
                {{ item.must_change ? '取消改密' : '要求改密' }}
              </button>
              <button
                v-if="activeKey === 'stale_login'"
                class="btn btn-sm btn-danger"
                @click="doDisable(item)"
              >
                停用
              </button>
              <span v-if="auth.profile?.id === item.id" class="muted self">（自己）</span>
            </td>
          </tr>
          <tr v-if="!activeItems.length">
            <td colspan="5" class="empty">这一桶是空的 —— 说明没有需要处理的账号</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 一次性临时密码：只出现一次 -->
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
.tip {
  padding: 10px 14px;
  margin-bottom: 14px;
  color: var(--text-2);
  font-size: 13px;
  border-color: rgba(240, 177, 60, 0.32);
  background: var(--warn-soft);
}
.cards {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 14px 16px;
  text-align: left;
  border-left: 3px solid var(--border-strong);
  transition:
    border-color 0.15s,
    background 0.15s;
}
.stat:hover {
  background: var(--bg-hover);
}
.stat.on {
  border-color: var(--primary);
  background: var(--bg-active);
}
.stat .num {
  font-size: 26px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.stat .name {
  color: var(--text-2);
  font-size: 13px;
}
.stat.tone-danger .num {
  color: var(--danger);
}
.stat.tone-warn .num {
  color: var(--warn);
}
.stat.tone-muted .num {
  color: var(--text-2);
}
.bucket-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.bucket-head .desc {
  margin-left: 8px;
  font-size: 13px;
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
.ops .btn {
  margin-right: 6px;
}
.self {
  font-size: 12px;
}
.danger {
  color: var(--danger);
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
  font-size: 18px;
  letter-spacing: 1px;
  text-align: center;
  cursor: pointer;
  user-select: all;
}
.hint-block {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.6;
}
.confirm-text {
  white-space: pre-line;
  color: var(--text-2);
  line-height: 1.7;
}
@media (max-width: 1024px) {
  .cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
