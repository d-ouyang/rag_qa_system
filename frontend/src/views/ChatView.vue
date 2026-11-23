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

/**
 * 空状态下的快捷问题：一条点击即提问，省去用户「不知道能问什么」的摸索。
 * 刻意覆盖知识库里的三类载体（PDF 制度 / Word 细则 / Excel 台账）与三种问法
 * （条款分档、表格统计、区间数值），让用户第一眼就建立「这里能问到什么粒度」的预期。
 * 问题文本与知识库真实文档对应，点击后能检索到内容，避免给出查不到答案的示例。
 */
const QUICK_ASKS: { text: string; src: string }[] = [
  { text: '客户投诉分几级？各级处理时限是多久？', src: '客户投诉与售后服务管理办法' },
  { text: '供应商绩效评估的五个维度及权重是什么？', src: '供应商准入与绩效评估细则' },
  { text: '门店巡检评分表中食品安全项有哪些检查内容？', src: '门店运营巡检评分表' },
  { text: '公司数据按敏感程度分为哪几级？', src: '信息安全与数据合规管理制度' },
  { text: '社区店和标准店的面积区间分别是多少？', src: '门店选址与开业筹备手册' },
  { text: '年度培训计划四个季度的总学时分别是多少？', src: '年度培训计划与课程清单' },
]

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
// 流式增量：只有用户还停在底部时才滚（p4.16 起展示层逐帧走 displayContent）
watch(
  () => sessions.messages[sessions.messages.length - 1]?.displayContent
    ?? sessions.messages[sessions.messages.length - 1]?.content,
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
        <span class="tag" v-if="sessions.lastMeta.intent_source === 'qa_set'">QA 直击 · 未调大模型</span>
        <span class="tag" v-else-if="sessions.lastMeta.route === 'rag_qa'">已检索知识库</span>
        <span class="tag" v-else>未检索 · 闲聊</span>
        <span class="tag">{{ INTENT_LABELS[sessions.lastMeta.intent] ?? sessions.lastMeta.intent }}</span>
      </div>
    </header>

    <div ref="scrollBox" class="message-scroll" @scroll="onScroll">
      <div v-if="sessions.historyLoading" class="empty-state">加载会话历史…</div>
      <template v-else-if="sessions.messages.length === 0">
        <div class="welcome">
          <img class="welcome-logo" src="/gq_logo.jpg" alt="锅圈" />
          <h3>企业知识库智能问答</h3>
          <div class="quick-asks">
            <button
              v-for="q in QUICK_ASKS"
              :key="q.text"
              class="quick-ask"
              type="button"
              :disabled="sessions.streaming"
              @click="sessions.ask(q.text)"
            >
              <span class="quick-ask-text">{{ q.text }}</span>
              <span class="quick-ask-src">来自《{{ q.src }}》</span>
            </button>
          </div>
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
  width: 100%;
  max-width: 680px;
  text-align: center;
  color: var(--text-3);
}
.welcome-logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 16px;
  border-radius: 14px;
  object-fit: cover;
  background: #fff;
  display: block;
}
.welcome h3 {
  color: var(--text-1);
  margin-bottom: 22px;
  font-size: 16px;
}
/* 快捷问题：两列卡片，点一下即提问。高度靠内容自适应，
   问题长短不一也不必强制等高，避免为对齐塞入空白。 */
.quick-asks {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  text-align: left;
}
.quick-ask {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-content);
  box-shadow: var(--shadow-card);
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
}
.quick-ask:hover:not(:disabled) {
  border-color: var(--primary);
  box-shadow: 0 2px 10px rgba(79, 110, 247, 0.12);
  transform: translateY(-1px);
}
.quick-ask:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.quick-ask-text {
  font-size: 13px;
  line-height: 1.55;
  color: var(--text-1);
}
.quick-ask-src {
  font-size: 11px;
  line-height: 1.4;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@media (max-width: 720px) {
  .quick-asks {
    grid-template-columns: 1fr;
  }
}
</style>
