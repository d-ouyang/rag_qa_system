<script setup lang="ts">
/**
 * 单条消息气泡：
 * - user：右侧、无头像；hover 气泡下方出现操作条（复制/编辑）+ 提问时间；
 *         编辑态原地变输入框（「编辑后将从此处重新开始对话」），发送后截断重问。
 * - assistant：左侧；可展开溯源资料；尾部 token/耗时徽章，hover 出明细浮层
 *              （输入 / 缓存命中 / 缓存未命中 / 输出 / 缓存命中率进度条）。
 */
import { computed, ref } from 'vue'
import type { ChatMessage, ChunkDetail, SourceItem } from '@/types'
import { useSessionStore } from '@/stores/sessions'
import { useUiStore } from '@/stores/ui'
import { getChunk, isValidChunkId } from '@/api/chunks'

const props = defineProps<{ message: ChatMessage; index: number }>()
const sessions = useSessionStore()
const ui = useUiStore()
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
    ui.toast('已复制到剪贴板', 'success')
  } catch {
    // 剪贴板 API 在非安全上下文（http 裸 IP 访问）或被拒权限时会抛错，
    // 必须如实告诉用户「没复制上」，而不是只改个按钮 title 了事
    ui.toast('复制失败：浏览器拒绝了剪贴板访问', 'error')
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
/**
 * 弹窗朝向：true = 朝上展开。
 * 徽章贴近视口底部时（典型：最后一条回答）朝下展开会被视口截断，
 * 还会把滚动容器的 scrollHeight 撑大 → 滚动条突然出现 → 整窗内容被顶一下。
 * 所以 hover 时量一次视口余量，下方放不下就朝上。
 */
const detailUp = ref(false)
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

/** 弹窗实测约 260px 高，下方余量不足这个值就朝上展开（留 20px 呼吸位） */
const POPOVER_EST_HEIGHT = 280
const POPOVER_WIDTH = 264

/**
 * 浮层用 position:fixed + 视口坐标，而不是 absolute。
 * absolute 相对徽章定位时，仍是滚动容器的子孙：朝下展开会抬高
 * scrollHeight → 贴底时整窗被顶一下；overflow:auto 还会把弹窗裁掉。
 * fixed 相对视口，不参与滚动容器的溢出计算。
 */
const popoverStyle = ref<Record<string, string>>({})

function openDetail(e: MouseEvent) {
  if (!detail.value) return // 只有耗时、没有 usage 的消息没有明细可弹
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  detailUp.value = window.innerHeight - rect.bottom < POPOVER_EST_HEIGHT
  let left = rect.left
  if (left + POPOVER_WIDTH > window.innerWidth - 12) {
    left = Math.max(12, window.innerWidth - POPOVER_WIDTH - 12)
  }
  popoverStyle.value = detailUp.value
    ? { left: `${left}px`, bottom: `${window.innerHeight - rect.top + 6}px`, top: 'auto' }
    : { left: `${left}px`, top: `${rect.bottom + 6}px`, bottom: 'auto' }
  showDetail.value = true
  // 滚动时徽章走了、浮层还钉在视口，对不齐 —— 关比错位好
  window.addEventListener('scroll', closeDetail, true)
}

function closeDetail() {
  showDetail.value = false
  window.removeEventListener('scroll', closeDetail, true)
}

function displayName(s: SourceItem): string {
  if (s.file_name) return s.file_name
  const parts = s.source.split('/')
  return parts[parts.length - 1] || s.source
}

// ----- 引用反查（P0-4b）：点引用取切片全文 -----
/**
 * 当前展开全文的 chunk_id（null = 都收着）。一次只展开一条：
 * 引用条本身很窄，同时展开几条会把对话挤得读不下去。
 */
const openChunkId = ref<string | null>(null)

/**
 * 每条引用的取数状态，按 chunk_id 存 —— 这是一层**缓存**，两个理由：
 *   1. 同一条引用反复展开不该每次都打一次接口；
 *   2. 「取过但失败了」也要记住。否则用户关掉再点开，会看到一次假的
 *      「正在取回」然后又弹同一个错，像是网络在抽风。
 */
const chunkStates = ref<Record<string, { loading: boolean; data?: ChunkDetail; error?: string }>>({})

/** 当前展开那一条的状态（未展开时为 null），模板里只读这一个就够了 */
const opened = computed(() =>
  openChunkId.value ? (chunkStates.value[openChunkId.value] ?? null) : null,
)
const openedData = computed<ChunkDetail | null>(() => opened.value?.data ?? null)

function toggleChunk(s: SourceItem): void {
  const id = s.chunk_id
  if (!isValidChunkId(id)) return // 遗留切片没有键：按钮压根不该出现，这里是兜底
  if (openChunkId.value === id) {
    openChunkId.value = null
    return
  }
  openChunkId.value = id
  if (chunkStates.value[id]) return // 已取过（成功或失败）→ 直接复用
  void loadChunk(id)
}

async function loadChunk(id: string): Promise<void> {
  chunkStates.value[id] = { loading: true }
  try {
    chunkStates.value[id] = { loading: false, data: await getChunk(id) }
  } catch (e) {
    // 后端给的 detail 就是一句人话（「引用内容已随文档删除」/
    // 「引用片段已失效，请刷新后重试」），前端**照原样展示**，不要重写文案：
    // 重写一次就多一处与后端对不上的风险，而且后端改口径时前端永远慢半拍。
    chunkStates.value[id] = {
      loading: false,
      error: e instanceof Error ? e.message : '取回原文失败',
    }
  }
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
                  <!-- 有 chunk_id 才能点。没有键的（P0-3 之前入库的遗留切片）
                       渲染成纯文本 + tooltip 说明原因，不给一个点了会失败的按钮 -->
                  <button
                    v-if="isValidChunkId(s.chunk_id)"
                    class="source-name is-link"
                    :title="s.source"
                    @click="toggleChunk(s)"
                  >
                    {{ displayName(s) }}
                    <span class="source-chev">{{ openChunkId === s.chunk_id ? '▲' : '▼' }}</span>
                  </button>
                  <span
                    v-else
                    class="source-name"
                    :title="s.source"
                  >{{ displayName(s) }}</span>
                  <span v-if="s.rerank_score != null" class="source-score">重排 {{ s.rerank_score.toFixed(3) }}</span>
                </div>
                <div class="source-snippet">{{ s.snippet }}</div>

                <!-- 展开区：切片全文 + 源文档信息（来自 MySQL，不是从路径猜的） -->
                <div v-if="isValidChunkId(s.chunk_id) && openChunkId === s.chunk_id" class="chunk-detail">
                  <div v-if="opened?.loading" class="chunk-hint">正在取回原文…</div>
                  <div v-else-if="opened?.error" class="chunk-hint is-error">{{ opened.error }}</div>
                  <template v-else-if="openedData">
                    <div class="chunk-meta">
                      <span class="chunk-file">{{ openedData.file_name }}</span>
                      <span v-if="openedData.page != null">第 {{ openedData.page + 1 }} 页</span>
                      <span>第 {{ openedData.chunk_index + 1 }} 片</span>
                      <span>{{ openedData.char_count }} 字</span>
                      <span v-if="openedData.upload_time">{{ openedData.upload_time }}</span>
                      <!-- 正文还在、MySQL 记录没了：如实标出来，这是「该跑重建脚本了」的信号 -->
                      <span v-if="!openedData.document_exists" class="chunk-warn">文档记录缺失</span>
                    </div>
                    <pre class="chunk-content">{{ openedData.content }}</pre>
                  </template>
                </div>
              </div>
            </div>
          </template>

          <!-- assistant 每轮详情：tokens + 耗时徽章。
               明细浮层只在 hover「Tokens 徽章」时出现（不是整行都触发）：
               弹窗放在徽章内部，hover 域 = 徽章 + 弹窗（间隙有桥接伪元素），
               鼠标从徽章滑进弹窗不会中断；「耗时」徽章只是展示，不弹明细。 -->
          <div
            v-if="message.role === 'assistant' && !message.streaming && (message.usage || message.elapsed_ms)"
            class="turn-meta"
          >
            <div
              v-if="message.usage"
              class="meta-badge token-badge"
              @mouseenter="openDetail"
              @mouseleave="closeDetail"
            >
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" /></svg>
              Tokens: {{ message.usage.input_tokens + message.usage.output_tokens }}
              <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>

              <!-- CodeBuddy 式明细浮层：默认朝下；视口下方放不下时朝上（.up） -->
              <div
                v-if="showDetail && detail"
                class="usage-popover"
                :class="{ up: detailUp }"
                :style="popoverStyle"
              >
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
            <span v-if="message.elapsed_ms" class="meta-badge">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
              耗时: {{ (message.elapsed_ms / 1000).toFixed(1) }}s
            </span>
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
/* Tokens 徽章是明细浮层的定位锚点与 hover 宿主 */
.token-badge {
  position: relative;
}
.usage-popover {
  /* 视口坐标由 JS 写入（top/left 或 bottom/left），不走文档流 */
  position: fixed;
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
  text-align: left;
  font-weight: 400;
}
/* 桥接徽章与弹窗之间的 6px 间隙：没有它，鼠标穿过间隙的那一下
   会触发徽章的 mouseleave，弹窗一闪就关，永远滑不进弹窗 */
.usage-popover::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  top: -8px;
  height: 8px;
}
.usage-popover.up::before {
  top: auto;
  bottom: -8px;
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
/* 可点的那条：与不可点的纯文本拉开区别（蓝字 + 手型），否则用户看不出能点 */
.source-name.is-link {
  color: var(--primary);
  background: none;
  border: none;
  padding: 0;
  font-size: inherit;
  font-family: inherit;
  cursor: pointer;
  min-width: 0; /* flex 子项允许收缩，不然长文件名会把重排分挤出去 */
}
.source-name.is-link:hover {
  text-decoration: underline;
}
.source-chev {
  font-size: 9px;
  margin-left: 3px;
  color: var(--text-3);
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

/* ---------- 引用反查：展开的切片全文 ---------- */
.chunk-detail {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
}
.chunk-hint {
  color: var(--text-3);
  font-size: 11px;
  padding: 2px 0;
}
/* 后端给的四种结论里，两种是失败：照原样展示，前端不重写文案 */
.chunk-hint.is-error {
  color: var(--danger);
}
.chunk-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  color: var(--text-3);
  font-size: 11px;
  margin-bottom: 4px;
}
.chunk-file {
  color: var(--text-2);
  font-weight: 500;
}
.chunk-warn {
  color: var(--danger);
}
/* 切片全文：保留原文的换行与空格（PDF 抽出来的文本里空行是有意义的），
   但允许长行换行，不然一个表格行能把气泡撑到屏幕外 */
.chunk-content {
  margin: 0;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-2);
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
</style>
