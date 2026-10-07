#!/usr/bin/env node
/**
 * P2-12c 浏览器验收：**在真浏览器里登两个账号，看不同的问答记录**。
 *
 * ===========================================================================
 * 为什么 HTTP 验收还不够、一定要过浏览器
 * ===========================================================================
 * `tests/acceptance_p2_12c.py` 已经用真token 验了隔离，但它验的是**接口层**。
 * 用户能亲手复现的形态是**界面**：「我登 chen.jie，看到的是我的会话；
 * 换成 zhao.min 登进去，看到的是他��会话」。这条链路上还隔着两件事：
 *
 *   1. **前端 store 有没有把上一个用户的会话留在内存里**。
 *      这是最阴的一条：`sessions` store 在 Pinia 里，刷新页面会重新拉，
 *      但**同一浏览器里 A 登出 → B 登录**（不刷新页面）时，
 *      若登出没清 store，B 会先看到 A 的历史再被异步覆盖——
 *      那一瞬间的泄漏用户是看得见的。
 *      本脚本刻意走「不刷新页面换账号」这条路径，就是抓这个。
 *   2. **会话列表 / 历史渲染是不是真的按后端返回的画**。
 *      后端隔离了但前端把两个 store 的数据 merge 了，等于没隔离。
 *
 * ===========================================================================
 * 唯一的两处「桩」，以及为什么
 * ===========================================================================
 *提问那一步会真的调模型（意图识别 + 生成，单次几十秒 + token 花费）。
 * 为了让脚本能在可接受的时间内跑完，**流式问答接口被打桩**：
 * `page.route` 拦下 `/api/v1/qa/ask/stream`，放行真实请求、只改写返回体里的
 * 文本增量 —— 于是：
 *   · 后端**照常**执行了完整的问答链路（建会话、落库、写归属），这部分是真的；
 *   · 前端**照常**走完流式渲染与入库逻辑，这部分也是真的；
 *   · 只有「模型生成的那段字」是假的。
 *
 * 为什么要这样而不是「整个接口都 mock」：整个 mock 掉的话，
 * 后端就没建会话，本脚本要验的「列表按归属分开」就没东西可验。
 * 而归属判定恰恰在后端 —— 所以必须放行真请求。
 *
 * 归属是否真的落库，本脚本**另外**用 API 直查一次 MySQL 确认（见 §3），
 * 不靠界面上的文字下结论。
 *
 * ===========================================================================
 * 前置（四端都要在）
 *   make infra && make db-upgrade && make seed-users-apply
 *   make api && make gateway && make frontend
 *   node worker/app.py            （解析链路；本脚本不传文档，可以不起）
 *
 * 运行：
 *   NODE_PATH=$HOME/.workbuddy/binaries/node/workspace/node_modules \
 *     node tests/acceptance_p2_12c_ui.mjs
 * 可用环境变量：
 *   UI_BASE  默认 http://localhost:5173
 *   SHOT_DIR 截图目录，默认 /tmp/p212c_shots
 *   HEADFUL=1 开有头浏览器
 */
import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

function loadPlaywright() {
  const tries = []
  for (const spec of ['playwright', 'playwright-core']) {
    try {
      return require(spec)
    } catch (e) {
      tries.push(`${spec}: ${e.code ?? e.message}`)
    }
  }
  console.error('❌ 找不到 playwright。已尝试：' + tries.join(' / '))
  process.exit(2)
}

const { chromium } = loadPlaywright()

// ------------------------------------------------------------------ 配置
const BASE = process.env.UI_BASE ?? 'http://localhost:5173'
const SHOT_DIR = process.env.SHOT_DIR ?? '/tmp/p212c_shots'
const HEADLESS = process.env.HEADFUL !== '1'

/**
 * 两个**平级**普通员工。
 *
 * 刻意不用 admin：用 admin 会让「他权限更高所以能看到」成为一个合理解释，
 * 而本轮要证明的恰恰是**权限再高也看不到别人的问答**。
 * 同一个账号跑两遍也证明不了什么 —— 那样「隔离」和「一个人用两个浏览器」不可区分。
 */
const ALICE = { user: 'chen.jie', pwd: 'Vb12c!UiProbe', tag: 'A' }
const BOB = { user: 'zhao.min', pwd: 'Vb12c!UiProbe', tag: 'B' }

// 每人的提问标记：会出现在会话标题里，用来在界面上认出「这是谁的会话」
const Q_ALICE = 'UI隔离验证_Alice的问题_9f3a'
const Q_BOB = 'UI隔离验证_Bob的问题_9f3b'

const T_UI = 30_000
const T_ASK = 60_000

// ------------------------------------------------------------------ 断言
let pass = 0
const failures = []
function check(name, cond, extra = '') {
  if (cond) {
    pass += 1
    console.log(`  [PASS] ${name}`)
  } else {
    failures.push(name + (extra ? `← ${extra}` : ''))
    console.log(`  [FAIL] ${name}${extra ? `  ← ${extra}` : ''}`)
  }
}
function criterion(text) {
  console.log(`\n—— ${text}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function tryWait(fn, timeout, every = 400) {
  const t0 = Date.now()
  let lastErr = null
  while (Date.now() - t0 < timeout) {
    try {
      const v = await fn()
      if (v) return v
    } catch (e) {
      lastErr = e
    }
    await sleep(every)
  }
  if (lastErr) console.log(`     （等待期间最后一次异常：${lastErr.message}）`)
  return null
}

// ------------------------------------------------------------------ 密码
/**
 * 种子账号的密码只能从库里改，所以这里直接调 .venv 的 python 改。
 *
 * 为什么不用管理端接口：那条路要 admin 登录，而 admin 是第三个账号，
 * 会把「两个账号」的验证变成三个，还多一份权限交互要维护。
 */
function setPwd(username, pwd, mustChange = 0) {
  const py = `
import sys
sys.path.insert(0, ${JSON.stringify(process.cwd())})
from dotenv import load_dotenv; load_dotenv()
from sqlalchemy import text
from core import password_policy as policy
from core.db import get_engine
with get_engine().begin() as c:
    uid = c.execute(text("SELECT id FROM \`user\` WHERE username=:u"), {"u": ${JSON.stringify(username)}}).scalar()
    if uid is None:
        raise SystemExit("NO_SUCH_USER")
    c.execute(text("UPDATE \`user\` SET password_hash=:h, must_change_password=:m WHERE id=:i"),
              {"h": policy.hash_password(${JSON.stringify(pwd)}), "m": ${mustChange}, "i": uid})
print("OK")
`
  const out = execFileSync('.venv/bin/python', ['-c', py], { encoding: 'utf8' })
  return out.includes('OK')
}

function restorePwd(username) {
  const py = `
import sys
sys.path.insert(0, ${JSON.stringify(process.cwd())})
from dotenv import load_dotenv; load_dotenv()
from sqlalchemy import text
from core import password_policy as policy
from core.db import get_engine
with get_engine().begin() as c:
    c.execute(text("UPDATE \`user\` SET password_hash=:h, must_change_password=1 WHERE username=:u"),
              {"h": policy.hash_password(policy.generate_temporary_password()), "u": ${JSON.stringify(username)}})
print("OK")
`
  execFileSync('.venv/bin/python', ['-c', py], { encoding: 'utf8' })
}

/** 直接查库确认会话归属（不信界面上的文字） */
function dbOwner(sessionId) {
  const py = `
import sys
sys.path.insert(0, ${JSON.stringify(process.cwd())})
from dotenv import load_dotenv; load_dotenv()
from sqlalchemy import text
from core.db import get_engine
with get_engine().connect() as c:
    r = c.execute(text("SELECT u.username FROM session s LEFT JOIN \`user\` u ON u.id=s.user_id WHERE s.id=:s"),
                  {"s": ${JSON.stringify(sessionId)}}).first()
print("OWNER=" + (r[0] if r and r[0] else "NONE"))
`
  const out = execFileSync('.venv/bin/python', ['-c', py], { encoding: 'utf8' })
  return out.match(/OWNER=(\S+)/)?.[1] ?? 'NONE'
}

/**
 * 清掉本脚本造出来的会话。
 *
 * 只能按**问题标记**清（标记在问题正文与会话标题里），因为这些会话
 * 在前端的会话列表里是可见的，按用户/时间清会误伤真人数据。
 *
 * @param {string} marker 标记片段，默认验收脚本自己的 `9f3`
 */
function cleanupSessions(marker = '9f3') {
  const py = `
import sys
sys.path.insert(0, ${JSON.stringify(process.cwd())})
from dotenv import load_dotenv; load_dotenv()
from sqlalchemy import text
from core.db import get_engine
with get_engine().begin() as c:
    ids = [r[0] for r in c.execute(text(
        "SELECT id FROM session WHERE title LIKE :p OR id IN "
        "(SELECT session_id FROM chat_message WHERE content LIKE :p)"),
        {"p": ${JSON.stringify(`%${marker}%`)} })]
    for i in ids:
        c.execute(text("DELETE FROM chat_message WHERE session_id=:i"), {"i": i})
        c.execute(text("DELETE FROM session WHERE id=:i"), {"i": i})
print("CLEANED=" + str(len(ids)))
`
  const out = execFileSync('.venv/bin/python', ['-c', py], { encoding: 'utf8' })
  return out.match(/CLEANED=(\d+)/)?.[1] ?? '?'
}

// ------------------------------------------------------------------ 页面动作
/**
 * 登录。
 *
 * 走界面上的表单而不是塞 localStorage —— 塞 localStorage 只能证明
 * 「带着 token 的请求能通」，而用户要验的是「他在界面上登进去看到的是自己的」。
 */
async function login(page, who) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('input[type="password"], input[name="username"]', { timeout: T_UI })
  const inputs = await page.$$('input')
  for (const el of inputs) {
    const type = await el.getAttribute('type')
    const name = (await el.getAttribute('name')) ?? ''
    const ph = (await el.getAttribute('placeholder')) ?? ''
    if (type === 'password') {
      await el.fill(who.pwd)
    } else if (name.includes('user') || ph.includes('用户') || ph.includes('账号') || type === 'text') {
      await el.fill(who.user)
    }
  }
  await page.click('button[type="submit"], .login-btn, button:has-text("登录")')
  await page.waitForFunction(() => !document.body.innerText.includes('登录中'), { timeout: T_UI })
  await sleep(1200) // 等首屏会话列表拉完
}

/** 登出（点界面上的按钮，不直接清 storage —— 那就绕过了产品逻辑） */
async function logout(page) {
  // ⚠️ 选择器必须用 `.logout-btn`（`AppSidebar.vue:280` 的真实类名）。
  //    第一版按「按钮文字含登出/退出」找，但那个按钮是**纯图标 + aria-label**，
  //    文字是空的 —— 于是永远找不到，脚本还以为「登出功能坏了」。
  //    教训：找控件要按稳定的类名/测试属性，不要按可见文字猜。
  const sel = '.logout-btn, button[aria-label="退出登录"]'
  const el = await page.$(sel)
  if (!el) return false
  await el.click()
  await tryWait(() => page.$('input[type="password"]'), 10_000)
  await sleep(800)
  return true
}

/** 页面是否已登录（登录页有密码框） */
async function atLoginPage(page) {
  return (await page.$('input[type="password"]')) !== null
}

/**
 * 提一个问题，并返回**这次提问落在哪条会话上**。
 *
 * ⚠️ 流式接口在这里被打桩：放行真请求，只把返回的文本增量换掉。
 *    理由见文件头「唯一的两处桩」。
 *
 * ⚠️ session id 从**响应首帧**取（`{"type":"session","session_id":...}`，
 *    `api/routes/qa.py:317`），有两个原因：
 *   1. session_id 走的是 **POST body**，不是请求头 —— 拦 `X-Session-Id` 拿不到
 *      （第一版就是这么写的，字段根本不存在）。
 *   2. 不能用「列表差集」推断「这次提问新开了哪条会话」。实测（诊断 2）：
 *      界面上已有当前会话时，前端会**续用那条**而不是新建 ——
 *      于是「新增 0 条」，脚本却判成「提问失败」，假红一路传染到后面所有依赖 sid 的断言。
 *    响应首帧是后端自己说的，不猜。
 */
async function ask(page, who, question) {
  let sid = null

  await page.route('**/api/v1/qa/ask/stream', async (route) => {
    const res = await route.fetch()
    const body = await res.text()
    // 只改写「答案正文」，meta / done 帧原样放行 ——
    // 改多了前端的状态机就跟着变，那就不是「只桩掉生成」了。
    const patched = body
      .split('\n')
      .filter(Boolean)
      .map((line) => {
        try {
          const obj = JSON.parse(line)
          if (obj.type === 'session' && obj.session_id) sid = obj.session_id
          if (obj.type === 'chunk') return JSON.stringify({ ...obj, content: '（桩回答）' })
          return line
        } catch {
          return line
        }
      })
      .join('\n')
    await route.fulfill({
      status: res.status(),
      headers: { 'content-type': 'application/x-ndjson' },
      body: patched + '\n',
    })
  })

  try {
    await page.fill('textarea', question)
    await page.keyboard.press('Enter')
    // 等首帧回来（拿到 sid）——固定等待是 flaky 的头号来源
    const got = await tryWait(() => (sid ? sid : null), T_ASK)
    return { sid: got ?? null }
  } finally {
    await page.unroute('**/api/v1/qa/ask/stream')
  }
}

/**
 * 抓当前会话列表里的 session id（从接口拿，比解析 DOM 稳）。
 *
 * ⚠️ 键名 `ragqa.auth.token` 来自 `frontend/src/stores/auth.ts` 的 `TOKEN_KEY`。
 *   写死在这里而不是 import：这是一个 .mjs，跑在 node 里，没有构建步骤可依赖。
 *   代价是前端改键名时这里会静默取到null → 断言全红，
 *   而红的原因看起来像「隔离坏了」。所以下面那条断言专门盯这一点。
 */
const TOKEN_KEY = 'ragqa.auth.token'

async function grabSessionIds(page) {
  return await page.evaluate(async (key) => {
    const raw = localStorage.getItem(key)
    if (!raw) return null          // null = 没登录，与「空列表」要能区分
    try {
      const res = await fetch('/api/v1/qa/sessions', {
        headers: { Authorization: `Bearer ${raw}` },
      })
      if (!res.ok) return null
      const list = await res.json()
      return list.map((i) => i.session_id)
    } catch {
      return null
    }
  }, TOKEN_KEY)
}

/** 界面上是否出现了某段文字（会话列表 / 历史区） */
async function pageMentions(page, text) {
  return (await page.evaluate((t) => document.body.innerText.includes(t), text))
}

// ------------------------------------------------------------------ 主流程
async function main() {
  fs.mkdirSync(SHOT_DIR, { recursive: true })

  console.log('='.repeat(70))
  console.log('  P2-12c 浏览器验收：登两个账号，看不同的问答记录')
  console.log('='.repeat(70))

  criterion('0. 前置：改两个种子账号的密码（要拿真 token，只能真登录）')
  check(`${ALICE.user} 密码已设为探针值`, setPwd(ALICE.user, ALICE.pwd))
  check(`${BOB.user} 密码已设为探针值`, setPwd(BOB.user, BOB.pwd))
  // ⚠️ 开跑前先清掉**上一轮残留**。第一版只在 finally 清，
  //    结果上次崩在 finally 第一条 → 残留会话把本轮的「列表里有几条」算歪了，
  //    而且 Alice 一登录就看到上次的 9f3a，看起来像「隔离没生效」。
  //    刻意分两步：先清验收标记（9f3），再清诊断标记（diag）——
  //    诊断脚本留下的会话不清掉，Alice 登录就会看到它们。
  const stale9f3 = cleanupSessions()
  const staleDiag = cleanupSessions('diag')
  check('开跑前清掉历史残留会话', stale9f3 !== null && staleDiag !== null,
    `9f3 清 ${stale9f3} 条 / diag 清 ${staleDiag} 条`)

  const browser = await chromium.launch({ headless: HEADLESS })
  // ⚠️ 用**同一个 context** 做 A→B：只有同一个浏览器里不刷新地换账号，
  //    才能验「Pinia 里的内存数据有没有被清掉」。
  //    两个 context 等于两个浏览器，A 的内存与 B 完全无关，验不到任何东西。
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()

  try {
    criterion('1. Alice 登录并提问')
    await login(page, ALICE)
    check('Alice 登录后不在登录页', !(await atLoginPage(page)))
    await page.screenshot({ path: path.join(SHOT_DIR, '1_alice_登录后.png') })

    const aliceBefore = await grabSessionIds(page)
    // null = 取不到 token（多半是 TOKEN_KEY 写错了），空数组 = 真的一条会话都没有。
    // 分开报是因为这两种红的**原因完全不同**，混在一起会把「脚本坏了」说成「隔离坏了」。
    check('Alice 能取到 token（前端 localStorage 键名对得上）',
      Array.isArray(aliceBefore), `grabSessionIds 返回 ${aliceBefore} —— 键名 ${TOKEN_KEY} 可能变了`)
    check('**Alice 登录后一条会话都没有**（库是干净的，没有别人的残留）',
      Array.isArray(aliceBefore) && aliceBefore.length === 0, `实际 ${JSON.stringify(aliceBefore)}`)

    const a = await ask(page, ALICE, Q_ALICE)
    check('提问后拿到了这次的 session id', a.sid !== null, `sid=${a.sid}`)
    const aliceSid = a.sid
    if (aliceSid) {
      check('**库里那条会话的归属是 Alice**（不信界面，直接查库）',
        dbOwner(aliceSid) === ALICE.user, `实际 ${dbOwner(aliceSid)}（会话 ${aliceSid}）`)
    }
    const afterAlice = await grabSessionIds(page)
    check('Alice 的列表里只有自己那一条',
      Array.isArray(afterAlice) && aliceSid !== null && afterAlice.length === 1
        && afterAlice[0] === aliceSid, `实际 ${JSON.stringify(afterAlice)}`)
    await sleep(1500)
    await page.screenshot({ path: path.join(SHOT_DIR, '2_alice_提问后.png') })
    check('界面上能看到 Alice 自己那句提问', await pageMentions(page, '9f3a'))

    criterion('2. 关键：Alice 登出（同一浏览器、不刷新），换成 Bob')
    const loggedOut = await logout(page)
    check('界面上能登出（走的是产品按钮，不是清 storage）', loggedOut)
    check('登出后回到登录页', await atLoginPage(page))
    // 此刻 store 里可能还留着 Alice 的会话 —— 下一条就是抓这个
    await page.screenshot({ path: path.join(SHOT_DIR, '3_登出后.png') })

    await login(page, BOB)
    check('Bob 登录后不在登录页', !(await atLoginPage(page)))
    await sleep(1500)
    const bobIds = await grabSessionIds(page)
    check('Bob 能取到 token', Array.isArray(bobIds), `返回 ${bobIds}`)
    await page.screenshot({ path: path.join(SHOT_DIR, '4_bob_登录后.png') })

    criterion('3. Bob 的会话列表里没有 Alice 那条（这是用户要亲眼看到的那件事）')
    const aliceInBobList = aliceSid && Array.isArray(bobIds) ? bobIds.includes(aliceSid) : false
    check('**Bob 的列表接口里没有 Alice 那条会话**', !aliceInBobList,
      aliceInBobList ? `Bob 拿到了 ${aliceSid}` : '')
    check('  └ Bob 的列表确实是空的（他一条会话都没有）',
      Array.isArray(bobIds) && bobIds.length === 0, `实际 ${JSON.stringify(bobIds)}`)
    const aliceVisible = await pageMentions(page, '9f3a')
    check('**Bob 的界面上看不到 Alice 那句提问**', !aliceVisible,
      aliceVisible ? '界面上出现了 9f3a' : '')
    // 更狠的一条：整个界面的文字里都不该有 Alice 的问题片段
    const fragVisible = await pageMentions(page, 'Alice的问题')
    check('  └ 连「Alice的问题」这个片段都搜不到', !fragVisible)

    criterion('4. Bob 自己提问，确认两份历史真的各自独立')
    const b = await ask(page, BOB, Q_BOB)
    check('提问后拿到了这次的 session id', b.sid !== null, `sid=${b.sid}`)
    const bobSid = b.sid
    if (bobSid) {
      check('**库里 Bob 那条会话的归属是 Bob**', dbOwner(bobSid) === BOB.user,
        `实际 ${dbOwner(bobSid)}（会话 ${bobSid}）`)
    }
    if (aliceSid && bobSid) {
      check('两条会话确实是不同的两条', aliceSid !== bobSid, `${aliceSid} vs ${bobSid}`)
    }
    await sleep(1500)
    await page.screenshot({ path: path.join(SHOT_DIR, '5_bob_提问后.png') })
    check('界面上能看到 Bob 自己那句提问', await pageMentions(page, '9f3b'))

    criterion('5. 回核：Alice 再登回来，看到的仍然只有她自己那条')
    check('Bob 能登出', await logout(page))
    await login(page, ALICE)
    const aliceAfter = await grabSessionIds(page)
    check('Alice 重新登录后列表里仍有自己那条',
      Array.isArray(aliceAfter) && aliceSid !== null && aliceAfter.includes(aliceSid),
      `实际 ${JSON.stringify(aliceAfter)}`)
    check('  └ **Alice 的列表里没有 Bob 那条**',
      bobSid ? Array.isArray(aliceAfter) && !aliceAfter.includes(bobSid) : true,
      bobSid ? `Alice 拿到了 ${bobSid}` : '')
    await sleep(1000)
    await page.screenshot({ path: path.join(SHOT_DIR, '6_alice_回来.png') })
  } finally {
    // ⚠️ 收尾三步必须**各自独立 try**。
    //    第一版把它们顺序裸写在 finally 里，结果第一步 cleanupSessions 一抛，
    //    后两步（还原密码）根本不会执行 —— 库就这么留下了探针密码。
    //    「收尾失败」和「收尾没做完」必须能区分，所以每步单独兜。
    await browser.close().catch(() => {})
    console.log(`\n  截图目录：${SHOT_DIR}`)
    console.log('  收尾：清掉本脚本造的会话 + 还原两个账号的密码')
    const steps = [
      ['清会话', () => {
        const n = cleanupSessions()
        const d = cleanupSessions('diag')
        check(`本脚本造的会话已清干净（9f3 ${n} 条 / diag ${d} 条）`, true)
      }],
      [`还原 ${ALICE.user} 密码`, () => {
        restorePwd(ALICE.user)
        check(`${ALICE.user} 密码已还原为随机临时密码 + 强制改密`, true)
      }],
      [`还原 ${BOB.user} 密码`, () => {
        restorePwd(BOB.user)
        check(`${BOB.user} 密码已还原为随机临时密码 + 强制改密`, true)
      }],
    ]
    for (const [name, fn] of steps) {
      try {
        fn()
      } catch (e) {
        // 不 rethrow：收尾失败不该盖掉主流程已经打出来的断言结果，
        // 但必须显式报出来，并且退出码非 0。
        console.error(`  ❌ 收尾步骤「${name}」失败：${e.message}`)
        failures.push(`收尾步骤「${name}」失败：${e.message.split('\n')[0]}`)
      }
    }
  }

  console.log('\n' + '='.repeat(70))
  console.log(`P2-12c 浏览器验收：${pass} 通过 / ${failures.length} 失败`)
  if (failures.length) {
    console.log('失败明细：')
    for (const f of failures) console.log('  ✗ ' + f)
  }
  console.log('='.repeat(70))
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => {
  console.error('\n💥 脚本自身崩了（这不算验收失败，但必须能一眼看出崩在哪）：')
  console.error(e)
  process.exit(2)
})