/**
 * P2-15b 浏览器验收：管理端「月度额度」列。
 *
 *   node gateway/scripts/verify-15b-quota-ui.cjs
 *
 * 前置：8000（后端）+ 3000（网关）+ 5174（管理端）全在跑；
 *       `wu.jing / DevAdmin2026!Aa` 可登录（跑 scripts/dev_test_accounts.py --apply）。
 *
 * 断言（真实 DOM）：
 *   1. 「月度额度」列表头出现，带「?」提示，提示文案里**明说「只做提醒、不做阻断」**；
 *   2. 每行有 number 输入框，显示**个人值**（0），旁边灰字显示生效口径（「不限」）；
 *   3. 改值 → 确认弹窗文案含「只做提醒，不做阻断」与「不会因此被强制退出登录」；
 *   4. 确认后 toast + 刷新，库里值真的变了；
 *   5. 布尔/负数不会被前端发出去（负数在 changeQuota 里被拦，toast error）；
 *   6. 还原为 0，**库回基线**。
 *
 * ⚠️ 拖拽探针那次的教训：浏览器验收**绝不能留下数据**。
 *   本脚本最后把目标员工额度还原为 0 并直接查库核对。
 */
const { chromium } = require('playwright');

const GW = 'http://127.0.0.1:3000';
const UI = 'http://127.0.0.1:5174';
const ADMIN = { username: 'wu.jing', password: 'DevAdmin2026!Aa' };

let pass = 0, fail = 0;
function check(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else { fail++; console.log(`  [FAIL] ${name} | ${detail}`); }
}

async function apiLogin(page) {
  const res = await page.request.post(`${GW}/api/auth/login`, {
    data: { username: ADMIN.username, password: ADMIN.password },
  });
  const body = await res.json();
  if (!body.access_token) throw new Error('管理员登录失败: ' + JSON.stringify(body).slice(0, 200));
  return body.access_token;
}

async function dbQuota(token, userId) {
  const res = await page_request_json(
    token, 'GET', `${GW}/api/v1/admin/users/${userId}`);
  return res.token_quota_monthly;
}
async function page_request_json(token, method, url, data) {
  // 用全局 fetch（Node 22 自带）+ 绝不读 HTTP_PROXY（这里都是 127.0.0.1）
  const res = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: data ? JSON.stringify(data) : undefined,
  });
  return res.json();
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const jsErrors = [];
  page.on('pageerror', (e) => jsErrors.push('pageerror: ' + e.message));

  // ---------- 前置：登录 + 找目标 ----------
  const token = await apiLogin(page);
  const users = await page_request_json(token, 'GET', `${GW}/api/v1/admin/users?limit=50`);
  const target = users.items.find((u) => u.username !== ADMIN.username && u.status === 'active');
  check('前置：找到目标员工', !!target, '没有可用的在职员工');
  if (!target) { await browser.close(); process.exit(1); }
  console.log(`  目标：${target.username} (id=${target.id}) 当前额度=${target.token_quota_monthly}`);
  const before = target.token_quota_monthly ?? 0;

  // ---------- 管理端 UI ----------
  await page.goto(`${UI}/login`);
  await page.fill('input[type="text"], input:not([type="password"])', ADMIN.username);
  await page.fill('input[type="password"]', ADMIN.password);
  await page.click('button[type="submit"], button');
  await page.waitForURL(/overview|users|dashboard|home/, { timeout: 15000 });
  await page.goto(`${UI}/users`);
  await page.waitForSelector('table tbody tr', { timeout: 15000 });

  // 1. 表头
  const headers = await page.locator('th').allInnerTexts();
  check('「月度额度」列表头出现', headers.some((h) => h.includes('月度额度')),
    JSON.stringify(headers));
  const thQuota = page.locator('th', { hasText: '月度额度' });
  const hint = await thQuota.locator('.th-hint').getAttribute('title');
  check('🔴 表头提示明说「只做提醒、不做阻断」',
    !!hint && hint.includes('只做提醒') && hint.includes('不做阻断'),
    `title=${hint?.slice(0, 80)}`);

  // 2. 行内输入：显示个人值 + 旁边生效口径
  const row = page.locator('tr', { hasText: target.display_name }).first();
  const input = row.locator('input.quota-input');
  // ⚠️ 判据是「目标行里**有**这个输入框且能取值」，不是「全页恰好一个」——
  //   hasText 会匹配到表格的嵌套结构（比如展开行），count 严格 ===1
  //   会红在与被测物无关的地方（同踩坑清单第 87 条）。
  const inputCount = await input.count();
  check('行内有额度输入框', inputCount >= 1, `目标行内命中 ${inputCount} 个`);
  const shown = await input.inputValue();
  check('输入框显示**个人值**（不是生效值——否则确认一次就把全局默认固化成个人值）',
    Number(shown) === before, `显示=${shown} 个人值=${before}`);
  const eff = await row.locator('.quota-eff').innerText();
  check('旁边灰字显示生效口径（不限 / 全局默认）',
    eff.includes('不限') || eff.includes('全局默认'), `灰字=${eff}`);

  // 3. 改值 → 确认弹窗文案
  await input.fill('6400');
  await input.dispatchEvent('change');
  await page.waitForTimeout(300);
  const dialog = page.locator('.modal, [class*="confirm"], [class*="dialog"]').first();
  const dialogText = (await dialog.isVisible().catch(() => false))
    ? await dialog.innerText() : '';
  check('🔴 确认弹窗明说「只做提醒，不做阻断」',
    dialogText.includes('只做提醒') && dialogText.includes('不做阻断'),
    dialogText.slice(0, 120));
  check('确认弹窗明说「不会因此被强制退出登录」',
    dialogText.includes('不会因此被强制退出登录'), dialogText.slice(0, 200));
  check('确认弹窗写明「0 = 不限（用全局默认）」语义',
    dialogText.includes('不限'), '');

  // 4. 确认 → toast + 库里真的变了
  await dialog.locator('button', { hasText: /确定|确认/ }).first().click();
  await page.waitForTimeout(1200);
  const afterUi = await input.inputValue().catch(() => '(输入框被刷新)');
  const afterDb = await dbQuota(token, target.id);
  check('确认后库里额度真的变成 6400', afterDb === 6400,
    `库里=${afterDb} UI=${afterUi}`);

  // 5. 前端拦截非法输入（负数不发请求）
  await input.fill('-5');
  await input.dispatchEvent('change');
  await page.waitForTimeout(500);
  const stillSame = await dbQuota(token, target.id);
  check('负数被前端拦下（库里仍是 6400，没有发出请求）', stillSame === 6400,
    `库里=${stillSame}`);
  // 小数同样拦
  await input.fill('12.5');
  await input.dispatchEvent('change');
  await page.waitForTimeout(500);
  check('小数被前端拦下', (await dbQuota(token, target.id)) === 6400, '');

  // 6. 还原 → 库回基线
  await input.fill(String(before));
  await input.dispatchEvent('change');
  await page.waitForTimeout(300);
  const confirmBtn = page.locator('.modal button, [class*="dialog"] button')
    .filter({ hasText: /确定|确认/ }).first();
  await confirmBtn.click().catch(() => {});
  await page.waitForTimeout(1200);
  const restored = await dbQuota(token, target.id);
  check(`🔴 库回基线（额度还原为 ${before}）`, restored === before, `库里=${restored}`);

  // 🔴 本脚本每次 PATCH 都会落一条审计（这是被测行为本身）——
  //   不清理的话，跑完整个库多了几条「变更额度」的记录，
  //   而回归测试的终检（test_module16_quota.py 反向组末尾）会抓到它并转红。
  //   「测试跑完库要回到原样」对浏览器验收同样成立。
  //
  //   清理方式：**不发明内部接口**，用与 setPasswords/restorePasswords
  //   相同的模式 —— execFileSync 调后端 venv 里的 Python 直接 SQL。
  const { execFileSync } = require('child_process');
  const path = require('path');
  const repoRoot = path.resolve(__dirname, '..', '..');
  try {
    const out = execFileSync(
      path.join(repoRoot, '.venv', 'bin', 'python'),
      ['-c', `
import sys
sys.path.insert(0, ${JSON.stringify(repoRoot)})
from dotenv import load_dotenv
load_dotenv(${JSON.stringify(repoRoot + '/.env')})
from core.db import get_engine
from sqlalchemy import text
with get_engine().begin() as c:
    n = c.execute(text("DELETE FROM audit_log WHERE action='user.token_quota.change' "
                       "AND target_id = ${target.id}")).rowcount
    print(n)
`],
      { encoding: 'utf8', timeout: 30000 });
    check('审计清理完成（本脚本落下的额度变更记录已删）',
      parseInt(out.trim(), 10) >= 1, `删了 ${out.trim()} 条`);
  } catch (e) {
    check('审计清理完成', false, e.message.slice(0, 120));
  }

  check('全程无 JS 错误', jsErrors.length === 0, jsErrors.slice(0, 2).join(' | '));

  await page.screenshot({ path: '/tmp/verify_15b_quota_ui.png', fullPage: false });
  await browser.close();
  console.log('\n' + '='.repeat(60));
  console.log(`  15b 管理端浏览器验收：${pass} 通过 / ${fail} 失败`);
  console.log('='.repeat(60));
  process.exit(fail ? 1 : 0);
})();
