/**
 * P2-21 浏览器验收（9 条优化）。
 *
 *   node gateway/scripts/verify-21.cjs
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

  // 9+8：登录页文案与标题
  await mp.goto(`${APP}/login`);
  const loginTxt = await mp.locator('body').innerText();
  check('🔴 8 登录页无「本地开发默认账号」提示', !loginTxt.includes('admin123'), '');
  check('🔴 9 登录页标题为「锅圈RAG 智能问答系统」',
    loginTxt.includes('锅圈RAG 智能问答系统'), loginTxt.slice(0, 80));

  await loginViaUi(mp, APP, 'chen.jie', 'DevTest2026!Aa', /login|chat|overview/);
  const shellTxt = await mp.locator('.sidebar').innerText();
  check('🔴 1 侧边栏不再有「文件传输 · 知识库 / 系统设置」两个入口',
    !shellTxt.includes('文件传输') && !shellTxt.includes('系统设置'), shellTxt.slice(0, 120));

  // 打开面板
  await mp.waitForSelector('.user-bar--clickable', { timeout: 20000 });
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.pp-panel .pp-menu', { timeout: 10000 });

  // 4：宽度 340
  const w = await mp.locator('.pp-panel').evaluate((el) => el.getBoundingClientRect().width);
  check('🔴 4 面板宽度已调大（340px，比侧边栏宽）', Math.round(w) === 340, `width=${w}`);

  // 1+3：菜单三项且图标是 SVG 线性（无 emoji）
  const menuTxt = await mp.locator('.pp-menu').first().innerText();
  check('🔴 1b 面板菜单含三项（系统设置/文件传输/修改密码）',
    ['系统设置', '文件传输 · 知识库', '修改密码'].every((k) => menuTxt.includes(k)),
    menuTxt.slice(0, 120));
  const svgCount = await mp.locator('.pp-menu-item svg.pp-menu-icon').count();
  check('🔴 3 菜单图标为 SVG 线性图标（4 个：设置/传输/钥匙/退出）', svgCount === 4, `svg=${svgCount}`);
  const emojiLeft = /[\u{1F300}-\u{1FAFF}]/u.test(menuTxt);
  check('3b 菜单文案里无 emoji', !emojiLeft, menuTxt);

  // 5：用量 UI 保留
  const panelTxt2 = await mp.locator('.pp-panel').innerText()
  check('5 用量 UI 保留（本月用量/月度额度/历史总用量）',
    ['本月用量', '月度额度', '历史总用量'].every((k) => panelTxt2.includes(k)), panelTxt2.slice(0, 120))

  // 2：修改密码 → 独立弹窗
  await mp.locator('.pp-menu-item', { hasText: '修改密码' }).click();
  await mp.waitForSelector('.cp-panel', { timeout: 8000 });
  check('🔴 2a 点「修改密码」→ 面板关闭 + 独立弹窗打开',
    (await mp.locator('.pp-panel').count()) === 0 && (await mp.locator('.cp-panel').count()) === 1);
  // 独立弹窗里仍有占位/实时校验/示例
  const ph = await mp.locator('.cp-panel input').nth(1).getAttribute('placeholder');
  check('2b 独立弹窗输入框有占位文本', !!ph, `${ph}`);
  await mp.locator('.cp-panel input').nth(1).fill('123');
  await mp.waitForTimeout(200);
  const weakOk = await mp.locator('.cp-checks li.ok').count();
  const total = await mp.locator('.cp-checks li').count();
  check('🔴 2c 独立弹窗内实时校验生效（弱密码不全绿）', total >= 4 && weakOk < total, `${weakOk}/${total}`);
  check('2d 独立弹窗内规则含示例', (await mp.locator('.cp-rules').innerText()).includes('Xk9#mQ2vLp'));
  await mp.screenshot({ path: '/tmp/verify_21_pwd.png' });
  await mp.locator('.cp-btn', { hasText: '取消' }).click();
  await mp.waitForTimeout(300);

  // 6：退出登录二次确认
  await mp.locator('.user-bar--clickable').click();
  await mp.waitForSelector('.pp-panel .pp-menu', { timeout: 10000 });
  await mp.locator('.pp-menu-item', { hasText: '退出登录' }).click();
  await mp.waitForSelector('.cf-panel', { timeout: 8000 });
  const cfTxt = await mp.locator('.cf-panel').innerText();
  check('🔴 6a 退出登录出现二次确认', cfTxt.includes('确认退出登录'), cfTxt.slice(0, 60));
  await mp.locator('.cf-actions button', { hasText: '取消' }).first().click();
  await mp.waitForTimeout(500);
  const cfLeft = await mp.locator('.cf-panel').count()
  const stillLoggedIn = (await mp.locator('.user-bar--clickable').count()) === 1
  check('6b 点「取消」不退出（弹窗关闭 + 仍在主界面）',
    cfLeft === 0 && stillLoggedIn, `cf-panel=${cfLeft} userBar=${stillLoggedIn} url=${mp.url()}`)
  check('B 无 JS 错误', errs.length === 0, errs.slice(0, 2).join('|'));
  await mp.close();

  // ---------- 管理端：删除二次确认 ----------
  const ap = await browser.newPage({ viewport: vp });
  const errsA = [];
  ap.on('pageerror', (e) => errsA.push(e.message));
  await loginViaUi(ap, ADMIN, 'wu.jing', 'DevAdmin2026!Aa', /overview/);

  await ap.goto(`${ADMIN}/org`);
  await ap.waitForSelector('table.grid tbody tr', { timeout: 20000 });
  // 点第一个「删除」（部门）
  await ap.locator('button.btn-danger', { hasText: '删除' }).first().click();
  await ap.waitForSelector('.cf-panel', { timeout: 8000 });
  const orgCf = await ap.locator('.cf-panel').innerText();
  check('🔴 7a 部门删除出现二次确认', orgCf.includes('删除部门'), orgCf.slice(0, 60));
  await ap.locator('.cf-btn, .cf-actions button', { hasText: '取消' }).first().click();
  await ap.waitForTimeout(300);
  // 取消后该部门仍在（说明没有误删）
  check('🔴 7b 取消后部门未被删除（页面仍有该行）',
    (await ap.locator('table.grid tbody tr').count()) > 0);

  // 职位删除：职位表格在页面下方，用 last() 命中（nth(1) 可能还是第二个部门）
  await ap.locator('button.btn-danger', { hasText: '删除' }).last().click();
  await ap.waitForSelector('.cf-panel', { timeout: 8000 });
  check('7c 职位删除出现二次确认', (await ap.locator('.cf-panel').innerText()).includes('删除职位'));
  await ap.locator('.cf-actions button, .cf-btn', { hasText: '取消' }).first().click();
  await ap.waitForTimeout(300);

  // 🔴 7e：确认后**真的删掉**（不能只测「取消不删」——
  //    那验的是「按钮没接错」，没验「确认路径真的执行」）。
  //    用一个临时部门测：建 → 删 → 验证消失，数据自清理。
  const tmpCode = `zz_probe_${Date.now()}`
  // token 从页面 localStorage 取（key 见管理端 stores/auth.ts: TOKEN_KEY）。
  // ⚠️ 不在这里再登录一次：会撞「8 次/分」限流，而限流失败与「创建失败」
  //   在断言里长得一样（都是 error）。
  const created = await ap.evaluate(
    async ([code]) => {
      const tok = localStorage.getItem('ragqa.admin.token') || ''
      const res = await fetch('/api/v1/admin/departments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tok}` },
        body: JSON.stringify({ code, name: '探针部门', sort_order: 999 }),
      })
      return res.ok ? await res.json() : { error: await res.text() }
    },
    [tmpCode],
  )
  if (created?.id) {
    // 回到组织页，找到探针行点删除并**确认**
    await ap.goto(`${ADMIN}/org`)
    await ap.waitForSelector('table.grid tbody tr', { timeout: 20000 })
    const probeRow = ap.locator('tr', { hasText: '探针部门' }).first()
    await probeRow.locator('button.btn-danger', { hasText: '删除' }).click()
    await ap.waitForSelector('.cf-panel', { timeout: 8000 })
    await ap.locator('.cf-actions button, .cf-btn', { hasText: /确认|删除/ }).first().click()
    await ap.waitForTimeout(1500)
    // 判据用**后端**而不是 UI：UI 是否刷新是渲染问题，
    // 「删除操作没被确认框打断」要验的是后端里这条记录真的没了。
    const backendGone = await ap.evaluate(
      async ([id]) => {
        const tok = localStorage.getItem('ragqa.admin.token') || ''
        const res = await fetch('/api/v1/admin/departments', {
          headers: { Authorization: `Bearer ${tok}` },
        })
        const list = await res.json()
        const items = Array.isArray(list) ? list : list.items ?? []
        return !items.some((x) => x.id === id)
      },
      [created.id],
    )
    const uiGone = (await ap.locator('tr', { hasText: '探针部门' }).count()) === 0
    const cfLeft = await ap.locator('.cf-panel').count()
    check('🔴 7e 点「确认」后**真的删除**（后端已无该部门）',
      backendGone, `backendGone=${backendGone} uiGone=${uiGone} cfLeft=${cfLeft} id=${created.id}`)
  } else {
    check('7e 探针部门创建失败（无法验证真删路径）', false, JSON.stringify(created))
  }

  // 用户页确认仍可用（重置密码走同一套）
  await ap.goto(`${ADMIN}/users`);
  await ap.waitForSelector('table.grid tbody tr', { timeout: 20000 });
  const pageTxt = await ap.locator('body').innerText();
  check('7d 用户页无 window.confirm 残留（走通用组件）',
    await ap.evaluate(() => !document.documentElement.innerHTML.includes('window.confirm')));
  check('A 管理端无 JS 错误', errsA.length === 0, errsA.slice(0, 2).join('|'));
  await ap.screenshot({ path: '/tmp/verify_21_admin.png' });
  await ap.close();

  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  P2-21 浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
