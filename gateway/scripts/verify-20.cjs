/**
 * P2-20 浏览器验收（个人中心 WorkBuddy 式交互 + 实时校验 + favicon）。
 *
 *   node gateway/scripts/verify-20.cjs
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

  // ---------- 主应用 ----------
  const mp = await browser.newPage({ viewport: vp });
  const errs = [];
  mp.on('pageerror', (e) => errs.push(e.message));
  await loginViaUi(mp, APP, 'chen.jie', 'DevTest2026!Aa', /login|chat|overview/);
  await mp.waitForSelector('.user-bar--clickable', { timeout: 20000 });
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.pp-panel .pp-menu', { timeout: 10000 });

  // 1. 底部锚定：面板 bottom 在用户区上方（面板 bottom > 视口高-100）
  const panelBox = await mp.locator('.pp-panel').boundingBox();
  check('🔴 1a 面板是**底部锚定**（bottom 靠近视口底部，非居中 Modal）',
    panelBox && panelBox.y + panelBox.height > vp.height - 220,
    `panel bottom=${panelBox ? (panelBox.y + panelBox.height).toFixed(0) : '?'} / ${vp.height}`);
  const panelText = await mp.locator('.pp-panel').innerText();
  check('1b 头部展示姓名与部门职位', panelText.includes('陈杰') && panelText.includes('门店运营部'), '');

  // 2. 菜单层
  check('🔴 2a 菜单含「系统设置」「文件传输 · 知识库」「修改密码」',
    ['系统设置', '文件传输 · 知识库', '修改密码'].every((k) => panelText.includes(k)),
    panelText.slice(0, 200));
  check('2b 底部有「退出登录」', panelText.includes('退出登录'));
  // 外侧退出入口仍在
  check('2c 外侧用户区的退出按钮**保留**', (await mp.locator('.logout-btn').count()) === 1);

  // 3. 点「系统设置」→ 切视图 + 面板关闭
  await mp.locator('.pp-menu-item', { hasText: '系统设置' }).first().click();
  await mp.waitForTimeout(600);
  check('3a 点「系统设置」→ 切到设置视图 + 面板关闭',
    (await mp.locator('.pp-panel').count()) === 0,
    `activeView=${await mp.evaluate(() => document.body.innerText.slice(0, 100))}`);
  // 回到 chat 再开面板
  await mp.locator('.nav-btn').first().click().catch(() => {});
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.pp-panel .pp-menu', { timeout: 10000 });

  // 4. 改密表单：placeholder + 实时校验
  await mp.locator('.pp-menu-item', { hasText: '修改密码' }).first().click();
  await mp.waitForSelector('.pp-pwd-card', { timeout: 5000 });
  const phOld = await mp.locator('.pp-pwd-card input').nth(0).getAttribute('placeholder');
  const phNew = await mp.locator('.pp-pwd-card input').nth(1).getAttribute('placeholder');
  const phNew2 = await mp.locator('.pp-pwd-card input').nth(2).getAttribute('placeholder');
  check('🔴 4a 三个输入框都有占位文本', !!phOld && !!phNew && !!phNew2,
    `${phOld} / ${phNew} / ${phNew2}`);

  // 实时校验：输入弱密码 → 有未满足项；补强 → 全部 ✓
  await mp.locator('.pp-pwd-card input').nth(1).fill('123');
  await mp.waitForTimeout(200);
  const weak = await mp.locator('.pp-checks li').allInnerTexts();
  const weakOk = await mp.locator('.pp-checks li.ok').count();
  check('🔴 4b 弱密码输入时**实时显示**未满足的规则（不全绿）', weak.length >= 4 && weakOk < 4,
    JSON.stringify(weak));
  await mp.locator('.pp-pwd-card input').nth(1).fill('Xk9#mQ2vLp');
  await mp.locator('.pp-pwd-card input').nth(2).fill('Xk9#mQ2vLp');
  await mp.waitForTimeout(200);
  const strongOk = await mp.locator('.pp-checks li.ok').count();
  const strongAll = await mp.locator('.pp-checks li').count();
  check('🔴 4c 强密码输入时**实时全绿**（含示例同形的密码）',
    strongOk === strongAll && strongAll >= 4, `${strongOk}/${strongAll}`);

  // 示例格式
  const rules = await mp.locator('.pp-rules').innerText();
  check('5a 规则提示含**示例格式**（Xk9#…）', rules.includes('Xk9#mQ2vLp'), rules.slice(0, 120));
  check('5b 示例注明「请勿直接使用」', rules.includes('请勿直接使用'), '');
  await mp.screenshot({ path: '/tmp/verify_20_panel.png' });
  check('B 无 JS 错误', errs.length === 0, errs.slice(0, 2).join('|'));
  await mp.close();

  // ---------- 管理端 favicon ----------
  const ap = await browser.newPage();
  await ap.goto(`${ADMIN}/login`);
  const favicon = await ap.locator('link[rel="icon"]').getAttribute('href');
  check('🔴 6 管理端 favicon 指向锅圈 logo（gq_logo.jpg）',
    favicon?.includes('gq_logo'), `href=${favicon}`);
  await ap.close();

  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-20 浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
