/** 知识库 store：文档列表 + 向量库统计 + 上传/删除 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as docsApi from '@/api/documents'
import type { KnowledgeDoc, VectorStats } from '@/types'
import { useUiStore } from './ui'

export const useDocumentStore = defineStore('documents', () => {
  const documents = ref<KnowledgeDoc[]>([])
  const stats = ref<VectorStats | null>(null)
  const loading = ref(false)
  const uploading = ref(false)
  /** 上传进度提示文案（解析/入库可能耗时数秒） */
  const uploadingName = ref('')

  async function refresh() {
    loading.value = true
    try {
      const [list, s] = await Promise.all([docsApi.listDocuments(), docsApi.getVectorStats()])
      documents.value = list.documents
      stats.value = s
    } finally {
      loading.value = false
    }
  }

  async function upload(file: File) {
    const ui = useUiStore()
    uploading.value = true
    uploadingName.value = file.name
    try {
      const res = await docsApi.uploadDocument(file)
      ui.toast(`「${res.file_name}」入库成功，新增 ${res.chunks_added} 个片段`, 'success')
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : '上传失败', 'error', 5000)
    } finally {
      uploading.value = false
      uploadingName.value = ''
      await refresh()
    }
  }

  async function remove(source: string) {
    const ui = useUiStore()
    try {
      const res = await docsApi.deleteDocument(source)
      ui.toast(`已删除 ${res.deleted_chunks} 个片段`, 'success')
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : '删除失败', 'error', 5000)
    } finally {
      await refresh()
    }
  }

  return { documents, stats, loading, uploading, uploadingName, refresh, upload, remove }
})
