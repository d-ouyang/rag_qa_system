<script setup lang="ts">
/**
 * 原文档预览浮层。
 *
 * 支持：
 *   · md/markdown：marked 渲染（>20k 字符走 Worker）+ DOMPurify 消毒；
 *   · txt/json/csv/log/yml/yaml/xml/html：纯文本直读（html 也按文本，不执行）；
 *   · pdf：浏览器内嵌查看；
 *   · png/jpg/jpeg/webp/gif/svg：图片内嵌；
 *   · docx/xlsx/pptx 等：暂不支持在线预览，保留下载入口（后续可接 Worker 解析）。
 *
 * 取文件走 `downloadDocument`（带登录凭据），下载按钮复用同一份 blob。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { downloadDocument } from '@/api/documents'
import { renderMarkdownSmart } from '@/lib/md'
import { docxToHtml } from '@/lib/docx'
import { xlsxToSheets, type XlsxSheet } from '@/lib/xlsx'

const props = defineProps<{
  open: boolean
  docId: number | null
  fileName?: string | null
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const loading = ref(false)
const error = ref('')
const text = ref('')
const html = ref('')
const pdfUrl = ref('')
const imageUrl = ref('')
const resolvedName = ref('')
const blob = ref<Blob | null>(null)
const xlsxSheets = ref<XlsxSheet[]>([])
const activeSheet = ref(0)

let objectUrl = ''
let listening = false

const ext = computed(() => {
  const name = (resolvedName.value || props.fileName || '').toLowerCase()
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot) : ''
})
const isPdf = computed(() => ext.value === '.pdf')
const isImage = computed(() =>
  ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg'].includes(ext.value),
)
const isMarkdown = computed(() => ['.md', '.markdown'].includes(ext.value))
const isDocx = computed(() => ext.value === '.docx')
const isXlsx = computed(() => ['.xlsx', '.xls'].includes(ext.value))
const IMAGE_MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
}
const isText = computed(() =>
  [
    '.md', '.markdown', '.txt', '.json', '.csv', '.log',
    '.yml', '.yaml', '.xml', '.html', '.htm',
  ].includes(ext.value),
)
const currentSheet = computed(() => xlsxSheets.value[activeSheet.value] ?? null)
const currentRows = computed(() => currentSheet.value?.rows ?? [])
const currentHeaders = computed(() => currentRows.value[0] ?? [])
const currentBodyRows = computed(() => currentRows.value.slice(1))

const kindLabel = computed(() => {
  if (isPdf.value) return 'PDF'
  if (isImage.value) return '图片'
  if (isMarkdown.value) return '文档'
  if (isText.value) return '文本'
  if (['.docx', '.doc'].includes(ext.value)) return '文档'
  if (['.xlsx', '.xls'].includes(ext.value)) return '表格'
  if (['.pptx', '.ppt'].includes(ext.value)) return '演示文稿'
  return '文件'
})

function reset() {
  loading.value = false
  error.value = ''
  text.value = ''
  html.value = ''
  pdfUrl.value = ''
  imageUrl.value = ''
  resolvedName.value = ''
  blob.value = null
  xlsxSheets.value = []
  activeSheet.value = 0
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
}

function close() {
  emit('close')
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}
function bindKey() {
  if (listening) return
  document.addEventListener('keydown', onKey)
  listening = true
}
function unbindKey() {
  if (!listening) return
  document.removeEventListener('keydown', onKey)
  listening = false
}

async function load() {
  if (!props.open || props.docId == null) return
  reset()
  loading.value = true
  try {
    const file = await downloadDocument(props.docId)
    blob.value = file.blob
    resolvedName.value = props.fileName || file.filename || `文档 ${props.docId}`
    if (isPdf.value || isImage.value) {
      // 下载接口返回的是 application/octet-stream：直接拿它做 object URL，
      // 浏览器会当成「文件」触发下载，iframe 预览就是空白。这里按扩展名
      // 重新包一层带正确 MIME 的 Blob，PDF 才能在内嵌查看器里渲染。
      const mime = isPdf.value
        ? 'application/pdf'
        : (IMAGE_MIME[ext.value] || file.blob.type || 'application/octet-stream')
      const typed = new Blob([file.blob], { type: mime })
      objectUrl = URL.createObjectURL(typed)
      if (isPdf.value) pdfUrl.value = objectUrl
      else imageUrl.value = objectUrl
    } else if (isDocx.value) {
      html.value = await docxToHtml(await file.blob.arrayBuffer())
    } else if (isXlsx.value) {
      xlsxSheets.value = await xlsxToSheets(await file.blob.arrayBuffer())
      activeSheet.value = 0
    } else if (isText.value) {
      const raw = await file.blob.text()
      if (isMarkdown.value) html.value = await renderMarkdownSmart(raw)
      else text.value = raw
    }
  } catch (e) {
    // 后端 detail 已是人话（无权限/开关关闭/文件丢失），照原样展示
    error.value = e instanceof Error ? e.message : '打不开这份文件'
  } finally {
    loading.value = false
  }
}

function download() {
  if (!blob.value) return
  const url = URL.createObjectURL(blob.value)
  const a = document.createElement('a')
  a.href = url
  a.download = resolvedName.value || `doc-${props.docId}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

watch(
  () => [props.open, props.docId] as const,
  ([open]) => {
    if (open) {
      bindKey()
      void load()
    } else {
      unbindKey()
      reset()
    }
  },
)

onBeforeUnmount(() => {
  unbindKey()
  reset()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="doc-preview-backdrop" @click="close">
      <div
        class="doc-preview"
        role="dialog"
        :aria-label="`预览 ${resolvedName || fileName || '原文档'}`"
        @click.stop
      >
        <header class="doc-preview-head">
          <div class="doc-preview-title">
            <span class="doc-preview-kind">{{ kindLabel }}</span>
            <strong :title="resolvedName || fileName || ''">{{ resolvedName || fileName || '原文档' }}</strong>
          </div>
          <div class="doc-preview-ops">
            <button type="button" class="doc-preview-btn" :disabled="!blob" @click="download">下载</button>
            <button type="button" class="doc-preview-btn" @click="close">关闭</button>
          </div>
        </header>
        <div class="doc-preview-body">
          <div v-if="loading" class="doc-preview-hint">正在打开…</div>
          <div v-else-if="error" class="doc-preview-hint is-error">{{ error }}</div>
          <template v-else>
            <object
              v-if="pdfUrl"
              :data="pdfUrl"
              type="application/pdf"
              class="doc-preview-pdf"
            >
              <!-- object 不支持时的兜底；两者都不支持时仍可右上角下载 -->
              <iframe :src="pdfUrl" :title="resolvedName" class="doc-preview-pdf" />
            </object>
            <img v-else-if="imageUrl" :src="imageUrl" :alt="resolvedName" class="doc-preview-img" />
            <div v-else-if="xlsxSheets.length" class="doc-preview-xlsx">
              <div v-if="xlsxSheets.length > 1" class="sheet-tabs">
                <button
                  v-for="(sheet, i) in xlsxSheets"
                  :key="sheet.name"
                  type="button"
                  class="sheet-tab"
                  :class="{ active: i === activeSheet }"
                  @click="activeSheet = i"
                >{{ sheet.name }}</button>
              </div>
              <div class="sheet-scroll">
                <table v-if="currentRows.length" class="sheet-table">
                  <thead>
                    <tr>
                      <th v-for="(cell, ci) in currentHeaders" :key="ci">{{ cell }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, ri) in currentBodyRows" :key="ri">
                      <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
                    </tr>
                  </tbody>
                </table>
                <div v-else class="doc-preview-hint">这个工作表是空的。</div>
                <div v-if="currentSheet?.truncated" class="doc-preview-hint">
                  仅展示前 1000 行 / 50 列，完整内容请下载。
                </div>
              </div>
            </div>
            <div v-else-if="html" class="doc-preview-md" v-html="html" />
            <pre v-else-if="text" class="doc-preview-text">{{ text }}</pre>
            <div v-else class="doc-preview-hint">
              该格式暂不支持在线预览，请点击右上角「下载」后查看。
            </div>
          </template>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.doc-preview-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(17, 24, 39, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.doc-preview {
  width: min(900px, 92vw);
  height: min(80vh, 760px);
  background: var(--bg-content, #fff);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.22);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.doc-preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.doc-preview-title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.doc-preview-title strong {
  font-size: 14px;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-preview-kind {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--primary);
  background: var(--primary-light);
  border-radius: 999px;
  padding: 1px 8px;
}
.doc-preview-ops {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.doc-preview-btn {
  font-size: 12px;
  color: var(--text-2);
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 12px;
}
.doc-preview-btn:hover:not(:disabled) {
  color: var(--text-1);
  border-color: var(--primary);
}
.doc-preview-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.doc-preview-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--bg-app);
}
.doc-preview-pdf {
  width: 100%;
  height: 100%;
  border: 0;
}
.doc-preview-img {
  display: block;
  max-width: 100%;
  max-height: 100%;
  margin: auto;
  object-fit: contain;
}
.doc-preview-text {
  margin: 0;
  padding: 16px 20px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.75;
  color: var(--text-2);
}
.doc-preview-md {
  padding: 16px 24px;
  font-size: 14px;
  line-height: 1.75;
  color: var(--text-2);
}
.doc-preview-md :deep(h1),
.doc-preview-md :deep(h2),
.doc-preview-md :deep(h3) {
  color: var(--text-1);
  margin: 18px 0 8px;
  line-height: 1.4;
}
.doc-preview-md :deep(h1) { font-size: 20px; }
.doc-preview-md :deep(h2) { font-size: 17px; }
.doc-preview-md :deep(h3) { font-size: 15px; }
.doc-preview-md :deep(p) { margin: 8px 0; }
.doc-preview-md :deep(table) {
  border-collapse: collapse;
  margin: 12px 0;
  width: 100%;
  font-size: 13px;
}
.doc-preview-md :deep(th),
.doc-preview-md :deep(td) {
  border: 1px solid var(--border);
  padding: 6px 10px;
  text-align: left;
}
.doc-preview-md :deep(th) {
  background: var(--bg-hover);
  color: var(--text-1);
}
.doc-preview-md :deep(code) {
  background: var(--bg-hover);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 12px;
}
.doc-preview-md :deep(pre) {
  background: var(--bg-hover);
  border-radius: 8px;
  padding: 10px 12px;
  overflow: auto;
}
.doc-preview-md :deep(blockquote) {
  margin: 8px 0;
  padding: 6px 12px;
  border-left: 3px solid var(--border);
  color: var(--text-3);
}
.doc-preview-md :deep(img) {
  max-width: 100%;
  height: auto;
}
.doc-preview-xlsx {
  padding: 12px 16px;
}
.sheet-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.sheet-tab {
  font-size: 12px;
  color: var(--text-2);
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 10px;
}
.sheet-tab.active {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-light);
}
.sheet-scroll {
  overflow: auto;
}
.sheet-table {
  border-collapse: collapse;
  font-size: 12px;
  min-width: 100%;
}
.sheet-table th,
.sheet-table td {
  border: 1px solid var(--border);
  padding: 4px 8px;
  text-align: left;
  white-space: nowrap;
}
.sheet-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-hover);
  color: var(--text-1);
}
.doc-preview-hint {
  padding: 24px;
  color: var(--text-3);
  font-size: 13px;
}
.doc-preview-hint.is-error {
  color: var(--danger);
}
</style>
