/**
 * 会话 store：会话列表 + 当前会话消息 + 流式问答。
 *
 * 与后端的关系：
 * - 后端会话是「首次提问后才存在」的内存对象（MemoryManager），
 *   前端本地先建一个 id（crypto.randomUUID().replace(/-/g,'') 与后端 uuid4().hex 同格式），
 *   第一次 ask 之后后端自然登记该 id。
 * - 会话列表 = 后端 GET /sessions ∪ 本地会话，以后端数据为准做增量合并。
 * - 置顶/自定义标题存后端（PATCH /sessions/{id}），刷新不丢。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as qaApi from '@/api/qa'
import type { ChatMessage, SessionUsage, SourceItem } from '@/types'

function isAbortError(e: unknown): boolean {
  return e instanceof DOMException
    ? e.name === 'AbortError'
    : e instanceof Error && e.name === 'AbortError'
}

export interface LocalSession {
  session_id: string
  /** 列表展示标题：自定义标题 > 首条用户消息 > 「新会话」 */
  title: string
  message_count: number
  last_active: number | null
  /** 会话累计 token 用量 */
  usage?: SessionUsage
  /** 是否置顶 */
  pinned: boolean
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
  /** 最近一次问答的意图/路由结果（轻量，重的每轮详情已随消息存展示） */
  const lastMeta = ref<{ intent?: string; route?: string }>({})
  /** 当前这一轮流式请求；停止按钮与登出都靠它中断 */
  let currentAbort: AbortController | null = null

  // ---------- getters ----------
  const currentSession = computed(() =>
    sessions.value.find((s) => s.session_id === currentId.value) ?? null,
  )
  /** 置顶会话（内部按最后活跃倒序） */
  const pinnedSessions = computed(() =>
    sessions.value
      .filter((s) => s.pinned)
      .sort((a, b) => (b.last_active ?? Infinity) - (a.last_active ?? Infinity)),
  )
  /** 普通会话（内部按最后活跃倒序，无活跃时间的本地新会话排最前） */
  const normalSessions = computed(() =>
    sessions.value
      .filter((s) => !s.pinned)
      .sort((a, b) => (b.last_active ?? Infinity) - (a.last_active ?? Infinity)),
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
          s.title ??
          (local?.title && local.title !== '新会话'
            ? local.title
            : `会话 ${s.session_id.slice(0, 8)}`)
        const merged: LocalSession = {
          session_id: s.session_id,
          title,
          message_count: s.message_count,
          last_active: s.last_active ?? null,
          usage: s.usage,
          pinned: !!s.pinned,
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
      pinned: false,
      local: true,
    })
    currentId.value = id
    messages.value = []
    lastMeta.value = {}
    return id
  }

  /** 切换会话：设置当前 id 并拉取历史消息（含每轮 sources/usage/ts 回填） */
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

  /** 发送问题并流式接收回答（NDJSON）。editFromIndex>0 时先截断历史再问（编辑重发） */
  async function ask(question: string, options?: { editFromIndex?: number }) {
    const text = question.trim()
    if (!text || streaming.value) return
    if (!currentId.value) createSession()
    const sessionId = currentId.value as string

    // 编辑重发：截掉被编辑消息及其后的全部历史，前端本地同步截断
    // （editFrom=0 即编辑首条提问：后端 truncate(0) 清空、本地清空，语义一致）
    const editFrom = options?.editFromIndex
    if (editFrom != null) {
      await qaApi.truncateSession(sessionId, editFrom)
      messages.value = messages.value.slice(0, editFrom)
      const session = sessions.value.find((s) => s.session_id === sessionId)
      if (session) session.message_count = editFrom
    }

    const session = sessions.value.find((s) => s.session_id === sessionId)
    if (session) {
      if (session.title === '新会话') session.title = text.slice(0, 24)
      session.message_count += 2
      session.last_active = Date.now() / 1000
    }

    const askTs = Date.now() / 1000
    messages.value.push({ role: 'user', content: text, ts: askTs })
    messages.value.push({ role: 'assistant', content: '', streaming: true })
    // 注意：必须经 reactive 数组取出的代理对象做增量更新，
    // 直接改刚 push 的原始对象不会触发依赖通知，界面将停在空气泡
    const assistantMsg = messages.value[messages.value.length - 1]

    const controller = new AbortController()
    currentAbort = controller
    streaming.value = true

    // 高频小帧先攒着，一帧动画里合并写一次，避免每个 token 都触发重渲染和滚动
    let pending = ''
    let rafId = 0
    const flushPending = () => {
      if (rafId) {
        cancelAnimationFrame(rafId)
        rafId = 0
      }
      if (!pending) return
      assistantMsg.content += pending
      pending = ''
    }
    const enqueueChunk = (piece: string) => {
      if (controller.signal.aborted) return
      pending += piece
      if (!rafId) {
        rafId = requestAnimationFrame(flushPending)
      }
    }

    try {
      await qaApi.askStream(text, sessionId, (frame) => {
        if (frame.type === 'session') {
          adoptSessionId(sessionId, frame.session_id)
        } else if (frame.type === 'meta') {
          assistantMsg.intent = frame.intent
          assistantMsg.sources = (frame.sources ?? []) as SourceItem[]
          lastMeta.value.intent = frame.intent
          lastMeta.value.route = frame.route
        } else if (frame.type === 'chunk') {
          enqueueChunk(frame.content)
        } else if (frame.type === 'done') {
          flushPending()
          // 每轮详情随消息存：尾部徽章 + 明细浮层的数据源，刷新后由历史接口恢复
          assistantMsg.usage = frame.usage
          assistantMsg.elapsed_ms = frame.elapsed_ms
        } else if (frame.type === 'error') {
          flushPending()
          assistantMsg.content +=
            (assistantMsg.content ? '\n\n' : '') + `⚠️ ${frame.detail}`
        }
      }, controller.signal)
    } catch (e) {
      flushPending()
      if (isAbortError(e) || controller.signal.aborted) {
        // 主动停止：留下已生成的文本，不当成请求失败
        if (!assistantMsg.usage) {
          assistantMsg.content = assistantMsg.content
            ? `${assistantMsg.content}\n\n（已停止生成）`
            : '（已停止生成）'
        }
      } else {
        assistantMsg.content +=
          (assistantMsg.content ? '\n\n' : '') +
          `⚠️ 请求失败：${e instanceof Error ? e.message : String(e)}`
      }
    } finally {
      flushPending()
      assistantMsg.streaming = false
      streaming.value = false
      if (currentAbort === controller) currentAbort = null
      // 异步刷新列表（后端此刻已登记该会话）
      void fetchSessions()
    }
  }

  /** 停止当前这一轮生成（断开 fetch，后端生成器随之退出） */
  function stopStream() {
    currentAbort?.abort()
  }

  /**
   * 服务端回传的 session_id 与本地不一致时改写本地条目。
   * 现在前端总会自己生成 id 并带上，两边通常相同；这里是为了消费 session 帧。
   */
  function adoptSessionId(localId: string, serverId: string) {
    if (!serverId || serverId === localId) return
    const row = sessions.value.find((s) => s.session_id === localId)
    if (row) row.session_id = serverId
    if (currentId.value === localId) currentId.value = serverId
  }

  /** 编辑历史提问并从该处重新开始对话（已有产物不删除，仅截断后重问） */
  async function editAndResend(messageIndex: number, newQuestion: string) {
    const msg = messages.value[messageIndex]
    if (!msg || msg.role !== 'user') return
    // 第一条消息直接原地改文本重问；非首条需先截断
    await ask(newQuestion, { editFromIndex: messageIndex })
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

  /** 重命名会话（后端持久化 + 本地立即生效） */
  async function renameSession(sessionId: string, title: string) {
    const trimmed = title.trim()
    if (!trimmed) return
    const session = sessions.value.find((s) => s.session_id === sessionId)
    if (session) session.title = trimmed.slice(0, 60)
    if (session?.local) return // 后端尚无该会话，首次 ask 后 fetchSessions 会再对齐
    try {
      await qaApi.updateSession(sessionId, { title: trimmed.slice(0, 60) })
    } catch (e) {
      console.error('重命名失败', e)
    }
  }

  /** 置顶 / 取消置顶 */
  async function togglePin(sessionId: string) {
    const session = sessions.value.find((s) => s.session_id === sessionId)
    if (!session || session.local) return
    session.pinned = !session.pinned
    try {
      await qaApi.updateSession(sessionId, { pinned: session.pinned })
    } catch (e) {
      session.pinned = !session.pinned // 失败回滚
      console.error('置顶失败', e)
    }
  }

  /**
   * 清空所有本地会话状态（登出时调用）。
   *
   * 为什么必须清：这些数据是「上一个登录用户的对话内容」，属于隐私。
   * 不清的话下一个登录的人（同一台电脑/同一浏览器）会直接看到别人的历史，
   * 而且因为前端拿的是内存里的旧数据，看起来就像「后端串号了」。
   * 服务端数据不动（那些属于原用户，换个账号自然看不到）。
   */
  function resetAll() {
    currentAbort?.abort()
    currentAbort = null
    sessions.value = []
    currentId.value = null
    messages.value = []
    lastMeta.value = {}
    streaming.value = false
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
    pinnedSessions,
    normalSessions,
    fetchSessions,
    createSession,
    selectSession,
    ask,
    stopStream,
    editAndResend,
    removeSession,
    renameSession,
    togglePin,
    resetAll,
  }
})
