/**
 * P2-14f 浏览器验收：管理端能下发 / 收回知识库写权限。
 *
 * 验的是**界面真的能用**，不是「接口能用」——
 * 14f 的后端已被 test_module12_kb_role.py 覆盖（44 项），
 * 那个 TestClient 喂不到真实 DOM。这里补的是另一段路：
 * 列表页那一列标签、行内下拉的选项来源、确认弹窗的文案、审计是否落库。
 *
 * 用法：node scripts/verify-14f-admin-ui.cjs
 * 前置：后端 8000 / 网关 3000 / 管理端 5174 全在。
 */
const { chromium } = require('playwright');

const ADMIN = process.env.ADMIN_URL || 'http://localhost:5174';
const USERNAME = 'wu.jing';
const PASSWORD = 'DevAdmin2026!Aa';

async function login(page) {
  await page.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('input[type=text]', { timeout: 20000 });
  await page.fill('input[type=text]', USERNAME);
  await page.fill('input[type=password]', PASSWORD);
  await page.click('button[type=submit]');
  await page
    .waitForFunction(() => !document.querySelector('input[type=password]'), { timeout: 20000 })
    .catch(() => {});
  await page.waitForTimeout(1500);
}

(async () => {
  const browser = await chromium.launch({ args: ['--no-proxy-server'] });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + String(e).slice(0, 200)));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });

  let fail = 0;
  const check = (name, ok, detail = '') => {
    console.log(`  ${ok ? '[PASS]' : '[FAIL]'} ${name}${ok ? '' : ' | ' + detail}`);
    if (!ok) fail += 1;
  };

  try {
    await login(page);
    check('管理端登录成功（wu.jing / 系统管理员）',
      !(await page.locator('input[type=password]').count()),
      '还停在登录页');

    await page.goto(ADMIN + '/users', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('table.grid tbody tr', { timeout: 20000 });
    await page.waitForTimeout(1200);

    const headers = await page.locator('table.grid thead th').allInnerTexts();
    check('表头有「知识库写权限」这一列', headers.some((h) => h.includes('知识库写权限')),
      JSON.stringify(headers));

    const rowCount = await page.locator('table.grid tbody tr').count();
    check('员工列表渲染出来了', rowCount > 0, `行数=${rowCount}`);

    // 每一行都该有一个「知识库写权限」badge，且**标签不是空的**
    const badges = await page.locator('table.grid tbody tr td:nth-child(6) .badge').allInnerTexts();
    check('每行都有档位标签', badges.length === rowCount, `badge 数=${badges.length} 行数=${rowCount}`);
    check('档位标签是中文而不是原始值（说明用的是后端下发的 label）',
      badges.length > 0 && badges.every((b) => !/^(none|ops|qa|dev|superadmin)$/.test(b.trim())),
      JSON.stringify(badges.slice(0, 5)));

    // 行内下拉：选项必须是五档，且与 badge 一致
    const selects = page.locator('table.grid tbody tr td.ops select.select.inline');
    const selCount = await selects.count();
    // 每行两个下拉（状态 + 角色）+ 知识库权限（自己那行没有）→ 至少应有 3 个/行
    check('行内下拉数量符合预期（状态 + 角色 + 知识库权限，自己那行少一个）',
      selCount >= rowCount * 2, `下拉数=${selCount} 行数=${rowCount}`);

    // 找 zhao.min 那一行，读它的 kb 下拉当前值
    const targetRow = page.locator('table.grid tbody tr', { hasText: 'zhao.min' });
    check('能找到 zhao.min 那一行（拿他当授权对象）', (await targetRow.count()) === 1,
      `匹配行数=${await targetRow.count()}`);
    const kbSel = targetRow.locator('select').nth(2);
    const beforeVal = await kbSel.inputValue();
    const optLabels = await kbSel.locator('option').allInnerTexts();
    check('知识库权限下拉有五个选项', optLabels.length === 5, JSON.stringify(optLabels));
    check('下拉当前值与库里一致（none）', beforeVal === 'none', `实际=${beforeVal}`);

    // 真改一次：none -> ops
    await kbSel.selectOption('ops');
    await page.waitForTimeout(700);
    const confirmText = await page.locator('.confirm-text').innerText().catch(() => '');
    check('弹出了二次确认', confirmText.length > 0, '没找到确认弹窗');
    check('确认文案说清了他将能做什么',
      /上传/.test(confirmText), confirmText.slice(0, 160));
    check('确认文案提醒了「知识库是全公司共用的」',
      /全公司共用|整库/.test(confirmText), confirmText.slice(0, 200));

    await page.locator('.modal, [role=dialog]').locator('button', { hasText: /确定|确认|保存/ }).last().click()
      .catch(async () => { await page.keyboard.press('Enter'); });
    await page.waitForTimeout(1800);

    const afterVal = await targetRow.locator('select').nth(2).inputValue();
    check('下拉已变成 ops（真下发成功）', afterVal === 'ops', `实际=${afterVal}`);
    const afterBadge = await targetRow.locator('td:nth-child(6) .badge').innerText();
    check('档位 badge 同步更新', !/none|只读/.test(afterBadge), `badge=${afterBadge}`);
    await page.screenshot({ path: '/tmp/admin_14f_granted.png' });

    // 收回：ops -> none
    await targetRow.locator('select').nth(2).selectOption('none');
    await page.waitForTimeout(700);
    const confirmText2 = await page.locator('.confirm-text').innerText().catch(() => '');
    check('收回时文案更重（提示对方不会收到通知）',
      /不会|收回|⚠/.test(confirmText2), confirmText2.slice(0, 200));
    await page.locator('.modal, [role=dialog]').locator('button', { hasText: /确定|确认|保存/ }).last().click()
      .catch(async () => { await page.keyboard.press('Enter'); });
    await page.waitForTimeout(1800);
    const backVal = await targetRow.locator('select').nth(2).inputValue();
    check('已收回为 none（库回到原样）', backVal === 'none', `实际=${backVal}`);
    await page.screenshot({ path: '/tmp/admin_14f_revoked.png' });

    // 自己那一行的三个下拉全部不渲染（`v-if="!isSelf"` 覆盖状态/角色/知识库权限）
    // ⚠️ 断言是「0 个」而不是「少了 1 个」—— 三个下拉共用同一个 v-if，
    // 所以自己那行是**一个都没有**，不是「只有两个」。
    const selfRow = page.locator('table.grid tbody tr', { hasText: 'wu.jing' });
    const selfSelCount = await selfRow.locator('select').count();
    check('自己那一行没有任何行内下拉（状态/角色/知识库权限都不给）',
      selfSelCount === 0, `下拉数=${selfSelCount}（预期 0）`);
    const selfBadge = await selfRow.locator('td:nth-child(6) .badge').innerText();
    check('自己那一行仍显示档位徽章（不给入口 ≠ 不让人知道）',
      selfBadge.trim().length > 0, `badge=${JSON.stringify(selfBadge)}`);
    check('徽章文案带能力描述（后端下发的 label 本身含「可上传 / 删除 / 重灌索引」）',
      /可上传|上传/.test(selfBadge) && /删除/.test(selfBadge), selfBadge);
    const selfText = await selfRow.innerText();
    check('自己那一行有「（自己）」标记', selfText.includes('自己'), selfText.slice(0, 120));

    // 审计页能看到刚才那两次
    await page.goto(ADMIN + '/audit', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1800);
    const auditText = await page.locator('body').innerText();
    check('审计页出现「变更知识库写权限」这条动作（中文标签而非英文动作名）',
      auditText.includes('变更知识库写权限'),
      auditText.slice(0, 300));
    await page.screenshot({ path: '/tmp/admin_14f_audit.png' });

    check('全程无 JS 错误', errs.length === 0, JSON.stringify(errs));
  } finally {
    await ctx.close();
    await browser.close();
  }
  console.log(`\n  ${fail === 0 ? '全部通过' : fail + ' 项失败'}`);
  process.exit(fail === 0 ? 0 : 1);
})();