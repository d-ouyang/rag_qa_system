// md.worker.ts —— markdown 解析放 Worker（大文本不卡主线程）。
// 只做解析、不做消毒：DOMPurify 依赖 DOM，放在主线程（md.ts）统一处理。
import { marked } from 'marked'

self.onmessage = (e: MessageEvent<{ id: number; text: string }>) => {
  const { id, text } = e.data
  try {
    const html = marked.parse(text, { async: false }) as string
    ;(self as unknown as Worker).postMessage({ id, html })
  } catch (err) {
    ;(self as unknown as Worker).postMessage({ id, html: '', error: String(err) })
  }
}
