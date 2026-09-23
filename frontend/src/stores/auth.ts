/**
 * 鉴权 store：token 的持有者，以及所有「登录态」相关动作的唯一入口。
 *
 * ---------------------------------------------------------------------------
 * 为什么这里用 localStorage（项目里唯一的例外）
 * ---------------------------------------------------------------------------
 * 会话数据、UI 状态都刻意不落 localStorage（要的是刷新后从后端拉取的真实状态），
 * 但**登录 token 必须落**，理由有三：
 *   · 刷新页面不该要求重新登录（sessionStorage 会随标签页关闭丢失）；
 *   · 新开标签页应当继承登录态（localStorage 跨标签共享，sessionStorage 不共享）；
 *   · 它不是业务数据，而是「身份凭证」——本来就是要持久保存的东西。
 *
 * 风险与代价（写在代码里而不是藏着）：localStorage 对 XSS 不设防，
 * 页面上一旦有注入脚本就能读走 token。当前系统的输入（用户提问）都经过
 * 后端处理且不会以 HTML 形式回显（前端全程文本插值，不用 v-html），
 * 所以这条路径的风险可控。真要彻底解决需要把 token 换成 HttpOnly Cookie + CSRF 防护，
 * 那是后续迭代的事，不是「忘了做」。
 *
 * ---------------------------------------------------------------------------
 * 为什么要把手动登出和 401 被动登出分开提示
 * ---------------------------------------------------------------------------
 * 两者的用户预期完全不同：主动登出是「我点的」，直接回登录页；
 * 被动登出（token 过期）用户毫无预期，必须给一句说明，
 * 否则他会以为「系统把我踢了」或者「页面坏了」。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as authApi from '@/api/auth'
import { ApiError, configureAuth } from '@/api/http'

const TOKEN_KEY = 'ragqa.auth.token'
const USER_KEY = 'ragqa.auth.username'
const EXPIRES_KEY = 'ragqa.auth.expires_at'

export type AuthStatus = 'idle' | 'verifying' | 'ready'

export const useAuthStore = defineStore('auth', () => {
  // ---------- state ----------
  /** JWT；空串表示未登录。localStorage 读取包在函数里，避免 SSR/隐私模式下直接抛错 */
  const token = ref<string>(readStorage(TOKEN_KEY) ?? '')
  const username = ref<string>(readStorage(USER_KEY) ?? '')
  /** token 到期时间（毫秒时间戳），仅用于「快过期」提示，不作为鉴权依据 */
  const expiresAt = ref<number>(Number(readStorage(EXPIRES_KEY) ?? 0))
  /** 启动时的 token 校验状态：verifying 期间不渲染主界面（避免闪一下再跳登录页） */
  const status = ref<AuthStatus>('idle')
  /** 被动登出的原因，登录页会展示它（空串表示没有需要提示的事） */
  const kickedReason = ref<string>('')

  // ---------- getters ----------
  const isAuthenticated = computed(() => token.value !== '')
  /** 是否已接近过期（剩余不足 10 分钟）——用于提示用户，不做强制动作 */
  const expiresSoon = computed(
    () => expiresAt.value > 0 && expiresAt.value - Date.now() < 10 * 60 * 1000,
  )

  // ---------- 内部工具 ----------
  function writeStorage(key: string, value: string): void {
    try {
      localStorage.setItem(key, value)
    } catch {
      /* 隐私模式下 setItem 会抛异常：不落盘也能用（只是刷新后要重新登录） */
    }
  }

  function clearStorage(): void {
    try {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
      localStorage.removeItem(EXPIRES_KEY)
    } catch {
      /* 同上 */
    }
  }

  /** 把凭据写进 state + localStorage（登录成功、刷新校验通过时都走这里） */
  function setSession(nextToken: string, name: string, expiresInSeconds = 0): void {
    token.value = nextToken
    username.value = name
    expiresAt.value = expiresInSeconds > 0 ? Date.now() + expiresInSeconds * 1000 : 0
    writeStorage(TOKEN_KEY, nextToken)
    writeStorage(USER_KEY, name)
    writeStorage(EXPIRES_KEY, String(expiresAt.value))
  }

  /** 清空登录态（主动登出、被动登出共用） */
  function clearSession(): void {
    token.value = ''
    username.value = ''
    expiresAt.value = 0
    clearStorage()
  }

  // ---------- 与 http 层的对接 ----------
  // 把「怎么取 token」和「401 怎么办」注入 http 客户端，
  // 这样 http.ts 不需要 import 本 store（避免循环依赖）
  configureAuth({
    getToken: () => token.value,
    onUnauthorized: (error: ApiError) => {
      // 已经是未登录状态（比如登录页并发请求）时不重复处理，避免提示刷屏
      if (!token.value) return
      // 文案只取「过期」与「其他失效」两种，不把网关的原始 message 嵌进来 ——
      // 那会拼出「登录状态已失效（登录状态无效，请重新登录），请重新登录」这种
      // 同义反复的句子，用户读完还是不知道自己该干什么。
      kickedReason.value =
        error.code === 'TOKEN_EXPIRED' ? '登录已过期，请重新登录' : '登录状态已失效，请重新登录'
      clearSession()
    },
  })

  // ---------- actions ----------
  /** 登录：成功即写入凭据；失败抛 ApiError 交给登录页展示。 */
  async function login(name: string, password: string): Promise<void> {
    const result = await authApi.login(name, password)
    kickedReason.value = ''
    setSession(result.access_token, result.user.username, result.expires_in)
  }

  /**
   * 启动校验：本地有 token 就向后端确认它还有效。
   *
   * 为什么要多这一次请求：token 可能已经过期，或者服务端换了签名密钥
   * （重启后密钥变了），此时本地看起来「已登录」，实际每个请求都会 401。
   * 与其让用户在主界面上连点几次报错，不如启动时先问一次。
   */
  async function verify(): Promise<void> {
    if (!token.value) {
      status.value = 'ready'
      return
    }
    status.value = 'verifying'
    try {
      const result = await authApi.fetchMe()
      username.value = result.user.username
    } catch (e) {
      // 401 已由 http 层的钩子处理（清凭据 + 记原因），这里只需保证不卡在 verifying
      if (!(e instanceof ApiError) || e.status !== 401) {
        // 网络问题不等于未登录：保留本地凭据，让用户进去后用重试解决
        console.warn('token 校验失败（非 401，保留登录态）', e)
      }
    } finally {
      status.value = 'ready'
    }
  }

  /** 主动登出：先通知服务端（可能失败，不影响本地清理），再清本地凭据。 */
  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      /* 登出接口失败不阻塞登出：本地凭据一定要清掉 */
    }
    clearSession()
    kickedReason.value = ''
  }

  /** 手动关掉登录页上的过期提示（用户已看到并准备重新登录） */
  function dismissNotice(): void {
    kickedReason.value = ''
  }

  return {
    token,
    username,
    expiresAt,
    status,
    kickedReason,
    isAuthenticated,
    expiresSoon,
    login,
    verify,
    logout,
    dismissNotice,
  }
})

/** 读 localStorage：隐私模式/超限时 getItem 也可能抛，统一兜底成 null。 */
function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}
