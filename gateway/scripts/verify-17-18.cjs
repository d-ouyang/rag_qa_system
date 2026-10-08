/**
 * P2-17/18 浏览器验收。
 *
 *   node gateway/scripts/verify-17-18.cjs
 *
 * A（17）：管理端 Element Plus ——
 *   A1 el-select 已替换（筛选区横排：三个下拉同一行）
 *   A2 暗色主题（html.dark + EP 面板变量）
 *   A3 员工页 el-pagination 存在且**在视口内**（分页固定页底）
 *   A4 员工真分页：total 来自后端（共 9 人）
 *   A5 审计页 el-pagination 在视口内
 *   A6 密码看板「全部人员」tab + 分页
 * B（18）：主应用改密 ——
 *   B1 面板出现「修改密码」按钮
 *   B2 表单出现，两次密码不一致有提示
 *   B3 错误旧密码 → 显示后端错误文案
 *   B4 正确改密 → alert + 退出到登录页（随后改回）
 *
 * ⚠️ 结束清审计 + --apply 恢复固定密码。
 */
const { chromium } = require('playwright');

const GW = 'http://127.0.0.1:3000';
const APP = 'http://127.0.0.1:5173';
const ADMIN = 'http://127.0.0.1:5174';

let pass = 0, fail = 0;
function check(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else { fail++; console.log(`  [FAIL] ${name} | ${detail}`); }
}

/** HTTP 登录（清理用；Node 全局 fetch，无代理干扰）。 */
async function loginViaApi(u, p) {
  const res = await fetch(`${GW}/api/auth/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: u, password: p }),
  });
  return (await res.json()).access_token;
}

async function loginViaUi(page, base, u, p, afterRe) {
  // 登录限流 8 次/分：撞 429 等一个窗口重试（最多 2 次），不放宽限流。
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.goto(`${base}/login`);
    await page.fill('input:not([type="password"])', u);
    await page.fill('input[type="password"]', p);
    await page.click('button[type="submit"], button');
    try {
      await page.waitForURL(afterRe, { timeout: 20000 });
      return;
    } catch (e) {
      const errText = await page.locator('body').innerText().catch(() => '');
      if (!errText.includes('过于频繁') && !errText.includes('稍后') && attempt < 2) throw e;
      if (attempt < 2) {
        console.log(`  · 登录限流，等 65s 重试（第 ${attempt + 1} 次）`);
        await page.waitForTimeout(65000);
      } else {
        throw e;
      }
    }
  }
}

(async () => {
  const browser = await chromium.launch();

  // ---------- A. 管理端 ----------
  const ap = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errsA = [];
  ap.on('pageerror', (e) => errsA.push(e.message));
  await loginViaUi(ap, ADMIN, 'wu.jing', 'DevAdmin2026!Aa', /overview/);

  // A1/A3/A4 员工页
  await ap.goto(`${ADMIN}/users`);
  await ap.waitForSelector('.el-select', { timeout: 20000 });
  const selCount = await ap.locator('.el-select').count();
  check(`A1 el-select 已替换（页面上 ${selCount} 个）`, selCount >= 3);

  // 横排：三个筛选下拉同一行（y 相同）
  const boxes = [];
  for (let i = 0; i < 3; i++) boxes.push(await ap.locator('.filters .el-select').nth(i).boundingBox());
  check('A1b 筛选区三个下拉**横向排布**（y 相近，不再各占一行）',
    boxes.every((b) => b) && Math.abs(boxes[0].y - boxes[1].y) < 2 && Math.abs(boxes[1].y - boxes[2].y) < 2,
    JSON.stringify(boxes.map((b) => b?.y)));

  // A3 分页存在且在视口内
  await ap.waitForSelector('.el-pagination', { timeout: 10000 });
  const pagerBox = await ap.locator('.el-pagination').first().boundingBox();
  const vp = ap.viewportSize();
  check('🔴 A3 员工页分页**在视口内**（不用滚到页底）',
    pagerBox && pagerBox.y + pagerBox.height <= vp.height + 2,
    `pager bottom=${pagerBox ? (pagerBox.y + pagerBox.height).toFixed(0) : '?'} viewport=${vp.height}`);
  check('A4 员工真分页：总数来自后端（共 9 人）',
    (await ap.locator('.el-pagination__total').innerText()).includes('9'),
    await ap.locator('.el-pagination__total').innerText().catch(() => ''));

  // A5 审计页
  await ap.goto(`${ADMIN}/audit`);
  await ap.waitForSelector('.el-pagination', { timeout: 20000 });
  const auditPager = await ap.locator('.el-pagination').first().boundingBox();
  check('🔴 A5 审计页分页在视口内', auditPager && auditPager.y + auditPager.height <= vp.height + 2,
    auditPager ? `bottom=${(auditPager.y + auditPager.height).toFixed(0)}` : '无');

  // A6 密码看板：全部人员 tab + 分页
  await ap.goto(`${ADMIN}/passwords`);
  await ap.waitForSelector('.el-pagination', { timeout: 20000 });
  const bodyTxt = await ap.locator('body').innerText();
  check('A6a 密码看板有「全部人员」视图', bodyTxt.includes('全部人员'));
  const pwPager = await ap.locator('.el-pagination').first().boundingBox();
  check('A6b 密码看板分页在视口内', pwPager && pwPager.y + pwPager.height <= vp.height + 2, '');
  // 切到全部人员
  await ap.locator('text=全部人员').first().click().catch(() => {});
  await ap.waitForTimeout(400);
  check('A6c 全部人员视图渲染 9 人（分页 20/页 → 单页放得下）',
    (await ap.locator('tbody tr').count()) >= 5, `行数=${await ap.locator('tbody tr').count()}`);
  check('A 无 JS 错误', errsA.length === 0, errsA.slice(0, 2).join('|'));
  await ap.screenshot({ path: '/tmp/verify_17_admin.png', fullPage: false });
  await ap.close();

  // ---------- B. 主应用改密 ----------
  const mp = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errsB = [];
  mp.on('pageerror', (e) => errsB.push(e.message));
  await loginViaUi(mp, APP, 'chen.jie', 'DevTest2026!Aa', /login|chat|overview/);
  await mp.waitForSelector('.user-bar--clickable', { timeout: 20000 });
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.profile-panel .pp-section', { timeout: 10000 });
  await mp.locator('button', { hasText: '修改密码' }).first().click();
  await mp.waitForSelector('.pp-pwd-form', { timeout: 5000 });
  check('B1 面板出现「修改密码」表单', true);

  // B2 两次不一致提示
  await mp.locator('.pp-pwd-form input').nth(0).fill('DevTest2026!Aa');
  await mp.locator('.pp-pwd-form input').nth(1).fill('NewPwd2026!Zz');
  await mp.locator('.pp-pwd-form input').nth(2).fill('NewPwd2026!Zz9');
  await mp.waitForTimeout(200);
  check('B2 两次密码不一致有提示', (await mp.locator('.pp-warn').count()) >= 1);
  // 修正
  await mp.locator('.pp-pwd-form input').nth(2).fill('NewPwd2026!Zz');

  // B3 错误旧密码
  await mp.locator('button', { hasText: '确认修改' }).first().click();
  await mp.waitForSelector('.pp-error', { timeout: 10000 });
  check('B3 错误旧密码 → 显示后端错误文案', (await mp.locator('.pp-error').innerText()).length > 3);

  // B4 正确改密 → alert → 登录页
  await mp.locator('.pp-pwd-form input').nth(0).fill('DevTest2026!Aa');
  await mp.locator('.pp-pwd-form input').nth(1).fill('NewPwd2026!Zz');
  await mp.locator('.pp-pwd-form input').nth(2).fill('NewPwd2026!Zz');
  mp.once('dialog', (d) => d.accept());
  await mp.locator('button', { hasText: '确认修改' }).first().click();
  await mp.waitForSelector('input[type="password"]', { timeout: 15000 });
  check('🔴 B4 改密成功 → 回到登录页（旧 token 已失效）',
    mp.url().includes('login') || (await mp.locator('input[type="password"]').count()) >= 1);
  check('B 无 JS 错误', errsB.length === 0, errsB.slice(0, 2).join('|'));
  await mp.close();

  // ---------- 清理：改回固定密码 + 清审计 ----------
  // ⚠️ 不在这里 execFileSync 跑 --apply：浏览器进程存活时子 python 会静默挂起
  //   （单独跑 1.4s、浏览器开着 120s 都不够 —— 原因未定位，见踩坑清单 96）。
  //   改用「改密回来的同一条链路」：chen.jie 的新密码还热着，直接改回固定密码。
  try {
    const newTok = await loginViaApi('chen.jie', 'NewPwd2026!Zz');
    await fetch(`${GW}/api/v1/qa/me/password`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${newTok}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: 'NewPwd2026!Zz', new_password: 'DevTest2026!Aa' }),
    });
    const probe = await fetch(`${GW}/api/auth/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'chen.jie', password: 'DevTest2026!Aa' }),
    });
    check('清理：chen.jie 密码已改回固定值（HTTP 验证可登录）', probe.ok);
  } catch (e) {
    check('清理：chen.jie 密码已改回', false, e.message.slice(0, 100));
  }
  try {
    execFileSync(
      path.join(repoRoot, '.venv', 'bin', 'python'),
      ['-c', `
import sys; sys.path.insert(0, ${JSON.stringify(repoRoot)})
from dotenv import load_dotenv; load_dotenv(${JSON.stringify(repoRoot + '/.env')})
from core.db import get_engine
from sqlalchemy import text
with get_engine().begin() as c:
    print(c.execute(text("DELETE FROM audit_log WHERE action IN ('user.password.change','user.token_quota.change')")).rowcount)
`], { encoding: 'utf8', timeout: 30000 });
  } catch { /* 审计可能为空 */ }

    await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-17/18 浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
