<script setup lang="ts">
/**
 * 单条消息气泡：
 * - user：右侧、无头像；hover 气泡下方出现操作条（复制/编辑）+ 提问时间；
 *         编辑态原地变输入框（「编辑后将从此处重新开始对话」），发送后截断重问。
 * - assistant：左侧；可展开溯源资料；尾部 token/耗时徽章，hover 出明细浮层
 *              （输入 / 缓存命中 / 缓存未命中 / 输出 / 缓存命中率进度条）。
 */
import { computed, ref } from 'vue'
import type { ChatMessage } from '@/types'
import { useSessionStore } from '@/stores/sessions'

const props = defineProps<{ message: ChatMessage; index: number }>()
const sessions = useSessionStore()
const showSources = ref(false)

/** v-focus：编辑输入框插入即聚焦 */
const vFocus = { mounted: (el: HTMLElement) => el.focus() }

// ----- 用户消息操作条 -----
const showActions = ref(false)
const editing = ref(false)
const editText = ref('')

function startEdit() {
  editText.value = props.message.content
  editing.value = true
  showActions.value = false
}

function cancelEdit() {
  editing.value = false
}

async function sendEdit() {
  const text = editText.value.trim()
  if (!text) return
  editing.value = false
  // 从本条消息处重新开始对话：后端截断 + 本地截断 + 重问（store 内部处理）
  await sessions.ask(text, { editFromIndex: props.index })
}

async function copyText() {
  try {
    await navigator.clipboard.writeText(props.message.content)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    console.error('复制失败')
  }
}
const copied = ref(false)

function fmtTs(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// ----- assistant token 明细浮层 -----
const showDetail = ref(false)
const detail = computed(() => {
  const u = props.message.usage
  if (!u) return null
  const input = u.input_tokens ?? 0
  const output = u.output_tokens ?? 0
  const hit = u.cache_read_tokens ?? 0
  const miss = Math.max(0, input - hit)
  return {
    total: input + output,
    input,
    output,
    hit,
    miss,
    rate: input > 0 ? (hit / input) * 100 : 0,
    hitPct: input > 0 ? Math.min(100, (hit / input) * 100) : 0,
  }
})

function fileName(source: string): string {
  const parts = source.split('/')
  return parts[parts.length - 1] || source
}
</script>

<template>
  <div class="bubble-row" :class="message.role">
    <div v-if="message.role === 'assistant'" class="avatar ai">R</div>
    <div
      class="bubble-col"
      :class="message.role"
      @mouseenter="message.role === 'user' && (showActions = true)"
      @mouseleave="message.role === 'user' && (showActions = false)"
    >
      <div class="bubble" :class="message.role">
        <!-- 用户消息编辑态 -->
        <template v-if="editing">
          <textarea
            v-focus
            v-model="editText"
            class="edit-area"
            rows="3"
            @keydown.enter.exact.prevent="sendEdit"
            @keydown.esc="cancelEdit"
          />
          <div class="edit-hint">
            <span class="hint-text">ⓘ 编辑后将从此处重新开始对话，已有产物不会被删除</span>
            <div class="edit-btns">
              <button class="btn ghost" @click="cancelEdit">取消</button>
              <button class="btn primary" @click="sendEdit">发送</button>
            </div>
          </div>
        </template>
        <template v-else>
          <!-- 流式等待首帧：意图识别 + LLM 首包需要数秒，先给明确的占位反馈 -->
          <div v-if="message.streaming && !message.content" class="thinking">
            正在思考<span class="dots"><i /><i /><i /></span>
          </div>
          <div v-else class="content">{{ message.content }}<span v-if="message.streaming" class="cursor" /></div>

          <!-- 溯源资料：可折叠 -->
          <template v-if="message.role === 'assistant' && message.sources && message.sources.length > 0">
            <button class="sources-toggle" @click="showSources = !showSources">
              引用资料 {{ message.sources.length }} 条 {{ showSources ? '▲' : '▼' }}
            </button>
            <div v-if="showSources" class="sources">
              <div v-for="s in message.sources" :key="s.index" class="source-item">
                <div class="source-head">
                  <span class="source-idx">资料{{ s.index }}</span>
                  <span class="source-name" :title="s.source">{{ fileName(s.source) }}</span>
                  <span v-if="s.rerank_score != null" class="source-score">重排 {{ s.rerank_score.toFixed(3) }}</span>
                </div>
                <div class="source-snippet">{{ s.snippet }}</div>
              </div>
            </div>
          </template>

          <!-- assistant 每轮详情：tokens + 耗时徽章，hover 明细浮层 -->
          <div
            v-if="message.role === 'assistant' && !message.streaming && (message.usage || message.elapsed_ms)"
            class="turn-meta"
            @mouseenter="showDetail = true"
            @mouseleave="showDetail = false"
          >
            <span class="meta-badge" @click="showDetail = !showDetail">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" /></svg>
              Tokens: {{ (message.usage ? message.usage.input_tokens + message.usage.output_tokens : 0) }}
              <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
            </span>
            <span v-if="message.elapsed_ms" class="meta-badge">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
              耗时: {{ (message.elapsed_ms / 1000).toFixed(1) }}s
            </span>

            <!-- CodeBuddy 式明细浮层 -->
            <div v-if="showDetail && detail" class="usage-popover">
              <div class="pop-head">
                <span>Token 消耗明细</span>
                <span class="pop-total">总计 {{ detail.total.toLocaleString() }}</span>
              </div>
              <div class="pop-row">
                <span class="dot" style="background: #4c8dff" /> 输入
                <span class="pop-val">{{ detail.input.toLocaleString() }}</span>
              </div>
              <div class="pop-row sub">
                <span class="dot" style="background: #34c77b" /> 缓存命中
                <span class="pop-val">{{ detail.hit.toLocaleString() }}</span>
              </div>
              <div class="pop-row sub">
                <span class="dot" style="background: #f0655a" /> 缓存未命中
                <span class="pop-val">{{ detail.miss.toLocaleString() }}</span>
              </div>
              <div class="pop-row">
                <span class="dot" style="background: #9d6bf0" /> 输出
                <span class="pop-val">{{ detail.output.toLocaleString() }}</span>
              </div>
              <div class="pop-rate">
                <div class="rate-head">⚡ 缓存命中率 <b>{{ detail.rate.toFixed(1) }}%</b></div>
                <div class="rate-bar">
                  <i class="seg hit" :style="{ width: detail.hitPct + '%' }" />
                  <i class="seg miss" :style="{ width: 100 - detail.hitPct + '%' }" />
                </div>
                <div class="rate-legend">
                  <span><i class="dot" style="background: #34c77b" />命中</span>
                  <span><i class="dot" style="background: #f0655a" />未命中</span>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 用户消息 hover 操作条（气泡下方右侧：时间 + 复制 + 编辑） -->
      <div
        v-if="message.role === 'user' && !editing"
        class="user-actions"
        :class="{ visible: showActions }"
      >
        <span v-if="message.ts" class="action-time">{{ fmtTs(message.ts) }}</span>
        <button class="action-btn" :title="copied ? '已复制' : '复制'" @click="copyText" @mouseenter="showActions = true">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
        </button>
        <button class="action-btn" title="编辑" @mouseenter="showActions = true" @click="startEdit">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" /></svg>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.bubble-row.user {
  flex-direction: row-reverse;
}
.avatar {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
}
.avatar.ai {
  background: linear-gradient(135deg, #4f6ef7, #7b5bf2);
}
/* 用户消息不再带「我」头像：气泡列占满剩余宽度，气泡自身右对齐 */
.bubble-col {
  min-width: 0;
  max-width: min(72%, 760px);
  display: flex;
  flex-direction: column;
}
.bubble-col.user {
  align-items: flex-end;
}
.bubble {
  border-radius: 12px;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.75;
}
.bubble.user {
  background: var(--primary);
  color: #fff;
  border-top-right-radius: 4px;
}
.bubble.assistant {
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-top-left-radius: 4px;
  align-self: flex-start;
}
.content {
  white-space: pre-wrap;
  word-break: break-word;
}
/* ----- 用户编辑态 ----- */
.edit-area {
  width: 100%;
  font: inherit;
  line-height: 1.7;
  color: var(--text-1);
  background: var(--bg-content, #fff);
  border: 1px solid var(--primary);
  border-radius: 8px;
  padding: 8px 10px;
  resize: vertical;
  outline: none;
}
.edit-hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
}
.hint-text {
  font-size: 12px;
  color: var(--text-3);
}
.edit-btns {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.btn {
  padding: 4px 14px;
  border-radius: 6px;
  font-size: 13px;
}
.btn.ghost {
  color: var(--text-2);
  background: var(--bg-hover);
}
.btn.primary {
  color: #fff;
  background: var(--primary);
}
.btn.primary:hover {
  background: var(--primary-hover);
}
/* ----- 用户 hover 操作条 ----- */
.user-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  padding-right: 2px;
  opacity: 0;
  transition: opacity 0.15s;
  height: 20px;
}
.user-actions.visible {
  opacity: 1;
}
.action-time {
  font-size: 11px;
  color: var(--text-3);
  margin-right: 2px;
}
.action-btn {
  width: 22px;
  height: 22px;
  border-radius: 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
}
.action-btn:hover {
  background: var(--bg-hover);
  color: var(--text-1);
}
/* ----- assistant 每轮 meta ----- */
.turn-meta {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}
.meta-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-3);
  border: 1px solid var(--border);
  background: var(--bg-content);
  border-radius: 20px;
  padding: 1px 8px;
  cursor: default;
}
.usage-popover {
  position: absolute;
  left: 0;
  top: calc(100% + 6px);
  z-index: 40;
  width: 264px;
  background: var(--bg-content, #fff);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.14);
  padding: 12px 14px;
  font-size: 12.5px;
  color: var(--text-1);
  cursor: default;
}
.pop-head {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  font-size: 13px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}
.pop-total {
  color: var(--text-2);
  font-weight: 500;
}
.pop-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2.5px 0;
}
.pop-row.sub {
  padding-left: 14px;
  color: var(--text-2);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 2.5px;
  flex-shrink: 0;
  display: inline-block;
}
.pop-val {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}
.pop-rate {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.rate-head {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 6px;
}
.rate-head b {
  color: #34c77b;
}
.rate-bar {
  display: flex;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--bg-hover);
}
.seg.hit {
  background: #34c77b;
}
.seg.miss {
  background: #f0655a;
}
.rate-legend {
  display: flex;
  gap: 14px;
  margin-top: 6px;
  color: var(--text-3);
  font-size: 11.5px;
}
.rate-legend span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.thinking {
  color: var(--text-3);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 2px;
}
.dots {
  display: inline-flex;
  gap: 3px;
  margin-left: 3px;
}
.dots i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--text-3);
  animation: dot-bounce 1.2s ease-in-out infinite;
}
.dots i:nth-child(2) {
  animation-delay: 0.2s;
}
.dots i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes dot-bounce {
  0%, 60%, 100% {
    opacity: 0.3;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}
.cursor {
  display: inline-block;
  width: 7px;
  height: 15px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--primary);
  animation: blink 0.9s step-end infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.sources-toggle {
  margin-top: 8px;
  font-size: 12px;
  color: var(--primary);
}
.sources {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.source-item {
  background: var(--bg-content);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
}
.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.source-idx {
  color: var(--primary);
  font-weight: 600;
}
.source-name {
  color: var(--text-2);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-score {
  margin-left: auto;
  color: var(--text-3);
  flex-shrink: 0;
}
.source-snippet {
  color: var(--text-3);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
