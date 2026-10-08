/**
 * P2-21：管理端通用二次确认（Promise 风格），与主应用同构。
 *
 *     const ok = await confirm.ask({ title: '删除该部门？', text: '…', danger: true })
 *     if (!ok) return
 *     await api.deleteDepartment(id)
 *
 * 背景：用户页与密码页各自实现过一遍 `ask()`，而**部门/职位的删除当时没有确认** ——
 * 「有的地方有、有的地方没有」正是「各自实现」的必然结果。收成一处后，
 * 新页面加确认只需一行；也保证了「所有删除按钮都有确认」这条可以机械核对
 * （见 `tests/acceptance` 的 UI 断言：页面里不存在 window.confirm）。
 *
 * ⚠️ 只做确认，不做动作：动作由调用方 `await` 之后执行。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface ConfirmOptions {
  title: string
  text?: string
  confirmText?: string
  danger?: boolean
}

interface ConfirmState extends ConfirmOptions {
  confirmText: string
  danger: boolean
}

export const useConfirmStore = defineStore('confirm', () => {
  const state = ref<ConfirmState | null>(null)
  let resolver: ((ok: boolean) => void) | null = null

  function ask(opts: ConfirmOptions): Promise<boolean> {
    state.value = { confirmText: '确认', danger: false, ...opts }
    return new Promise<boolean>((resolve) => {
      resolver = resolve
    })
  }

  function settle(ok: boolean) {
    state.value = null
    const r = resolver
    resolver = null
    r?.(ok)
  }

  return { state, ask, settle }
})
