// xlsx.worker.ts —— .xlsx/.xls 解析放 Worker（SheetJS 纯 JS）。
// 返回每个工作表的「字符串二维数组」，主线程用 Vue 表格渲染（不注入 HTML，天然免消毒）。
import * as XLSX from 'xlsx'

const MAX_ROWS = 1000
const MAX_COLS = 50

export interface XlsxSheet {
  name: string
  rows: string[][]
  truncated: boolean
}

self.onmessage = (e: MessageEvent<{ id: number; buffer: ArrayBuffer }>) => {
  const { id, buffer } = e.data
  try {
    const wb = XLSX.read(new Uint8Array(buffer), { type: 'array' })
    const sheets: XlsxSheet[] = (wb.SheetNames || []).map((name) => {
      const ws = wb.Sheets[name]
      const raw = XLSX.utils.sheet_to_json<unknown[]>(ws, {
        header: 1,
        raw: false,
        defval: '',
      }) as unknown[][]
      const colTruncated = raw.some((r) => r.length > MAX_COLS)
      const truncated = raw.length > MAX_ROWS || colTruncated
      const rows = raw.slice(0, MAX_ROWS).map((r) =>
        r.slice(0, MAX_COLS).map((c) => (c == null ? '' : String(c))),
      )
      return { name, rows, truncated }
    })
    ;(self as unknown as Worker).postMessage({ id, sheets })
  } catch (err) {
    ;(self as unknown as Worker).postMessage({ id, sheets: [], error: String(err) })
  }
}
