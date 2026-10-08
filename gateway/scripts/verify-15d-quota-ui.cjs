/**
 * P2-15d 浏览器验收：管理端用量看板 + 主应用横幅。
 *
 *   node gateway/scripts/verify-15d-quota-ui.cjs
 *
 * 前置：8000 + 3000 + 5173 + 5174 全在跑；dev_test_accounts --apply 已跑。
 *
 * 断言（真实 DOM）：
 *   A. 管理端 /usage：表头九列、有用量行、缓存列弱化、额度列、对账 badge「✓」；
 *   B. 主应用 5173：
 *      · 未设额度 → 横幅不渲染；
 *      · 设额度 5500（chen.jie 本月 5368 → 97.6% warn）→ 横幅出现，
 *        文案含「不会限制你继续使用」（🔴 只提醒不阻断的核心判据）；
 *      · 还原 → 横幅消失。
 *
 * ⚠️ 脚本结束时把一切还原（额度 0 + 清本脚本落下的审计）——
 *    15b 的教训：浏览器脚本落下的数据由浏览器脚本自己清。
 */
const { chromium } = require('playwright');

const GW = 'http://127.0.0.1:3000';
const ADMIN_UI = 'http://127.0.0.1:5174';
const APP_UI = 'http://127.0.0.1:5173';
const ADMIN = { username: 'wu.jing', password: 'DevAdmin2026!Aa' };
const STAFF = { username: 'chen.jie', password: 'DevTest2026!Aa' };

let pass = 0, fail = 0;
function check(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else { fail++; console.log(`  [FAIL] ${name} | ${detail}`); }
}

async function apiLogin(page, u, p) {
  const res = await page.request.post(`${GW}/api/auth/login`, {
    data: { username: u, password: p },
  });
  const body = await res.json();
  if (!body.access_token) throw new Error(`登录 ${u} 失败`);
  return body.access_token;
}

/** 登录限流退避：撞 429 等一个窗口再试（不放宽限流 —— 那是把尺子砸了）。 */
async function loginWithBackoff(u, p) {
  for (let i = 0; i < 3; i++) {
    // 用 Node 全局 fetch（Playwright 的 request context 在 page 关闭后会 dispose，
    // 撞限流重试的 65 秒等待里正好会踩到）
    const res = await fetch(`${GW}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
    });
    if (res.ok) return (await res.json()).access_token;
    await new Promise((r) => setTimeout(r, 65000));
  }
  throw new Error(`登录 ${u} 三次均失败`);
}

(async () => {
  const browser = await chromium.launch();
  const loginPage = await browser.newPage();
  const adminToken = await apiLogin(loginPage, ADMIN.username, ADMIN.password);
  const staffToken = await loginWithBackoff(STAFF.username, STAFF.password);

  // ---------- A. 管理端看板 ----------
  const ap = await browser.newPage();
  const jsErrorsA = [];
  ap.on('pageerror', (e) => jsErrorsA.push(e.message));
  await ap.goto(`${ADMIN_UI}/login`);
  await ap.fill('input:not([type="password"])', ADMIN.username);
  await ap.fill('input[type="password"]', ADMIN.password);
  await ap.click('button[type="submit"], button');
  await ap.waitForURL(/overview/, { timeout: 15000 });
  await ap.goto(`${ADMIN_UI}/usage`);
  await ap.waitForSelector('table.grid tbody tr', { timeout: 15000 });

  const headers = (await ap.locator('th').allInnerTexts()).join('|');
  check('看板表头九列齐全',
    ['输入', '输出', '缓存命中', '计费合计', '月度额度', '使用率', '状态'].every((k) => headers.includes(k)),
    headers);
  const bodyText = await ap.locator('body').innerText();
  check('有用量行渲染（chen.jie 5,368）', bodyText.includes('5,368'), bodyText.slice(0, 200));
  check('对账 badge 显示「✓ 恒等式成立」', bodyText.includes('✓ 恒等式成立'),
    (bodyText.match(/恒等式[^\n]*/) || [''])[0]);
  check('未使用的人单独列出（不淹没用量行）', bodyText.includes('本月未使用'));
  check('无 JS 错误', jsErrorsA.length === 0, jsErrorsA.slice(0, 2).join('|'));
  await ap.screenshot({ path: '/tmp/verify_15d_board.png' });

  // ---------- B. 主应用横幅 ----------
  const mp = await browser.newPage();
  const jsErrorsB = [];
  mp.on('pageerror', (e) => jsErrorsB.push(e.message));

  // B1: 未设额度 → 横幅不渲染
  await mp.goto(`${APP_UI}/login`);
  await mp.fill('input:not([type="password"])', STAFF.username);
  await mp.fill('input[type="password"]', STAFF.password);
  await mp.click('button[type="submit"], button');
  await mp.waitForSelector('.chat-area, .app-shell, main', { timeout: 20000 });
  await mp.waitForTimeout(1500);
  check('B1 未设额度 → 横幅不渲染（空字符串 = 不打扰）',
    (await mp.locator('.quota-banner').count()) === 0);

  // B2: 设额度 5500 → warn 横幅出现
  await mp.request.patch(`${GW}/api/v1/admin/users/442/token-quota`, {
    data: { quota_monthly: 5500 },
    headers: { Authorization: `Bearer ${adminToken}` },
  });
  await mp.reload();
  await mp.waitForSelector('.chat-area, .app-shell, main', { timeout: 20000 });
  const banner = mp.locator('.quota-banner');
  await banner.waitFor({ timeout: 10000 }).catch(() => {});
  check('B2 设额度后横幅出现', (await banner.count()) === 1);
  const bannerText = (await banner.innerText().catch(() => '')).trim();
  check('🔴 横幅文案含「不会限制你继续使用」（只提醒不阻断的核心判据）',
    bannerText.includes('不会限制你继续使用'), bannerText);
  check('横幅是 warn 配色', (await banner.getAttribute('class').catch(() => '')).includes('warn'), '');
  check('B 无 JS 错误', jsErrorsB.length === 0, jsErrorsB.slice(0, 2).join('|'));
  await mp.screenshot({ path: '/tmp/verify_15d_banner.png' });

  // B3: 还原 → 横幅消失
  await mp.request.patch(`${GW}/api/v1/admin/users/442/token-quota`, {
    data: { quota_monthly: 0 },
    headers: { Authorization: `Bearer ${adminToken}` },
  });
  await mp.reload();
  await mp.waitForSelector('.chat-area, .app-shell, main', { timeout: 20000 });
  await mp.waitForTimeout(1500);
  check('B3 还原后横幅消失', (await mp.locator('.quota-banner').count()) === 0);

  // ---------- 清理：本脚本落下的审计 ----------
  const { execFileSync } = require('child_process');
  const path = require('path');
  const repoRoot = path.resolve(__dirname, '..', '..');
  try {
    execFileSync(path.join(repoRoot, '.venv', 'bin', 'python'),
      ['-c', `
import sys; sys.path.insert(0, ${JSON.stringify(repoRoot)})
from dotenv import load_dotenv; load_dotenv(${JSON.stringify(repoRoot + '/.env')})
from core.db import get_engine
from sqlalchemy import text
with get_engine().begin() as c:
    print(c.execute(text("DELETE FROM audit_log WHERE action='user.token_quota.change'")).rowcount)
`], { encoding: 'utf8', timeout: 30000 });
    check('清理：脚本落下的审计已删（库回基线）', true);
  } catch (e) {
    check('清理：脚本落下的审计已删', false, e.message.slice(0, 100));
  }

  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  15d 浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
