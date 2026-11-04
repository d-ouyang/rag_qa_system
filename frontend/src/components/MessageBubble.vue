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
import DocumentPreview from '@/components/DocumentPreview.vue'

const props = defineProps<{ message: ChatMessage; index: number }>()
const sessions = useSessionStore()
const ui = useUiStore()
const showSources = ref(false)
/** 检索详情面板展开态（P2-23；消息带 retrieval_debug 才有得展） */
const showDebug = ref(false)

/** 分数统一 4 位小数展示；null/undefined 显示 —（阈值未启用、轨迹缺失等） */
function fmtScore(v?: number | null): string {
  return typeof v === 'number' ? v.toFixed(4) : '—'
}

/** 额度气泡里的千分位数字（P3-1）。非数字一律 0，不把 NaN 显示给用户 */
function fmtNum(v?: number | null): string {
  return typeof v === 'number' && Number.isFinite(v)
    ? v.toLocaleString('zh-CN')
    : '0'
}

/**
 * 检索详情数据源：**优先用落库的那份**（P4-5 —— 刷新 / 历史回看仍在），
 * 没有再回退到当轮流式的 retrieval 帧（P2-23 —— 只在这一次流式过程中可见）。
 */
const dbg = computed(() => props.message.qa_trace ?? props.message.retrieval_debug)

/**
 * top1 距阈值还差多少（仅 top1 存在、阈值启用、且 top1 未过阈值时给出）。
 * 拒答气泡上这就是「差多少分」的直接答案。
 */
const scoreGap = computed<number | null>(() => {
  const d = dbg.value
  if (!d || typeof d.top1_before_filter !== 'number' || typeof d.threshold !== 'number') return null
  if (d.top1_before_filter >= d.threshold) return null
  return d.threshold - d.top1_before_filter
})

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

/** 弹窗实测约 380px 高（含 token + 耗时明细），下方余量不足这个值就朝上展开（留 20px 呼吸位） */
const POPOVER_EST_HEIGHT = 380
const POPOVER_WIDTH = 264

/**
 * 浮层用 position:fixed + 视口坐标，而不是 absolute。
 * absolute 相对徽章定位时，仍是滚动容器的子孙：朝下展开会抬高
 * scrollHeight → 贴底时整窗被顶一下；overflow:auto 还会把弹窗裁掉。
 * fixed 相对视口，不参与滚动容器的溢出计算。
 */
const popoverStyle = ref<Record<string, string>>({})

function openDetail(e: MouseEvent) {
  // 两个徽章共用一个浮层：只有 tokens、只有耗时、或两者都有，都能弹
  if (!detail.value && !props.message.timings) return
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

// ----- 引用详情（P0-4b / p4.14+）：点引用展开详情 -----
/**
 * 当前展开哪一条：切片用 `c:<chunk_id>`，QA 直击（无切片）用 `q:<index>`。
 * 一次只展开一条：引用条本身很窄，同时展开几条会把对话挤得读不下去。
 */
const openKey = ref<string | null>(null)
const keyOf = (s: SourceItem): string =>
  isValidChunkId(s.chunk_id) ? `c:${s.chunk_id}` : `q:${s.index}`
const isOpen = (s: SourceItem): boolean => openKey.value === keyOf(s)

/**
 * 每条切片的取数状态，按 chunk_id 存 —— 这是一层**缓存**，两个理由：
 *   1. 同一条引用反复展开不该每次都打一次接口；
 *   2. 「取过但失败了」也要记住。否则用户关掉再点开，会看到一次假的
 *      「正在取回」然后又弹同一个错，像是网络在抽风。
 */
const chunkStates = ref<Record<string, { loading: boolean; data?: ChunkDetail; error?: string }>>({})

/** 当前展开的切片状态（QA 条目不取数，恒为 null） */
const opened = computed(() => {
  const k = openKey.value
  if (!k || !k.startsWith('c:')) return null
  return chunkStates.value[k.slice(2)] ?? null
})
const openedData = computed<ChunkDetail | null>(() => opened.value?.data ?? null)

function toggleSource(s: SourceItem): void {
  const key = keyOf(s)
  if (openKey.value === key) {
    openKey.value = null
    return
  }
  openKey.value = key
  const id = s.chunk_id
  if (!isValidChunkId(id) || chunkStates.value[id]) return // QA 条目 / 已取过 → 不请求
  void loadChunk(id)
}

/** 从 source 取 doc_id：优先后端字段，退化到 chunk_id 的 `<doc_id>:` 前缀 */
function docIdOf(s: SourceItem): number | null {
  if (typeof s.doc_id === 'number' && Number.isFinite(s.doc_id)) return s.doc_id
  const cid = s.chunk_id
  if (cid && cid.includes(':')) {
    const head = cid.split(':')[0]
    if (/^\d+$/.test(head)) return Number(head)
  }
  return null
}

/** 打开原文档预览（下载/预览都在浮层里；docx/xlsx 暂回退为下载） */
const previewOpen = ref(false)
const previewDocId = ref<number | null>(null)
const previewName = ref<string | null>(null)

function openPreview(s: SourceItem): void {
  const id = docIdOf(s)
  if (!id) return
  previewDocId.value = id
  previewName.value = displayName(s)
  previewOpen.value = true
}

// ----- 引用资料按「源文件」聚合（纯前端展示层） -----
/**
 * 后端给的 `sources` 是**按切片粒度**、按相关性排好序的（一个文件命中 3 片就有 3 条）。
 * 直接照搬会出现「同一个文件名重复 3 行、每行都挂一个查看原文档」——
 * 用户要的是「一个文件一个入口，文件内再用切片区分」。
 *
 * 所以这里只做展示层聚合：
 *   - 分组键优先 doc_id（同一份文档的切片必然同 doc_id），退化为文件名；
 *   - 组内切片按重排分由高到低（同分保持后端原顺序）；
 *   - 组的位置 = 组内最高分切片在后端序列里的位置 ——
 *     即「1、3、5 同源则整组落在 1 的位置，2、4 依次跟在后面」。
 *
 * ⚠️ 后端 `sources` 顺序、落库内容一概不动：这是纯渲染层重排，
 * 刷新后历史消息走同一套逻辑，结果一致（不落库 display_sources，避免两份真相）。
 */
type SourceGroup = {
  key: string
  fileName: string
  source: string
  docId: number | null
  chunks: SourceItem[]
}

const scoreOf = (s: SourceItem): number =>
  typeof s.rerank_score === 'number' ? s.rerank_score : Number.NEGATIVE_INFINITY

const sourceGroups = computed<SourceGroup[]>(() => {
  const list = props.message.sources ?? []
  const map = new Map<string, SourceGroup>()

  for (const s of list) {
    const docId = docIdOf(s)
    const fileName = displayName(s)
    const key = docId != null ? `d:${docId}` : `f:${fileName || s.source}`
    const g = map.get(key)
    if (g) {
      g.chunks.push(s)
    } else {
      map.set(key, { key, fileName, source: s.source, docId, chunks: [s] })
    }
  }

  const groups = [...map.values()]
  for (const g of groups) {
    // 显式比较而不是相减：无 score 的切片是 -Infinity，相减会得到 NaN，
    // 落进「按原序」分支，反而把有分的排到后面去
    g.chunks.sort((a, b) => {
      const sa = scoreOf(a)
      const sb = scoreOf(b)
      if (sa !== sb) return sb > sa ? 1 : -1
      return a.index - b.index
    })
  }
  // chunks[0] 已是组内最高分那条；用它在后端序列里的位置当组的锚点
  groups.sort((a, b) => a.chunks[0].index - b.chunks[0].index)
  return groups
})

/**
 * 组内每一行只露「哪一片」：资料序号已在行首，文件名在组头，
 * 这里补 section / 切片序号，让同文件的几片能被一眼区分开。
 */
function chunkLabel(s: SourceItem): string {
  if (s.qa_id) return `QA 集 ${s.qa_id}`
  const sec = (s.section || '').trim()
  if (sec) return sec.length > 24 ? `${sec.slice(0, 24)}…` : sec
  const cid = s.chunk_id
  if (cid && cid.includes(':')) {
    const n = Number(cid.split(':')[1])
    if (Number.isFinite(n)) return `第 ${n + 1} 片`
  }
  return '引用片段'
}

/** 耗时展示：<1s 用毫秒（直击常在几十毫秒，按秒显示会变成 0.0s） */
function fmtElapsed(ms?: number): string {
  if (ms == null) return ''
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`
}

/** 耗时明细里的值：缺失显示「—」，避免空白行让人以为坏了 */
function fmtMs(ms?: number): string {
  return ms == null ? '—' : fmtElapsed(ms)
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
    <img v-if="message.role === 'assistant'" class="avatar ai" src="/gq_logo.jpg" alt="锅圈" />
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
          <!-- P3-1 额度拦截气泡：与普通「⚠️ 请求失败」分开。
               这里的失败是**确定的**（重试仍是同一个 429），
               所以给的出路不是「再试一次」，而是「找管理员改额度」。 -->
          <div v-if="message.quota_blocked" class="quota-blocked">
            <span class="quota-blocked__icon">◉</span>
            <div class="quota-blocked__body">
              <div class="quota-blocked__title">
                额度已用完（已用 {{ fmtNum(message.quota_blocked.used) }} /
                限量 {{ fmtNum(message.quota_blocked.quota) }}）
                <span v-if="message.quota_blocked.period" class="quota-blocked__period">
                  {{ message.quota_blocked.period }}
                </span>
              </div>
              <div class="quota-blocked__msg">{{ message.quota_blocked.message }}</div>
            </div>
          </div>
          <!-- 流式等待首帧：意图识别 + LLM 首包需要数秒，先给明确的占位反馈 -->
          <div v-else-if="message.streaming && !message.content" class="thinking">
            正在思考<span class="dots"><i /><i /><i /></span>
          </div>
          <div v-else class="content">{{ message.content }}<span v-if="message.streaming" class="cursor" /></div>

          <!-- 溯源资料：按**源文件**聚合展示（同文档多切片合成一组，见 sourceGroups）。
               组头 = 文件名 + 唯一一个「查看原文档」；组内每行 = 一个切片（资料N + 重排分），
               点行展开切片全文 / QA 标准答案，不再显示简介。 -->
          <template v-if="message.role === 'assistant' && message.sources && message.sources.length > 0">
            <button class="sources-toggle" @click="showSources = !showSources">
              引用资料 {{ message.sources.length }} 条<template v-if="sourceGroups.length < message.sources.length">（{{ sourceGroups.length }} 个文件）</template> {{ showSources ? '▲' : '▼' }}
            </button>
            <div v-if="showSources" class="sources">
              <div v-for="g in sourceGroups" :key="g.key" class="source-group">
                <!-- 组头：一份源文件一行，只在这里给「查看原文档」入口 -->
                <div class="source-group-head">
                  <span class="source-file" :title="g.source">{{ g.fileName }}</span>
                  <span v-if="g.chunks.length > 1" class="source-group-count">{{ g.chunks.length }} 片</span>
                  <span v-else-if="g.chunks[0].qa_id" class="source-group-count">QA 集</span>
                  <button v-if="g.docId != null" class="doc-link" @click="openPreview(g.chunks[0])">
                    查看原文档
                  </button>
                </div>

                <div
                  v-for="s in g.chunks"
                  :key="s.index"
                  class="source-item"
                  :class="{ 'is-open': isOpen(s) }"
                >
                  <div class="source-head">
                    <span class="source-idx">资料{{ s.index }}</span>
                    <!-- 所有引用都可点：切片反查全文；QA 直击没有切片，展开标准答案 + 来源条款 -->
                    <button
                      class="source-name is-link"
                      :title="`${displayName(s)}${s.chunk_id ? ` · ${s.chunk_id}` : ''}`"
                      @click="toggleSource(s)"
                    >
                      {{ chunkLabel(s) }}
                      <span class="source-chev">{{ isOpen(s) ? '▲' : '▼' }}</span>
                    </button>
                    <span v-if="s.rerank_score != null" class="source-score">重排 {{ s.rerank_score.toFixed(3) }}</span>
                  </div>

                  <!-- 展开区：同一块区域展示详情，不再有「简介 + 全文」两处重复 -->
                  <div v-if="isOpen(s)" class="chunk-detail">
                    <!-- 检索切片：按 chunk_id 反查全文 -->
                    <template v-if="isValidChunkId(s.chunk_id)">
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
                    </template>
                    <!-- 无切片键：QA 直击只给来源信息（答案就是气泡正文）；遗留切片只能给摘要 -->
                    <template v-else>
                      <div class="chunk-meta">
                        <span class="chunk-file">{{ displayName(s) }}</span>
                        <span v-if="s.section">{{ s.section }}</span>
                        <span v-if="s.qa_id">QA 集 {{ s.qa_id }}</span>
                        <span v-if="s.match_via">
                          命中：{{ s.match_via === 'exact' ? '精确' : '语义' }}
                          <template v-if="s.match_score != null"> {{ s.match_score.toFixed(2) }}</template>
                        </span>
                        <span v-if="!s.qa_id" class="chunk-warn">无引用键</span>
                      </div>
                      <div v-if="s.qa_id" class="chunk-hint">
                        该答案来自人工策划的 QA 问答集{{ g.docId != null ? '，可查看原文档核对' : '（未关联到知识库文档）' }}。
                      </div>
                      <pre v-else class="chunk-content">{{ s.snippet }}</pre>
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </template>

          <!-- 检索详情（P2-23）：流式 retrieval 调试帧到达才有（admin 默认开启）。
               🔴 拒答气泡（sources 为空）同样可见 —— 本面板的核心价值：
               拒答时能看到「检索到了什么、top1 距阈值差多少分」。 -->
          <template v-if="message.role === 'assistant' && dbg">
            <button class="sources-toggle" @click="showDebug = !showDebug">
              检索详情 {{ showDebug ? '▲' : '▼' }}
            </button>
            <div v-if="showDebug" class="retrieval-debug">
              <div class="debug-row">
                <span class="debug-key">改写后查询</span>
                <span class="debug-val">{{ dbg.rewritten_query || '（无）' }}</span>
              </div>
              <div class="debug-row">
                <span class="debug-key">意图</span>
                <span class="debug-val">{{ dbg.intent }}（{{ dbg.route }}）</span>
              </div>
              <div class="debug-row">
                <span class="debug-key">阈值</span>
                <span class="debug-val">{{ fmtScore(dbg.threshold) }}</span>
              </div>
              <div class="debug-row">
                <span class="debug-key">top1 分数（过滤前）</span>
                <span class="debug-val">
                  {{ fmtScore(dbg.top1_before_filter) }}<template v-if="scoreGap != null">（距阈值还差 {{ scoreGap.toFixed(4) }}）</template>
                </span>
              </div>
              <!-- P3-4 混合召回。bm25_hits === null 表示「混合检索没开」，
                   这一整行不渲染 —— 不渲染比渲染成「BM25 0 条」诚实：
                   后者是「开了但一条没命中」，排查方向完全不同。 -->
              <div v-if="dbg.bm25_hits != null" class="debug-row">
                <span class="debug-key">BM25 关键词召回</span>
                <span class="debug-val">
                  {{ dbg.bm25_hits }} 条<template v-if="dbg.bm25_only">（其中 {{ dbg.bm25_only }} 条为向量未捞到、BM25 独有）</template>
                </span>
              </div>
              <div v-if="dbg.bm25_hits != null" class="debug-row">
                <span class="debug-key">RRF 融合 top1</span>
                <span class="debug-val">{{ dbg.rrf_top || '（无）' }}</span>
              </div>
              <div v-if="dbg.candidates.length" class="debug-candidates">
                <div
                  v-for="(c, i) in dbg.candidates"
                  :key="i"
                  class="debug-cand"
                  :class="{ failed: !c.passed_threshold }"
                >
                  <span class="cand-name" :title="c.chunk_id ?? ''">{{ c.file_name || c.chunk_id || '（无键切片）' }}</span>
                  <span class="cand-score">{{ fmtScore(c.rerank_score) }}</span>
                  <span class="cand-pass">{{ c.passed_threshold ? '✓' : '✗' }}</span>
                </div>
              </div>
              <div class="debug-foot">
                候选 {{ dbg.candidates.length }} 条 ·
                阈值过滤 {{ dbg.filtered_count ?? 0 }} 条 ·
                进入回答 {{ dbg.docs_returned ?? 0 }} 条
              </div>
            </div>
          </template>

          <!-- assistant 每轮详情：tokens + 耗时徽章。
               两个徽章都属于「本轮明细」，hover **任一个** 都弹同一个浮层：
               浮层是 meta-hover 的子节点（不再塞在 Tokens 徽章里），
               所以只有耗时的老消息也能弹；间隙有桥接伪元素，鼠标滑进浮层不中断。 -->
          <div
            v-if="message.role === 'assistant' && !message.streaming && (message.usage || message.elapsed_ms)"
            class="turn-meta"
          >
            <!-- P4-6 低置信徽章：top1 落在犬牙交错区（0.25~0.35），只提示不改答案 -->
            <span
              v-if="message.low_confidence"
              class="meta-badge grounded-badge g-low"
              title="本轮检索最高分处于低置信区，答案可能不完整，建议核对原文"
            >低置信</span>
            <!-- P4-6 依据校验徽章：通过=绿；未通过=红（title 列出未支撑论断）；未校验不显示 -->
            <span
              v-if="message.grounded?.grounded === true"
              class="meta-badge grounded-badge g-ok"
              title="答案的关键论断已通过与检索资料的依据比对"
            >依据校验 ✓</span>
            <span
              v-else-if="message.grounded?.grounded === false"
              class="meta-badge grounded-badge g-bad"
              :title="`以下论断未在检索资料中找到依据：${(message.grounded.unsupported || []).join('；') || '（详见溯源页）'}`"
            >⚠️ 依据未确认</span>
            <div class="meta-hover" @mouseenter="openDetail" @mouseleave="closeDetail">
              <div v-if="message.usage" class="meta-badge token-badge">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" /></svg>
              Tokens: {{ message.usage.input_tokens + message.usage.output_tokens }}
              <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
            </div>
            <span v-if="message.elapsed_ms" class="meta-badge">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
              耗时: {{ fmtElapsed(message.elapsed_ms) }}
            </span>

              <!-- CodeBuddy 式明细浮层：默认朝下；视口下方放不下时朝上（.up） -->
              <div
                v-if="showDetail && (detail || message.timings)"
                class="usage-popover"
                :class="{ up: detailUp }"
                :style="popoverStyle"
              >
                <!-- 只有 usage 的消息：Token 明细；只有耗时的老消息：直接看耗时明细 -->
                <template v-if="detail">
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
                </template>

                <!-- 耗时明细：哪个阶段慢，一眼可见（尤其是生成阶段的 60s 超时重试） -->
                <div v-if="message.timings" class="pop-rate">
                  <div class="rate-head">⏱ 耗时明细</div>
                  <div class="pop-row"><span>检索/改写</span><span class="pop-val">{{ fmtMs(message.timings.prepare_ms) }}</span></div>
                  <div class="pop-row"><span>生成</span><span class="pop-val">{{ fmtMs(message.timings.generate_ms) }}</span></div>
                  <div class="pop-row"><span>依据校验</span><span class="pop-val">{{ fmtMs(message.timings.grounded_ms) }}</span></div>
                  <div class="pop-row"><span>总计</span><span class="pop-val">{{ fmtMs(message.timings.total_ms) }}</span></div>
                  <div
                    v-if="message.timings.llm_timeout_ms && (message.timings.generate_ms ?? 0) >= message.timings.llm_timeout_ms"
                    class="chunk-hint is-error"
                  >
                    生成超过 {{ Math.round((message.timings.llm_timeout_ms || 0) / 1000) }}s，通常是模型请求超时后自动重试
                  </div>
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
    <DocumentPreview
      :open="previewOpen"
      :doc-id="previewDocId"
      :file-name="previewName"
      @close="previewOpen = false"
    />
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
  object-fit: cover;
  background: #fff;
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

/* ----- P3-1 额度拦截气泡 -----
   与顶部横幅（.quota-banner）同色系但更实：横幅是提醒，这里是「这一轮没有回答」，
   视觉上必须能一眼区分「答了」与「没答」。 */
.quota-blocked {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, #f0616d 12%, transparent);
  border: 1px solid color-mix(in srgb, #f0616d 35%, transparent);
}
.quota-blocked__icon {
  font-size: 13px;
  color: #e5737d;
  line-height: 1.75;
}
.quota-blocked__body {
  min-width: 0;
}
.quota-blocked__title {
  color: #e5737d;
  font-weight: 600;
}
.quota-blocked__period {
  margin-left: 6px;
  font-weight: 400;
  opacity: 0.75;
}
.quota-blocked__msg {
  margin-top: 2px;
  color: var(--text-2);
  font-size: 13px;
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
.grounded-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  cursor: help;
}
.g-ok { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
.g-bad { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
.g-low { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
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
/* Tokens / 耗时两枚徽章的公共 hover 宿主：浮层挂在这里，
   hover 域 = 两枚徽章 + 浮层（间隙由 .usage-popover::before 桥接），
   所以只有耗时的老消息也能弹出耗时明细。 */
.meta-hover {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
/* Tokens 徽章曾是浮层锚点，现在浮层已上移，保留相对定位只为不下沉到旧布局 */
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

/* ---------- 检索详情面板（P2-23） ---------- */
.retrieval-debug {
  margin-top: 6px;
  background: var(--bg-content);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
}
.debug-row {
  display: flex;
  gap: 8px;
  padding: 2px 0;
  line-height: 1.6;
}
.debug-key {
  color: var(--text-3);
  flex-shrink: 0;
  min-width: 7em;
}
.debug-val {
  color: var(--text-1);
  word-break: break-word;
}
.debug-candidates {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.debug-cand {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
/* 被阈值过滤掉的候选整行压灰，一眼与「进入回答的」区分开 */
.debug-cand.failed {
  color: var(--text-3);
}
.cand-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.cand-score {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.cand-pass {
  flex-shrink: 0;
  width: 1.2em;
  text-align: center;
}
.debug-foot {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
  color: var(--text-3);
  font-size: 11px;
}
.sources {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 一份源文件 = 一个分组卡片：组头是文件名 + 唯一的「查看原文档」，
   组内每个切片一行。这样同一个文件命中多片时不再重复文件名。 */
.source-group {
  background: var(--bg-content);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
}
.source-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 6px;
  border-bottom: 1px dashed var(--border);
}
.source-file {
  color: var(--text-1);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.source-group-count {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-3);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0 6px;
  line-height: 15px;
}
/* 组头里的入口往右靠；margin-top 归零，垂直居中才算齐 */
.source-group-head .doc-link {
  margin-top: 0;
  margin-left: auto;
  flex-shrink: 0;
  font-size: 11px;
}
.source-item {
  padding: 6px 0 0;
  font-size: 12px;
}
/* 组内切片之间用细分隔线，最后一个不加，免得和卡片边框叠成双线 */
.source-item + .source-item {
  margin-top: 4px;
  border-top: 1px solid var(--border);
  padding-top: 6px;
}
.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
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
.doc-link {
  margin-top: 8px;
  font-size: 12px;
  color: var(--primary);
  padding: 2px 0;
}
.doc-link:hover {
  text-decoration: underline;
}
</style>
