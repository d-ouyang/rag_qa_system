<script setup lang="ts">
/**
 * RAG 知识库文档管理页（右侧内容区 · knowledge 视图）。
 * 由左侧「文件传输 · 知识库」入口进入：
 * - 统计卡片：文档数 / 片段总数 / 向量库类型 / 嵌入模型
 * - 上传区：点击或拖拽上传，解析切分后向量化入库
 * - 文档列表：按来源分组展示片段数，支持删除
 */
import { onMounted, ref } from 'vue'
import { getDocumentChunks } from '@/api/documents'
import { useDocumentStore } from '@/stores/documents'
import { useUiStore } from '@/stores/ui'
import type { DocumentChunk, KnowledgeDoc } from '@/types'

const docs = useDocumentStore()
const ui = useUiStore()
const fileInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)

// ---------- 片段详情抽屉 ----------
/** 当前打开片段详情的文档（null = 抽屉关闭） */
const activeDoc = ref<KnowledgeDoc | null>(null)
const chunks = ref<DocumentChunk[]>([])
const chunksLoading = ref(false)

async function openChunks(d: KnowledgeDoc) {
  activeDoc.value = d
  chunks.value = []
  chunksLoading.value = true
  try {
    const res = await getDocumentChunks(d.source)
    chunks.value = res.chunks
  } catch (e) {
    ui.toast(e instanceof Error ? e.message : '读取片段失败', 'error')
    activeDoc.value = null
  } finally {
    chunksLoading.value = false
  }
}

function closeChunks() {
  activeDoc.value = null
}

onMounted(() => void docs.refresh())

function pickFile() {
  fileInput.value?.click()
}

function onFileChosen(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files?.[0]) void docs.upload(input.files[0])
  input.value = ''
}

function onDrop(e: DragEvent) {
  dragOver.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) void docs.upload(file)
}

function confirmRemove(source: string, name: string) {
  if (window.confirm(`确认从知识库删除「${name}」？该操作会移除其全部向量片段。`)) {
    void docs.remove(source)
  }
}

const ACCEPT = '.pdf,.doc,.docx,.txt,.md,.xlsx,.xls,.pptx,.csv,.json,.html,.htm'
</script>

<template>
  <section class="knowledge-view" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="onDrop">
    <header class="page-header">
      <div>
        <h2>知识库管理</h2>
        <p class="page-desc">上传企业文档建立知识库，问答时自动检索引用。支持 PDF / Word / Excel / PPT / CSV / HTML / JSON / TXT / Markdown。</p>
      </div>
      <button class="btn-primary" @click="ui.switchView('chat')">返回会话</button>
    </header>

    <!-- 统计卡片 -->
    <div class="stats-row">
      <div class="card stat">
        <div class="stat-value">{{ docs.documents.length }}</div>
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
        <div class="stat-label">嵌入模型 · {{ docs.stats?.embedding_dimension ?? '—' }} 维 · {{ docs.stats?.embedding_device ?? '—' }}</div>
      </div>
    </div>

    <!-- 上传区 -->
    <div
      class="card dropzone"
      :class="{ over: dragOver, busy: docs.uploading }"
      @click="!docs.uploading && pickFile()"
    >
      <template v-if="docs.uploading">
        <div class="spinner" />
        <p class="drop-title">正在解析入库：{{ docs.uploadingName }}</p>
        <p class="drop-hint">文档解析、切分与向量化可能需要数十秒，请勿关闭页面</p>
      </template>
      <template v-else>
        <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="drop-icon">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="17 8 12 3 7 8" />
          <line x1="12" y1="3" x2="12" y2="15" />
        </svg>
        <p class="drop-title">点击选择文件，或将文件拖拽到此处</p>
        <p class="drop-hint">单文件 ≤ 50MB，同名文件重复上传会覆盖旧版本</p>
      </template>
      <input ref="fileInput" type="file" hidden :accept="ACCEPT" @change="onFileChosen" />
    </div>

    <!-- 文档列表 -->
    <div class="card doc-table">
      <div class="table-head">
        <span class="col-name">文件名</span>
        <span class="col-type">类型</span>
        <span class="col-chunks">片段数</span>
        <span class="col-op">操作</span>
      </div>
      <div v-if="docs.loading" class="empty-state">加载中…</div>
      <div v-else-if="docs.documents.length === 0" class="empty-state">
        知识库为空，上传第一份文档开始使用
      </div>
      <div v-for="d in docs.documents" :key="d.source" class="table-row">
        <span class="col-name" :title="d.source">
          <span class="file-dot" :data-type="d.file_type" />{{ d.file_name }}
        </span>
        <span class="col-type tag">{{ d.file_type }}</span>
        <span class="col-chunks">{{ d.chunk_count }}</span>
        <span class="col-op">
          <button class="chunks-btn" @click="openChunks(d)">片段</button>
          <button class="del-btn" @click="confirmRemove(d.source, d.file_name)">删除</button>
        </span>
      </div>
    </div>

    <!-- 片段详情抽屉：展示文档被切成的每个片段的全文（模型实际看到的内容） -->
    <Transition name="drawer">
      <div v-if="activeDoc" class="chunk-drawer-mask" @click.self="closeChunks">
        <aside class="chunk-drawer">
          <header class="drawer-header">
            <div class="drawer-title">
              <h3>{{ activeDoc.file_name }}</h3>
              <p class="drawer-sub">
                {{ activeDoc.chunk_count }} 个片段 · 检索、引用、喂给模型的都是这些片段，不是原文档整体
              </p>
            </div>
            <button class="drawer-close" title="关闭" @click="closeChunks">✕</button>
          </header>
          <div class="drawer-body">
            <div v-if="chunksLoading" class="empty-state">读取片段中…</div>
            <div v-else-if="chunks.length === 0" class="empty-state">未找到片段</div>
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

.dropzone {
  border: 1.5px dashed #c4c9d4;
  border-radius: 12px;
  padding: 34px 20px;
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

.doc-table {
  overflow: hidden;
}
.table-head,
.table-row {
  display: grid;
  grid-template-columns: 1fr 90px 90px 80px;
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
}
.col-chunks {
  color: var(--text-2);
}
.col-op {
  justify-self: end;
  display: flex;
  gap: 4px;
}
.chunks-btn {
  font-size: 12.5px;
  color: var(--text-3);
  padding: 3px 10px;
  border-radius: 5px;
  transition: all 0.12s;
}
.chunks-btn:hover {
  color: var(--primary);
  background: var(--primary-light);
}
.del-btn {
  font-size: 12.5px;
  color: var(--text-3);
  padding: 3px 10px;
  border-radius: 5px;
  transition: all 0.12s;
}
.del-btn:hover {
  color: var(--danger);
  background: var(--danger-light);
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
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.6;
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
