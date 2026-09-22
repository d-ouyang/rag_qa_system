/**
 * UI 全局状态：左侧导航的当前激活视图 + 全局提示条。
 *
 * 职责边界：左侧边栏只改这里的 activeView（导航职责），
 * 右侧内容区只读它来决定渲染哪个视图（展示职责）——
 * 两侧不直接互相引用组件，全部经由此 store 解耦。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ActiveView = 'chat' | 'knowledge' | 'settings'

export interface Toast {
  id: number
  text: string
  kind: 'success' | 'error' | 'info'
}

let toastSeq = 0

export const useUiStore = defineStore('ui', () => {
  const activeView = ref<ActiveView>('chat')
  /** 顶部轻提示（上传成功/删除失败等场景） */
  const toasts = ref<Toast[]>([])

  function switchView(view: ActiveView) {
    activeView.value = view
  }

  function toast(text: string, kind: Toast['kind'] = 'info', duration = 3200) {
    const id = ++toastSeq
    toasts.value.push({ id, text, kind })
    setTimeout(() => {
      toasts.value = toasts.value.filter((t) => t.id !== id)
    }, duration)
  }

  return { activeView, toasts, switchView, toast }
})
