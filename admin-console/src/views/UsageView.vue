<script setup lang="ts">
/**
 * P2-15d：用量看板 —— 按人 × 本月的 token 用量。
 *
 * 三条刻意的边界（都写在 §15 计划里）：
 *
 * 1. **只有管理员能看**。后端 `GET /admin/usage/board` 是 require_admin ——
 *    「谁花了多少」是成本信息，不是人事信息，hr 也看不到。
 * 2. **百分比与档位全部后端算好**（usage_percent / status / status_label）。
 *    前端只负责渲染 —— 同一个百分比两个口径的教训（15a）不再犯。
 * 3. 看板底部有**对账行**（全库总计 = 按人合计 + 未归属）。
 *    它平时是一行小字，但它是这个页面「数字可信」的来源 ——
 *    有对账行的看板，漏算会自己站出来。
 */
import { computed, onMounted, ref } from 'vue'
import ToastStack from '@/components/ToastStack.vue'
import { newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import { ApiError } from '@/api/http'

const loading = ref(false)
const toasts = ref<ToastItem[]>([])
function toast(kind: ToastItem['kind'], text: string) {
  toasts.value.push({ ...newToast(kind, text) })
}

function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

interface UsageRow {
  user_id: number
  username: string
  display_name: string
  department_id: number | null
  department_name: string | null
  quota_override: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  requests: number
  session_count: number
  effective_quota: number
  billable_tokens: number
  usage_percent: number | null
  status: 'ok' | 'warn' | 'over'
  status_label: string
}

interface UsageBoard {
  period_start_day: number
  warn_percent: number
  over_percent: number
  status_labels: Record<'ok' | 'warn' | 'over', string>
  rows: UsageRow[]
  unattributed: { input_tokens: number; output_tokens: number; messages: number }
  grand_total: { input_tokens: number; output_tokens: number; messages: number }
}

const board = ref<UsageBoard | null>(null)
const departments = ref<api.DepartmentRow[]>([])
// P2-18：部门筛选选项（空值 = 全部部门）
const deptFilterOptions = computed(() => [
  // ⚠️ null 在 EP 里显示 placeholder —— 「全部」用 '' 哨兵（同 UsersView）
  { value: '', label: '全部部门' },
  ...departments.value.map((d) => ({ value: d.id, label: d.name })),
])
const filterDept = ref<number | ''>('')

const rows = computed(() => {
  if (!board.value) return []
  if (filterDept.value === '') return board.value.rows
  return board.value.rows.filter((r) => r.department_id === filterDept.value)
})

/** 只列出「本月动过的人」由后端决定（usage_board 含 0 用量的人，二者并存是有意的）。 */
const hasUsage = computed(() => rows.value.filter((r) => r.billable_tokens > 0))
const zeroUsage = computed(() => rows.value.filter((r) => r.billable_tokens === 0))

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('zh-CN')
}

function quotaText(r: UsageRow): string {
  if (r.effective_quota > 0) return fmt(r.effective_quota)
  return '不限'
}

/**
 * 档位徽章的 class。色值由 CSS 变量给，这里只选语义名 ——
 * ⚠️ 刻意不用「危险/danger」这种名字：已超额在本项的定义里不是「出事了」，
 * 只是「用超了，需要有人看一眼」（见 quota_policy.STATUS_COLORS 的注释）。
 */
function statusClass(s: UsageRow['status']): string {
  return `badge-quota-${s}`
}

async function load() {
  loading.value = true
  try {
    const [b, opt] = await Promise.all([
      api.fetchUsageBoard(),
      api.fetchOptions().catch(() => null),
    ])
    board.value = b
    departments.value = opt?.departments ?? []
  } catch (e) {
    toast('error', e instanceof ApiError ? e.message : '看板加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="list-page">
    <ToastStack :items="toasts" @dismiss="dismiss" />
    <div class="head">
      <div>
        <h1 class="page-title">用量看板</h1>
        <p class="page-sub">
          按人 × 自然月 · 计费口径 = 输入 + 输出（缓存命中不计入）
          · 阈值 {{ board?.warn_percent ?? '—' }}% 提醒 / {{ board?.over_percent ?? '—' }}% 已超额
        </p>
      </div>
      <el-select
                v-model="filterDept"
                placeholder="部门"
                @change="load"
    >
      <el-option
        v-for="o in deptFilterOptions"
        :key="String(o.value)"
        :label="o.label"
        :value="o.value"
      />
            </el-select>
    </div>

    <div v-if="board" class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>姓名 / 登录名</th>
            <th>部门</th>
            <th class="num">输入</th>
            <th class="num">输出</th>
            <th class="num">
              缓存命中
              <em class="th-hint" title="服务商侧 prompt 缓存，单价与普通输入不同，刻意不计入计费总额。">?</em>
            </th>
            <th class="num">计费合计</th>
            <th class="num">
              月度额度
              <em class="th-hint" title="0 或「不限」= 未设额度。在员工页的「月度额度」列设置。">?</em>
            </th>
            <th class="num">使用率</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in hasUsage" :key="r.user_id">
            <td>
              <div class="who">
                <strong>{{ r.display_name }}</strong>
                <span class="muted mono">{{ r.username }}</span>
              </div>
            </td>
            <td>{{ r.department_name ?? '—' }}</td>
            <td class="num mono">{{ fmt(r.input_tokens) }}</td>
            <td class="num mono">{{ fmt(r.output_tokens) }}</td>
            <td class="num mono muted">{{ fmt(r.cache_read_tokens) }}</td>
            <td class="num mono"><strong>{{ fmt(r.billable_tokens) }}</strong></td>
            <td class="num mono">{{ quotaText(r) }}</td>
            <td class="num mono">
              {{ r.usage_percent == null ? '—' : `${r.usage_percent.toFixed(0)}%` }}
            </td>
            <td>
              <span class="badge" :class="statusClass(r.status)">{{ r.status_label }}</span>
            </td>
          </tr>
          <tr v-if="!hasUsage.length">
            <td colspan="9" class="empty">本月还没有人产生用量</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 0 用量的在职员工：单独一小块，避免把「谁用得多」淹没 -->
    <div v-if="zeroUsage.length" class="card zero-block">
      <p class="zero-title">本月未使用（{{ zeroUsage.length }} 人）</p>
      <p class="muted">
        {{ zeroUsage.map((r) => r.display_name).join('、') }}
      </p>
    </div>

    <!-- 对账行：这个页面「数字可信」的来源 -->
    <p v-if="board" class="reconcile muted">
      对账：全库 {{ fmt(board.grand_total.input_tokens) }} input =
      按人 {{ fmt(board.grand_total.input_tokens - board.unattributed.input_tokens) }}
      + 未归属 {{ fmt(board.unattributed.input_tokens) }}
      <span
        class="reconcile-badge"
        :class="board.grand_total.input_tokens === board.unattributed.input_tokens + rows.reduce((a, r) => a + r.input_tokens, 0) ? 'ok' : 'bad'"
      >{{
        board.grand_total.input_tokens === board.unattributed.input_tokens + rows.reduce((a, r) => a + r.input_tokens, 0)
          ? '✓ 恒等式成立' : '⚠ 恒等式不成立（有漏算）'
      }}</span>
      · 未归属的「主人已被删除」用量会单列，正常应为 0
    </p>

    <p v-if="!board && loading" class="muted">加载中…</p>
  </div>
</template>

<style scoped>
.zero-block {
  margin-top: 16px;
  padding: 14px 16px;
}
.zero-title {
  font-weight: 600;
  margin: 0 0 4px;
  font-size: 13px;
}
.reconcile {
  font-size: 12px;
  margin-top: 12px;
}
.reconcile-badge {
  margin-left: 6px;
  padding: 1px 8px;
  border-radius: 999px;
  font-weight: 600;
}
.reconcile-badge.ok {
  background: rgba(63, 185, 80, 0.15);
  color: #3fb950;
}
.reconcile-badge.bad {
  background: rgba(240, 97, 109, 0.18);
  color: #f0616d;
}
</style>
