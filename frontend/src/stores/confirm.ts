/**
 * P2-21：通用二次确认（Promise 风格）。
 *
 *     const ok = await confirm.ask({ title: '删除该部门？', text: '…', danger: true })
 *     if (!ok) return
 *     await api.deleteDepartment(id)
 *
 * 为什么做成 store 而不是「各页面自己一份」：
 *   管理端的用户页与密码页各自实现过一遍（两份 `ask()` + 两份模板里的弹窗），
 *   而更要紧的是**部门与职位的删除当时根本没有确认** —— 「有的地方有、有的地方没有」
 *   正是「各自实现」的必然结果。收成一处之后，新页面想加确认只需一行。
 *
 * ⚠️ 只做确认，**不做动作**：动作由调用方在 `await` 之后执行。
 *   反过来的设计（store 里带 action）会让「用户点了确认但接口失败」的路径
 *   变得难以处理（重试、错误提示都藏在 store 里）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface ConfirmOptions {
  title: string
  text?: string
  /** 确认按钮文案（默认「确认」） */
  confirmText?: string
  /** 危险操作：确认按钮红色（删除类操作一律传 true） */
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
    state.value = {
      confirmText: '确认',
      danger: false,
      ...opts,
    }
    return new Promise<boolean>((resolve) => {
      resolver = resolve
    })
  }

  /** 由弹窗组件调用：用户点了确认（true）或取消（false）。 */
  function settle(ok: boolean) {
    state.value = null
    const r = resolver
    resolver = null
    r?.(ok)
  }

  return { state, ask, settle }
})
