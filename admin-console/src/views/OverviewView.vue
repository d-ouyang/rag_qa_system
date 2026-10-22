<script setup lang="ts">
/**
 * 概览 —— 管理端的落地页（P2-13a 占位页的正式化）。
 *
 * 这里不再是一句话 + 占位卡，而是一个**只读汇总看板**：
 *   · KPI 卡：员工总数 / 在职 / 停用 / 离职 / 部门 / 职位 / 待改密 / 密码风险
 *   · 部门人数分布：从员工列表按 department_id 聚合，部门名走 /options 下发
 *   · 快速入口：跳到六个真实管理模块
 *
 * 数据全部来自已有接口（员工列表 / 组织选项 / 密码看板），不新增后端端点。
 * 用量看板刻意不在这里拉 —— 它 require_admin，HR 进来会 403，
 * 而概览要「谁都能进得去」。
 */
import { computed, onMounted, ref } from 'vue'
import ToastStack from '@/components/ToastStack.vue'
import { newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const loading = ref(false)
const loadedAt = ref<string | null>(null)
const toasts = ref<ToastItem[]>([])
function toast(kind: ToastItem['kind'], text: string) {
  toasts.value.push({ ...newToast(kind, text) })
}
function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

const users = ref<api.UserRow[]>([])
const options = ref<api.Options | null>(null)
const pwBoard = ref<api.PasswordBoard | null>(null)

// ---- KPI 派生 ----
const total = computed(() => users.value.length)
const byStatus = computed(() => {
  const c = { active: 0, disabled: 0, resigned: 0 }
  for (const u of users.value) c[u.status] += 1
  return c
})
const deptCount = computed(() => options.value?.departments.length ?? 0)
const posCount = computed(() => options.value?.positions.length ?? 0)
const pwCount = computed(() => pwBoard.value?.counts ?? null)
const mustChange = computed(() => pwCount.value?.must_change ?? 0)
/** 密码风险 = 已过期 + 已锁定（这两项在本项目定义里是「需要有人立刻处理」）。 */
const pwRisk = computed(() => (pwCount.value ? pwCount.value.expired + pwCount.value.locked : 0))

// ---- 部门人数分布（按 department_id 聚合，部门名来自 options）----
const deptStats = computed(() => {
  const counts = new Map<number, number>()
  for (const u of users.value) {
    if (u.department_id != null) counts.set(u.department_id, (counts.get(u.department_id) ?? 0) + 1)
  }
  const max = Math.max(1, ...Array.from(counts.values()))
  return (options.value?.departments ?? [])
    .map((d) => ({ name: d.name, count: counts.get(d.id) ?? 0 }))
    .sort((a, b) => b.count - a.count)
    .map((d) => ({ ...d, pct: Math.round((d.count / max) * 100) }))
})

const quickLinks = [
  { name: 'users', title: '员工管理', desc: '新建 / 编辑员工，调整部门、职位、角色与额度' },
  { name: 'org', title: '组织架构', desc: '维护部门层级与负责人、职位序列' },
  { name: 'passwords', title: '密码看板', desc: '待改密、即将过期、已过期、锁定与长期未登录' },
  { name: 'usage', title: '用量看板', desc: '按人 × 自然月的 token 用量与额度水位（管理员）' },
  { name: 'audit', title: '审计日志', desc: '谁在什么时候改了什么，全量留痕' },
  { name: 'traces', title: '问答溯源', desc: '每条问答的路由、检索与依据校验轨迹' },
]

async function load() {
  loading.value = true
  try {
    const [u, opt] = await Promise.all([
      api.fetchUsers({ limit: 1000, include_resigned: true }),
      api.fetchOptions(),
    ])
    users.value = u.items
    options.value = opt
    // 密码看板 HR 也能看；单独 catch，失败不影响其余看板
    pwBoard.value = await api.fetchPasswordBoard().catch(() => null)
    loadedAt.value = new Date().toLocaleString('zh-CN', { hour12: false })
  } catch (e) {
    toast('error', e instanceof ApiError ? e.message : '概览数据加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="overview">
    <ToastStack :items="toasts" @dismiss="dismiss" />

    <!-- 头部：问候 + 刷新 -->
    <div class="head">
      <div>
        <h1 class="page-title">概览</h1>
        <p class="page-sub">
          你好，{{ auth.profile?.display_name || '管理员' }}。这是企业知识库管理端 ——
          在这里管员工、组织架构与密码策略，并查看使用量与问答溯源。
        </p>
      </div>
      <button class="btn btn-sm" :disabled="loading" @click="load">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <!-- KPI 卡 -->
    <div class="kpi-grid">
      <div class="kpi card">
        <div class="kpi-label">员工总数</div>
        <div class="kpi-value">{{ total }}</div>
        <div class="kpi-hint">含离职</div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">在职</div>
        <div class="kpi-value ok">{{ byStatus.active }}</div>
        <div class="kpi-hint">可正常登录</div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">停用</div>
        <div class="kpi-value warn">{{ byStatus.disabled }}</div>
        <div class="kpi-hint">账号已禁用</div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">离职</div>
        <div class="kpi-value muted">{{ byStatus.resigned }}</div>
        <div class="kpi-hint">保留记录不删行</div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">部门</div>
        <div class="kpi-value">{{ deptCount }}</div>
        <div class="kpi-hint">含子部门</div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">职位</div>
        <div class="kpi-value">{{ posCount }}</div>
        <div class="kpi-hint">职级序列</div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">待改密</div>
        <div class="kpi-value" :class="{ warn: mustChange > 0 }">{{ mustChange }}</div>
        <div class="kpi-hint">
          即将过期 {{ pwCount?.expiring_soon ?? 0 }} · 长期未登录 {{ pwCount?.stale_login ?? 0 }}
        </div>
      </div>
      <div class="kpi card">
        <div class="kpi-label">密码风险</div>
        <div class="kpi-value" :class="{ danger: pwRisk > 0 }">{{ pwRisk }}</div>
        <div class="kpi-hint">已过期 {{ pwCount?.expired ?? 0 }} · 锁定 {{ pwCount?.locked ?? 0 }}</div>
      </div>
    </div>

    <!-- 下半部：部门分布 + 快速入口 -->
    <div class="lower">
      <div class="card dept-card">
        <div class="card-head">部门人数分布</div>
        <ul class="dept-list">
          <li v-for="d in deptStats" :key="d.name">
            <span class="dept-name">{{ d.name }}</span>
            <span class="dept-bar">
              <i class="dept-fill" :style="{ width: d.pct + '%' }" />
            </span>
            <span class="dept-count mono">{{ d.count }}</span>
          </li>
          <li v-if="!deptStats.length" class="muted empty-row">暂无部门数据</li>
        </ul>
      </div>

      <div class="card quick-card">
        <div class="card-head">快速入口</div>
        <div class="quick-grid">
          <router-link
            v-for="q in quickLinks"
            :key="q.name"
            class="quick-item"
            :to="{ name: q.name }"
          >
            <span class="quick-title">{{ q.title }}</span>
            <span class="quick-desc">{{ q.desc }}</span>
          </router-link>
        </div>
      </div>
    </div>

    <p v-if="loadedAt" class="muted foot">数据更新于 {{ loadedAt }}</p>
    <p v-else-if="loading" class="muted foot">加载中…</p>
  </div>
</template>

<style scoped>
.overview {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

/* KPI 卡网格 */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}
@media (max-width: 980px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
.kpi {
  padding: 16px 18px;
}
.kpi-label {
  color: var(--text-3);
  font-size: 12px;
}
.kpi-value {
  font-size: 30px;
  font-weight: 600;
  line-height: 1.2;
  margin-top: 6px;
  font-variant-numeric: tabular-nums;
}
.kpi-value.ok {
  color: var(--success);
}
.kpi-value.warn {
  color: var(--warn);
}
.kpi-value.danger {
  color: var(--danger);
}
.kpi-value.muted {
  color: var(--text-2);
}
.kpi-hint {
  color: var(--text-3);
  font-size: 11px;
  margin-top: 4px;
}

/* 下半部两栏 */
.lower {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr);
  gap: 14px;
}
@media (max-width: 980px) {
  .lower {
    grid-template-columns: 1fr;
  }
}
.card-head {
  font-weight: 600;
  font-size: 14px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}

/* 部门分布条 */
.dept-card .dept-list {
  list-style: none;
  padding: 8px 16px 14px;
}
.dept-list li {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 0;
}
.dept-name {
  flex: 0 0 132px;
  font-size: 13px;
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dept-bar {
  flex: 1 1 auto;
  height: 8px;
  background: var(--bg-app);
  border-radius: 999px;
  overflow: hidden;
}
.dept-fill {
  display: block;
  height: 100%;
  background: var(--primary);
  border-radius: 999px;
  transition: width 0.3s ease;
}
.dept-count {
  flex: 0 0 32px;
  text-align: right;
  font-size: 13px;
  color: var(--text-1);
}
.empty-row {
  padding: 12px 0;
  text-align: center;
}

/* 快速入口 */
.quick-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
  padding: 12px;
}
.quick-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.quick-item:hover {
  border-color: var(--primary);
  background: var(--bg-hover);
}
.quick-title {
  font-weight: 600;
  font-size: 13px;
}
.quick-desc {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
}

.foot {
  font-size: 12px;
}
</style>
