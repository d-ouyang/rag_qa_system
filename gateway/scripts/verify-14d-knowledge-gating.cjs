/**
 * P2-14d 浏览器验收：主应用知识库页的入口收敛。
 *
 *   node gateway/scripts/verify-14d-knowledge-gating.cjs
 *
 * ---------------------------------------------------------------------------
 * 为什么必须用浏览器而不是 curl
 * ---------------------------------------------------------------------------
 * curl 只能证明「后端拒绝了越权写」—— 那是 14e 已经验过的事。
 * 14d 要验的是**界面上根本没有那个入口**，而这只有在真 DOM 里才存在：
 *   · 上传区（一个 div + 一个 hidden file input）渲不渲染；
 *   · 每行的「重试 / 删除」按钮在不在；
 *   · 页面**任意空白处**拖文件会不会触发上传。
 *
 * 最后一条是本项最容易漏的：拖放事件 `@drop` 挂在**整个 section** 上
 * （不是挂在上传区上），所以「上传区不渲染」并不等于拖不动 ——
 * 只看模板会以为已经收紧了，而那个洞要靠监听网络请求才抓得到。
 *
 * ---------------------------------------------------------------------------
 * 双账号对照，而不是只看一个
 * ---------------------------------------------------------------------------
 * 只验「只读用户看不到上传区」有个陷阱：页面加载失败时**也**看不到。
 * 所以同时登录一个有写权限的账号做对照 ——
 * 「只读看不见、有权限看得见」才构成一对判据。
 *
 * 截图：/tmp/kb_14d_readonly.png / /tmp/kb_14d_writable.png
 */
const { chromium } = require('playwright')
const { execFileSync } = require('child_process')
const path = require('path')

const APP = process.env.APP_BASE || 'http://localhost:5173'
const REPO = path.resolve(__dirname, '..', '..')

// [登录名, 密码, 期望是否有写权限]
//
// ⚠️ **密码由本脚本自己设，不依赖 `dev_test_accounts.py` 的固定密码。**
// 第一版直接用那两个固定密码，于是「跑完 tests/test_module14d_frontend_gating.py
// 再跑本脚本」必红 —— 那个测试收尾时会把两个账号换成随机临时密码
// （它唯一合法的还原方式，见该文件「自备凭据」段）。
//
// 一个脚本的成败取决于**跑它之前跑过什么**，那它测的就不是自己的被测物。
// 这里的做法与 14d 测试、acceptance_p2_12b 同一规格。
const ACCOUNTS = [
  ['zhao.min', 'P2d14dUiZhao2026!Xx', false], // kb_role = none
  ['chen.jie', 'P2d14dUiChen2026!Xx', true], // kb_role = superadmin
]

/** 用后端的 password_policy 设/还原测试账号的密码（不自己实现 bcrypt） */
function withRepoPython(code) {
  return execFileSync(path.join(REPO, '.venv/bin/python'), ['-c', code], {
    cwd: REPO,
    encoding: 'utf-8',
  })
}

function setPasswords() {
  const pairs = ACCOUNTS.map(([u, p]) => `'${u}':'${p}'`).join(',')
  withRepoPython(
    'from dotenv import load_dotenv; load_dotenv()\n' +
      'from sqlalchemy import text\n' +
      'from core import password_policy as policy\n' +
      'from core.db import get_engine\n' +
      `pairs = {${pairs}}\n` +
      'e = get_engine()\n' +
      'with e.begin() as c:\n' +
      '    for u, p in pairs.items():\n' +
      "        n = c.execute(text('UPDATE `user` SET password_hash=:h, must_change_password=0 WHERE username=:u'), {'h': policy.hash_password(p), 'u': u}).rowcount\n" +
      "        assert n == 1, f'{u} 不在库里（先跑 make seed-users-apply）'\n" +
      "print('已设 ' + str(len(pairs)) + ' 个测试账号的固定密码')"
  )
}

function restorePasswords() {
  withRepoPython(
    'from dotenv import load_dotenv; load_dotenv()\n' +
      'from sqlalchemy import text\n' +
      'from core import password_policy as policy\n' +
      'from core.db import get_engine\n' +
      `names = [${ACCOUNTS.map(([u]) => `'${u}'`).join(',')}]\n` +
      'e = get_engine()\n' +
      'with e.begin() as c:\n' +
      '    for u in names:\n' +
      "        c.execute(text('UPDATE `user` SET password_hash=:h, must_change_password=1 WHERE username=:u'), {'h': policy.hash_password(policy.generate_temporary_password()), 'u': u})\n" +
      "print('已换回随机临时密码 —— 与 tests/test_module14d_frontend_gating.py 同一规格')"
  )
}

let pass = 0
let fail = 0
const check = (name, ok, detail = '') => {
  if (ok) {
    pass += 1
    console.log(`  [PASS] ${name}`)
  } else {
    fail += 1
    console.log(`  [FAIL] ${name} | ${detail}`)
  }
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * 登录限流退避：最多等 2 个窗口。
 * 与 tests/test_module14d_frontend_gating.py 的 `_login_with_backoff` 同一条理由。
 */
const LOGIN_THROTTLE_WINDOW_S = 62
const LOGIN_THROTTLE_MAX_RETRY = 2

async function openKnowledgeWithBackoff(browser, username, password, attempt) {
  if (attempt > LOGIN_THROTTLE_MAX_RETRY) {
    // ⚠️ 这里**不再等**：等下去只会让脚本挂住，而挂住比红更难查。
    // 仍然要跑一次好让调用方拿到一个「页面是空的」结果，由它报红。
    console.log(`  （${username} 连撞 ${LOGIN_THROTTLE_MAX_RETRY} 次限流，不再等`
      + ' —— 若这条红，请隔一分钟再跑）')
    return openKnowledge(browser, username, password, attempt + 1)
  }
  await wait(LOGIN_THROTTLE_WINDOW_S * 1000)
  return openKnowledge(browser, username, password, attempt + 1)
}

/** 登录 → 进入知识库页 → 回传该页面的 DOM 探针结果 */
async function openKnowledge(browser, username, password, attempt = 0) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const jsErrors = []
  // 探针拦掉的上传请求会在控制台留下一条 net::ERR_FAILED。
  // ⚠️ **不能**靠控制台文本判断「这条是不是探针造成的」——
  // 那条消息里**没有 URL**（实测就是 'Failed to load resource: net::ERR_FAILED'），
  // 所以按字符串分流会把真实的资源失败也一起放过。
  // 唯一可靠的做法是另开 `requestfailed`，那里带 URL。
  const failedUploads = []
  const failedOthers = []
  ctx.on('page', (p) => {
    p.on('pageerror', (e) => jsErrors.push('pageerror: ' + String(e).slice(0, 200)))
    p.on('console', (m) => {
      if (m.type() !== 'error') return
      // 控制台里的资源失败由 requestfailed 统一判（那边有 URL），这里不重复计
      if (m.text().includes('net::ERR_FAILED')) return
      jsErrors.push('console: ' + m.text().slice(0, 200))
    })
    p.on('requestfailed', (r) => {
      const line = `${r.method()} ${r.url()}`
      if (r.url().includes('/documents/upload')) failedUploads.push(line)
      else failedOthers.push(line)
    })
  })

  const page = await ctx.newPage()
  await page.goto(APP, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('input[type=text]', { timeout: 20000 })
  await page.fill('input[type=text]', username)
  await page.fill('input[type=password]', password)
  await page.click('button[type=submit]')
  await page
    .waitForFunction(() => !document.querySelector('input[type=password]'), { timeout: 20000 })
    .catch(() => {})
  await wait(1500)

  // 🔴 **登录失败要当场识别并退避**，不能让它混进后面的断言里。
  // 网关对登录的限流是 8 次/分钟/IP（写死在 auth.controller.ts 的 @Throttle）；
  // 本脚本要登 2 个账号，而 tests/test_module14d_frontend_gating.py 也要登 2 个 ——
  // 连着跑就必然撞上。第一版没有这段，于是表现是
  // 「`.nav-btn` 找不到 → TimeoutError」，而真正的原因是限流，
  // 一个与被测物完全无关的原因（踩坑清单第 83 条的形状：
  // 症状与理论矛盾时，矛盾的那个更可信）。
  const stillOnLogin = await page.locator('input[type=password]').count()
  if (stillOnLogin > 0) {
    const msg = (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 160)
    if (msg.includes('太频繁') || msg.includes('频繁')) {
      console.log(`  （${username} 撞上登录限流 8 次/分钟，等 62s 后重试）`)
      await ctx.close()
      return openKnowledgeWithBackoff(browser, username, password, attempt + 1)
    }
    console.log(`  （${username} 登录未成功：${msg}）`)
  }

  // 切到知识库（侧边栏 .nav-btn，active 的是当前视图）
  await page.click('.nav-btn:has-text("知识库")')
  await page.waitForSelector('.knowledge-view', { timeout: 15000 })
  await wait(1500)

  const result = await page.evaluate(() => {
    const all = (sel) => Array.from(document.querySelectorAll(sel))
    const rows = all('.table-row')
    return {
      dropzoneExists: !!document.querySelector('.dropzone'),
      readonlyNoteExists: !!document.querySelector('.readonly-note'),
      fileInputCount: document.querySelectorAll('input[type=file]').length,
      rowCount: rows.length,
      firstRowOps: rows.length
        ? Array.from(rows[0].querySelectorAll('.op-btn')).map((b) => b.textContent.trim())
        : [],
      deleteCount: all('.op-btn.danger').length,
      retryCount: all('.op-btn').filter((b) => b.textContent.trim() === '重试').length,
      readonlyText: document.querySelector('.readonly-note')
        ? document.querySelector('.readonly-note').textContent.replace(/\s+/g, ' ').trim()
        : '',
    }
  })

  return { ctx, page, result, jsErrors, failedUploads, failedOthers }
}

;(async () => {
  console.log('='.repeat(66))
  console.log('  P2-14d 浏览器验收：知识库入口收敛')
  console.log('='.repeat(66))

  // ⚠️ --no-proxy-server：本机 HTTP_PROXY 会让 localhost 的请求走代理返 502
  const browser = await chromium.launch({ args: ['--no-proxy-server'] })
  const byName = {}

  // 自备凭据（理由见文件头）：设 → 跑 → **无论如何**换回随机临时密码。
  // 漏了 finally 就等于给两个账号留了后门，而「测试留下的东西」
  // 是下一次测试失败最难查的来源。
  setPasswords()

  try {
    for (const [username, password, expectWrite] of ACCOUNTS) {
      const { ctx, page, result, jsErrors, failedUploads, failedOthers } =
        await openKnowledge(browser, username, password)
      if (result.rowCount === 0 && result.dropzoneExists === false && result.readonlyNoteExists === false) {
        // 一个都没渲染 = 大概率是**登录没成功**（限流或密码不对），
        // 而不是「权限收敛得太干净」。不区分的话，后面 20 条断言
        // 会全部以「[FAIL]」的形式指向一个跟被测物无关的原因。
        console.log(`  [FAIL] ${username} 登录并进入知识库页 | 页面没有任何内容 —— `
          + '先确认后端(8000)/网关(3000)/前端(5173)在跑，且没有撞上登录限流（8 次/分钟）')
        process.exitCode = 1
        await ctx.close()
        continue
      }
      byName[username] = { result, expectWrite }
      const who = expectWrite ? '（有写权限）' : '（只读）'

      check(`${username} 登录并进入知识库页（读到 ${result.rowCount} 行文档）`,
        result.rowCount > 0, JSON.stringify(result).slice(0, 300))

      if (expectWrite) {
        check(`${username} ${who}看得到上传区`, result.dropzoneExists === true)
        check(`${username} ${who}没有只读说明卡`, result.readonlyNoteExists === false)
        check(`${username} ${who}有 file input（能真的选文件）`,
          result.fileInputCount === 1, `实际 ${result.fileInputCount}`)
        check(`${username} ${who}每行有 重试 / 删除 两个按钮`,
          result.firstRowOps.includes('重试') && result.firstRowOps.includes('删除'),
          `实际=${JSON.stringify(result.firstRowOps)}`)
        check(`${username} ${who}删除按钮数 = 行数`,
          result.deleteCount === result.rowCount,
          `删除 ${result.deleteCount} vs 行 ${result.rowCount}`)
      } else {
        check(`${username} ${who}看不到上传区`, result.dropzoneExists === false)
        check(`${username} ${who}看到的是只读说明卡`, result.readonlyNoteExists === true)
        check(`${username} ${who}没有 file input（连隐藏的都没有）`,
          result.fileInputCount === 0, `实际 ${result.fileInputCount}`)
        check(`${username} ${who}每行只剩 片段 / 下载`,
          !result.firstRowOps.includes('重试') && !result.firstRowOps.includes('删除'),
          `实际=${JSON.stringify(result.firstRowOps)}`)
        check(`${username} ${who}全页0 个删除按钮`, result.deleteCount === 0,
          `实际 ${result.deleteCount}`)
        check(`${username} ${who}全页 0 个重试按钮`, result.retryCount === 0,
          `实际 ${result.retryCount}`)
        check(`${username} ${who}说明卡讲清了「只读」与「找谁开通」`,
          result.readonlyText.includes('只读') && result.readonlyText.includes('管理员'),
          result.readonlyText.slice(0, 140))
      }

      // 🔴 拖拽的洞：@drop 挂在整个 section 上。监听网络请求才抓得到 ——
      // 界面判断（有没有上传区）证明不了它，因为 onDrop 可以在没有上传区时触发。
      //
      // ⚠️ **上传请求一律 abort，绝不真发**。
      // 第一版没拦，于是「有写权限」那一侧的对照组拖拽真的上传了 probe.txt ——
      // 验收脚本往用户的知识库里塞了一份垃圾，还多了一个 Celery 任务。
      // 拦掉之后判据依然成立：我们要验的是「**前端有没有发起请求**」，
      // 而请求在 route 层就被掐断根本到不了后端。
      let uploadHits = 0
      const onReq = (req) => {
        if (req.method() === 'POST' && req.url().includes('/documents/upload')) uploadHits += 1
      }
      const blockUpload = (route) => {
        if (route.request().method() === 'POST' && route.request().url().includes('/documents/upload')) {
          route.abort()
        }
      }
      await page.route('**/api/v1/documents/upload*', blockUpload)
      await page.route('**/api/v1/documents/upload/batch*', blockUpload)
      ctx.on('request', onReq)
      await page.evaluate(() => {
        const dt = new DataTransfer()
        dt.items.add(new File(['x'], 'probe.txt', { type: 'text/plain' }))
        document
          .querySelector('.knowledge-view')
          .dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }))
      })
      await wait(1500)
      ctx.off('request', onReq)
      await page.unroute('**/api/v1/documents/upload*')
      await page.unroute('**/api/v1/documents/upload/batch*')

      if (expectWrite) {
        check('有写权限时拖拽确实会走上传入口（对照组：说明拖放事件本身是通的）',
          uploadHits >= 1, `实际 ${uploadHits} 次 —— 若为 0，说明这条探针本身没生效`)
        // 拦截规则本身也要有数：拖一次就该拦一次。
        // 「拦了 0 次但请求也不发」和「拦了 1 次」在 uploadHits 上都是 1，
        // 靠 uploadHits 区分不出来。
        check('被拦的 upload 请求数 = 拖拽探针数（拦截规则本身没写歪）',
          failedUploads.length === uploadHits,
          `拦了 ${failedUploads.length} 次 / 探针 ${uploadHits} 次`)
      } else {
        check('🔴 只读用户把文件拖到页面空白处 → 没有发出上传请求',
          uploadHits === 0, `实际发出 ${uploadHits} 次`)
        const body = await page.evaluate(() => document.body.innerText)
        check('只读用户拖文件 → 明确告知没有权限（而不是静默无反应）',
          body.includes('没有上传知识库文档的权限'), body.slice(0, 200))
        // 对称的一条：只读侧连「被拦」都不该发生 —— 因为根本没发出去。
        // 有这条才分得清「被守卫挡住了」与「压根没走到那一步」，
        // 而这两种在uploadHits=0 上是同一个值。
        check('只读侧 0 个 upload 请求被拦（说明是真的没发，不是发了被拦）',
          failedUploads.length === 0, `实际 ${failedUploads.length} 次`)
      }

      check(`${username} 全程无 JS 错误`, jsErrors.length === 0, jsErrors.slice(0, 3).join(' | '))
      // 探针自己拦掉的 upload 失败是预期的（我们就是不让它发出去）；
      // 除此之外任何请求失败都要红 —— 那是真的资源没加载出来。
      check(`${username} 除被拦的 upload 外没有其它请求失败`,
        failedOthers.length === 0, failedOthers.slice(0, 3).join(' | '))

      await page.screenshot({
        path: expectWrite ? '/tmp/kb_14d_writable.png' : '/tmp/kb_14d_readonly.png',
      })
      await ctx.close()
    }

    // ---- 一对判据：同一个界面对两个账号给出相反的结果 ----
    const ro = byName['zhao.min']
    const rw = byName['chen.jie']
    if (ro && rw) {
      check('🔴 同一页面对两个账号给出相反的入口（有权限看得到 / 只读看不到）',
        ro.result.dropzoneExists === false && rw.result.dropzoneExists === true,
        `只读=${ro.result.dropzoneExists} 有权限=${rw.result.dropzoneExists}`)
      check('两个账号看到的文档数相同（收敛只动写入口，不动读）',
        ro.result.rowCount === rw.result.rowCount,
        `只读 ${ro.result.rowCount} vs 有权限 ${rw.result.rowCount}`)
    }
  } finally {
    await browser.close()
    // ⚠️ 必须还原，哪怕上面抛了异常。留下的固定密码 = 留在库里的后门。
    try {
      restorePasswords()
    } catch (e) {
      console.log('  [FAIL] 还原测试账号密码失败 | ' + String(e).slice(0, 200))
      process.exitCode = 1
    }
  }

  console.log('\n' + '='.repeat(66))
  console.log(`  结果：${pass} 通过 / ${fail} 失败`)
  console.log('='.repeat(66))
  process.exit(fail ? 1 : 0)
})().catch((e) => {
  console.error('脚本异常：', e)
  process.exit(1)
})
