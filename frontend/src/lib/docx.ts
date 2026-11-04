// docx → 消毒后的 HTML（转换在 Worker，消毒在主线程）。
import DOMPurify from 'dompurify'

interface Pending {
  resolve: (html: string) => void
  reject: (e: Error) => void
}

let worker: Worker | null = null
let seq = 0
const pending = new Map<number, Pending>()

function getWorker(): Worker {
  if (!worker) {
    worker = new Worker(new URL('./docx.worker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = (e: MessageEvent<{ id: number; html: string; error?: string }>) => {
      const p = pending.get(e.data.id)
      if (!p) return
      pending.delete(e.data.id)
      if (e.data.error) p.reject(new Error(e.data.error))
      else p.resolve(DOMPurify.sanitize(e.data.html))
    }
  }
  return worker
}

/** ArrayBuffer 会被转移给 Worker（调用方不再需要它）。 */
export function docxToHtml(buffer: ArrayBuffer): Promise<string> {
  const id = ++seq
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    getWorker().postMessage({ id, buffer }, [buffer])
  })
}
