<script setup lang="ts">
/**
 * 审计日志（P2-13d）—— **纯只读**。
 *
 * --------------------------------------------------------------------------
 * 这个页面上没有任何写操作，这不是省略，是设计
 * --------------------------------------------------------------------------
 * 审计的价值在于「出事那天能查出来」，而它一旦能被改，就不再是证据。
 * 后端 `audit_log` 表没有 `update_time` / `deleted_at` 两列，仓储层也没有
 * update / delete —— 所以这个页面在结构上就没有「编辑」按钮可做。
 *
 * --------------------------------------------------------------------------
 * detail 怎么展示：结构化，不是一坨 JSON
 * --------------------------------------------------------------------------
 * 后端把 detail 存成 JSON 文本，这里按「动作类型」翻译成人话：
 *状态变更 → `active → disabled`、资料变更 → `手机号 130… → 131…`。
 * 直接 `JSON.stringify` 甩进行内也能用，但排查的人得自己翻译键名 ——
 * 而「排查」正是这个页面存在的理由。
 */
import { computed, onMounted, ref } from 'vue'
import ToastStack from '@/components/ToastStack.vue'
import { formatDateTime, newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import type { AuditRow } from '@/api/admin'
import { ApiError } from '@/api/http'

const PAGE_SIZE = 30

// ---------- 数据 ----------
const rows = ref<AuditRow[]>([])
const total = ref(0)
const actions = ref<{ value: string; label: string }[]>([])
const targetTypes = ref<string[]>([])
const loading = ref(false)
const toasts = ref<ToastItem[]>([])

const filterActor = ref('')
const filterAction = ref('')
const filterType = ref('')
const filterKeyword = ref('')
const offset = ref(0)

function toast(kind: ToastItem['kind'], text: string) {
  const item = newToast(kind, text)
  toasts.value.push(item)
  setTimeout(() => dismiss(item.id), kind === 'ok' ? 2600 : 4200)
}
function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

async function load() {
  loading.value = true
  try {
    const res = await api.fetchAuditLogs({
      actor: filterActor.value.trim() || null,
      action: filterAction.value || null,
      target_type: filterType.value || null,
      keyword: filterKeyword.value.trim() || null,
      limit: PAGE_SIZE,
      offset: offset.value,
    })
    rows.value = res.items
    total.value = res.total
    // 动作候选来自服务端：前端自己维护一份清单，迟早会和服务端漂移
    if (res.actions.length && !actions.value.length) actions.value = res.actions
    if (res.target_types.length && !targetTypes.value.length) targetTypes.value = res.target_types
  } catch (e) {
    toast('error', e instanceof ApiError ? e.message : '加载审计日志失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

let searchTimer: number | undefined
function onSearchInput() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    offset.value = 0
    void load()
  }, 300)
}

function applyFilter() {
  offset.value = 0
  void load()
}

function resetFilters() {
  filterActor.value = ''
  filterAction.value = ''
  filterType.value = ''
  filterKeyword.value = ''
  offset.value = 0
  void load()
}

function page(delta: number) {
  const next = offset.value + delta * PAGE_SIZE
  if (next < 0) return
  if (next >= total.value) return
  offset.value = next
  void load()
}

const pageStart = computed(() => (total.value === 0 ? 0 : offset.value + 1))
const pageEnd = computed(() => Math.min(offset.value + PAGE_SIZE, total.value))

// --------------------------------------------------------------------------- //
// detail 翻译
// --------------------------------------------------------------------------- //
const TYPE_LABEL: Record<string, string> = {
  user: '员工',
  department: '部门',
  position: '职位',
  auth: '登录',
}
const ROLE_TEXT: Record<string, string> = { admin: '管理员', hr: '人事', user: '普通员工' }
/** 与后端 `core/user_repo.py` 的 STATUS_* 一一对应；漏一个就退回英文原值，不显示 undefined。 */
const STATUS_TEXT: Record<string, string> = {
  active: '在职',
  disabled: '停用',
  resigned: '离职',
}

type Change = [unknown, unknown]

function changes(value: unknown): [string, Change][] {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return Object.entries(value as Record<string, Change>)
  }
  return []
}

/** 字段名 → 人话。审计里的键名是给代码看的，这里负责翻译。 */
const FIELD_LABEL: Record<string, string> = {
  display_name: '姓名',
  email: '邮箱',
  phone: '手机号',
  gender: '性别',
  department_id: '部门',
  position_id: '职位',
  parent_id: '上级部门',
  leader_user_id: '部门负责人',
  sort_order: '排序',
  name: '名称',
  code: '编码',
  level: '职级',
  sequence: '序列',
}

function fieldText(key: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '空'
  if (key.endsWith('_id')) return `#${value}`
  if (key === 'sequence') return String(value)
  return String(value)
}

/** 把一条 detail 翻成一行行「字段：旧 → 新」。返回空数组表示这条没有可比对的变化。 */
function detailLines(row: AuditRow): string[] {
  const d = row.detail
  if (!d) return []
  const out: string[] = []
  const bag = d as Record<string, unknown>

  // ⚠️ 后端对「状态变更 / 角色变更 / 改密开关」统一用 `from` / `to` 两个键，
  // 不是按字段名分开的（status.from / role.from）。第一版这里按字段名去读，
  // 于是 from 永远是 undefined，明细列整列显示「—」——
  // 而这在页面上看着完全像「这条日志没有明细」，不报错、不空指针。
  // 反向验证：把 from 改回 bag.status，详情就空了。
  const from = bag.from
  const to = bag.to
  if (row.action === 'user.status.change') {
    if (from !== undefined && to !== undefined) {
      out.push(`状态：${STATUS_TEXT[String(from)] ?? String(from)} → ${STATUS_TEXT[String(to)] ?? String(to)}`)
    }
  }
  if (row.action === 'user.role.change') {
    if (from !== undefined && to !== undefined) {
      out.push(`角色：${ROLE_TEXT[String(from)] ?? String(from)} → ${ROLE_TEXT[String(to)] ?? String(to)}`)
    }
  }
  if (row.action === 'user.password.must_change') {
    if (from !== undefined && to !== undefined) {
      out.push(`强制改密：${from ? '开' : '关'} → ${to ? '开' : '关'}`)
    }
  }
  if (row.action === 'user.password.reset') {
    out.push('已签发一次性临时密码（明文不入审计）')
  }
  if (row.action === 'department.create' || row.action === 'position.create') {
    // 建档类动作没有「旧值」可比，把它建了什么列出来比空着有用。
    // 名称不在 detail 里（后端放在 target_label 那一列），所以这里不编它。
    if (bag.code) out.push(`编码：${String(bag.code)}`)
    if (bag.sequence) out.push(`序列：${String(bag.sequence)}`)
    if (bag.level) out.push(`职级：${String(bag.level)}`)
    if (bag.parent_id) out.push(`上级部门：#${String(bag.parent_id)}`)
    if (bag.leader_user_id) out.push(`部门负责人：#${String(bag.leader_user_id)}`)
  }

  for (const [key, pair] of changes(bag.changes)) {
    const [oldV, newV] = pair
    if (oldV === null || oldV === undefined) {
      out.push(`${FIELD_LABEL[key] ?? key}：空 → ${fieldText(key, newV)}`)
    } else {
      out.push(
        `${FIELD_LABEL[key] ?? key}：${fieldText(key, oldV)} → ${fieldText(key, newV)}`,
      )
    }
  }
  return out
}
</script>

<template>
  <div>
    <div class="head">
      <div>
        <h1 class="page-title">审计日志</h1>
        <p class="page-sub">
          谁 · 何时 · 对谁 · 做了什么 · 从哪个 IP —— <strong>只读，且不可改不可删</strong>
        </p>
      </div>
      <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </div>

    <div class="card filters">
      <input
        v-model="filterActor"
        class="input search"
        placeholder="按操作人登录名筛"
        @input="onSearchInput"
      />
      <select v-model="filterAction" class="select" @change="applyFilter">
        <option value="">全部动作</option>
        <option v-for="a in actions" :key="a.value" :value="a.value">{{ a.label }}</option>
      </select>
      <select v-model="filterType" class="select" @change="applyFilter">
        <option value="">全部对象</option>
        <option v-for="t in targetTypes" :key="t" :value="t">{{ TYPE_LABEL[t] ?? t }}</option>
      </select>
      <input
        v-model="filterKeyword"
        class="input search"
        placeholder="在对象与明细里搜"
        @input="onSearchInput"
      />
      <button class="btn btn-ghost" @click="resetFilters">清空筛选</button>
    </div>

    <div class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>时间</th>
            <th>操作人</th>
            <th>动作</th>
            <th>对象</th>
            <th>明细</th>
            <th>来源 IP</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td class="muted nowrap">{{ formatDateTime(row.created_at) }}</td>
            <td>
              <div class="who">
                <strong>{{ row.actor_username }}</strong>
                <span class="muted">{{ row.actor_role ? (ROLE_TEXT[row.actor_role] ?? row.actor_role) : '—' }}</span>
              </div>
            </td>
            <td class="nowrap">{{ row.action_label }}</td>
            <td>
              <div class="who">
                <span>{{ row.target_label ?? `#${row.target_id ?? '—'}` }}</span>
                <span class="muted">{{ TYPE_LABEL[row.target_type] ?? row.target_type }}</span>
              </div>
            </td>
            <td>
              <div v-if="detailLines(row).length" class="detail">
                <span v-for="(line, i) in detailLines(row)" :key="i" class="line">{{ line }}</span>
              </div>
              <span v-else class="muted">—</span>
            </td>
            <td class="mono muted">{{ row.ip ?? '—' }}</td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="6" class="empty">没有符合条件的记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <span class="muted">
        第 {{ pageStart }}–{{ pageEnd }} 条 / 共 {{ total }} 条
      </span>
      <div class="pager-btns">
        <button class="btn btn-sm" :disabled="offset === 0" @click="page(-1)">上一页</button>
        <button class="btn btn-sm" :disabled="pageEnd >= total" @click="page(1)">下一页</button>
      </div>
    </div>

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
  flex: 1 1 180px;
  width: auto;
}
.filters .select {
  width: auto;
  min-width: 130px;
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
.nowrap {
  white-space: nowrap;
}
.detail {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 13px;
  color: var(--text-2);
}
.line::before {
  content: '· ';
  color: var(--text-3);
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}
.pager-btns {
  display: flex;
  gap: 8px;
}
</style>