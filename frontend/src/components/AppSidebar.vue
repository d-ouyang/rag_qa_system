<script setup lang="ts">
/**
 * 左侧边栏（导航职责）：
 * ① 项目信息（名称/版本/服务状态）
 * ② 会话列表（新建 / 切换 / 删除，切换后右侧加载该会话历史）
 * ③ 底部功能入口：文件传输 → 知识库管理；系统设置 → 配置页
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useSessionStore } from '@/stores/sessions'
import { useSettingsStore } from '@/stores/settings'
import { useUiStore, type ActiveView } from '@/stores/ui'

const ui = useUiStore()
const sessions = useSessionStore()
const settingsStore = useSettingsStore()

const showDelete = ref<string | null>(null)

const version = computed(() => settingsStore.health?.version ?? '—')
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
  // 30s 轮询一次服务状态灯；settings 接口不参与轮询（重且数据几乎不变）
  healthTimer = setInterval(pingHealth, 30_000)
})
onBeforeUnmount(() => {
  if (healthTimer) clearInterval(healthTimer)
})

function navTo(view: ActiveView) {
  ui.switchView(view)
}

function onNewSession() {
  sessions.createSession()
  ui.switchView('chat')
}

function onSelect(sessionId: string) {
  void sessions.selectSession(sessionId)
  ui.switchView('chat')
}

function onRemove(sessionId: string) {
  void sessions.removeSession(sessionId).then(() => {
    // 删完当前会话后自动落到最近一个会话（没有则新建）
    if (!sessions.currentId) {
      if (sessions.sortedSessions.length > 0) {
        void sessions.selectSession(sessions.sortedSessions[0].session_id)
      } else {
        sessions.createSession()
      }
    }
  })
  showDelete.value = null
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

    <!-- ② 会话列表 -->
    <div class="session-list" :class="{ loading: sessions.listLoading }">
      <div class="list-title">
        会话列表
        <span class="list-count">{{ sessions.sortedSessions.length }}</span>
      </div>
      <div v-if="sessions.listLoading && sessions.sortedSessions.length === 0" class="list-hint">
        加载中…
      </div>
      <template v-else>
        <div
          v-for="s in sessions.sortedSessions"
          :key="s.session_id"
          class="session-item"
          :class="{ active: s.session_id === sessions.currentId && ui.activeView === 'chat' }"
          @click="onSelect(s.session_id)"
          @mouseenter="showDelete = s.session_id"
          @mouseleave="showDelete = null"
        >
          <div class="session-main">
            <div class="session-title">{{ s.title }}</div>
            <div class="session-sub">
              <span>{{ s.message_count }} 条消息</span>
              <span v-if="s.local" class="local-badge">未发送</span>
            </div>
          </div>
          <span class="session-time">{{ fmtTime(s.last_active) }}</span>
          <button
            v-if="showDelete === s.session_id"
            class="session-del"
            title="删除会话"
            @click.stop="onRemove(s.session_id)"
          >
            ✕
          </button>
        </div>
        <div v-if="sessions.sortedSessions.length === 0" class="list-hint">暂无会话</div>
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
.session-time {
  font-size: 11px;
  color: var(--text-3);
  flex-shrink: 0;
}
.session-item.active .session-time {
  display: none;
}
.session-del {
  display: none;
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 20px;
  border-radius: 4px;
  color: var(--text-3);
  font-size: 11px;
}
.session-item:hover .session-del,
.session-item.active:hover .session-del {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.session-del:hover {
  background: var(--danger-light);
  color: var(--danger);
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
</style>
