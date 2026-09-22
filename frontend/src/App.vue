<script setup lang="ts">
/**
 * 根布局：左（侧边栏）+ 右（内容区）双栏。
 *
 * 布局与交互职责划分：
 * - 左侧 AppSidebar：项目信息展示、会话列表管理（新建/切换/删除）、
 *   全局导航入口（知识库管理 / 系统设置）。只负责「导航与选择」，不渲染业务内容。
 * - 右侧内容区：根据 uiStore.activeView 渲染 ChatView / KnowledgeView / SettingsView，
 *   只负责「展示与业务交互」，不包含导航。
 * 两侧通过 Pinia store 通信，组件之间零直接依赖。
 */
import { onMounted } from 'vue'
import AppSidebar from '@/components/AppSidebar.vue'
import ChatView from '@/views/ChatView.vue'
import KnowledgeView from '@/views/KnowledgeView.vue'
import SettingsView from '@/views/SettingsView.vue'
import ToastStack from '@/components/ToastStack.vue'
import { useSessionStore } from '@/stores/sessions'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
const sessionStore = useSessionStore()

onMounted(() => {
  // 初始化：拉取后端已有会话；无任何会话则本地新建一个空会话
  void sessionStore.fetchSessions().then(() => {
    if (sessionStore.sessions.length === 0) {
      sessionStore.createSession()
    } else {
      void sessionStore.selectSession(sessionStore.sortedSessions[0].session_id)
    }
  })
})
</script>

<template>
  <div class="app-shell">
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
</style>
