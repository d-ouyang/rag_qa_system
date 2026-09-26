<script setup lang="ts">
/**
 * 会话主页面（右侧内容区 · chat 视图）：
 * 头部（会话标题/元信息）+ 消息流 + 输入框。
 * 消息数据来自 sessionStore（切换会话时已加载历史，提问时本地流式拼接）。
 */
import { nextTick, onMounted, ref, watch } from 'vue'
import MessageBubble from '@/components/MessageBubble.vue'
import ChatInput from '@/components/ChatInput.vue'
import { useSessionStore } from '@/stores/sessions'

const sessions = useSessionStore()
const scrollBox = ref<HTMLElement | null>(null)
/** 距底部小于这个像素视为「还在看最新内容」，继续跟着流往下滚 */
const NEAR_BOTTOM_PX = 80
/** 用户上滑阅读时为 false，避免新帧把视口拽回底部 */
let stickToBottom = true

const INTENT_LABELS: Record<string, string> = {
  knowledge_query: '知识查询',
  operation_guide: '操作指导',
  policy_consult: '政策咨询',
  comparison_analysis: '对比分析',
  data_statistics: '数据统计',
  troubleshooting: '故障排查',
  chitchat: '闲聊',
}

function distanceFromBottom(el: HTMLElement) {
  return el.scrollHeight - el.scrollTop - el.clientHeight
}

/** 只认用户触发的滚动。程序设置 scrollTop 也会冒出 scroll 事件，不能据此取消跟随。 */
function onScroll(e: Event) {
  if (!e.isTrusted) return
  const el = scrollBox.value
  if (!el) return
  stickToBottom = distanceFromBottom(el) <= NEAR_BOTTOM_PX
}

function scrollToBottom() {
  const el = scrollBox.value
  if (!el || !stickToBottom) return
  el.scrollTop = el.scrollHeight
}

// 新的一轮提问：回到底部并重新跟随
watch(
  () => sessions.messages.length,
  () => {
    stickToBottom = true
    void nextTick(scrollToBottom)
  },
)
// 流式增量：只有用户还停在底部时才滚
watch(
  () => sessions.messages[sessions.messages.length - 1]?.content,
  () => {
    if (stickToBottom) void nextTick(scrollToBottom)
  },
)
onMounted(scrollToBottom)
</script>

<template>
  <section class="chat-view">
    <header class="chat-header">
      <div class="header-left">
        <h2 class="chat-title">{{ sessions.currentSession?.title ?? '新会话' }}</h2>
        <span v-if="sessions.currentSession" class="session-id">#{{ sessions.currentSession.session_id.slice(0, 8) }}</span>
      </div>
      <div v-if="sessions.lastMeta.intent" class="header-meta">
        <span class="tag" v-if="sessions.lastMeta.route === 'rag_qa'">已检索知识库</span>
        <span class="tag" v-else>未检索 · 闲聊</span>
        <span class="tag">{{ INTENT_LABELS[sessions.lastMeta.intent] ?? sessions.lastMeta.intent }}</span>
      </div>
    </header>

    <div ref="scrollBox" class="message-scroll" @scroll="onScroll">
      <div v-if="sessions.historyLoading" class="empty-state">加载会话历史…</div>
      <template v-else-if="sessions.messages.length === 0">
        <div class="welcome">
          <div class="welcome-logo">R</div>
          <h3>企业知识库智能问答</h3>
          <p>基于向量检索 + CrossEncoder 重排 + 多轮对话记忆。试着问我知识库里的内容，或先到「文件传输 · 知识库」上传文档。</p>
        </div>
      </template>
      <template v-else>
        <MessageBubble v-for="(m, i) in sessions.messages" :key="i" :message="m" :index="i" />
      </template>
    </div>

    <ChatInput
      :disabled="sessions.streaming"
      :streaming="sessions.streaming"
      @send="(q) => sessions.ask(q)"
      @stop="sessions.stopStream()"
    />
  </section>
</template>

<style scoped>
.chat-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.header-left {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.chat-title {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-id {
  font-size: 12px;
  color: var(--text-3);
  font-family: ui-monospace, monospace;
}
.header-meta {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.message-scroll {
  flex: 1;
  overflow-y: auto;
  /* 滚动条槽位恒定预留：滚动条「时有时无」会反复挤占内容宽度，
     消息文本随之换行抖动（hover 弹窗、流式追加都会触发）。 */
  scrollbar-gutter: stable;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.welcome {
  margin: auto;
  max-width: 420px;
  text-align: center;
  color: var(--text-3);
}
.welcome-logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 16px;
  border-radius: 14px;
  background: linear-gradient(135deg, #4f6ef7, #7b5bf2);
  color: #fff;
  font-size: 26px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.welcome h3 {
  color: var(--text-1);
  margin-bottom: 8px;
  font-size: 16px;
}
.welcome p {
  font-size: 13px;
  line-height: 1.7;
}
</style>
