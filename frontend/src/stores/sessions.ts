/**
 * 会话 store：会话列表 + 当前会话消息 + 流式问答。
 *
 * 与后端的关系：
 * - 后端会话是「首次提问后才存在」的内存对象（MemoryManager），
 *   前端本地先建一个 id（crypto.randomUUID().replace(/-/g,'') 与后端 uuid4().hex 同格式），
 *   第一次 ask 之后后端自然登记该 id。
 * - 会话列表 = 后端 GET /sessions ∪ 本地会话，以后端数据为准做增量合并。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as qaApi from '@/api/qa'
import type { ChatMessage, SourceItem } from '@/types'

export interface LocalSession {
  session_id: string
  /** 列表展示标题：取该会话第一条用户消息，未提问时为「新会话」 */
  title: string
  message_count: number
  last_active: number | null
  /** 是否为前端本地创建、后端尚无记录 */
  local: boolean
}

export const useSessionStore = defineStore('sessions', () => {
  // ---------- state ----------
  const sessions = ref<LocalSession[]>([])
  const currentId = ref<string | null>(null)
  const messages = ref<ChatMessage[]>([])
  const streaming = ref(false)
  const listLoading = ref(false)
  const historyLoading = ref(false)
  /** 最近一次问答的意图/路由结果，展示在聊天头部 */
  const lastMeta = ref<{ intent?: string; route?: string; elapsedMs?: number }>({})

  // ---------- getters ----------
  const currentSession = computed(() =>
    sessions.value.find((s) => s.session_id === currentId.value) ?? null,
  )
  /** 按最后活跃时间倒序（无活跃时间的本地新会话排最前） */
  const sortedSessions = computed(() =>
    [...sessions.value].sort((a, b) => (b.last_active ?? Infinity) - (a.last_active ?? Infinity)),
  )

  // ---------- actions ----------
  /** 拉取后端会话列表并与本地合并（后端有数据的覆盖本地记录） */
  async function fetchSessions() {
    listLoading.value = true
    try {
      const remote = await qaApi.listSessions()
      for (const s of remote) {
        const local = sessions.value.find((x) => x.session_id === s.session_id)
        const title =
          local?.title && local.title !== '新会话'
            ? local.title
            : `会话 ${s.session_id.slice(0, 8)}`
        const merged: LocalSession = {
          session_id: s.session_id,
          title,
          message_count: s.message_count,
          last_active: s.last_active ?? null,
          local: false,
        }
        if (local) sessions.value[sessions.value.indexOf(local)] = merged
        else sessions.value.push(merged)
      }
    } finally {
      listLoading.value = false
    }
  }

  /** 新建本地会话并切换为当前 */
  function createSession(): string {
    const id = crypto.randomUUID().replace(/-/g, '')
    sessions.value.unshift({
      session_id: id,
      title: '新会话',
      message_count: 0,
      last_active: Date.now() / 1000,
      local: true,
    })
    currentId.value = id
    messages.value = []
    lastMeta.value = {}
    return id
  }

  /** 切换会话：设置当前 id 并拉取历史消息 */
  async function selectSession(sessionId: string) {
    if (currentId.value === sessionId && messages.value.length > 0) return
    currentId.value = sessionId
    messages.value = []
    lastMeta.value = {}
    historyLoading.value = true
    try {
      const session = sessions.value.find((s) => s.session_id === sessionId)
      // 本地未提问的新会话：后端必无历史，跳过请求
      if (session?.local && session.message_count === 0) return
      const history = await qaApi.getSessionHistory(sessionId)
      messages.value = history.messages.map((m) => ({ ...m }))
      if (session) {
        session.message_count = history.message_count
        if (history.message_count > 0 && (session.title === '新会话' || session.title.startsWith('会话 '))) {
          const firstUser = history.messages.find((m) => m.role === 'user')
          if (firstUser) session.title = firstUser.content.slice(0, 24)
        }
      }
    } finally {
      historyLoading.value = false
    }
  }

  /** 发送问题并流式接收回答（NDJSON） */
  async function ask(question: string) {
    const text = question.trim()
    if (!text || streaming.value) return
    if (!currentId.value) createSession()
    const sessionId = currentId.value as string

    const session = sessions.value.find((s) => s.session_id === sessionId)
    if (session) {
      if (session.title === '新会话') session.title = text.slice(0, 24)
      session.message_count += 2
      session.last_active = Date.now() / 1000
    }

    messages.value.push({ role: 'user', content: text })
    messages.value.push({ role: 'assistant', content: '', streaming: true })
    // 注意：必须经 reactive 数组取出的代理对象做增量更新，
    // 直接改刚 push 的原始对象不会触发依赖通知，界面将停在空气泡
    const assistantMsg = messages.value[messages.value.length - 1]

    streaming.value = true
    try {
      await qaApi.askStream(text, sessionId, (frame) => {
        if (frame.type === 'meta') {
          assistantMsg.intent = frame.intent
          assistantMsg.sources = (frame.sources ?? []) as SourceItem[]
          lastMeta.value.intent = frame.intent
          lastMeta.value.route = frame.route
        } else if (frame.type === 'chunk') {
          assistantMsg.content += frame.content
        } else if (frame.type === 'done') {
          lastMeta.value.elapsedMs = frame.elapsed_ms
        } else if (frame.type === 'error') {
          assistantMsg.content +=
            (assistantMsg.content ? '\n\n' : '') + `⚠️ ${frame.detail}`
        }
      })
    } catch (e) {
      assistantMsg.content +=
        (assistantMsg.content ? '\n\n' : '') +
        `⚠️ 请求失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      assistantMsg.streaming = false
      streaming.value = false
      // 异步刷新列表（后端此刻已登记该会话）
      void fetchSessions()
    }
  }

  /** 删除会话（后端清记忆 + 本地移除列表项） */
  async function removeSession(sessionId: string) {
    try {
      await qaApi.deleteSession(sessionId)
    } catch {
      /* 后端不存在该会话不算失败（幂等），继续移除本地条目 */
    }
    sessions.value = sessions.value.filter((s) => s.session_id !== sessionId)
    if (currentId.value === sessionId) {
      currentId.value = null
      messages.value = []
      lastMeta.value = {}
    }
  }

  return {
    sessions,
    currentId,
    messages,
    streaming,
    listLoading,
    historyLoading,
    lastMeta,
    currentSession,
    sortedSessions,
    fetchSessions,
    createSession,
    selectSession,
    ask,
    removeSession,
  }
})
