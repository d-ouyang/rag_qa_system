/** 知识库文档管理接口（对应后端 api/routes/documents.py） */
import { del, get, uploadFile } from './http'
import type { DocumentChunksResponse, DocumentListResponse, VectorStats } from '@/types'

export const listDocuments = () => get<DocumentListResponse>('/api/v1/documents/')

export const getVectorStats = () => get<VectorStats>('/api/v1/documents/stats')

export const getDocumentChunks = (source: string) =>
  get<DocumentChunksResponse>(`/api/v1/documents/chunks?source=${encodeURIComponent(source)}`)

export const uploadDocument = (file: File) =>
  uploadFile<{
    file_name: string
    source: string
    file_type: string
    chunks_added: number
    total_chunks: number
  }>('/api/v1/documents/upload', file)

export const deleteDocument = (source: string) =>
  del<{ source: string; deleted_chunks: number; file_removed: boolean }>(
    `/api/v1/documents/?source=${encodeURIComponent(source)}`,
  )
