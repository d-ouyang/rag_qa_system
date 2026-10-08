/** 问答与会话接口（对应后端 api/routes/qa.py） */
import { del, get, patchJson, postJson, postNdjson } from './http'
import type { ChatMessage, SessionInfo, SourceItem, StreamFrame } from '@/types'

/**
 * P2-15d：本人本月 token 用量 + 顶部横幅文案。
 *
 * 🔴 `banner` 由后端 `quota_policy.banner_text()` 生成 —— **前端只显示，不自己拼**。
 *    `banner === ''` 表示不打扰（未设额度或未到阈值），不是「没加载出来」。
 *    这个端点永远不拒绝提问（没有「还能不能问」的字段）—— 只提醒不阻断。
 */
export interface MyQuota {
  used: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  effective_quota: number
  usage_percent: number | null
  status: 'ok' | 'warn' | 'over'
  status_label: string
  banner: string
}

export const fetchMyQuota = () => get<MyQuota>('/api/v1/qa/quota/me')

/** P2-16a：个人信息面板（基本资料 + 当月/历史用量）。数字全部后端算好，前端只渲染。 */
export interface MyProfile {
  profile: {
    display_name: string
    username: string
    employee_no: string
    email: string | null
    phone: string | null
    department: string | null
    position: string | null
    role: string
    role_label: string
    kb_role: string
    joined_at: string
  }
  month_usage: {
    input_tokens: number
    output_tokens: number
    cache_read_tokens: number
    billable_tokens: number
    effective_quota: number
    usage_percent: number | null
    status: 'ok' | 'warn' | 'over'
    status_label: string
  }
  total_usage: {
    input_tokens: number
    output_tokens: number
    requests: number
    billable_tokens: number
  }
}

export const fetchMyProfile = () => get<MyProfile>('/api/v1/qa/me/profile')

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
    // 直接复用 SourceItem，不在这里再抄一遍字段：
    // 抄一份就多一处漂移点 —— 后端给 sources 加字段时，只有一处会记得改。
    sources: SourceItem[]
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
