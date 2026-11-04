// docx.worker.ts —— .docx → HTML 放 Worker（mammoth 纯 JS，不依赖 DOM）。
// 只做转换、不做消毒：HTML 回到主线程后由 docx.ts 用 DOMPurify 消毒。
import * as mammoth from 'mammoth'

self.onmessage = async (e: MessageEvent<{ id: number; buffer: ArrayBuffer }>) => {
  const { id, buffer } = e.data
  try {
    const result = await mammoth.convertToHtml({ arrayBuffer: buffer })
    ;(self as unknown as Worker).postMessage({ id, html: result.value })
  } catch (err) {
    ;(self as unknown as Worker).postMessage({ id, html: '', error: String(err) })
  }
}
