<script setup lang="ts">
/**
 * P2-16a / P2-20 / P2-21：个人中心面板（点侧边栏左下角用户区弹出）。
 *
 * 结构（参考 WorkBuddy 的交互）：
 *   头像/姓名/部门职位（顶）→ 基本资料 → 用量 → 菜单层 → 退出登录（底）。
 *
 * P2-21 三处调整：
 *  ① **「系统设置 / 文件传输·知识库」两个入口从侧边栏移进来**（侧边栏本体不再有它们）；
 *     图标换用侧边栏原来那套线性 SVG（用户明确「那个图标比展开的好看」）；
 *  ② **修改密码改为独立弹窗**（`ChangePasswordDialog`）—— 本面板只 emit 事件，
 *     不做表单（面板是「看信息 + 导航」，改密是完整任务，两种交互模型不混在一起）；
 *  ③ 退出登录走**通用二次确认**（`stores/confirm.ts`，全站同一套弹窗）。
 *
 * 数据打开时拉一次（低频查看，不轮询）。
 */
import { ref, watch } from 'vue'
import { fetchMyProfile, type MyProfile } from '@/api/qa'
import { useAuthStore } from '@/stores/auth'
import { useConfirmStore } from '@/stores/confirm'
import { useUiStore } from '@/stores/ui'

const auth = useAuthStore()
const confirm = useConfirmStore()
const ui = useUiStore()

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  /** P2-21：请求打开「修改密码」独立弹窗（由父层挂载组件） */
  (e: 'change-password'): void
}>()

const profile = ref<MyProfile | null>(null)
const loading = ref(false)
const error = ref('')

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    loading.value = true
    error.value = ''
    profile.value = null
    try {
      profile.value = await fetchMyProfile()
    } catch {
      error.value = '个人信息加载失败，请稍后重试'
    } finally {
      loading.value = false
    }
  },
)

/** 菜单项：切视图 + 关面板 */
function navTo(view: 'settings' | 'knowledge') {
  ui.switchView(view)
  onClose()
}

/** P2-21：退出登录二次确认（文案说明「数据不会丢」—— 不说清会让人不敢点） */
async function onLogout() {
  const ok = await confirm.ask({
    title: '确认退出登录？',
    text: '退出后需要用账号密码重新登录。你的会话记录与知识库内容都不会丢失。',
    confirmText: '退出登录',
  })
  if (!ok) return
  onClose()
  await auth.logout()
}

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('zh-CN')
}

function onClose() {
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <!-- 点击遮罩关闭；面板锚定在侧边栏左下角用户区上方（WorkBuddy 式） -->
    <div v-if="open" class="pp-mask" @click.self="onClose">
      <div class="pp-panel" role="dialog" aria-label="个人中心">
        <!-- ① 头部 -->
        <header class="pp-card pp-head-card">
          <div class="pp-avatar">{{ profile?.profile.display_name.slice(0, 1) ?? '…' }}</div>
          <div class="pp-head-text">
            <p class="pp-name">{{ profile?.profile.display_name ?? '—' }}</p>
            <p class="pp-sub muted">
              {{ profile?.profile.department ?? '未分配部门' }} · {{ profile?.profile.position ?? '未分配职位' }}
            </p>
          </div>
        </header>

        <p v-if="loading" class="pp-state">加载中…</p>
        <p v-else-if="error" class="pp-state pp-error">{{ error }}</p>

        <template v-else-if="profile">
          <!-- ② 基本资料 -->
          <div class="pp-card">
            <dl class="pp-rows">
              <div class="pp-row"><dt>登录名</dt><dd class="mono">{{ profile.profile.username }}</dd></div>
              <div class="pp-row"><dt>工号</dt><dd class="mono">{{ profile.profile.employee_no }}</dd></div>
              <div class="pp-row"><dt>角色</dt><dd>{{ profile.profile.role_label }}</dd></div>
              <div class="pp-row"><dt>邮箱</dt><dd>{{ profile.profile.email ?? '—' }}</dd></div>
              <div class="pp-row"><dt>手机</dt><dd class="mono">{{ profile.profile.phone ?? '—' }}</dd></div>
              <div class="pp-row"><dt>入职</dt><dd>{{ profile.profile.joined_at.slice(0, 10) }}</dd></div>
            </dl>
          </div>

          <!-- ③ 用量（P2-16a，用户明确希望保留） -->
          <div class="pp-card">
            <div class="pp-usage-line">
              <span class="pp-usage-label">本月用量</span>
              <span class="mono">{{ fmt(profile.month_usage.billable_tokens) }}</span>
            </div>
            <div class="pp-usage-line">
              <span class="pp-usage-label">月度额度</span>
              <span class="mono">
                {{ profile.month_usage.effective_quota > 0 ? fmt(profile.month_usage.effective_quota) : '不限' }}
                <template v-if="profile.month_usage.usage_percent != null">
                  （{{ profile.month_usage.usage_percent.toFixed(1) }}%）
                </template>
              </span>
            </div>
            <div class="pp-usage-line">
              <span class="pp-usage-label">历史总用量</span>
              <span class="mono">{{ fmt(profile.total_usage.billable_tokens) }} · {{ profile.total_usage.requests }} 次</span>
            </div>
            <p class="pp-note muted">超额只提醒，不限制使用；删除会话会同时移除其用量记录。</p>
          </div>

          <!-- ④ 菜单层：图标用侧边栏原来那套线性 SVG（用户要求） -->
          <nav class="pp-menu">
            <button class="pp-menu-item" @click="navTo('settings')">
              <svg class="pp-menu-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
              <span>系统设置</span>
              <span class="pp-menu-arrow">›</span>
            </button>
            <button class="pp-menu-item" @click="navTo('knowledge')">
              <svg class="pp-menu-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="17 8 12 3 7 8" />
                <line x1="12" y1="3" x2="12" y2="15" />
              </svg>
              <span>文件传输 · 知识库</span>
              <span class="pp-menu-arrow">›</span>
            </button>
            <!-- P2-21：钥匙图标为单色线性（不用 emoji —— 彩色 emoji 与主题不搭） -->
            <button class="pp-menu-item" @click="emit('change-password')">
              <svg class="pp-menu-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="7.5" cy="15.5" r="4.5" />
                <path d="M10.7 12.3 20 3" />
                <path d="M16 7l3 3" />
              </svg>
              <span>修改密码</span>
              <span class="pp-menu-arrow">›</span>
            </button>
          </nav>

          <!-- ⑤ 退出登录（外侧用户区右侧的退出入口保留） -->
          <div class="pp-menu pp-menu--last">
            <button class="pp-menu-item pp-logout" @click="onLogout">
              <svg class="pp-menu-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                <polyline points="16 17 21 12 16 7" />
                <line x1="21" y1="12" x2="9" y2="12" />
              </svg>
              <span>退出登录</span>
            </button>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩：透明（点击空白关闭）—— 轻量弹出，不是模态 */
.pp-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
}

/* 🔴 底部锚定：贴着侧边栏左下角用户区上方。
   P2-21：宽度调大（用户反馈字段换行）—— 比 --sidebar-width 大是有意的。 */
.pp-panel {
  position: fixed;
  left: 10px;
  bottom: 74px;
  width: 340px;
  max-height: calc(100vh - 100px);
  overflow-y: auto;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e5e5);
  border-radius: 14px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pp-card {
  background: var(--bg-app, rgba(0, 0, 0, 0.02));
  border-radius: 10px;
  padding: 12px;
}

.pp-head-card {
  display: flex;
  align-items: center;
  gap: 12px;
}

.pp-avatar {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--primary, #4a6cf7);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 600;
  flex-shrink: 0;
}

.pp-name {
  margin: 0;
  font-weight: 600;
  font-size: 15px;
}

.pp-sub {
  margin: 2px 0 0;
  font-size: 12px;
}

.pp-rows {
  margin: 0;
  display: grid;
  gap: 5px;
}

.pp-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
}

.pp-row dt {
  color: var(--text-3, #888);
  flex-shrink: 0;
}

.pp-row dd {
  margin: 0;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pp-usage-line {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  padding: 3px 0;
}

.pp-usage-label {
  color: var(--text-3, #888);
}

.pp-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pp-menu--last {
  border-top: 1px solid var(--border, #eee);
  padding-top: 6px;
}

.pp-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  border: none;
  background: none;
  color: inherit;
  font-size: 13px;
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
}

.pp-menu-item:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.05));
}

/* 🔴 单色线性图标（currentColor）—— 不用彩色 emoji */
.pp-menu-icon {
  flex-shrink: 0;
  color: var(--text-2, #666);
}

.pp-menu-arrow {
  margin-left: auto;
  color: var(--text-3, #999);
}

.pp-logout,
.pp-logout .pp-menu-icon {
  color: #d9534f;
}

.pp-note {
  font-size: 11px;
  margin: 6px 0 0;
}

.pp-state {
  text-align: center;
  color: var(--text-3, #888);
  padding: 24px 0;
}

.pp-error {
  color: #d9534f;
  font-size: 12px;
}

.muted {
  color: var(--text-3, #888);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
