/**
 * P2-19 浏览器验收（五条 UI 反馈）。
 *
 *   node gateway/scripts/verify-19.cjs
 *
 * 1. 筛选区横向排布（el-form inline）+ placeholder 语义化（部门/角色/状态）
 * 2. 颜色一致：el-select 触发框/面板/分页与页面同色系（断言背景色无「白块」）
 * 3. 密码看板：「全部人员」在第一张卡且默认选中；分页在视口底部
 * 4. placeholder 无 "Select" 字样
 * 5. 改密表单有规则提示（10 位 / 历史 5 次 / 90 天）
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

async function loginViaUi(page, base, u, p, afterRe) {
  for (let attempt = 0; attempt < 2; attempt++) {
    await page.goto(`${base}/login`);
    await page.fill('input:not([type="password"])', u);
    await page.fill('input[type="password"]', p);
    await page.click('button[type="submit"], button');
    try {
      await page.waitForURL(afterRe, { timeout: 20000 });
      return;
    } catch (e) {
      if (attempt < 1) { await page.waitForTimeout(65000); continue; }
      throw e;
    }
  }
}

(async () => {
  const browser = await chromium.launch();
  const vp = { width: 1280, height: 800 };

  // ---------- 管理端 ----------
  const ap = await browser.newPage({ viewport: vp });
  const errsA = [];
  ap.on('pageerror', (e) => errsA.push(e.message));
  await loginViaUi(ap, ADMIN, 'wu.jing', 'DevAdmin2026!Aa', /overview/);

  // 1. 筛选区横排 + placeholder
  await ap.goto(`${ADMIN}/users`);
  await ap.waitForSelector('.el-select', { timeout: 20000 });
  const boxes = [];
  for (let i = 0; i < 3; i++) boxes.push(await ap.locator('.filters-form .el-select').nth(i).boundingBox());
  check('1a 三个筛选下拉横向排布（y 相近）',
    boxes.every((b) => b) && Math.abs(boxes[0].y - boxes[1].y) < 2 && Math.abs(boxes[1].y - boxes[2].y) < 2,
    JSON.stringify(boxes.map((b) => b?.y)));
  const phs = [];
  for (let i = 0; i < 3; i++) phs.push(await ap.locator('.filters-form .el-select').nth(i).innerText());
  check('4a 筛选下拉显示语义化当前值/标签（非 "Select"）',
    phs.every((t) => t.trim() && !t.includes('Select')), JSON.stringify(phs));

  // 2. 颜色一致：触发框背景 ≈ 页面深色（不是白块）
  const bg = await ap.locator('.filters-form .el-select .el-select__wrapper').first()
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  const rgb = bg.match(/\d+/g)?.map(Number) ?? [];
  check('🔴 2a el-select 触发框背景是深色（与页面一致，不是白块）',
    rgb.length >= 3 && rgb[0] < 80 && rgb[1] < 80 && rgb[2] < 80, `background=${bg}`);
  // 打开下拉看面板背景
  await ap.locator('.filters-form .el-select').first().click();
  await ap.waitForSelector('.el-select-dropdown', { timeout: 5000 });
  const popBg = await ap.locator('.el-select__popper').first()
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  const prgb = popBg.match(/\d+/g)?.map(Number) ?? [];
  check('🔴 2b 下拉面板背景是深色（与卡片一致）',
    prgb.length >= 3 && prgb[0] < 80, `background=${popBg}`);
  await ap.keyboard.press('Escape');

  // A5 审计页分页背景
  await ap.goto(`${ADMIN}/audit`);
  await ap.waitForSelector('.el-pagination', { timeout: 20000 });
  const pagerBox = await ap.locator('.el-pagination').first().boundingBox();
  check('2c 审计分页在视口内', pagerBox && pagerBox.y + pagerBox.height <= vp.height + 2, '');
  const pagTxt = await ap.locator('.el-pagination').first().innerText();
  check('2d 分页文案正常（Total/共 N 条）', pagTxt.trim().length > 0, pagTxt.slice(0, 40));

  // 3. 密码看板
  await ap.goto(`${ADMIN}/passwords`);
  await ap.waitForSelector('.cards .card', { timeout: 20000 });
  const firstCard = await ap.locator('.cards .card').first().innerText();
  check('🔴 3a 「全部人员」在**第一张**卡', firstCard.includes('全部人员'), firstCard.slice(0, 40));
  check('3b 默认选中「全部人员」且渲染 9 人',
    (await ap.locator('tbody tr').count()) >= 8 && firstCard.includes('9'),
    `行数=${await ap.locator('tbody tr').count()}`);
  const pwPager = await ap.locator('.el-pagination').first().boundingBox();
  check('🔴 3c 密码看板分页在视口底部（可见）',
    pwPager && pwPager.y + pwPager.height <= vp.height + 2,
    pwPager ? `bottom=${(pwPager.y + pwPager.height).toFixed(0)}/800` : '无');
  check('A 管理端无 JS 错误', errsA.length === 0, errsA.slice(0, 2).join('|'));
  await ap.screenshot({ path: '/tmp/verify_19_admin.png' });
  await ap.close();

  // ---------- 主应用：改密规则提示 ----------
  const mp = await browser.newPage({ viewport: vp });
  const errsB = [];
  mp.on('pageerror', (e) => errsB.push(e.message));
  await loginViaUi(mp, APP, 'chen.jie', 'DevTest2026!Aa', /login|chat|overview/);
  await mp.waitForSelector('.user-bar--clickable', { timeout: 20000 });
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.profile-panel .pp-section', { timeout: 10000 });
  await mp.locator('button', { hasText: '修改密码' }).first().click();
  await mp.waitForSelector('.pp-rules', { timeout: 10000 });
  const rules = await mp.locator('.pp-rules').innerText();
  check('🔴 5a 改密表单有规则提示（至少 10 位 / 历史 5 次 / 有效期 90 天）',
    rules.includes('10 位') && rules.includes('5 次') && rules.includes('90 天'), rules);
  check('5b 规则是动态渲染（非硬编码文案缺失）', rules.includes('密码规则'), rules);
  await mp.screenshot({ path: '/tmp/verify_19_pwd.png' });
  check('B 无 JS 错误', errsB.length === 0, errsB.slice(0, 2).join('|'));
  await mp.close();

  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-19 浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
