/**
 * P2-21c 验收：文案通俗化 + toast 位置。
 *   node gateway/scripts/verify-21c.cjs
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

const TERMS = ['服务端', '后端', '前端', '客户端', 'token_version'];

(async () => {
  const browser = await chromium.launch();
  const vp = { width: 1280, height: 800 };

  // ---------- 主应用：退出确认文案 ----------
  const mp = await browser.newPage({ viewport: vp });
  await loginViaUi(mp, APP, 'chen.jie', 'DevTest2026!Aa', /login|chat|overview/);
  await mp.waitForSelector('.user-bar--clickable', { timeout: 20000 });
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.pp-panel .pp-menu', { timeout: 10000 });
  await mp.locator('.pp-menu-item', { hasText: '退出登录' }).click();
  await mp.waitForSelector('.cf-panel', { timeout: 8000 });
  const cf = await mp.locator('.cf-panel').innerText();
  check('🔴 1 退出确认文案改为你给的版本', cf.includes('退出后需要用账号密码重新登录。你的会话记录与知识库内容都不会丢失。'), cf);
  check('1b 确认弹窗无专业术语', !TERMS.some((t) => cf.includes(t)), cf);
  await mp.locator('.cf-actions button', { hasText: '取消' }).first().click();
  await mp.waitForTimeout(300);
  await mp.close();

  // ---------- 管理端 ----------
  const ap = await browser.newPage({ viewport: vp });
  await loginViaUi(ap, ADMIN, 'wu.jing', 'DevAdmin2026!Aa', /overview/);

  // 2. 部门删除文案
  await ap.goto(`${ADMIN}/org`);
  await ap.waitForSelector('table.grid tbody tr', { timeout: 20000 });
  await ap.locator('button.btn-danger', { hasText: '删除' }).first().click();
  await ap.waitForSelector('.cf-panel', { timeout: 8000 });
  const deptCf = await ap.locator('.cf-panel').innerText();
  check('🔴 2 部门删除文案改为你给的版本',
    deptCf.includes('删除后不可恢复。若该部门下还有子部门或成员，则无法删除。'), deptCf);
  check('2b 无专业术语', !TERMS.some((t) => deptCf.includes(t)), deptCf);
  await ap.locator('.cf-actions button', { hasText: '取消' }).first().click();
  await ap.waitForTimeout(300);

  // 3. 职位删除文案（职位表在下方，last()）
  await ap.locator('button.btn-danger', { hasText: '删除' }).last().click();
  await ap.waitForSelector('.cf-panel', { timeout: 8000 });
  const posCf = await ap.locator('.cf-panel').innerText();
  check('🔴 3 职位删除文案改为你给的版本',
    posCf.includes('删除后不可恢复。若还有员工挂在该职位上，则无法删除。'), posCf);
  await ap.locator('.cf-actions button', { hasText: '取消' }).first().click();
  await ap.waitForTimeout(300);

  // 4. toast 位置：中间顶部（触发一次 toast：切到审计页点清空筛选会 toast？直接断言样式）
  await ap.goto(`${ADMIN}/audit`);
  await ap.waitForSelector('.toasts, .el-pagination', { timeout: 20000 });
  // toast 不常驻 —— 断言组件样式（computed style of .toasts container 可能不存在）
  const toastStyle = await ap.evaluate(() => {
    const el = document.querySelector('.toasts')
    if (!el) return null
    const cs = getComputedStyle(el)
    return { left: cs.left, right: cs.right, transform: cs.transform }
  })
  if (toastStyle) {
    check('🔴 4 toast 在中间顶部（left/right 0 + margin auto 居中）',
      toastStyle.left === '0px' && toastStyle.right === '0px',
      JSON.stringify(toastStyle))
  } else {
    // 无 toast 时从样式表断言
    const fromCss = await ap.evaluate(() => {
      for (const sheet of document.styleSheets) {
        try {
          for (const rule of sheet.cssRules) {
            if (rule.selectorText && rule.selectorText.includes('.toasts') && rule.style.position === 'fixed') {
              return { left: rule.style.left, transform: rule.style.transform, right: rule.style.right }
            }
          }
        } catch { /* 跨域样式表跳过 */ }
      }
      return null
    })
    check('🔴 4 toast 样式为中间顶部（left/right 0 + margin auto）',
      fromCss && fromCss.left === '0px' && fromCss.right === '0px',
      JSON.stringify(fromCss))
  }

  // 登录页脚注（术语清理）。⚠️ 先清登录态 —— 已登录访问 /login 会被路由守卫
  //   重定向回 overview，页面上自然找不到登录页文案（第一版断言就栽在这）。
  await ap.evaluate(() => localStorage.clear())
  await ap.goto(`${ADMIN}/login`);
  const loginTxt = await ap.locator('body').innerText();
  check('5a 管理端登录页脚注改为「仅限管理与人事账号」',
    loginTxt.includes('本入口仅限管理与人事账号使用') && !loginTxt.includes('鉴权网关'),
    loginTxt.slice(0, 150));

  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-21c 验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
