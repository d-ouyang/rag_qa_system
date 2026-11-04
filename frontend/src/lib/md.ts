// markdown → HTML。
// 判断纪律（与 office 项目一致）：大文本走 Worker（>20k 字符），小文本主线程同步解析；
// 两条路径最终都在主线程过 DOMPurify —— 预览的是知识库原文，仍按不可信内容处理。
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const WORKER_THRESHOLD = 20_000

let worker: Worker | null = null
let seq = 0
const pending = new Map<number, (html: string) => void>()

function getWorker(): Worker {
  if (!worker) {
    worker = new Worker(new URL('./md.worker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = (e: MessageEvent<{ id: number; html: string; error?: string }>) => {
      const resolve = pending.get(e.data.id)
      if (!resolve) return
      pending.delete(e.data.id)
      resolve(e.data.error ? '' : e.data.html)
    }
  }
  return worker
}

function sanitize(html: string): string {
  return DOMPurify.sanitize(html)
}

/** 同步解析（小文本直接用）。 */
export function renderMarkdown(text: string): string {
  return sanitize(marked.parse(text ?? '', { async: false }) as string)
}

/** 带判断的解析：大文本走 Worker（异步），小文本主线程同步。 */
export function renderMarkdownSmart(text: string): Promise<string> {
  if (!text) return Promise.resolve('')
  if (text.length < WORKER_THRESHOLD) return Promise.resolve(renderMarkdown(text))
  const id = ++seq
  return new Promise((resolve) => {
    pending.set(id, (html) => resolve(sanitize(html)))
    getWorker().postMessage({ id, text })
  })
}
