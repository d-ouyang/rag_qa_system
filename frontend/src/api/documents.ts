/**
 * 知识库文档管理接口（对应后端 `api/routes/documents.py`）。
 *
 * ---------------------------------------------------------------------------
 * P0-3b 起这里发生了根本变化：上传不再「等待解析完成」
 * ---------------------------------------------------------------------------
 * P0-3a 之后后端的上传是**异步**的：接口只做三件事（落盘、写一条 `pending`
 * 记录、把任务投进队列），然后立刻返回 202。响应里**没有 `chunks_added`** ——
 * 那个数在那一刻根本不存在，解析甚至还没开始。
 *
 * 所以本文件不再提供「上传就知道结果」的接口，调用方必须改成
 * **提交后轮询 `GET /api/v1/documents/` 读 `status`**。
 * 这不是接口缺了个字段，而是异步化的必然结果（详见 P0-3a 迭代文档 §3.8）。
 *
 * ---------------------------------------------------------------------------
 * 身份键从「磁盘路径」换成了 `doc_id`
 * ---------------------------------------------------------------------------
 * 旧接口用 `source`（也就是磁盘绝对路径）当身份键，所有操作都靠路径字符串匹配。
 * 新接口一律用 `doc_id`（自增主键）：
 *   · 路径不可信（uuid 名、容器/本机路径不同、可能被改写）；
 *   · 字符串匹配在 URL 编码、大小写、符号链接上都有坑；
 *   · 路径会暴露服务器目录结构（旧接口把绝对路径吐给了浏览器）。
 */

import { del, get, getFile, postJson, uploadFile, type DownloadedFile } from './http'
import type {
  DeleteDocumentResponse,
  DocumentChunksResponse,
  DocumentListResponse,
  DocStatus,
  ParseQueueStatus,
  ReparseAccepted,
  UploadAccepted,
  VectorStats,
} from '@/types'

/** 列表查询参数（都可选；不传就是「全部」） */
export interface ListDocumentsParams {
  status?: DocStatus
  project_id?: string
  limit?: number
  offset?: number
}

/** 按 `doc_id` 列文档（**读 MySQL**，不是向量库 —— 正在解析的文档在向量库里还没有切片） */
export function listDocuments(params: ListDocumentsParams = {}) {
  const qs = new URLSearchParams()
  if (params.status) qs.set('status', params.status)
  if (params.project_id) qs.set('project_id', params.project_id)
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  const query = qs.toString()
  return get<DocumentListResponse>(`/api/v1/documents/${query ? `?${query}` : ''}`)
}

export const getVectorStats = () => get<VectorStats>('/api/v1/documents/stats')

/**
 * 上传文档 → **202 受理**（不代表解析完成，更不代表成功）。
 *
 * ⚠️ 不传 `project_id`：后端两张入口（建记录、列文档）的默认值都是 `default`，
 * 前端再写一个字面量就等于多出一处会漂移的常量。等多租户真的来了，
 * project_id 应当来自登录态，而不是模块级常量。
 */
export const uploadDocument = (file: File) =>
  uploadFile<UploadAccepted>('/api/v1/documents/upload', file)

/** 查看某文档的全部切分片段（按 chunk_index 升序） */
export const getDocumentChunks = (docId: number) =>
  get<DocumentChunksResponse>(`/api/v1/documents/${docId}/chunks`)

/**
 * 手动重新触发解析（用于失败后重试、或队列丢过任务时补投）。
 *
 * 为什么这里传空对象 `{}` 而不是发一个「没有请求体」的 POST：
 * 网关的 `fixRequestBody` 只在 `Content-Type` 是 JSON 时才重写请求体，
 * 带 `{}` 走的是**已被验证过的那条路径**（问答接口同样是 JSON body），
 * 不必再引入一种没验证过的请求形状。后端该接口不声明请求体，会忽略它。
 */
export const reparseDocument = (docId: number) =>
  postJson<ReparseAccepted>(`/api/v1/documents/${docId}/reparse`, {})

/** 删除文档：后端会**依次**清掉向量切片 → 磁盘文件 → MySQL 记录（缺一即脏数据） */
export const deleteDocument = (docId: number) =>
  del<DeleteDocumentResponse>(`/api/v1/documents/${docId}`)

/** 下载原始文件（带 token 取 blob，见 http.ts 的 getFile 注释） */
export const downloadDocument = (docId: number): Promise<DownloadedFile> =>
  getFile(`/api/v1/documents/download?doc_id=${docId}`)

/**
 * 解析链路运行状态（队列积压 + worker 存活 + 文档状态计数）。
 *
 * ⚠️ 服务端会阻塞约 1 秒（worker 存活探测走 `inspect().ping()`，必须等满超时窗口）。
 * 调用方**必须降频**，不要跟文档列表同频轮询 ——
 * 具体做法见 `stores/documents.ts` 的 `QUEUE_EVERY_N_TICKS`。
 */
export const getParseQueueStatus = () => get<ParseQueueStatus>('/api/v1/system/queue')
