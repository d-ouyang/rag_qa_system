/**
 * 知识库 store：文档列表 + 解析状态轮询 + 向量库统计 + 上传/重试/删除/下载。
 *
 * ===========================================================================
 * 为什么轮询放在 store 里，而不是视图的 onMounted
 * ===========================================================================
 * 后端从 P0-3a 起是异步解析：「提交」与「解析完成」之间隔着一个后台进程，
 * 中间只能靠**轮询** `status` 知道进度。
 *
 * 如果轮询写在 `KnowledgeView.vue` 的 `onMounted` 里，会有两个问题：
 *
 * 1. **切走就断**：`App.vue` 用 `v-if` 渲染视图，切到会话页会**卸载**组件，
 *    轮询随之停止 —— 于是「关掉页面对后台任务没影响」这件事在前端变成了
 *    「切走页面对后台任务没影响，但你也永远不会知道它什么时候跑完」。
 * 2. **完成提示会丢**：用户在等一个几十秒的大文件，顺手去聊天页看一眼，
 *    回来后才发现早解析完了 —— 或者更糟，回来时看到的是上一次的快照。
 *
 * 放 store 里则天然解决：Pinia store 是单例，视图来去不影响它。
 * 用户在聊天页也能收到「「年报.pdf」解析完成，共 42 个片段」的提示。
 *
 * ===========================================================================
 * 「只轮询该轮询的」——三条自我限制
 * ===========================================================================
 * 一个不做限制的轮询器就是一台持续烧服务的机器。这里有三道闸：
 *
 * 1. **没有非终态文档就不轮询**（`inFlightCount === 0` 时直接停）。
 *    知识库全部解析完成后，页面完全安静；
 * 2. **页面不可见时跳过这一轮**（`document.hidden`）。后台标签页每 2 秒发一次
 *    请求是纯浪费；回到前台时立刻补一次同步，用户不会看到过期状态；
 * 3. **失败退避**：连续失败时把间隔指数拉长（2s → 4s → … → 30s 封顶）。
 *    后端挂了的时候，前端不该以 2 秒一次的频率去敲一扇已经锁了的门。
 *
 * 另外用**递归 setTimeout** 而不是 `setInterval`：后者在请求比间隔还慢时
 * 会叠发（上一发还没回来就发下一发），把「慢」放大成「越来越慢」。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as docsApi from '@/api/documents'
import type { DocStatusCounts, KnowledgeDoc, ParseQueueStatus, VectorStats } from '@/types'
import { useUiStore } from './ui'

/** 轮询间隔。2 秒：能让「排队 → 解析中 → 完成」肉眼连贯；这个接口只读一行 MySQL，很便宜 */
const POLL_INTERVAL_MS = 2000

/** 失败退避的上限。再久就该由用户手动刷新，而不是让页面替他一直等 */
const POLL_MAX_INTERVAL_MS = 30_000

/**
 * 解析链路状态（队列 + worker 存活）每多少个 tick 拉一次。
 *
 * 为什么降频：`GET /api/v1/system/queue` 在**服务端要阻塞约 1 秒**
 * （worker 存活探测走 `inspect().ping()`，必须等满一个超时窗口 —— 见 P0-3a 迭代文档 §3.12）。
 * 如果跟文档列表同频（每 2 秒一次），服务端就有一半时间在等 Worker 应答，
 * 而这些信息本来也不需要秒级新鲜度（worker 掉线不会在两秒内自己恢复）。
 */
const QUEUE_EVERY_N_TICKS = 6

/** 终态提示里的失败原因截断长度（表格里仍展示全文） */
const TOAST_REASON_MAX = 60

/** 非终态 = 「还在动」= 值得继续轮询 */
function isInFlight(d: KnowledgeDoc): boolean {
  return d.status === 'pending' || d.status === 'parsing'
}

export const useDocumentStore = defineStore('documents', () => {
  const documents = ref<KnowledgeDoc[]>([])
  /** 全库状态计数（服务端算的，不受列表 limit 影响） */
  const counts = ref<DocStatusCounts | null>(null)
  const totalChunks = ref(0)
  const stats = ref<VectorStats | null>(null)
  /** 解析链路运行状态；拿不到时为 null（运维信息不该打断业务） */
  const queue = ref<ParseQueueStatus | null>(null)

  /** 首次加载 / 手动刷新时为 true（会显示「加载中…」）；后台轮询**不会**置它，否则表格每 2 秒闪一次 */
  const loading = ref(false)
  /** 正在提交上传（仅 HTTP 那一下，通常几十毫秒） */
  const submitting = ref(false)
  const submittingName = ref('')
  /** 是否正在后台轮询（用于界面上的小圆点） */
  const polling = ref(false)

  let timer: ReturnType<typeof setTimeout> | null = null
  let tick = 0
  let failures = 0
  /**
   * 「我们正在盯着的」doc_id。
   *
   * 只有进过这个集合的文档，其终态才会弹提示 —— 否则第一次打开知识库时
   * 会把历史里所有已成功的文档逐个提示一遍（十几条 toast 同时糊在屏幕上）。
   * 上传/重试时立刻加入，所以「上传后太快、第一次轮询就已是 success」也能提示到。
   */
  const watched = new Set<number>()

  const inFlightCount = computed(() => documents.value.filter(isInFlight).length)

  // ------------------------------------------------------------------ 取数
  /**
   * 拉一次列表（+ 统计）。
   *
   * `silent` = 后台轮询：不置 `loading`（避免表格闪「加载中」），也不把异常抛给调用方。
   */
  async function fetchList(silent: boolean): Promise<boolean> {
    if (!silent) loading.value = true
    try {
      const [list, s] = await Promise.all([
        docsApi.listDocuments({ limit: 1000 }),
        docsApi.getVectorStats(),
      ])
      announceTransitions(list.documents)
      documents.value = list.documents
      counts.value = list.counts
      totalChunks.value = list.total_chunks
      stats.value = s
      return true
    } catch (e) {
      // 后台轮询失败不该弹 toast（用户没做任何操作，弹了也不知道该干什么），
      // 交给上面的失败退避处理；首次加载失败才需要告诉用户。
      if (!silent) {
        const ui = useUiStore()
        ui.toast(e instanceof Error ? e.message : '读取知识库失败', 'error', 5000)
      }
      return false
    } finally {
      if (!silent) loading.value = false
    }
  }

  /** 拉一次解析链路状态。失败只记 warn —— 拿不到运维信息不该变成一次业务报错 */
  async function fetchQueue(): Promise<void> {
    try {
      queue.value = await docsApi.getParseQueueStatus()
    } catch (e) {
      console.warn('读取解析链路状态失败（忽略）', e)
    }
  }

  // ------------------------------------------------------ 状态迁移 → 提示
  /**
   * 对比上一轮快照与新快照，对**我们盯着的**那些文档在进入终态时提示一次。
   *
   * 两条规则：
   *   · 还在动的文档 → 纳入观察。所以「刷新页面时正好有文档在解析」也能收到完成提示，
   *     这正是「关掉浏览器再打开，状态和进度都还在」在前端的落点；
   *   · 已经是终态的文档 → 只有**观察过**它才提示。否则第一次打开知识库时，
   *     历史里所有已成功的文档会各弹一条 toast，十几条同时糊在屏幕上。
   *
   * 提示只发一次：发出后立刻从 `watched` 移出，后续轮询不会再弹。
   */
  function announceTransitions(next: KnowledgeDoc[]): void {
    const ui = useUiStore()
    for (const d of next) {
      if (isInFlight(d)) {
        watched.add(d.doc_id)
        continue
      }
      if (!watched.has(d.doc_id)) {
        continue
      }
      watched.delete(d.doc_id)
      if (d.status === 'success') {
        ui.toast(`「${d.file_name}」解析完成，共 ${d.chunk_count} 个片段`, 'success')
      } else if (d.status === 'fail') {
        ui.toast(`「${d.file_name}」解析失败：${truncate(d.fail_reason || '未知原因')}`, 'error', 8000)
      }
    }
  }

  function truncate(text: string): string {
    return text.length > TOAST_REASON_MAX ? `${text.slice(0, TOAST_REASON_MAX)}…` : text
  }

  // ------------------------------------------------------------ 轮询调度
  /** 页面是否可见。沙箱/测试环境下 `document` 可能不全，所以做能力判断 */
  function pageHidden(): boolean {
    return typeof document !== 'undefined' && document.hidden === true
  }

  function currentInterval(): number {
    if (failures === 0) return POLL_INTERVAL_MS
    return Math.min(POLL_INTERVAL_MS * 2 ** failures, POLL_MAX_INTERVAL_MS)
  }

  /**
   * 按需启动轮询。**幂等**：已经有定时器就什么都不做，
   * 所以可以放心地在每次刷新、每次上传之后调用。
   */
  function ensurePolling(): void {
    if (timer !== null) return
    if (inFlightCount.value === 0) {
      polling.value = false
      return
    }
    polling.value = true
    timer = setTimeout(runTick, currentInterval())
  }

  /**
   * 只清掉定时器句柄，**不动 `polling`**。
   *
   * 每一轮 `syncNow()` 开头都要清掉旧句柄（否则会和新句柄叠成两个轮询），
   * 但它紧接着又会重新排一个 —— 若在这里顺手把 `polling` 置 false，
   * 界面上那个「正在自动刷新」的圆点就会以轮询周期为频率闪烁。
   * 「停掉这个定时器」和「停止轮询这件事」是两回事，分开写。
   */
  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  /** 真正停止轮询：清句柄 + 灭指示灯 */
  function stopPolling(): void {
    clearTimer()
    polling.value = false
  }

  async function runTick(): Promise<void> {
    timer = null
    // 页面不可见：跳过这一轮请求，但保留轮询（回到前台会立刻补一次）
    if (pageHidden()) {
      ensurePolling()
      return
    }
    await syncNow(true)
  }

  /**
   * 跑一轮同步。所有取数路径都汇到这里，只有一处维护
   * 「失败计数 / 队列降频 / 要不要继续轮询」这三件事。
   *
   * `silent` = 后台轮询（不显示加载态、不弹错）。手动刷新与首次加载传 false。
   */
  async function syncNow(silent: boolean): Promise<void> {
    clearTimer()
    tick += 1
    const ok = await fetchList(silent)
    failures = ok ? 0 : failures + 1
    // 队列状态低频拉取：第 1 轮也拉（用户刚上传完，最需要看到「worker 在不在」）
    if (tick % QUEUE_EVERY_N_TICKS === 1) void fetchQueue()
    ensurePolling()
  }

  // 回到前台时补一次同步：否则用户看到的是切走前那一瞬间的快照
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
      if (!pageHidden() && inFlightCount.value > 0) void syncNow(true)
    })
  }

  // ---------------------------------------------------------------- 动作
  /** 首次进入 / 手动刷新：显示加载态 */
  async function refresh(): Promise<void> {
    await syncNow(false)
  }

  /** 单批文件数上限（与后端 BATCH_UPLOAD_MAX_FILES 一致；前端先拦一道，省一趟请求） */
  const BATCH_MAX_FILES = 50

  /**
   * 上传（p1.5c 起支持批量）：单个 File 或多文件/文件夹选出的 File 列表。
   *
   * 单个与批量统一走后端批量接口 —— 一个口径，不出现「单个走 A 接口、
   * 多个走 B 接口」两套行为漂移。后端逐份独立受理，单份失败不拖垮整批。
   */
  async function upload(files: File | File[]): Promise<void> {
    const ui = useUiStore()
    const list = Array.isArray(files) ? files : [files]
    if (list.length === 0) return
    if (list.length > BATCH_MAX_FILES) {
      ui.toast(`一次最多提交 ${BATCH_MAX_FILES} 份文件（这次选了 ${list.length} 份），请分批`, 'error', 6000)
      return
    }
    submitting.value = true
    submittingName.value = list.length === 1 ? list[0].name : `${list.length} 份文件`
    try {
      const res = await docsApi.uploadDocumentsBatch(list)
      // 受理成功的立刻纳入「盯着」的集合：万一后端起得快，第一次轮询就已经是 success，
      // 这时仍然应该给一条完成提示，而不是静默。
      let queueFailed = 0
      for (const r of res.results) {
        if (r.ok && r.doc_id != null) {
          watched.add(r.doc_id)
          if (!r.queued) queueFailed += 1
        }
      }
      if (res.accepted > 0) {
        // 用 info 而不是 success：**受理 ≠ 完成**，这时候说「成功」是在骗用户
        const first = res.results.find((r) => r.ok)
        ui.toast(
          res.accepted === 1 && first
            ? `「${first.file_name}」已提交，正在后台解析`
            : `已提交 ${res.accepted} 份文档，正在后台解析`,
          'info',
        )
      }
      // 已入库但没排上队：必须显眼，否则这些文档会一直停在 pending 而用户不知道原因
      if (queueFailed > 0) {
        ui.toast(
          `${queueFailed} 份已保存但排队失败（消息队列不可用），可稍后在列表里点「重试」补投`,
          'error',
          8000,
        )
      }
      // 被跳过的（类型不支持/超限/空文件）没进列表，toast 是唯一的告知途径：
      // 列出前几份的名字与原因，剩下的给个数
      if (res.skipped > 0) {
        const failed = res.results.filter((r) => !r.ok)
        const head = failed.slice(0, 3).map((r) => `${r.file_name}（${r.error}）`).join('；')
        const more = failed.length > 3 ? ` 等 ${failed.length} 份` : ''
        ui.toast(`${res.skipped} 份被跳过：${head}${more}`, 'error', 9000)
      }
      await refresh()
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : '上传失败', 'error', 5000)
    } finally {
      submitting.value = false
      submittingName.value = ''
    }
  }

  async function retry(doc: KnowledgeDoc): Promise<void> {
    const ui = useUiStore()
    try {
      const res = await docsApi.reparseDocument(doc.doc_id)
      watched.add(doc.doc_id)
      if (res.queued) {
        ui.toast(`「${doc.file_name}」已重新排队解析`, 'info')
      } else {
        ui.toast(`「${doc.file_name}」重新排队失败：${res.detail ?? '消息队列不可用'}`, 'error', 8000)
      }
      await refresh()
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : '重试失败', 'error', 5000)
    }
  }

  async function remove(doc: KnowledgeDoc): Promise<void> {
    const ui = useUiStore()
    try {
      const res = await docsApi.deleteDocument(doc.doc_id)
      watched.delete(doc.doc_id)
      const name = res.file_name ?? doc.file_name
      if (res.record_removed) {
        ui.toast(`已删除「${name}」，清理 ${res.deleted_chunks} 个片段`, 'success')
      } else {
        // 幂等：删一个已经不存在的文档不算失败，但要如实说清「没删到东西」
        ui.toast(`「${name}」已不存在（可能已被删除）`, 'info')
      }
      // 磁盘文件没删掉是「不可再生资源」的残留，值得单独提醒
      if (!res.file_removed) {
        ui.toast(`「${name}」的磁盘文件未能删除（已保留记录外的残留，可查看后端日志）`, 'error', 8000)
      }
      await refresh()
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : '删除失败', 'error', 5000)
    }
  }

  /**
   * 下载原文件。
   *
   * 必须走 fetch（见 http.ts 的 getFile）：`<a href>` 带不上 Authorization 头。
   * 拿到 blob 后用临时 object URL 触发保存，并**立刻 revoke** ——
   * 不 revoke 的话每个下载过的文件都会一直被浏览器持有到页面关闭。
   */
  async function download(doc: KnowledgeDoc): Promise<void> {
    const ui = useUiStore()
    let url = ''
    try {
      const { blob, filename } = await docsApi.downloadDocument(doc.doc_id)
      url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      // 后端已按 RFC 5987 给了原始文件名（含中文），这里是它没给时的兜底
      a.download = filename || doc.file_name
      document.body.appendChild(a)
      a.click()
      a.remove()
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : '下载失败', 'error', 5000)
    } finally {
      if (url) URL.revokeObjectURL(url)
    }
  }

  /** 登出时调用：停掉轮询并清空数据（与分析链路的 resetAll 同一个契约） */
  function reset(): void {
    stopPolling()
    watched.clear()
    documents.value = []
    counts.value = null
    totalChunks.value = 0
    stats.value = null
    queue.value = null
    tick = 0
    failures = 0
  }

  return {
    documents,
    counts,
    totalChunks,
    stats,
    queue,
    loading,
    submitting,
    submittingName,
    polling,
    inFlightCount,
    refresh,
    upload,
    retry,
    remove,
    download,
    reset,
  }
})
