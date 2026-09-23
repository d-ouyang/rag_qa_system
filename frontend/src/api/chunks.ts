/**
 * 切片反查接口（对应后端 api/routes/chunks.py，P0-4a 新增）。
 *
 * 存在的意义：问答接口只给 200 字的摘要，用户点引用要看的是**切片全文**
 * 和它出自哪份文档 —— 这两样都由这个接口按 `chunk_id` 现取。
 */
import { get } from './http'
import type { ChunkDetail } from '@/types'

/**
 * chunk_id 的形状由后端契约固定为 `<doc_id>:<chunk_index>`。
 *
 * 这里在**发请求之前**先校验一次，有两个理由：
 *   1. 非法串拼进路径会打到别的路由上（比如 `/api/v1/chunks/..%2Fxxx`），
 *      发之前拦掉比指望网关拦更可靠；
 *   2. 校验失败就能直接给用户一句人话，不用等一个 400 回来再翻译一遍。
 *
 * 为什么**不**用 `encodeURIComponent` 包一层：它会把冒号变成 `%3A`，
 * 而中间那层代理有可能再编码一次变成 `%253A`，把本来能过的请求搞坏
 * （冒号在 URL 路径段里本来就是合法字符）。改成「先校验、合法才拼」，
 * 既能挡掉非法输入，又不需要编码。
 */
const CHUNK_ID_RE = /^\d+:\d+$/

export function isValidChunkId(chunkId: string | null | undefined): chunkId is string {
  return typeof chunkId === 'string' && CHUNK_ID_RE.test(chunkId)
}

/** 取切片全文与源文档信息。调用前必须先过 `isValidChunkId`。 */
export const getChunk = (chunkId: string) =>
  get<ChunkDetail>(`/api/v1/chunks/${chunkId}`)
