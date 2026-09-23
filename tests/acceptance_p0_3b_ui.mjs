#!/usr/bin/env node
/**
 * P0-3b 前端验收：浏览器端到端跑一遍「上传 → 轮询 → 终态 → 重试 → 删除」。
 *
 * ===========================================================================
 * 它验的是什么（以及为什么必须用浏览器，而不是再写一遍 HTTP 断言）
 * ===========================================================================
 * P0-3a 已经把**后端**的异步链路验穿了（`tests/acceptance_p0_3a.py`，55 项）。
 * 所以这里不复测接口形状，只验**前端独有的那部分**：
 *
 *   1. 提交后能不能「看着它做」—— 状态列真的从 排队中 → 解析中 → 已完成 走一遍，
 *      而且中间态是**轮询**看来的（不是一次请求拿到最终结果）；
 *   2. 轮询放在 store 里而不是视图的 onMounted 里，到底有没有用 ——
 *      切到会话页（视图被 v-if 卸载）之后，还能不能收到「解析完成」的提示；
 *   3. 按钮状态机是否按 status 置灰（成功的不许重试、没片段的不许看片段）；
 *   4. 失败原因是否**可读地对人展示**，以及「重试」能不能真的把文档送回队列。
 *
 * 这些都是 DOM 上的行为，HTTP 断言里看不见。
 *
 * ===========================================================================
 * 两处刻意的「桩」，以及为什么用桩
 * ===========================================================================
 * A. 「解析进程未检测到」的告警条：需要 worker 真的停着。而本脚本**不该去杀
 *    用户的 worker**（杀完还得负责起回来，一旦脚本中断就把环境留成半死状态）。
 *    所以这里用 `page.route` 把 `/api/v1/system/queue` 的响应改成 worker.ok=false，
 *    只验**前端渲染分支**。后端「没 worker 时确实报 ok=false」由 P0-3a 的
 *    验收脚本与 module9 覆盖 —— 两边合起来才是完整证据。
 *
 * B. 待解析文档 + 上述告警条同时成立：告警的前提是「有文档在排队 + 没有 worker」，
 *    用真 worker 时这两个条件会互相抵消（worker 在，它就把任务领走了）。
 *    所以这一步同时 stub 了列表接口，注入一条 pending 记录。
 *
 * 除这两处外，其余全部走真链路：真 MySQL / 真 Redis / 真 Worker / 真向量库 /
 * 真 NestJS 网关（也就真鉴权）。
 *
 * ===========================================================================
 * 前置（四端都要在，缺一个就会有一组断言失败）
 * ===========================================================================
 *   make infra && make db-upgrade
 *   make api      # FastAPI  8000
 *   make gateway  # NestJS   3000
 *   make worker   # Celery   （不起它，文档会一直停在「排队中」）
 *   make frontend # Vite     5173
 *
 * 运行：
 *   NODE_PATH=$(npm root -g) node tests/acceptance_p0_3b_ui.mjs
 * 可用环境变量：
 *   UI_BASE   默认 http://localhost:5173
 *   SHOT_DIR  截图目录，默认 /tmp/p03b_shots
 *   HEADFUL=1 开有头浏览器（自己看一遍时用）
 */

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

// ESM 不认 NODE_PATH，而 playwright 装在全球目录下 —— 用 CJS require 借道。
function loadPlaywright() {
  const tries = []
  for (const spec of ['playwright', 'playwright-core']) {
    try {
      return require(spec)
    } catch (e) {
      tries.push(`${spec}: ${e.code ?? e.message}`)
    }
  }
  console.error('❌ 找不到 playwright。请用：NODE_PATH=$(npm root -g) node ' + import.meta.filename)
  console.error('   已尝试：' + tries.join(' / '))
  process.exit(2)
}

const { chromium } = loadPlaywright()

// ------------------------------------------------------------------ 配置
const BASE = process.env.UI_BASE ?? 'http://localhost:5173'
const API = process.env.API_BASE ?? 'http://127.0.0.1:3000' // 网关（登录也走它）
const SHOT_DIR = process.env.SHOT_DIR ?? '/tmp/p03b_shots'
const HEADLESS = process.env.HEADFUL !== '1'
const USER = process.env.ADMIN_USER ?? 'admin'
const PASS = process.env.ADMIN_PASS ?? 'admin123'

/** 本轮唯一后缀：让夹具文件名不与历史残留撞车，也方便出错后善后 */
const RUN = Math.random().toString(16).slice(2, 8)
const FIX_DIR = '/tmp'
const GOOD = { name: `p03b_轮询验证_${RUN}.md`, file: path.join(FIX_DIR, `p03b_轮询验证_${RUN}.md`) }
const SWAP = { name: `p03b_切页验证_${RUN}.md`, file: path.join(FIX_DIR, `p03b_切页验证_${RUN}.md`) }
const BAD = { name: `p03b_坏文档_${RUN}.docx`, file: path.join(FIX_DIR, `p03b_坏文档_${RUN}.docx`) }
const ALL_NAMES = [GOOD.name, SWAP.name, BAD.name]

// 时间预算：首次解析要加载嵌入模型（本机 CPU，冷启动可达几十秒）
const T_FIRST_PARSE = 180_000
const T_PARSE = 120_000
const T_UI = 20_000

// ------------------------------------------------------------------ 断言
let pass = 0
const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    pass += 1
    console.log(`  [PASS] ${name}`)
  } else {
    failures.push(name + (extra ? `  ← ${extra}` : ''))
    console.log(`  [FAIL] ${name}${extra ? `  ← ${extra}` : ''}`)
  }
}
function criterion(text) {
  console.log(`\n—— ${text}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * 轮询等待：返回命中的值，超时返回 null（**不抛** —— 让断言统一在 check 里判，
 * 超时时还能打印「到底见过哪些状态」，比一个 TimeoutError 有用得多）。
 */
async function tryWait(fn, timeout, every = 400) {
  const t0 = Date.now()
  let lastError = null
  while (Date.now() - t0 < timeout) {
    try {
      const v = await fn()
      if (v) return v
    } catch (e) {
      lastError = e
    }
    await sleep(every)
  }
  if (lastError) console.log(`     （等待期间最后一次异常：${lastError.message}）`)
  return null
}

// ------------------------------------------------------------------ 夹具
function writeFixtures() {
  // 3000+ 字 × chunk_size 500 ⇒ 稳定切成多片，「片段数 > 1」这条断言才有意义
  const sections = []
  for (let i = 1; i <= 8; i += 1) {
    sections.push(
      `## 第 ${i} 节：解析链路的行为约定\n\n` +
        `本节用于验证异步解析链路的切片与入库行为。企业知识库的核心指标之一，是` +
        `「一份文档被切成多少个片段」——因为检索、引用、喂给模型的都是片段，不是原文档。` +
        `片段切得越粗，单次召回的上下文越杂；切得越细，跨段落的语义就越容易被割裂。` +
        `因此切片参数（chunk_size 与 chunk_overlap）属于**知识库级别的约定**，` +
        `一旦改动，历史文档与新文档就会落在两套口径上。第 ${i} 节的重复是刻意的：` +
        `它保证这里必然产生多个片段，而不是只有一段。段落编号 ${i}-${i}${i} 仅用于区分。\n\n`,
    )
  }
  fs.writeFileSync(GOOD.file, `# P0-3b 前端轮询验收文档（${RUN}）\n\n${sections.join('')}`, 'utf8')
  fs.writeFileSync(SWAP.file, `# P0-3b 切页轮询验收文档（${RUN}）\n\n${sections.join('')}`, 'utf8')

  // 坏文件：后缀在白名单里（.docx），内容是垃圾 —— 走得到解析那一步，才可能失败
  fs.writeFileSync(BAD.file, Buffer.from('这不是一个真的 docx，只是一串字节。'.repeat(40), 'utf8'))

  for (const f of [...ALL_NAMES]) {
    const p = path.join(FIX_DIR, f)
    console.log(`  夹具 ${p}  ${fs.statSync(p).size} 字节`)
  }
}

// ------------------------------------------------- 善后（对齐 P0-3a 的做法）
/** 直接走 HTTP 清干净，不依赖浏览器还活着 —— 脚本中途崩了也不留脏数据 */
async function cleanupViaApi() {
  const base = API
  try {
    const login = await fetch(`${base}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: USER, password: PASS }),
    })
    const token = (await login.json()).access_token
    if (!token) return
    const list = await (
      await fetch(`${base}/api/v1/documents/?limit=1000`, { headers: { Authorization: `Bearer ${token}` } })
    ).json()
    for (const d of list.documents ?? []) {
      if (!ALL_NAMES.includes(d.file_name)) continue
      await fetch(`${base}/api/v1/documents/${d.doc_id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      console.log(`  善后：已删除 doc_id=${d.doc_id} ${d.file_name}`)
    }
  } catch (e) {
    console.log(`  ⚠️ 善后失败（不影响结论，但请手工确认 upload/ 无残留）：${e.message}`)
  }
}

function removeFixtureFiles() {
  for (const n of ALL_NAMES) {
    const p = path.join(FIX_DIR, n)
    if (fs.existsSync(p)) fs.unlinkSync(p)
  }
}

// ------------------------------------------------------------------ 主流程
let browser
let page
const consoleErrors = []
const pageErrors = []
const shot = []

async function snap(name) {
  const p = path.join(SHOT_DIR, `${name}.png`)
  await page.screenshot({ path: p, fullPage: true })
  shot.push(p)
  return p
}

/** 读出页面上记录过的全部 toast 文案（由 addInitScript 注入的收集器维护） */
const readToasts = () => page.evaluate(() => window.__toastLog ?? [])
const countToasts = async (needle) => (await readToasts()).filter((t) => t.includes(needle)).length

/** 某个文件名对应的表格行 */
const rowOf = (name) => page.locator('.table-row').filter({ hasText: name })

/** 行的状态（'pending' | 'parsing' | 'success' | 'fail' | null） */
async function rowStatus(name) {
  const row = rowOf(name)
  if ((await row.count()) === 0) return null
  const el = row.first().locator('.status')
  if ((await el.count()) === 0) return null
  return el.first().getAttribute('data-s')
}

const OP = ['片段', '下载', '重试', '删除']
const opBtn = (name, label) =>
  rowOf(name).first().locator('.op-btn').nth(OP.indexOf(label))

async function opDisabled(name, label) {
  return opBtn(name, label).isDisabled()
}

async function main() {
  fs.mkdirSync(SHOT_DIR, { recursive: true })
  writeFixtures()
  console.log(`\n运行后缀 RUN=${RUN}\n截图目录 ${SHOT_DIR}\n`)

  // --no-proxy-server：本机有 Clash，Chromium 对 localhost 一般会自动绕过代理，
  // 显式关掉可以免掉「因代理导致的 502」这种与代码无关的干扰（本项目的已知坑 #2）。
  browser = await chromium.launch({ headless: HEADLESS, args: ['--no-proxy-server'] })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    acceptDownloads: true,
    locale: 'zh-CN',
  })
  page = await context.newPage()
  page.setDefaultTimeout(T_UI)

  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const text = m.text()
    // 只有 favicon 的 404 是噪声（index.html 没放图标），其余一律记账
    if (text.includes('favicon')) return
    consoleErrors.push(text)
  })
  page.on('pageerror', (e) => pageErrors.push(String(e)))
  page.on('dialog', (d) => d.accept()) // 删除确认框

  // toast 收集器：只在 DOM 上「有没有这一条」是不够的 —— 提示 3.2 秒就消失，
  // 断言晚一步就抓不到了。这里持续记录**每一个出现过的新元素**，事后随时可查。
  //
  // ⚠️ 记的是「元素」不是「文案」：同一份文档重复失败两次，两次提示的文字**一模一样**，
  //    按文案去重会把第二次吞掉，于是「重试真的又跑了一遍」这条断言会假失败
  //    （首轮就踩到了：`1 → 1`）。用 WeakSet 认元素，同一节点只记一次，
  //    文案重复则如实记两条。同时另存一份去重文案，给「有没有出现过某句话」用。
  await page.addInitScript(() => {
    window.__toastLog = [] // 按顺序，一次一条（允许重复文案）
    window.__toasts = [] // 去重后的文案，便于 `includes` 判断
    const seen = new WeakSet()
    const collect = () => {
      document.querySelectorAll('.toast').forEach((el) => {
        if (seen.has(el)) return
        seen.add(el)
        const t = (el.textContent ?? '').trim()
        if (!t) return
        window.__toastLog.push(t)
        if (!window.__toasts.includes(t)) window.__toasts.push(t)
      })
    }
    const boot = () => {
      collect()
      new MutationObserver(collect).observe(document.body, { childList: true, subtree: true })
    }
    if (document.body) boot()
    else document.addEventListener('DOMContentLoaded', boot)
  })

  // =========================================================== 1. 登录
  criterion('1. 登录并进入知识库')
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.fill('input[type=text]', USER)
  await page.fill('input[type=password]', PASS)
  await snap('01-login')
  await page.click('button.submit')
  const shell = await tryWait(async () => (await page.locator('.app-shell').count()) === 1, T_UI)
  check('登录后进入主界面（出现侧边栏）', shell)
  if (!shell) {
    console.log('  登录失败，后续无法进行。')
    return
  }

  await page.getByRole('button', { name: '文件传输 · 知识库' }).click()
  const view = await tryWait(async () => (await page.locator('.knowledge-view').count()) === 1, T_UI)
  check('切换到知识库视图', view)
  check('页面标题为「知识库管理」', (await page.locator('.page-header h2').innerText()) === '知识库管理')

  // =========================================================== 2. 初始状态
  criterion('2. 初始状态与链路状态条')
  const loaded = await tryWait(
    async () => (await page.locator('.empty-state', { hasText: '加载中' }).count()) === 0,
    T_UI,
  )
  check('列表加载完成（不再显示「加载中…」）', loaded)

  const chips = await page.locator('.chain-bar .chip').count()
  check('链路状态条有 4 个状态计数格', chips === 4, `实际 ${chips}`)

  const workerText = await page.locator('.chain-bar .worker').innerText()
  check(
    '后端报告解析进程在线（worker 真在跑）',
    workerText.includes('在线'),
    workerText.replace(/\s+/g, ' '),
  )
  const label = await page.locator('.chip[data-k="success"]').innerText()
  check('「已完成」计数格文案正确', /已完成\s*\d+/.test(label.replace(/\s+/g, ' ')), label)
  await snap('02-knowledge-empty')

  // ================================================== 3. 上传 → 轮询 → 完成
  criterion('3. 上传后按 status 轮询，直到完成（核心）')
  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.locator('.dropzone').click(),
  ])
  check('点击上传区唤起文件选择框', Boolean(chooser))
  await chooser.setFiles(GOOD.file)

  const appeared = await tryWait(async () => (await rowOf(GOOD.name).count()) > 0, T_UI)
  check('上传的文档出现在列表里', appeared)

  const acceptedToast = await tryWait(
    async () => (await countToasts('已提交，正在后台解析')) > 0,
    T_UI,
  )
  check('提交后提示「已提交，正在后台解析」（不是「入库成功」）', acceptedToast)
  await snap('03-uploaded')

  // 状态流转：每 400ms 采一次，记录**见过的全部状态**。
  // 只看最终值是不够的 —— 若前端其实是一次性拿结果，中间态就永远不会出现。
  const seen = new Set()
  const t0 = Date.now()
  let finished = false
  while (Date.now() - t0 < T_FIRST_PARSE) {
    const s = await rowStatus(GOOD.name)
    if (s) seen.add(s)
    if (s === 'success') {
      finished = true
      break
    }
    if (s === 'fail') break
    await sleep(400)
  }
  const seenList = [...seen].join(' → ')
  check('状态最终为「已完成」', finished, `轨迹：${seenList}`)
  check(
    '过程中观察到过中间态（证明是轮询出来的，而非一次拿结果）',
    seen.has('pending') || seen.has('parsing'),
    `轨迹：${seenList}`,
  )
  check('轨迹里出现过「已提交但未完成」的状态', seen.has('parsing') || seen.has('pending'), seenList)

  const chunksCell = (await rowOf(GOOD.name).first().locator('.col-chunks').innerText()).trim()
  const chunkN = Number(chunksCell)
  check('片段数 > 0', Number.isFinite(chunkN) && chunkN > 0, `表格里是「${chunksCell}」`)

  const doneToast = await tryWait(async () => (await countToasts('解析完成，共')) > 0, T_UI)
  const texts = await readToasts()
  const doneText = texts.find((t) => t.includes('解析完成，共')) ?? ''
  check('收到「解析完成，共 N 个片段」提示', doneToast, JSON.stringify(texts))
  check(
    '提示里的片段数与表格一致',
    doneText.includes(`共 ${chunksCell} 个片段`),
    `toast=${doneText} / 表格=${chunksCell}`,
  )
  await snap('04-success')

  // 按钮状态机
  check('完成后「片段」按钮可用', !(await opDisabled(GOOD.name, '片段')))
  check('完成后「重试」按钮置灰（成功不支持重解析）', await opDisabled(GOOD.name, '重试'))
  check('完成后「下载」按钮可用', !(await opDisabled(GOOD.name, '下载')))
  const retryTip = await opBtn(GOOD.name, '重试').getAttribute('title')
  check('置灰的「重试」给出了原因（不是无声禁用）', /已解析成功/.test(retryTip ?? ''), retryTip ?? '')

  // 手动刷新一次：终态文档不该再弹一遍提示
  const beforeRefresh = (await readToasts()).length
  await page.click('.refresh-btn')
  await sleep(1500)
  const afterRefresh = (await readToasts()).length
  check('刷新一次不会重复弹终态提示', afterRefresh === beforeRefresh, `${beforeRefresh} → ${afterRefresh}`)

  // ============================ 4. 切走页面，轮询仍继续（store 而非视图）
  criterion('4. 切到会话页（知识库视图被卸载）后，仍能收到完成提示')
  const [chooser2] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.locator('.dropzone').click(),
  ])
  await chooser2.setFiles(SWAP.file)
  await tryWait(async () => (await rowOf(SWAP.name).count()) > 0, T_UI)

  // 立刻切走：App.vue 用 v-if，KnowledgeView 会被**卸载**。
  // 用它自己的「返回会话」按钮（而不是点侧边栏某个会话）—— 一个会卸载自己的按钮，
  // 是最直接的那条路径。
  await page.locator('.knowledge-view .page-header button').click()
  await tryWait(async () => (await page.locator('.knowledge-view').count()) === 0, T_UI)
  const unmounted = (await page.locator('.knowledge-view').count()) === 0
  check('切走后知识库视图确实被卸载（v-if 生效）', unmounted)

  let toastWhileGone = false
  const t1 = Date.now()
  while (Date.now() - t1 < T_PARSE) {
    const list = await readToasts()
    if (list.some((t) => t.includes(SWAP.name) && t.includes('解析完成，共'))) {
      toastWhileGone = (await page.locator('.knowledge-view').count()) === 0
      break
    }
    await sleep(400)
  }
  check('视图不在时收到了它的完成提示', toastWhileGone)
  await snap('05-toast-in-chat-view')

  await page.getByRole('button', { name: '文件传输 · 知识库' }).click()
  await tryWait(async () => (await page.locator('.knowledge-view').count()) === 1, T_UI)
  const swapStatus = await tryWait(async () => {
    const s = await rowStatus(SWAP.name)
    return s === 'success' ? s : null
  }, T_UI)
  check('切回知识库，状态已是「已完成」（离开期间没有停在旧快照）', swapStatus === 'success')

  // ==================================================== 5. 失败与重试
  criterion('5. 损坏文件 → 失败可读 → 前端能重试')
  const [chooser3] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.locator('.dropzone').click(),
  ])
  await chooser3.setFiles(BAD.file)

  const badDone = await tryWait(async () => {
    const s = await rowStatus(BAD.name)
    return s === 'fail' || s === 'success' ? s : null
  }, T_FIRST_PARSE)
  check('损坏文件最终落到「失败」（而不是一直转圈）', badDone === 'fail', `实际 ${badDone}`)

  const failRow = page.locator('.fail-row').first()
  const failReason = (await failRow.locator('.fail-reason').innerText()).trim()
  check('失败原因整行展示出来了', failReason.length > 0)
  check('失败原因可读（中文、说明原因，不是堆栈）', /损坏|提取|失败/.test(failReason) && !failReason.includes('Traceback'), failReason)
  check('失败原因里带了文件名（知道是哪一份）', failReason.includes(BAD.name.slice(0, 12)), failReason)
  check('失败时「重试」按钮可用', !(await opDisabled(BAD.name, '重试')))
  check('失败时「片段」按钮置灰（没有切片可看）', await opDisabled(BAD.name, '片段'))
  const failToastN = await countToasts('解析失败：')
  check('收到失败提示（而不是静默）', failToastN >= 1, `${failToastN} 条`)
  await snap('06-fail')

  // 重试：真的把文档送回队列
  const beforeRetry = await readToasts()
  await opBtn(BAD.name, '重试').click()
  const requeued = await tryWait(async () => (await countToasts('已重新排队解析')) > 0, T_UI)
  check('点「重试」后提示「已重新排队解析」', requeued)

  const backToFlight = await tryWait(async () => {
    const s = await rowStatus(BAD.name)
    return s === 'pending' || s === 'parsing' ? s : null
  }, T_UI)
  check('重试后状态离开「失败」，回到排队/解析中', Boolean(backToFlight), `实际 ${backToFlight}`)

  const failedAgain = await tryWait(async () => {
    const s = await rowStatus(BAD.name)
    if (s !== 'fail') return null
    const n = await page.locator('.fail-row .fail-attempt').count()
    return n > 0 ? true : null
  }, T_PARSE)
  check('重试后再次失败，且显示「已尝试 2 次」', failedAgain)
  const attemptText = (await page.locator('.fail-row .fail-attempt').first().innerText()).trim()
  check('尝试次数文案为「已尝试 2 次」', /已尝试\s*2\s*次/.test(attemptText), attemptText)
  const afterRetry = await readToasts()
  check(
    '重试后又收到一次失败提示（第二次真的跑了）',
    afterRetry.filter((t) => t.includes('解析失败：')).length > failToastN,
    `${failToastN} → ${afterRetry.filter((t) => t.includes('解析失败：')).length}`,
  )
  await snap('07-retry-then-fail-again')

  // ==================================================== 6. 片段抽屉 / 下载
  criterion('6. 片段抽屉与下载')
  await opBtn(GOOD.name, '片段').click()
  const drawer = await tryWait(async () => (await page.locator('.chunk-drawer').count()) === 1, T_UI)
  check('「片段」打开详情抽屉', drawer)
  const drawerTitle = (await page.locator('.drawer-title h3').innerText()).trim()
  check('抽屉标题是文档名', drawerTitle === GOOD.name, drawerTitle)
  // ⚠️ 必须等：开抽屉时会异步去取片段，刚打开那一瞬列表还是空的（显示「读取片段中…」）。
  //    上一版在这里直接 `count()`，撞上竞态 → 数到 0。等「数够」再断言，顺手把
  //    「抽屉不藏片段」这条也一起验了（接口返回全文，不截断）。
  const cards = await tryWait(async () => {
    const n = await page.locator('.chunk-card').count()
    return n >= chunkN ? n : null
  }, T_UI)
  check(
    '抽屉里列出了全部片段（接口不截断，抽屉也不藏）',
    cards !== null,
    `表格 ${chunkN} / 抽屉 ${cards ?? 0}`,
  )
  const content = (await page.locator('.chunk-content').first().innerText()).trim()
  check('片段正文非空（模型看到的就是它）', content.length > 0, `${content.length} 字`)
  await snap('08-chunk-drawer')
  await page.click('.drawer-close')
  check(
    '抽屉能关掉',
    await tryWait(async () => (await page.locator('.chunk-drawer').count()) === 0, T_UI),
  )

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    opBtn(GOOD.name, '下载').click(),
  ])
  const dlName = download.suggestedFilename()
  const dlPath = await download.path()
  const dlSize = dlPath ? fs.statSync(dlPath).size : 0
  check('下载触发了保存（不是跳转到一个 401 页面）', Boolean(download))
  check('下载文件名保留原名（含中文）', dlName === GOOD.name, dlName)
  check('下载内容非空', dlSize > 0, `${dlSize} 字节`)

  // ============================= 7. worker 离线告警（前端渲染分支，stub）
  criterion('7. 「有文档在排队但解析进程不在」的告警（stub 后端响应，只验前端分支）')
  const fakeNow = new Date().toISOString().replace('T', ' ').slice(0, 19)
  // 用 URL 判定函数而不是 glob：glob 的 `?` 不是通配符，且 `**/documents/**` 会
  // 把 /stats、/{id}/chunks、/download 一起吞掉（那些响应里没有 documents 字段）。
  //
  // 匹配器与处理器都具名保存：`unroute` 要按**同一个函数引用**才能摘掉路由，
  // 另写一个等价的箭头函数是摘不掉的（会静默保留 stub，后面的断言就全假了）。
  const queueMatcher = (url) => url.pathname === '/api/v1/system/queue'
  const queueHandler = async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        queue: { name: 'rag.parse', broker_ok: true, depth: 3, unacked: 0, detail: '' },
        worker: {
          ok: false,
          workers: [],
          detail: '1 秒内没有 worker 应答（worker 未启动或已卡死）',
        },
        documents: { pending: 3, parsing: 0, success: 12, fail: 1, total: 16 },
        ping_timeout_seconds: 1.0,
      }),
    })
  }
  const listMatcher = (url) => url.pathname === '/api/v1/documents/'
  const listHandler = async (route) => {
    const res = await route.fetch()
    const body = await res.json()
    body.documents.push({
      doc_id: 999999,
      project_id: 'default',
      file_name: `p03b_等待解析的文档_${RUN}.pdf`,
      storage_path: `upload/p03b_等待解析的文档_${RUN}.pdf`,
      file_size: 20480,
      status: 'pending',
      chunk_count: 0,
      fail_reason: '',
      attempt_count: 1,
      upload_time: fakeNow,
      parse_started_at: null,
    })
    body.counts.pending += 1
    body.counts.total += 1
    await route.fulfill({
      status: res.status(),
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  }

  await page.route(queueMatcher, queueHandler)
  await page.route(listMatcher, listHandler)

  await page.click('.refresh-btn')
  const warn = await tryWait(async () => {
    const n = await page.locator('.chain-warn').count()
    return n > 0 ? (await page.locator('.chain-warn').innerText()).trim() : null
  }, T_UI)
  check('出现解析链路告警条', Boolean(warn), warn ?? '（没有出现）')
  check('告警说清了「有文档在等」与「没有进程在跑」', /等待解析/.test(warn ?? '') && /没有检测到解析进程/.test(warn ?? ''), warn ?? '')
  check('告警给出了可执行的下一步（启动 worker）', /make worker/.test(warn ?? ''), warn ?? '')
  const workerOffline = await page.locator('.chain-bar .worker').innerText()
  check('状态条里「解析进程」显示未检测到', workerOffline.includes('未检测到'), workerOffline.replace(/\s+/g, ' '))
  await snap('09-worker-offline-warning')

  await page.unroute(queueMatcher, queueHandler)
  await page.unroute(listMatcher, listHandler)
  await page.click('.refresh-btn')
  const warnGone = await tryWait(async () => (await page.locator('.chain-warn').count()) === 0, T_UI)
  check('恢复真实数据后告警消失（不是一旦出现就常驻）', warnGone)

  // ==================================================== 8. 删除（三处清干净）
  criterion('8. 删除：确认框 + 三处清理')
  for (const name of [BAD.name, SWAP.name, GOOD.name]) {
    const before = await rowOf(name).count()
    await opBtn(name, '删除').click()
    const gone = await tryWait(async () => (await rowOf(name).count()) === 0, T_UI)
    check(`删除「${name.slice(0, 14)}…」后行消失`, before === 1 && gone)
  }
  const delToast = await countToasts('已删除「')
  check('删除有提示（含清理的片段数）', delToast >= 3, `${delToast} 条`)

  await page.click('.refresh-btn')
  await sleep(1200)
  const emptyText = await page.locator('.doc-table').innerText()
  check('列表回到空态文案', /知识库为空/.test(emptyText), emptyText.replace(/\s+/g, ' ').slice(0, 80))
  await snap('10-empty-again')

  // ==================================================== 9. 控制台干净
  criterion('9. 控制台与页面错误')
  check('无 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('无未捕获的页面异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
}

// ------------------------------------------------------------------ 入口
try {
  await main()
} catch (e) {
  console.error(`\n💥 脚本异常终止：${e?.stack ?? e}`)
  failures.push(`脚本异常：${e?.message ?? e}`)
  if (page) {
    try {
      await snap('99-crash')
    } catch {
      /* 截图失败不影响结论 */
    }
  }
} finally {
  console.log('\n==== 善后 ====')
  await cleanupViaApi()
  removeFixtureFiles()
  console.log(`截图 ${shot.length} 张：`)
  for (const s of shot) console.log(`  ${s}`)
  if (browser) await browser.close()

  const total = pass + failures.length
  console.log(`\n==== 汇总：${pass} 通过 / ${failures.length} 失败（共 ${total} 项）====`)
  for (const f of failures) console.log(`  ✗ ${f}`)
  process.exit(failures.length === 0 ? 0 : 1)
}
