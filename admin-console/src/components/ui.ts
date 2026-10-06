/**
 * 管理端共用的两个小组件：模态框与 toast。
 *
 * --------------------------------------------------------------------------
 * 为什么自己写这两块（不引 UI 库）
 * --------------------------------------------------------------------------
 * 引一个组件库会带进来几百 KB 与一套设计语言，而这个应用一共只有
 * 「一个模态框 + 一行提示」两种浮层需求。自己写的代价约 80 行，
 * 换来的是：没有依赖要升级、样式完全跟着令牌走（深色主题不用再覆盖一遍）。
 *
 * 模态框刻意做的三件事，都是「不做就会出现奇怪 bug」的：
 *
 *   1. **ESC 关闭** —— 表单填一半想退出时，人会本能按 ESC；
 *   2. **打开时 body 锁滚动** —— 否则滚轮会滚到背后的表格上，
 *      而用户以为自己在滚弹窗里的内容；
 *   3. **点遮罩关闭但点内容不关** —— 点遮罩是「我想退出」，
 *      点内容区是「我在选东西」，把后者也关掉会让人不敢点任何地方。
 */
import { onBeforeUnmount, onMounted, watch } from 'vue'

export interface ToastItem {
  id: number
  kind: 'ok' | 'warn' | 'error'
  text: string
}

let toastSeq = 0

export function newToast(kind: ToastItem['kind'], text: string): ToastItem {
  return { id: ++toastSeq, kind, text }
}

/** 打开模态框时锁住页面滚动；组件卸载或关闭时恢复。 */
export function useScrollLock(active: () => boolean): void {
  let previous = ''
  const apply = (on: boolean) => {
    if (on) {
      previous = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = previous
    }
  }
  onMounted(() => watch(active, (v) => apply(v), { immediate: true }))
  onBeforeUnmount(() => apply(false))
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  // MySQL 的 DATETIME 返回 "2026-10-07 01:16:35"（无时区），
  // 直接 new Date() 会被当成 UTC —— 差 8 小时，表现为「刚做的操作显示成 8 小时后」。
  const normalized = value.includes('T') ? value : value.replace(' ', 'T')
  const d = new Date(normalized)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
