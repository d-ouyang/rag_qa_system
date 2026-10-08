/**
 * P2-19b 验收：审计日志筛选区（用户截图指出的遗漏页）。
 *
 *   node gateway/scripts/verify-19b-audit.cjs
 *
 * 断言：
 *   1. 筛选区一行横排（4 个控件同一行）
 *   2. 两个下拉不再显示 "Select"（空值时显示「全部动作/全部对象」）
 *   3. 分页在视口内
 */
const { chromium } = require('playwright');

const GW = 'http://127.0.0.1:3000';
const ADMIN = 'http://127.0.0.1:5174';

let pass = 0, fail = 0;
function check(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else { fail++; console.log(`  [FAIL] ${name} | ${detail}`); }
}

(async () => {
  const browser = await chromium.launch();
  const ap = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errs = [];
  ap.on('pageerror', (e) => errs.push(e.message));

  // 登录（UI）
  for (let attempt = 0; attempt < 2; attempt++) {
    await ap.goto(`${ADMIN}/login`);
    await ap.fill('input:not([type="password"])', 'wu.jing');
    await ap.fill('input[type="password"]', 'DevAdmin2026!Aa');
    await ap.click('button[type="submit"], button');
    try {
      await ap.waitForURL(/overview/, { timeout: 20000 });
      break;
    } catch (e) {
      if (attempt < 1) { await ap.waitForTimeout(65000); continue; }
      throw e;
    }
  }

  await ap.goto(`${ADMIN}/audit`);
  await ap.waitForSelector('.filters-form .el-select', { timeout: 20000 });

  // 1. 横排：四个控件同一行
  const boxes = [];
  for (let i = 0; i < 4; i++) {
    boxes.push(await ap.locator('.filters-form .el-form-item').nth(i).boundingBox());
  }
  const ys = boxes.map((b) => Math.round(b?.y ?? -1));
  check('1a 筛选四控件一行横排（y 相近）',
    ys.every((y) => y >= 0) && Math.max(...ys) - Math.min(...ys) < 3, JSON.stringify(ys));

  // 2. 下拉显示语义值（非 "Select"）
  const texts = [];
  for (let i = 0; i < 2; i++) {
    texts.push(await ap.locator('.filters-form .el-select').nth(i).innerText());
  }
  check('🔴 2a 下拉不再显示 "Select"（显示「全部动作/全部对象」）',
    texts.every((t) => t.trim() && !t.includes('Select')), JSON.stringify(texts));
  const pageTxt = await ap.locator('body').innerText();
  check('2b 整页无 "Select" 字样', !pageTxt.includes('Select'), '');

  // 3. 分页在视口内
  const pager = await ap.locator('.el-pagination').first().boundingBox();
  const vp = ap.viewportSize();
  check('3a 分页在视口内', pager && pager.y + pager.height <= vp.height + 2,
    pager ? `bottom=${(pager.y + pager.height).toFixed(0)}/800` : '无');

  check('无 JS 错误', errs.length === 0, errs.slice(0, 2).join('|'));
  await ap.screenshot({ path: '/tmp/verify_19b_audit.png' });
  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-19b 审计页验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
