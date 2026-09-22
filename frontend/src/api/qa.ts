/** 问答与会话接口（对应后端 api/routes/qa.py） */
import { del, get, patchJson, postJson, postNdjson } from './http'
import type { ChatMessage, SessionInfo, StreamFrame } from '@/types'

export const listSessions = () => get<SessionInfo[]>('/api/v1/qa/sessions')

export const getSessionHistory = (sessionId: string) =>
  get<{ session_id: string; message_count: number; messages: ChatMessage[] }>(
    `/api/v1/qa/sessions/${encodeURIComponent(sessionId)}`,
  )

export const deleteSession = (sessionId: string) =>
  del<{ session_id: string; cleared: boolean }>(`/api/v1/qa/sessions/${encodeURIComponent(sessionId)}`)

/** 重命名 / 置顶会话（title / pinned 不传即不改） */
export const updateSession = (
  sessionId: string,
  patch: { title?: string; pinned?: boolean },
) => patchJson<{ session_id: string; pinned: boolean; title?: string }>(
  `/api/v1/qa/sessions/${encodeURIComponent(sessionId)}`,
  patch,
)

/** 截断会话历史到前 keepMessages 条（编辑重发用），返回截断后的消息条数 */
export const truncateSession = (sessionId: string, keepMessages: number) =>
  postJson<{ session_id: string; message_count: number }>(
    `/api/v1/qa/sessions/${encodeURIComponent(sessionId)}/truncate`,
    { keep_messages: keepMessages },
  )

export const askSync = (question: string, sessionId: string) =>
  postJson<{
    session_id: string
    answer: string
    intent: string
    route: string
    intent_source: string
    standalone_question?: string | null
    sources: { index: number; source: string; snippet: string; rerank_score?: number | null; vector_similarity?: number | null }[]
    elapsed_ms: number
  }>('/api/v1/qa/ask', { question, session_id: sessionId })

/** 流式问答：NDJSON 逐帧回调 */
export function askStream(
  question: string,
  sessionId: string,
  onFrame: (frame: StreamFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  return postNdjson(
    '/api/v1/qa/ask/stream',
    { question, session_id: sessionId },
    onFrame as (frame: unknown) => void,
    signal,
  )
}
