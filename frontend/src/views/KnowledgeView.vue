<script setup lang="ts">
/**
 * RAG 知识库文档管理页（右侧内容区 · knowledge 视图）。
 *
 * ---------------------------------------------------------------------------
 * P0-3b 起这一页的交互模型变了：从「等它做完」改成「看着它做」
 * ---------------------------------------------------------------------------
 * 上传接口现在是**异步**的（202 受理，解析交给后台 Worker），所以：
 *   · 上传完不能立刻说「入库成功，新增 N 个片段」——那个数还不存在；
 *   · 必须按 `status` 展示进度：排队中 → 解析中 → 已完成 / 失败。
 *
 * 轮询逻辑**不在这里**，在 `stores/documents.ts`。理由写在那个文件头上：
 * 本组件由 `App.vue` 用 `v-if` 渲染，切到会话页就会被卸载 ——
 * 轮询若挂在这里，用户一切走就再也收不到「解析完成」的提示了。
 *
 * ---------------------------------------------------------------------------
 * 两个小设计
 * ---------------------------------------------------------------------------
 * · `activeDocId` 存 id、`activeDoc` 用 computed 从列表里取：抽屉打开期间
 *   状态会变（轮询每 2 秒刷新一次），存对象快照的话抽屉会一直显示旧状态；
 * · `chunksStatus` 记下「片段是哪一刻取的」：若之后状态变了，抽屉里给一条
 *   「状态已更新，片段可能已变化 / 重新加载」，而不是让用户对着空列表困惑。
 */
import { computed, onMounted, ref } from 'vue'
import { getDocumentChunks } from '@/api/documents'
import { useDocumentStore } from '@/stores/documents'
import { useUiStore } from '@/stores/ui'
import type { DocStatus, DocumentChunk, KnowledgeDoc } from '@/types'

const docs = useDocumentStore()
const ui = useUiStore()
const fileInput = ref<HTMLInputElement | null>(null)
const dirInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)

// ---------- 片段详情抽屉 ----------
/** 当前打开片段的文档 id（null = 抽屉关闭）。存 id 不存对象，理由见文件头 */
const activeDocId = ref<number | null>(null)
const activeDoc = computed(
  () => docs.documents.find((d) => d.doc_id === activeDocId.value) ?? null,
)
const chunks = ref<DocumentChunk[]>([])
/** 取片段那一刻的文档状态，用来判断抽屉里的内容是新鲜的还是过期的 */
const chunksStatus = ref<DocStatus | null>(null)
const chunksLoading = ref(false)

const chunksStale = computed(
  () => activeDoc.value !== null && chunksStatus.value !== null && chunksStatus.value !== activeDoc.value.status,
)

async function loadChunks() {
  const target = activeDoc.value
  if (!target) return
  chunksLoading.value = true
  try {
    const res = await getDocumentChunks(target.doc_id)
    chunks.value = res.chunks
    chunksStatus.value = res.status
  } catch (e) {
    ui.toast(e instanceof Error ? e.message : '读取片段失败', 'error')
    activeDocId.value = null
    chunksStatus.value = null
  } finally {
    chunksLoading.value = false
  }
}

function openChunks(d: KnowledgeDoc) {
  activeDocId.value = d.doc_id
  chunks.value = []
  chunksStatus.value = null
  void loadChunks()
}

function closeChunks() {
  activeDocId.value = null
  chunksStatus.value = null
}

onMounted(() => void docs.refresh())

// ---------- 状态文案与可用性 ----------
const STATUS_TEXT: Record<DocStatus, string> = {
  pending: '排队中',
  parsing: '解析中',
  success: '已完成',
  fail: '失败',
}
const statusText = (s: DocStatus): string => STATUS_TEXT[s]

/** 有片段才允许打开抽屉。失败/排队中的文档在向量库里没有切片 */
const canShowChunks = (d: KnowledgeDoc): boolean => d.chunk_count > 0

function chunksTitle(d: KnowledgeDoc): string {
  return canShowChunks(d) ? '查看被切成的片段' : '解析完成后才有片段'
}

/**
 * 「重试」只对失败的文档开放。
 * 三种不可用状态各给一句**能解释原因**的提示 —— 按钮置灰而不说为什么，
 * 用户只会以为是页面坏了。
 */
function retryTitle(d: KnowledgeDoc): string {
  if (d.status === 'fail') return '重新排队解析'
  if (d.status === 'success') return '已解析成功，不支持重解析；如需重跑请先删除再重新上传'
  return '正在排队或解析中，无需重试'
}
const canRetry = (d: KnowledgeDoc): boolean => d.status === 'fail'

/** 从文件名推后缀。document 表**不存 file_type**（只有文件名），所以只能推 */
function fileExt(name: string): string {
  const i = name.lastIndexOf('.')
  return i > 0 ? name.slice(i + 1).toLowerCase() : '—'
}

/** 字节数 → 人类可读（1024 进制，与后端「50MB 上限」的口径一致） */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// ---------- 上传（p1.5c 起支持多文件 / 文件夹） ----------
function pickFile() {
  fileInput.value?.click()
}

function pickDir() {
  dirInput.value?.click()
}

/**
 * 文件选择与文件夹选择共用这一个入口：
 * 浏览器选文件夹（webkitdirectory）返回的 FileList 本身就已递归展开
 * （每个 File 带 webkitRelativePath），与多选文件的形态完全一致，
 * 后端逐份独立受理，不需要前端再做目录遍历。
 */
function onFileChosen(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files?.length) void docs.upload(Array.from(input.files))
  input.value = ''
}

function onDrop(e: DragEvent) {
  dragOver.value = false
  // 多文件拖拽直接拿 files 列表；「拖入文件夹」的递归展开（webkitGetAsEntry）
  // 不在本版范围 —— 需要递归文件夹时请用「选择文件夹」按钮
  const files = e.dataTransfer?.files
  if (files?.length) void docs.upload(Array.from(files))
}

function confirmRemove(d: KnowledgeDoc) {
  // 正在解析/排队的文档，删除会中断这次解析 —— 这一点必须说在确认框里，
  // 否则用户点完删除、稍后再传一次，会以为是「系统重复解析了」。
  const extra =
    d.status === 'parsing'
      ? '\n\n注意：该文档正在解析中，删除会中断本次解析。'
      : d.status === 'pending'
        ? '\n\n注意：该文档还在排队，删除后不会再被解析。'
        : ''
  const ok = window.confirm(
    `确认从知识库删除「${d.file_name}」？\n会同时移除磁盘原文件、全部向量片段与元数据记录。${extra}`,
  )
  if (ok) void docs.remove(d)
}

/**
 * 解析链路的告警：只在**真的有事**时显示。
 *
 * 判据来自后端 `/api/v1/system/queue` —— 它同时给了「队列积压」和「worker 存活」。
 * 单独看任何一个都不构成故障（积压 30 条本身没问题，worker 空闲也正常），
 * 但「有文档在排队 + 没有 worker 在应答」就是真故障，而且**用户能自己修**
 * （启动 worker）。没有这条提示，用户看到的只是「一直转圈」。
 */
const chainWarning = computed(() => {
  const c = docs.counts
  const q = docs.queue
  if (!c || !q) return ''
  const busy = c.pending + c.parsing
  if (busy === 0) return ''
  if (!q.worker.ok) {
    return `有 ${busy} 份文档在等待解析，但没有检测到解析进程在运行 —— 请启动它（make worker），否则它们会一直停在「排队中」。`
  }
  if (q.queue.broker_ok === false) {
    return `解析队列不可达（${q.queue.detail || '消息队列未启动'}），排队中的文档不会开始解析。`
  }
  return ''
})

const ACCEPT = '.pdf,.doc,.docx,.txt,.md,.xlsx,.xls,.pptx,.csv,.json,.html,.htm'
</script>

<template>
  <section class="knowledge-view" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="onDrop">
    <header class="page-header">
      <div>
        <h2>知识库管理</h2>
        <p class="page-desc">
          上传企业文档建立知识库，问答时自动检索引用。支持 PDF / Word / Excel / PPT / CSV / HTML / JSON / TXT / Markdown。
          <strong>提交后立即返回，解析在后台进行，可以离开本页。</strong>
        </p>
      </div>
      <button class="btn-primary" @click="ui.switchView('chat')">返回会话</button>
    </header>

    <!-- 统计卡片 -->
    <div class="stats-row">
      <div class="card stat">
        <div class="stat-value">{{ docs.counts?.total ?? docs.documents.length }}</div>
        <div class="stat-label">文档数</div>
      </div>
      <div class="card stat">
        <div class="stat-value">{{ docs.stats?.total_vectors ?? '—' }}</div>
        <div class="stat-label">片段总数</div>
      </div>
      <div class="card stat">
        <div class="stat-value small">{{ docs.stats?.vector_store_type ?? '—' }}</div>
        <div class="stat-label">向量库</div>
      </div>
      <div class="card stat wide">
        <div class="stat-value small">{{ docs.stats?.embedding_model ?? '—' }}</div>
        <div class="stat-label">
          嵌入模型 · {{ docs.stats?.embedding_dimension ?? '—' }} 维 · {{ docs.stats?.embedding_device ?? '—' }}
        </div>
      </div>
    </div>

    <!-- 解析链路状态：状态计数 + 低频自动刷新 + 手动刷新 + 真故障告警 -->
    <div v-if="docs.counts" class="card chain-bar">
      <div class="chain-chips">
        <span class="chip" data-k="pending">排队中 <b>{{ docs.counts.pending }}</b></span>
        <span class="chip" data-k="parsing">解析中 <b>{{ docs.counts.parsing }}</b></span>
        <span class="chip" data-k="success">已完成 <b>{{ docs.counts.success }}</b></span>
        <span class="chip" data-k="fail">失败 <b>{{ docs.counts.fail }}</b></span>
      </div>
      <div class="chain-right">
        <span v-if="docs.polling" class="live"><i />正在自动刷新</span>
        <span v-if="docs.queue" class="worker" :data-ok="docs.queue.worker.ok">
          解析进程：{{ docs.queue.worker.ok ? '在线' : '未检测到' }}
        </span>
        <button class="refresh-btn" :disabled="docs.loading" @click="docs.refresh()">
          {{ docs.loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>
    <p v-if="chainWarning" class="chain-warn">{{ chainWarning }}</p>

    <!-- 上传区 -->
    <div class="card dropzone" :class="{ over: dragOver, busy: docs.submitting }" @click="!docs.submitting && pickFile()">
      <template v-if="docs.submitting">
        <div class="spinner" />
        <p class="drop-title">正在提交：{{ docs.submittingName }}</p>
        <p class="drop-hint">只是把文件交出去，通常不到一秒；解析在后台进行，提交完可以离开本页</p>
      </template>
      <template v-else>
        <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="drop-icon">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="17 8 12 3 7 8" />
          <line x1="12" y1="3" x2="12" y2="15" />
        </svg>
        <p class="drop-title">点击选择文件（可多选），或将文件拖拽到此处</p>
        <p class="drop-hint">
          单份 ≤ 50MB、一次最多 50 份，同名文件重复上传会覆盖旧版本。提交后立即返回，解析由后台进程完成，可以离开本页。
          <button class="dir-pick" @click.stop="pickDir">或选择整个文件夹上传</button>
        </p>
      </template>
      <input ref="fileInput" type="file" hidden multiple :accept="ACCEPT" @change="onFileChosen" />
      <!-- 选文件夹：浏览器会把文件夹递归展开成 File 列表（含子目录） -->
      <input ref="dirInput" type="file" hidden webkitdirectory @change="onFileChosen" />
    </div>

    <!-- 文档列表 -->
    <div class="card doc-table">
      <div class="table-head">
        <span class="col-name">文件名</span>
        <span class="col-type">类型</span>
        <span class="col-status">状态</span>
        <span class="col-chunks">片段数</span>
        <span class="col-size">大小</span>
        <span class="col-op">操作</span>
      </div>
      <div v-if="docs.loading && docs.documents.length === 0" class="empty-state">加载中…</div>
      <div v-else-if="docs.documents.length === 0" class="empty-state">知识库为空，上传第一份文档开始使用</div>
      <template v-for="d in docs.documents" :key="d.doc_id">
        <div class="table-row">
          <span class="col-name" :title="d.file_name">
            <span class="file-dot" :data-type="fileExt(d.file_name)" />{{ d.file_name }}
          </span>
          <span class="col-type tag">{{ fileExt(d.file_name) }}</span>
          <span class="col-status">
            <span class="status" :data-s="d.status"><i class="sd" />{{ statusText(d.status) }}</span>
          </span>
          <span class="col-chunks">{{ d.status === 'success' ? d.chunk_count : '—' }}</span>
          <span class="col-size">{{ formatSize(d.file_size) }}</span>
          <span class="col-op">
            <button class="op-btn" :disabled="!canShowChunks(d)" :title="chunksTitle(d)" @click="openChunks(d)">片段</button>
            <button class="op-btn" title="下载原文件" @click="docs.download(d)">下载</button>
            <button class="op-btn" :disabled="!canRetry(d)" :title="retryTitle(d)" @click="docs.retry(d)">重试</button>
            <button class="op-btn danger" @click="confirmRemove(d)">删除</button>
          </span>
        </div>
        <!-- 失败原因单独一行：它是**给用户看的下一步动作**（换文件重传 / 点重试），
             塞进单元格会被截断成一句没用的话 -->
        <div v-if="d.status === 'fail' && d.fail_reason" class="fail-row">
          <span class="fail-reason" :title="d.fail_reason">{{ d.fail_reason }}</span>
          <span v-if="d.attempt_count > 1" class="fail-attempt">已尝试 {{ d.attempt_count }} 次</span>
        </div>
      </template>
    </div>

    <!-- 片段详情抽屉：展示文档被切成的每个片段的全文（模型实际看到的内容） -->
    <Transition name="drawer">
      <div v-if="activeDoc" class="chunk-drawer-mask" @click.self="closeChunks">
        <aside class="chunk-drawer">
          <header class="drawer-header">
            <div class="drawer-title">
              <h3>{{ activeDoc.file_name }}</h3>
              <p class="drawer-sub">
                <span class="status" :data-s="activeDoc.status"><i class="sd" />{{ statusText(activeDoc.status) }}</span>
                · {{ activeDoc.chunk_count }} 个片段
                <br />
                检索、引用、喂给模型的都是这些片段，不是原文档整体
              </p>
            </div>
            <button class="drawer-close" title="关闭" @click="closeChunks">✕</button>
          </header>
          <div class="drawer-body">
            <!-- 打开抽屉期间状态变了（比如刚好解析完）：提示并给一个重新加载，
                 而不是让用户对着「未找到片段」猜 -->
            <div v-if="chunksStale" class="drawer-stale">
              状态已更新为「{{ statusText(activeDoc.status) }}」，片段可能已变化。
              <button @click="loadChunks()">重新加载</button>
            </div>
            <div v-if="chunksLoading" class="empty-state">读取片段中…</div>
            <div v-else-if="chunks.length === 0" class="empty-state">
              {{ activeDoc.status === 'success' ? '未找到片段' : '该文档还没有片段（解析尚未完成）' }}
            </div>
            <div v-for="c in chunks" :key="c.index" class="chunk-card">
              <div class="chunk-head">
                <span class="chunk-idx">片段 {{ c.index }}</span>
                <span class="chunk-meta">{{ c.char_count }} 字符<span v-if="c.page"> · 第 {{ c.page }} 页</span></span>
              </div>
              <div class="chunk-content">{{ c.content }}</div>
            </div>
          </div>
        </aside>
      </div>
    </Transition>
  </section>
</template>

<style scoped>
.knowledge-view {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px 32px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.page-header h2 {
  font-size: 17px;
}
.page-desc {
  margin-top: 4px;
  font-size: 12.5px;
  color: var(--text-3);
  line-height: 1.7;
  max-width: 760px;
}
.page-desc strong {
  color: var(--text-2);
  font-weight: 600;
}

.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.stat {
  padding: 14px 18px;
}
.stat-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--primary);
}
.stat-value.small {
  font-size: 16px;
  font-weight: 600;
}
.stat-label {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-3);
}

/* ---------- 解析链路状态条 ---------- */
.chain-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  flex-wrap: wrap;
}
.chain-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.chip {
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px 11px;
}
.chip b {
  font-weight: 600;
  color: var(--text-1);
  margin-left: 2px;
}
.chip[data-k='pending'] b {
  color: #b58a2b;
}
.chip[data-k='parsing'] b {
  color: var(--primary);
}
.chip[data-k='success'] b {
  color: var(--success);
}
.chip[data-k='fail'] b {
  color: var(--danger);
}
.chain-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.live {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-3);
}
.live i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--success);
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  50% {
    opacity: 0.25;
  }
}
.worker {
  font-size: 12px;
  color: var(--text-3);
}
.worker[data-ok='false'] {
  color: #b58a2b;
}
.refresh-btn {
  font-size: 12.5px;
  color: var(--text-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 4px 12px;
  transition: all 0.12s;
}
.refresh-btn:hover:not(:disabled) {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-light);
}
.refresh-btn:disabled {
  color: var(--text-3);
  cursor: default;
}
.chain-warn {
  margin-top: -6px;
  font-size: 12.5px;
  line-height: 1.7;
  color: #8a5a00;
  background: #fff8e6;
  border: 1px solid #f2dfae;
  border-radius: var(--radius-sm);
  padding: 9px 13px;
}

/* ---------- 上传区 ---------- */
.dropzone {
  border: 1.5px dashed #c4c9d4;
  border-radius: 12px;
  padding: 30px 20px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  text-align: center;
}
.dropzone:hover,
.dropzone.over {
  border-color: var(--primary);
  background: var(--primary-light);
}
.dropzone.busy {
  cursor: wait;
}
.drop-icon {
  color: var(--text-3);
  margin-bottom: 4px;
}
.drop-title {
  font-size: 14px;
  color: var(--text-2);
}
.drop-hint {
  font-size: 12px;
  color: var(--text-3);
  max-width: 620px;
  line-height: 1.7;
}
/* 「选择文件夹」入口：做成链接式小按钮，不抢主操作（点区域 = 选文件）的视觉权重 */
.dir-pick {
  color: var(--primary);
  font-size: 12px;
  text-decoration: underline;
  text-underline-offset: 3px;
  padding: 0 2px;
}
.dir-pick:hover {
  color: var(--primary-hover);
}
.spinner {
  width: 26px;
  height: 26px;
  border: 3px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-bottom: 6px;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---------- 文档表格 ---------- */
.doc-table {
  overflow: hidden;
}
.table-head,
.table-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 68px 96px 62px 72px 188px;
  align-items: center;
  gap: 8px;
  padding: 10px 18px;
}
.table-head {
  font-size: 12px;
  color: var(--text-3);
  border-bottom: 1px solid var(--border);
  background: var(--bg-sidebar);
}
.table-row {
  font-size: 13.5px;
  border-bottom: 1px solid var(--border);
}
.table-row:last-child {
  border-bottom: none;
}
.table-row:hover {
  background: var(--bg-sidebar);
}
.col-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--primary);
  flex-shrink: 0;
}
.file-dot[data-type='pdf'] {
  background: #e54545;
}
.file-dot[data-type='docx'],
.file-dot[data-type='doc'] {
  background: #2b6de0;
}
.file-dot[data-type='xlsx'],
.file-dot[data-type='xls'],
.file-dot[data-type='csv'] {
  background: #1d9e6e;
}
.file-dot[data-type='pptx'] {
  background: #e8762c;
}
.col-type {
  justify-self: start;
  text-transform: lowercase;
}
.col-chunks,
.col-size {
  color: var(--text-2);
}
.col-op {
  justify-self: end;
  display: flex;
  gap: 2px;
}

/* 状态徽标：颜色只作辅助，文案本身就能读懂（色盲/截图转灰也能用） */
.status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12.5px;
  color: var(--text-2);
  white-space: nowrap;
}
.status .sd {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--text-3);
}
.status[data-s='pending'] .sd {
  background: #d9a441;
}
.status[data-s='parsing'] .sd {
  background: var(--primary);
  animation: pulse 1.2s ease-in-out infinite;
}
.status[data-s='success'] .sd {
  background: var(--success);
}
.status[data-s='fail'] .sd {
  background: var(--danger);
}
.status[data-s='fail'] {
  color: var(--danger);
}

.op-btn {
  font-size: 12.5px;
  color: var(--text-3);
  padding: 3px 9px;
  border-radius: 5px;
  transition: all 0.12s;
  white-space: nowrap;
}
.op-btn:hover:not(:disabled) {
  color: var(--primary);
  background: var(--primary-light);
}
.op-btn.danger:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-light);
}
.op-btn:disabled {
  color: var(--text-3);
  opacity: 0.4;
  cursor: not-allowed;
}

/* 失败原因整行铺开：这一行的作用是告诉用户下一步该干什么 */
.fail-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 0 18px 10px 34px;
  margin-top: -6px;
  font-size: 12.5px;
  border-bottom: 1px solid var(--border);
}
.fail-reason {
  flex: 1;
  min-width: 0;
  color: var(--danger);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fail-attempt {
  color: var(--text-3);
  flex-shrink: 0;
}

/* ---------- 片段详情抽屉 ---------- */
.chunk-drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(31, 35, 41, 0.32);
  z-index: 200;
  display: flex;
  justify-content: flex-end;
}
.chunk-drawer {
  width: min(560px, 88vw);
  height: 100%;
  background: var(--bg-content);
  box-shadow: -6px 0 24px rgba(31, 35, 41, 0.12);
  display: flex;
  flex-direction: column;
}
.drawer-enter-active .chunk-drawer,
.drawer-leave-active .chunk-drawer {
  transition: transform 0.22s ease;
}
.drawer-enter-from .chunk-drawer,
.drawer-leave-to .chunk-drawer {
  transform: translateX(100%);
}
.drawer-enter-active.chunk-drawer-mask,
.drawer-leave-active.chunk-drawer-mask {
  transition: opacity 0.22s ease;
}
.drawer-enter-from.chunk-drawer-mask,
.drawer-leave-to.chunk-drawer-mask {
  opacity: 0;
}
.drawer-header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.drawer-title {
  flex: 1;
  min-width: 0;
}
.drawer-title h3 {
  font-size: 15px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.drawer-sub {
  margin-top: 5px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.8;
}
.drawer-close {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  color: var(--text-3);
  flex-shrink: 0;
}
.drawer-close:hover {
  background: var(--bg-hover);
  color: var(--text-1);
}
.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.drawer-stale {
  font-size: 12.5px;
  line-height: 1.7;
  color: #8a5a00;
  background: #fff8e6;
  border: 1px solid #f2dfae;
  border-radius: var(--radius-sm);
  padding: 9px 13px;
}
.drawer-stale button {
  color: var(--primary);
  text-decoration: underline;
  font-size: 12.5px;
}
.chunk-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  background: var(--bg-app);
}
.chunk-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 6px;
}
.chunk-idx {
  font-size: 13px;
  font-weight: 600;
  color: var(--primary);
}
.chunk-meta {
  font-size: 11.5px;
  color: var(--text-3);
}
.chunk-content {
  font-size: 12.5px;
  line-height: 1.8;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
