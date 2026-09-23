<script setup lang="ts">
/**
 * 根布局：登录态分流 + 左（侧边栏）右（内容区）双栏。
 *
 * 渲染分三种状态，互斥：
 *   verifying  —— 正在校验本地 token（极短），此时既不显示主界面也不显示登录页。
 *                 为什么要有这个中间态：若不校验直接渲染主界面，token 已失效的用户
 *                 会先看到主界面闪一下、然后被 401 踢回登录页，观感像「页面崩了」。
 *   !isAuthenticated —— 登录页。
 *   其余       —— 主界面（侧边栏 + 内容区）。
 *
 * 登录/登出时的副作用都收在这里，而不是散在侧边栏与登录页里：
 *   · 登录成功 → 拉会话列表、恢复上次的会话、切回问答视图；
 *   · 登出（含 token 过期的被动登出）→ 清空本地会话数据（隐私，见 sessions.resetAll）。
 * 这样「进入系统要做什么」只有一处定义，不会出现「从某个入口登录进来少了初始化」。
 */
import { onMounted, watch } from 'vue'
import AppSidebar from '@/components/AppSidebar.vue'
import ChatView from '@/views/ChatView.vue'
import KnowledgeView from '@/views/KnowledgeView.vue'
import LoginView from '@/views/LoginView.vue'
import SettingsView from '@/views/SettingsView.vue'
import ToastStack from '@/components/ToastStack.vue'
import { useAuthStore } from '@/stores/auth'
import { useSessionStore } from '@/stores/sessions'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
const auth = useAuthStore()
const sessionStore = useSessionStore()

/** 进入主界面后的初始化：拉后端会话，无会话则本地建一个，有则选中最近活跃的那个。 */
async function initWorkspace() {
  await sessionStore.fetchSessions()
  if (sessionStore.sessions.length === 0) {
    sessionStore.createSession()
    return
  }
  const target = sessionStore.pinnedSessions[0] ?? sessionStore.normalSessions[0]
  await sessionStore.selectSession(target.session_id)
}

onMounted(async () => {
  // 先校验本地 token：失效的话 http 层已经清掉凭据，这里自然落到登录页
  await auth.verify()
  if (auth.isAuthenticated) await initWorkspace()
})

watch(
  () => auth.isAuthenticated,
  async (loggedIn) => {
    if (loggedIn) {
      ui.switchView('chat')
      await initWorkspace()
    } else {
      // 主动登出与被动登出共用这条路径：都要清掉上一个用户的本地数据
      sessionStore.resetAll()
    }
  },
)
</script>

<template>
  <!-- 校验中：只给一个安静的状态提示，不渲染任何业务界面 -->
  <div v-if="auth.status === 'verifying'" class="boot-screen">
    <div class="boot-spinner" />
    <p>正在校验登录状态…</p>
  </div>

  <LoginView v-else-if="!auth.isAuthenticated" />

  <div v-else class="app-shell">
    <AppSidebar />
    <main class="content-area">
      <ChatView v-if="ui.activeView === 'chat'" />
      <KnowledgeView v-else-if="ui.activeView === 'knowledge'" />
      <SettingsView v-else-if="ui.activeView === 'settings'" />
    </main>
    <ToastStack />
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100%;
  overflow: hidden;
}

.content-area {
  flex: 1;
  min-width: 0; /* 允许内部子元素收缩滚动 */
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--bg-content);
}

.boot-screen {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--text-3);
  font-size: 13px;
  background: var(--bg-app);
}

.boot-spinner {
  width: 22px;
  height: 22px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: boot-spin 0.7s linear infinite;
}

@keyframes boot-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
