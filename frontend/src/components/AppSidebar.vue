<script setup lang="ts">
/**
 * 左侧边栏（导航职责）：
 * ① 项目信息（名称/版本/服务状态）
 * ② 会话列表（新建 / 切换 / 删除，切换后右侧加载该会话历史）
 * ③ 底部功能入口：文件传输 → 知识库管理；系统设置 → 配置页
 * ④ 当前登录用户 + 退出登录
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useSessionStore, type LocalSession } from '@/stores/sessions'
import { useSettingsStore } from '@/stores/settings'
import { useUiStore, type ActiveView } from '@/stores/ui'

const ui = useUiStore()
const sessions = useSessionStore()
const settingsStore = useSettingsStore()
const auth = useAuthStore()

/** 头像占位字符：用户名首字母（没有用户名时退化成问号，不显示空白） */
const userInitial = computed(() => (auth.username || '?').slice(0, 1).toUpperCase())

/**
 * 退出登录。
 *
 * 不做二次确认：登出是可逆操作（重新登录即可，会话数据在后端），
 * 弹确认框反而增加一次无意义的点击；而「误触登出」的代价只是再输一次密码。
 * 清空本地数据、回到登录页由 auth store + App.vue 的 watch 统一处理。
 */
async function onLogout() {
  await auth.logout()
  ui.toast('已退出登录', 'info')
}

/** 当前展开「⋯」菜单的会话 id */
const menuFor = ref<string | null>(null)
/** 行内重命名状态 */
const renamingId = ref<string | null>(null)
const renameText = ref('')

/** v-focus：重命名输入框插入即聚焦 */
const vFocus = { mounted: (el: HTMLInputElement) => el.focus() }

function toggleMenu(id: string) {
  menuFor.value = menuFor.value === id ? null : id
}

function onDocClick(e: MouseEvent) {
  // 点击菜单/行外任意区域收起菜单
  if (!(e.target as HTMLElement).closest('.session-item')) menuFor.value = null
}

const version = computed(() => settingsStore.health?.version ?? '—')

/** 置顶分组 + 全部分组（置顶为空时不显示「置顶」组头） */
const sessionGroups = computed(() => {
  const groups: { label: string; items: LocalSession[] }[] = []
  if (sessions.pinnedSessions.length) {
    groups.push({ label: '置顶', items: sessions.pinnedSessions })
  }
  groups.push({
    label: sessions.pinnedSessions.length ? '全部会话' : '会话',
    items: sessions.normalSessions,
  })
  return groups
})

const serviceUp = ref<boolean | null>(null)
let healthTimer: ReturnType<typeof setInterval> | null = null

async function pingHealth() {
  try {
    // 只探测 /system/health（轻量），配置数据不需要轮询
    await settingsStore.pingHealth()
    serviceUp.value = true
  } catch {
    serviceUp.value = false
  }
}

onMounted(() => {
  void pingHealth()
  document.addEventListener('click', onDocClick)
  // 30s 轮询一次服务状态灯；settings 接口不参与轮询（重且数据几乎不变）
  healthTimer = setInterval(pingHealth, 30_000)
})
onBeforeUnmount(() => {
  if (healthTimer) clearInterval(healthTimer)
  document.removeEventListener('click', onDocClick)
})

function navTo(view: ActiveView) {
  ui.switchView(view)
}

function onNewSession() {
  sessions.createSession()
  ui.switchView('chat')
}

function onSelect(sessionId: string) {
  menuFor.value = null
  void sessions.selectSession(sessionId)
  ui.switchView('chat')
}

function onTogglePin(sessionId: string) {
  void sessions.togglePin(sessionId)
  menuFor.value = null
}

function startRename(s: LocalSession) {
  renamingId.value = s.session_id
  renameText.value = s.title
  menuFor.value = null
}

async function confirmRename() {
  if (renamingId.value) await sessions.renameSession(renamingId.value, renameText.value)
  renamingId.value = null
}

function cancelRename() {
  renamingId.value = null
}

function onRemove(sessionId: string) {
  menuFor.value = null
  void sessions.removeSession(sessionId).then(() => {
    // 删完当前会话后自动落到最近一个会话（没有则新建）
    if (!sessions.currentId) {
      const next = sessions.pinnedSessions[0] ?? sessions.normalSessions[0]
      if (next) {
        void sessions.selectSession(next.session_id)
      } else {
        sessions.createSession()
      }
    }
  })
}

function fmtTime(ts: number | null): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return sameDay ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}
</script>

<template>
  <aside class="sidebar">
    <!-- ① 项目信息 -->
    <div class="project-card" @click="navTo('chat')">
      <div class="project-logo">R</div>
      <div class="project-meta">
        <div class="project-name">RAG 智能问答系统</div>
        <div class="project-sub">
          <span class="tag">v{{ version }}</span>
          <span class="health" :class="serviceUp === true ? 'up' : serviceUp === false ? 'down' : ''">
            {{ serviceUp === true ? '服务正常' : serviceUp === false ? '服务离线' : '检测中…' }}
          </span>
        </div>
      </div>
    </div>

    <button class="new-session" @click="onNewSession">＋ 新建会话</button>

    <!-- ② 会话列表（置顶分组 + 普通分组） -->
    <div class="session-list" :class="{ loading: sessions.listLoading }">
      <div class="list-title">
        会话列表
        <span class="list-count">{{ sessions.sessions.length }}</span>
      </div>
      <div v-if="sessions.listLoading && sessions.sessions.length === 0" class="list-hint">
        加载中…
      </div>
      <template v-else>
        <template v-for="group in sessionGroups" :key="group.label">
          <div v-if="group.items.length" class="group-label">{{ group.label }}</div>
          <div
            v-for="s in group.items"
            :key="s.session_id"
            class="session-item"
            :class="{ active: s.session_id === sessions.currentId && ui.activeView === 'chat' }"
            @click="onSelect(s.session_id)"
          >
            <!-- 重命名态：行内输入框 -->
            <template v-if="renamingId === s.session_id">
              <input
                v-focus
                v-model="renameText"
                class="rename-input"
                maxlength="60"
                @click.stop
                @keyup.enter="confirmRename"
                @keyup.esc="cancelRename"
                @blur="confirmRename"
              >
            </template>
            <template v-else>
              <div class="session-main">
                <div class="session-title">
                  <svg v-if="s.pinned" class="pin-icon" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5" /><path d="M9 3h6l1 7 3 3H5l3-3z" /></svg>
                  {{ s.title }}
                </div>
                <div class="session-sub">
                  <span>{{ s.message_count }} 条消息</span>
                  <span v-if="s.usage && s.usage.requests > 0" class="usage-badge" :title="`输入 ${s.usage.input_tokens} / 输出 ${s.usage.output_tokens} tokens`">
                    {{ s.usage.input_tokens + s.usage.output_tokens }} tok
                  </span>
                  <span v-if="s.local" class="local-badge">未发送</span>
                </div>
              </div>
              <span class="session-time">{{ fmtTime(s.last_active) }}</span>
              <button
                class="session-more"
                title="更多操作"
                @click.stop="toggleMenu(s.session_id)"
              >
                ⋯
              </button>
              <!-- 「⋯」下拉菜单：置顶 / 重命名 / 删除 -->
              <div v-if="menuFor === s.session_id" class="session-menu" @click.stop>
                <button class="menu-item" @click="onTogglePin(s.session_id)">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5" /><path d="M9 3h6l1 7 3 3H5l3-3z" /></svg>
                  {{ s.pinned ? '取消置顶' : '置顶' }}
                </button>
                <button class="menu-item" @click="startRename(s)">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" /></svg>
                  重命名
                </button>
                <button class="menu-item danger" @click="onRemove(s.session_id)">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                  删除会话
                </button>
              </div>
            </template>
          </div>
        </template>
        <div v-if="sessions.sessions.length === 0" class="list-hint">暂无会话</div>
      </template>
    </div>

    <!-- ③ 功能入口 -->
    <div class="sidebar-footer">
      <button
        class="nav-btn"
        :class="{ active: ui.activeView === 'knowledge' }"
        @click="navTo('knowledge')"
      >
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="17 8 12 3 7 8" />
          <line x1="12" y1="3" x2="12" y2="15" />
        </svg>
        文件传输 · 知识库
      </button>
      <button
        class="nav-btn"
        :class="{ active: ui.activeView === 'settings' }"
        @click="navTo('settings')"
      >
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
        系统设置
      </button>
    </div>

    <!-- ④ 当前登录用户 + 退出登录 -->
    <div class="user-bar">
      <div class="user-avatar">{{ userInitial }}</div>
      <div class="user-meta">
        <span class="user-name" :title="auth.username">{{ auth.username || '未登录' }}</span>
        <span v-if="auth.expiresSoon" class="user-hint">登录即将过期</span>
      </div>
      <button class="logout-btn" title="退出登录" aria-label="退出登录" @click="onLogout">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
          <polyline points="16 17 21 12 16 7" />
          <line x1="21" y1="12" x2="9" y2="12" />
        </svg>
      </button>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-width);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  overflow: hidden;
}

.project-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 16px 12px;
  cursor: pointer;
}
.project-logo {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: linear-gradient(135deg, #4f6ef7, #7b5bf2);
  color: #fff;
  font-weight: 700;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.project-name {
  font-size: 14px;
  font-weight: 600;
}
.project-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 3px;
}
.health {
  font-size: 12px;
  color: var(--text-3);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.health::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-3);
}
.health.up::before {
  background: var(--success);
}
.health.up {
  color: var(--success);
}
.health.down::before {
  background: var(--danger);
}
.health.down {
  color: var(--danger);
}

.new-session {
  margin: 4px 16px 12px;
  padding: 8px 0;
  border-radius: var(--radius-sm);
  background: var(--primary);
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  transition: background 0.15s;
}
.new-session:hover {
  background: var(--primary-hover);
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px;
}
.list-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-3);
  padding: 4px 8px 6px;
}
.list-count {
  background: var(--bg-hover);
  border-radius: 8px;
  padding: 0 6px;
  font-size: 11px;
  line-height: 16px;
}
.group-label {
  font-size: 11px;
  color: var(--text-3);
  padding: 8px 8px 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.group-label::before {
  content: '';
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--text-3);
  opacity: 0.6;
}
.session-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s;
}
.session-item:hover {
  background: var(--bg-hover);
}
.session-item.active {
  background: var(--bg-active);
}
.session-main {
  flex: 1;
  min-width: 0;
}
.session-title {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 4px;
}
.pin-icon {
  color: var(--primary);
  flex-shrink: 0;
}
.rename-input {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  padding: 4px 8px;
  border: 1px solid var(--primary);
  border-radius: 6px;
  outline: none;
  background: var(--bg-content, #fff);
  color: var(--text-1);
}
.session-sub {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-3);
  margin-top: 2px;
}
.local-badge {
  color: #b8860b;
  background: #fdf6e3;
  padding: 0 5px;
  border-radius: 3px;
}
.usage-badge {
  color: var(--primary);
  background: var(--primary-light);
  padding: 0 5px;
  border-radius: 3px;
}
.session-time {
  font-size: 11px;
  color: var(--text-3);
  flex-shrink: 0;
}
/* hover / 激活态：时间让位给「⋯」按钮，二者不再重叠 */
.session-more {
  display: none;
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
  font-size: 15px;
  line-height: 1;
  letter-spacing: 1px;
}
.session-more:hover {
  background: var(--bg-active);
  color: var(--text-1);
}
.session-item:hover .session-time,
.session-item.active .session-time {
  display: none;
}
.session-item:hover .session-more,
.session-item.active .session-more {
  display: inline-flex;
}
/* 「⋯」下拉操作菜单 */
.session-menu {
  position: absolute;
  right: 6px;
  top: calc(100% - 2px);
  z-index: 30;
  min-width: 132px;
  background: var(--bg-content, #fff);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12);
  padding: 4px;
  display: flex;
  flex-direction: column;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text-1);
  text-align: left;
  white-space: nowrap;
}
.menu-item:hover {
  background: var(--bg-hover);
}
.menu-item.danger {
  color: var(--danger);
}
.menu-item.danger:hover {
  background: var(--danger-light);
}
.list-hint {
  font-size: 12px;
  color: var(--text-3);
  text-align: center;
  padding: 16px 0;
}

.sidebar-footer {
  border-top: 1px solid var(--border);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--text-2);
  text-align: left;
  transition: background 0.12s;
}
.nav-btn:hover {
  background: var(--bg-hover);
}
.nav-btn.active {
  background: var(--bg-active);
  color: var(--primary);
  font-weight: 500;
}

/* 登录用户区：贴在侧边栏最底部，与功能入口用分割线隔开 */
.user-bar {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
}
.user-avatar {
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  border-radius: 50%;
  background: var(--primary-light);
  color: var(--primary);
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.user-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.user-name {
  font-size: 13px;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-hint {
  font-size: 11px;
  color: var(--danger);
}
.logout-btn {
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  border-radius: 6px;
  color: var(--text-3);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.12s, color 0.12s;
}
.logout-btn:hover {
  background: var(--danger-light);
  color: var(--danger);
}
</style>
