// xlsx → 每个工作表的字符串二维数组（解析在 Worker）。
export interface XlsxSheet {
  name: string
  rows: string[][]
  truncated: boolean
}

interface Pending {
  resolve: (sheets: XlsxSheet[]) => void
  reject: (e: Error) => void
}

let worker: Worker | null = null
let seq = 0
const pending = new Map<number, Pending>()

function getWorker(): Worker {
  if (!worker) {
    worker = new Worker(new URL('./xlsx.worker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = (e: MessageEvent<{ id: number; sheets: XlsxSheet[]; error?: string }>) => {
      const p = pending.get(e.data.id)
      if (!p) return
      pending.delete(e.data.id)
      if (e.data.error) p.reject(new Error(e.data.error))
      else p.resolve(e.data.sheets)
    }
  }
  return worker
}

/** ArrayBuffer 会被转移给 Worker（调用方不再需要它）。 */
export function xlsxToSheets(buffer: ArrayBuffer): Promise<XlsxSheet[]> {
  const id = ++seq
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    getWorker().postMessage({ id, buffer }, [buffer])
  })
}
