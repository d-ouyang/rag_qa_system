/**
 * 管理端的登录态 store。
 *
 * 与主前端 `stores/auth.ts` 的差别只有一处，但很关键：
 * **登录成功 ≠ 能进管理端。**
 *
 * 主前端拿到 token 就进主界面（那里所有人都能用）；这里登录之后还要问一次
 * `/api/v1/admin/me`：普通员工会被 403 拒回来，而**这一条必须变成一句人话**
 * （「你的账号没有管理端权限」），不能是「登录失败」—— 后者会让人以为密码错了，
 * 反复重试直到账号被锁（而锁定文案与密码错完全相同，于是进入死循环排查）。
 *
 * token 仍然落 localStorage（全项目唯一的例外，理由见主前端同名文件的文件头）。
 * key 与主应用不同：两个应用在同一台机器的同一个浏览器下共存，
 * 共用 key 会互相顶掉登录态（表现为「打开管理端后主应用掉线了」）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as adminApi from '@/api/admin'
import * as authApi from '@/api/auth'
import { ApiError, configureAuth } from '@/api/http'

const TOKEN_KEY = 'ragqa.admin.token'
const USER_KEY = 'ragqa.admin.username'
const EXPIRES_KEY = 'ragqa.admin.expires_at'

export type AuthStatus = 'idle' | 'verifying' | 'ready' | 'forbidden'

export const useAuthStore = defineStore('adminAuth', () => {
  const token = ref<string>(readStorage(TOKEN_KEY) ?? '')
  const username = ref<string>(readStorage(USER_KEY) ?? '')
  const expiresAt = ref<number>(Number(readStorage(EXPIRES_KEY) ?? 0))
  const status = ref<AuthStatus>('idle')
  /** 被动登出的原因（token 过期 / 失效），登录页展示 */
  const kickedReason = ref<string>('')
  /** 被管理端拒之门外的原因（非管理员账号）。空串 = 没被拒 */
  const deniedReason = ref<string>('')
  const profile = ref<adminApi.ActorProfile | null>(null)

  const isAuthenticated = computed(() => token.value !== '')
  /** 已经确认过有管理端权限（用它可以决定是否渲染侧边栏） */
  const ready = computed(() => status.value === 'ready' && profile.value !== null)

  function writeStorage(key: string, value: string): void {
    try {
      localStorage.setItem(key, value)
    } catch {
      /* 隐私模式：不落盘也能用，只是刷新后要重新登录 */
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

  function setSession(nextToken: string, name: string, expiresInSeconds = 0): void {
    token.value = nextToken
    username.value = name
    expiresAt.value = expiresInSeconds > 0 ? Date.now() + expiresInSeconds * 1000 : 0
    writeStorage(TOKEN_KEY, nextToken)
    writeStorage(USER_KEY, name)
    writeStorage(EXPIRES_KEY, String(expiresAt.value))
  }

  function clearSession(): void {
    token.value = ''
    username.value = ''
    expiresAt.value = 0
    profile.value = null
    clearStorage()
  }

  configureAuth({
    getToken: () => token.value,
    onUnauthorized: (error: ApiError) => {
      if (!token.value) return
      kickedReason.value =
        error.code === 'TOKEN_EXPIRED' ? '登录已过期，请重新登录' : '登录状态已失效，请重新登录'
      clearSession()
    },
  })

  /**
   * 登录：先换 token，再立刻向管理端确认权限。
   *
   * 为什么两步合成一个 action：调用方（登录页）不该知道「要先登录再验权」
   * 这个顺序；少知道一个步骤，就少一处「登录后直接跳主页、结果被 403 弹回来」。
   */
  async function login(name: string, password: string): Promise<void> {
    const result = await authApi.login(name, password)
    kickedReason.value = ''
    deniedReason.value = ''
    // ⚠️ 顺序不能反：**先落 token，再问 /me**。
    // token 是 `configureAuth.getToken` 的数据源，没落就发请求 = 裸请求 = 401，
    // 于是「权限二次确认」永远失败，且报的是「账号已停用或不存在」——
    // 一条把人指向完全错误方向的提示（13a 就是这么写的，浏览器里 100% 登不进来）。
    setSession(result.access_token, result.user.username, result.expires_in)
    try {
      const actor = await adminApi.fetchProfile()
      profile.value = actor
      status.value = 'ready'
    } catch (e) {
      const apiError = e instanceof ApiError ? e : null
      if (apiError && apiError.status === 403) {
        deniedReason.value = apiError.message || '该账号没有管理端权限'
      } else if (apiError && apiError.status === 401) {
        deniedReason.value = '账号已停用或不存在，请联系管理员'
      } else {
        deniedReason.value = apiError?.message || '无法进入管理端'
      }
      clearSession()
      throw new Error(deniedReason.value)
    }
  }

  /** 刷新页面后恢复登录态：有 token 就重新拉一次身份。 */
  async function verify(): Promise<void> {
    if (!token.value) {
      status.value = 'idle'
      return
    }
    status.value = 'verifying'
    deniedReason.value = ''
    try {
      profile.value = await adminApi.fetchProfile()
      username.value = profile.value.username
      status.value = 'ready'
    } catch (e) {
      const apiError = e instanceof ApiError ? e : null
      if (apiError?.status === 403) {
        deniedReason.value = apiError.message || '该账号没有管理端权限'
        clearSession()
        status.value = 'forbidden'
      } else if (apiError?.status === 401) {
        deniedReason.value = '账号已停用或不存在，请联系管理员'
        clearSession()
        status.value = 'forbidden'
      } else {
        // 网络问题不等于未登录：保留凭据，让用户进去后重试
        console.warn('管理端身份校验失败（非鉴权错误，保留登录态）', e)
        profile.value = null
        status.value = 'idle'
      }
    }
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      /* 服务端登出失败不影响本地清凭据 */
    }
    clearSession()
    kickedReason.value = ''
    deniedReason.value = ''
    status.value = 'idle'
  }

  function dismissNotice(): void {
    kickedReason.value = ''
    deniedReason.value = ''
  }

  return {
    token,
    username,
    expiresAt,
    status,
    kickedReason,
    deniedReason,
    profile,
    isAuthenticated,
    ready,
    login,
    verify,
    logout,
    dismissNotice,
  }
})

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}
