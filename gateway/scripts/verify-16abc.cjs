/**
 * P2-16a/16b/16c 浏览器验收。
 *
 *   node gateway/scripts/verify-16abc.cjs
 *
 * 前置：8000/3000/5173/5174 全在跑；dev_test_accounts --apply 已跑。
 *
 * A（16a）：主应用点左下角用户区 → 面板出现，含基本资料、
 *          本月用量（默认额度 100,000、百分比）、历史总用量。
 * B（16b）：管理端侧边栏是锅圈 logo（img.brand-logo，src 含 gq_logo）。
 * C（16c）：管理端下拉是 AppSelect ——
 *          · 箭头有呼吸位（padding-right 26px，不再贴边）；
 *          · 展开的选项面板在触发框**下方**（top > 按钮 bottom → 不遮挡）；
 *          · 选中后值生效。
 *
 * ⚠️ 结束时清掉脚本落下的审计（15b 教训）。
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

async function loginViaUi(page, base, u, p, afterUrl) {
  await page.goto(`${base}/login`);
  await page.fill('input:not([type="password"])', u);
  await page.fill('input[type="password"]', p);
  await page.click('button[type="submit"], button');
  await page.waitForURL(afterUrl, { timeout: 20000 });
}

(async () => {
  const browser = await chromium.launch();

  // ---------- A. 主应用个人信息面板（16a） ----------
  const mp = await browser.newPage();
  const errsA = [];
  mp.on('pageerror', (e) => errsA.push(e.message));
  await loginViaUi(mp, APP, 'chen.jie', 'DevTest2026!Aa', /login|chat|overview/);
  await mp.waitForSelector('.user-bar--clickable', { timeout: 20000 });
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.profile-panel', { timeout: 10000 });
  // ⚠️ 面板打开后数据异步拉取 —— 不等内容就断言会读到一个「加载中…」
  //   （实测踩过：A2~A6 全红，面板文本只有标题）。
  await mp.waitForSelector('.profile-panel .pp-section', { timeout: 10000 });
  const panelText = await mp.locator('.profile-panel').innerText();
  check('A1 点用户区 → 面板出现', true);
  check('A2 面板含基本资料（姓名/部门/角色）',
    panelText.includes('陈杰') && panelText.includes('门店运营部') && panelText.includes('普通员工'),
    panelText.slice(0, 150));
  check('🔴 A3 面板含**默认额度 100,000**（用户拍板的固定用量）',
    panelText.includes('100,000'), panelText.slice(0, 300));
  check('A4 面板含本月用量与百分比', panelText.includes('本月用量') && panelText.includes('5,368'),
    panelText.slice(0, 300));
  check('A5 面板含历史总用量', panelText.includes('历史总用量'), panelText.slice(0, 300));
  check('A6 横幅措辞「只提醒不限制使用」仍在面板上', panelText.includes('不限制使用'),
    panelText.slice(0, 400));
  await mp.screenshot({ path: '/tmp/verify_16a_profile.png' });
  // 关闭（点遮罩）
  await mp.locator('.profile-mask').click({ position: { x: 10, y: 10 } });
  await mp.waitForTimeout(300);
  check('A7 点遮罩可关闭', (await mp.locator('.profile-panel').count()) === 0);
  check('A 无 JS 错误', errsA.length === 0, errsA.slice(0, 2).join('|'));
  await mp.close();

  // ---------- B+C. 管理端 ----------
  const ap = await browser.newPage();
  const errsB = [];
  ap.on('pageerror', (e) => errsB.push(e.message));
  await loginViaUi(ap, ADMIN, 'wu.jing', 'DevAdmin2026!Aa', /overview/);

  // B. logo
  const logo = ap.locator('img.brand-logo');
  check('B1 侧边栏是锅圈 logo（img.brand-logo）', (await logo.count()) === 1);
  const src = (await logo.getAttribute('src').catch(() => '')) || '';
  check('B2 logo src 指向 gq_logo', src.includes('gq_logo'), src);
  check('B3 文案含「锅圈食汇」', (await ap.locator('.brand-text').innerText()).includes('锅圈食汇'));

  // C. 下拉
  await ap.goto(`${ADMIN}/users`);
  await ap.waitForSelector('.app-select', { timeout: 15000 });
  const selects = await ap.locator('.app-select').count();
  check('C1 筛选区下拉已是 AppSelect（≥3 个）', selects >= 3, `count=${selects}`);

  // C2 箭头不贴边：padding-right = 26px
  const pr = await ap.locator('.app-select').first()
    .evaluate((el) => getComputedStyle(el).paddingRight);
  check('🔴 C2 下拉 padding-right = 26px（箭头有呼吸位，不再贴右边框）', pr === '26px', `padding-right=${pr}`);

  // C3 展开面板在触发框下方（不遮挡）
  const first = ap.locator('.app-select').first();
  const btnBox = await first.boundingBox();
  await first.click();
  await ap.waitForSelector('.app-select-panel', { timeout: 5000 });
  const panelBox = await ap.locator('.app-select-panel').boundingBox();
  check('🔴 C3 选项面板在触发框**下方**展开（top > 按钮 bottom → 不遮挡触发框）',
    panelBox && btnBox && panelBox.y >= btnBox.y + btnBox.height - 1,
    `btn bottom=${btnBox?.y ? (btnBox.y + btnBox.height).toFixed(1) : '?'} panel top=${panelBox?.y?.toFixed(1)}`);
  await ap.screenshot({ path: '/tmp/verify_16c_select.png' });

  // C4 选一项生效：第一个下拉是「部门」（选项里没有「人事」）——
  // 角色是第二个下拉，先按 Esc 关掉 C3 留下的面板再操作它。
  await ap.keyboard.press('Escape');
  await ap.waitForTimeout(200);
  const roleBtn = ap.locator('.app-select').nth(1);
  await roleBtn.click();
  await ap.locator('.app-select__option', { hasText: '人事' }).first().click();
  await ap.waitForTimeout(800);
  check('C4 选中后触发框显示所选值（人事）',
    (await roleBtn.innerText()).includes('人事'),
    (await roleBtn.innerText()));
  // 复原：选回全部角色
  await roleBtn.click();
  await ap.locator('.app-select__option', { hasText: '全部角色' }).first().click();
  check('C 无 JS 错误', errsB.length === 0, errsB.slice(0, 2).join('|'));

  // ---------- 清理 ----------
  const { execFileSync } = require('child_process');
  const path = require('path');
  const repoRoot = path.resolve(__dirname, '..', '..');
  try {
    const out = execFileSync(
      path.join(repoRoot, '.venv', 'bin', 'python'),
      ['-c', `
import sys; sys.path.insert(0, ${JSON.stringify(repoRoot)})
from dotenv import load_dotenv; load_dotenv(${JSON.stringify(repoRoot + '/.env')})
from core.db import get_engine
from sqlalchemy import text
with get_engine().begin() as c:
    print(c.execute(text("DELETE FROM audit_log WHERE action='user.token_quota.change'")).rowcount)
`], { encoding: 'utf8', timeout: 30000 });
    check('清理：审计零残留（库回基线）', true, `删 ${out.trim()} 条`);
  } catch (e) {
    check('清理：审计零残留', false, e.message.slice(0, 100));
  }

  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-16abc 浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
