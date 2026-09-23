#!/usr/bin/env node
/**
 * P0-4b 前端验收：浏览器端到端跑一遍「点引用 → 展开切片全文」。
 *
 * ===========================================================================
 * 它验的是什么（以及为什么必须用浏览器，而不是再写一遍 HTTP 断言）
 * ===========================================================================
 * P0-4a 已经把**后端**验穿了（`tests/acceptance_p0_4a.py`，41 项）：
 * chunk_id 契约、反查接口的四种返回、孤儿清理、重建前后一致。
 * 所以这里不复测接口形状，只验**前端独有的那部分**：
 *
 *   1. 引用条到底是不是**可点的**（有 chunk_id 时是按钮，没有时是纯文本）；
 *   2. 点开之后拿到的是**全文**而不是那 200 字摘要（这是它存在的全部意义）；
 *   3. 源文档信息展示出来了吗（文件名 / 第几片 / 字数 —— 都来自 MySQL）；
 *   4. 交互闭环：再点收起、一次只展开一条、展开过的不再重复请求；
 *   5. 后端说「查不到」时，界面展示的是不是后端那句人话。
 *
 * 这些都是 DOM 上的行为，HTTP 断言里看不见。
 *
 * ===========================================================================
 * 一处刻意的桩，以及为什么用桩
 * ===========================================================================
 * 「文档已被删除」这个分支需要真的删掉一份文档，而删掉之后**这条引用就再也
 * 问不出来了**（检索不到），场景无法用真链路复现。所以用 `page.route` 把反查
 * 请求改成 404 + 后端的真实错误体，只验**前端的渲染分支**。
 * 后端「文档删除后确实返回这个 404 与这句文案」由 P0-4a 的验收脚本覆盖 ——
 * 两边合起来才是完整证据。
 *
 * 除这一处外全部走真链路：真 MySQL / 真 Chroma / 真检索 / 真 NestJS 网关（也就真鉴权）。
 *
 * ===========================================================================
 * 前置
 * ===========================================================================
 *   make infra && make db-upgrade
 *   make api      # FastAPI  8000
 *   make gateway  # NestJS   3000
 *   make frontend # Vite     5173
 *   知识库里至少有一份已解析成功的文档（否则问不出引用）
 *
 * 运行：
 *   NODE_PATH=$(npm root -g) node tests/acceptance_p0_4b_ui.mjs
 * 可用环境变量：
 *   UI_BASE   默认 http://localhost:5173
 *   SHOT_DIR  截图目录，默认 /tmp/p04b_shots
 *   HEADFUL=1 开有头浏览器（自己看一遍时用）
 */

import fs from 'node:fs'
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
const SHOT_DIR = process.env.SHOT_DIR ?? '/tmp/p04b_shots'
const HEADLESS = process.env.HEADFUL !== '1'
const USER = process.env.ADMIN_USER ?? 'admin'
const PASS = process.env.ADMIN_PASS ?? 'admin123'

const T_UI = 20_000
/** 一次真实问答要跑意图识别 + 检索 + 重排 + LLM，给它 90 秒 */
const T_ANSWER = 90_000

/** 问题固定问知识库里真实存在的那份文档，保证能检索到带 chunk_id 的切片 */
const QUESTION = '离职流程需要注意什么？'

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

/** 轮询等待：返回命中的值，超时返回 null（不抛，让断言统一在 check 里判） */
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

// ------------------------------------------------------------------ 主流程
async function main() {
  fs.mkdirSync(SHOT_DIR, { recursive: true })
  const browser = await chromium.launch({ headless: HEADLESS })
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })

  const consoleErrors = []
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`))

  /** 反查请求计数：用来验「展开过的不再重复请求」 */
  let chunkRequests = 0
  page.on('request', (r) => {
    if (r.url().includes('/api/v1/chunks/')) chunkRequests += 1
  })

  const snap = async (name) => {
    const p = `${SHOT_DIR}/${name}.png`
    await page.screenshot({ path: p, fullPage: false })
    console.log(`     📷 ${p}`)
  }

  try {
    // ========================================================== 1. 登录
    criterion('1. 登录进入问答页')
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.fill('input[type=text]', USER)
    await page.fill('input[type=password]', PASS)
    await page.click('button.submit')
    const shell = await tryWait(() => page.locator('.sidebar').count().then((n) => n > 0 || null), T_UI)
    check('登录后进入主界面（出现侧边栏）', !!shell)
    if (!shell) {
      console.log('  登录失败，后续无法进行。')
      return
    }

    // ========================================================== 2. 发问拿到引用
    criterion('2. 提一个知识库问题，等引用出现')
    await page.fill('textarea', QUESTION)
    await page.click('button.btn-primary.send')
    const toggle = await tryWait(
      () => page.locator('.sources-toggle').first().isVisible().then((v) => v || null),
      T_ANSWER,
    )
    check('回答带回了引用资料（出现「引用资料 N 条」）', !!toggle)
    if (!toggle) {
      await snap('00-no-sources')
      return
    }

    // 等流式结束（还在输出时 sources 可能尚未回填完整）
    await tryWait(
      () => page.locator('.cursor').count().then((n) => n === 0 || null),
      T_ANSWER,
    )
    await page.locator('.sources-toggle').first().click()
    const items = page.locator('.source-item')
    const n = await items.count()
    check('引用列表展开且条数 > 0', n > 0, `count=${n}`)
    await snap('01-sources')

    // ========================================================== 3. 可点性
    criterion('3. 引用条是不是真的可点（有 chunk_id 才是按钮）')
    const clickable = page.locator('.source-name.is-link')
    const plain = page.locator('.source-name:not(.is-link)')
    const nClickable = await clickable.count()
    check('至少有一条引用渲染成了可点按钮', nClickable > 0, `可点=${nClickable} 普通=${await plain.count()}`)
    check(
      '每条引用要么可点、要么是明确不可点的纯文本（不存在第三种）',
      nClickable + (await plain.count()) === n,
    )

    // ========================================================== 4. 展开取全文
    criterion('4. 点开引用 → 拿到切片全文（不是 200 字摘要）')
    const first = items.first()
    const snippet = (await first.locator('.source-snippet').innerText()).trim()
    await first.locator('.source-name.is-link').click()
    const content = await tryWait(
      () => first.locator('.chunk-content').isVisible().then((v) => v || null),
      T_UI,
    )
    check('点开后出现切片全文区', !!content)
    if (!content) {
      await snap('02-no-content')
      return
    }
    const full = (await first.locator('.chunk-content').innerText()).trim()
    check('全文非空', full.length > 0, `${full.length} 字`)
    check(
      '**拿到的是全文而不是摘要**（正文比 snippet 长，且 snippet 是它的前缀）',
      full.length > snippet.length && full.startsWith(snippet.slice(0, 30)),
      `snippet=${snippet.length} 全文=${full.length}`,
    )
    await snap('03-chunk-open')

    // ========================================================== 5. 源文档信息
    criterion('5. 源文档信息来自 MySQL（文件名 / 片号 / 字数）')
    const meta = (await first.locator('.chunk-meta').innerText()).trim()
    console.log(`     元信息：${meta.replace(/\s+/g, ' ')}`)
    check('展示文件名', /\S/.test(meta))
    check('展示「第 N 片」', /第 \d+ 片/.test(meta), meta)
    check('展示字数', /\d+ 字/.test(meta), meta)
    check('没有把「文档记录缺失」误标出来（这份文档是好的）', !meta.includes('文档记录缺失'))

    // ========================================================== 6. 交互闭环
    criterion('6. 交互闭环：收起 / 一次只展开一条 / 不重复请求')
    await first.locator('.source-name.is-link').click()
    await sleep(300)
    check('再点一次收起', (await page.locator('.chunk-detail').count()) === 0)

    if (nClickable >= 2) {
      const second = items.nth(1)
      await first.locator('.source-name.is-link').click()
      await tryWait(() => first.locator('.chunk-content').isVisible().then((v) => v || null), T_UI)
      await second.locator('.source-name.is-link').click()
      await tryWait(() => second.locator('.chunk-content').isVisible().then((v) => v || null), T_UI)
      check(
        '**一次只展开一条**（点开第二条时第一条自动收起）',
        (await page.locator('.chunk-detail').count()) === 1,
        `展开数=${await page.locator('.chunk-detail').count()}`,
      )
      await second.locator('.source-name.is-link').click()
      await sleep(300)
    }

    const before = chunkRequests
    await first.locator('.source-name.is-link').click()
    await tryWait(() => first.locator('.chunk-content').isVisible().then((v) => v || null), T_UI)
    check(
      '**展开过的不再重复请求**（有缓存，不是每次点都打接口）',
      chunkRequests === before,
      `请求数 ${before} → ${chunkRequests}`,
    )
    await first.locator('.source-name.is-link').click()
    await sleep(200)

    // ========================================================== 7. 后端说查不到时
    criterion('7. 后端返回「查不到」时，界面展示的是后端那句人话')
    const DELETE_MSG = '引用内容已随文档删除'
    // 用 predicate 而不是 glob：chunk_id 里有冒号，glob 匹配 URL 时对这类字符不保险
    await page.route(
      (url) => url.pathname.startsWith('/api/v1/chunks/'),
      (route) =>
        route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: DELETE_MSG }),
        }),
    )
    // 必须挑一条**没展开过**的引用：展开过的已经进了组件内的缓存，
    // 再点不会发请求，桩就永远拦不到（那一组断言会变成在验缓存，而不是在验错误分支）
    const usedIdx = new Set([0, 1])
    let idx = -1
    for (let i = 0; i < n; i += 1) {
      if (usedIdx.has(i)) continue
      if ((await items.nth(i).locator('.source-name.is-link').count()) > 0) {
        idx = i
        break
      }
    }
    if (idx < 0) {
      console.log('     （可点的引用都已被展开过，跳过本组）')
    } else {
      const target = items.nth(idx)
      await target.locator('.source-name.is-link').click()
      const err = await tryWait(
        () => target.locator('.chunk-hint.is-error').isVisible().then((v) => v || null),
        T_UI,
      )
      check('展示错误提示', !!err)
      if (err) {
        const txt = (await target.locator('.chunk-hint.is-error').innerText()).trim()
        check('**文案原样采用后端给的**（前端不自己重写一遍）', txt === DELETE_MSG, txt)
      }
      await snap('04-chunk-deleted')
    }
    await page.unroute((url) => url.pathname.startsWith('/api/v1/chunks/'))

    // ========================================================== 8. 控制台
    criterion('8. 全程无控制台报错')
    const real = consoleErrors.filter((t) => !/Failed to load resource/.test(t))
    check('无前端运行时报错', real.length === 0, real.slice(0, 3).join(' | '))
  } finally {
    await browser.close()
  }
}

main()
  .then(() => {
    console.log(`\n${'='.repeat(66)}`)
    console.log(`断言：${pass} 通过 / ${failures.length} 失败`)
    if (failures.length) {
      console.log('\n失败项：')
      for (const f of failures) console.log(`  · ${f}`)
    }
    console.log('='.repeat(66))
    process.exit(failures.length ? 1 : 0)
  })
  .catch((e) => {
    console.error('\n💥 脚本异常：', e)
    process.exit(2)
  })
