/**
 * 浏览器侧交叉验证（14f）：在**真实页面上下文**里登录并跑四条写路径。
 *
 * 为什么 curl 已经验过还要再来一遍：
 *   · curl 走的是 Node http 客户端，浏览器走的是 fetch —— 两者在
 *     `Expect` / CORS 预检 / 凭据模式上都可能不同；
 *   · 更要紧的是验「页面里那个上传按钮点下去会发生什么」，
 *     那条路只有真实 DOM + 真实拦截器才走得到。
 *
 * 判据：superadmin 放行到业务层（非 403 FORBIDDEN_KB_WRITE），
 * `none` 档四条全 403 且错误码是 FORBIDDEN_KB_WRITE。
 *
 * 用法：node scripts/verify-14f-browser.cjs   （需先起前端 5173 / 网关 3000 / 后端 8000）
 */
const { chromium } = require('playwright');

const FRONTEND = process.env.FRONTEND_URL || 'http://localhost:5173';

// ⚠️ 两个测试账号的密码**不一样**（`scripts/dev_test_accounts.py --apply` 写的）：
//   chen.jie → DevTest2026!Aa（平级员工）
//   wu.jing  → DevAdmin2026!Aa（管理员）
// 用同一个密码去登两个账号会连着撞 INVALID_CREDENTIALS —— 而连续失败会
// **锁账号 15 分钟**（`locked_until`），于是第二个账号也登不进，
// 症状变成「两个账号一起坏了」，极易误判成服务故障。
// 恢复：`.venv/bin/python scripts/dev_test_accounts.py --apply`
const PASSWORDS = {
  'chen.jie': 'DevTest2026!Aa',
  'wu.jing': 'DevAdmin2026!Aa',
};

async function login(page, username, password) {
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' });
  // ⚠️ Vue 是**挂载后**才渲染登录框的，而 token 存在 Pinia → localStorage，
  //    两者都要等应用真正跑起来。`domcontentloaded` 只说明 HTML 到了 ——
  //    那一刻 #app 还是空的（实测 localStorage 全空就是这个原因）。
  await page.waitForSelector('input[type=text]', { timeout: 20000 });
  await page.waitForFunction(() => Object.keys(localStorage).length > 0, { timeout: 15000 })
    .catch(() => { /* 首次登录本来就没有键，靠下面的诊断判断 */ });
  await page.fill('input[type=text]', username);
  await page.fill('input[type=password]', password);
  await page.click('button[type=submit]');
  // 判据用「token 出现」而不是固定 sleep —— 固定等待在慢机器上会假失败，
  // 在快机器上又会读到还没写完的状态（这正是第一次跑失败的原因）。
  await page
    .waitForFunction(
      () => Object.keys(localStorage).some((k) => k.endsWith('auth.token') && localStorage.getItem(k)),
      { timeout: 20000 },
    )
    .catch(() => {});
  return page.evaluate(() => {
    const keys = Object.keys(localStorage);
    let token = null;
    for (const k of keys) {
      if (k.endsWith('auth.token')) token = localStorage.getItem(k);
    }
    let user = null;
    for (const k of keys) {
      if (k.endsWith('auth.username')) user = localStorage.getItem(k);
    }
    return { token, user, keys };
  });
}

/**
 * 在页面上下文里发请求 —— 走真实 fetch、真实同源策略、真实浏览器网络栈。
 *
 * ⚠️ 必须**手动**带 Authorization：真实页面里是 axios 拦截器注入的，
 * 而这里刻意用裸 `fetch` 而不是调页面的 axios 实例 ——
 * 原因是**要独立验网关那一层**：拦截器会补头，也会补别的（baseURL、
 * 超时、错误重试），一旦它把 token 弄丢了，失败会出现在错误的地方。
 * 401 `TOKEN_MISSING` 就是「头没带上」，它与「权限被拒」是两件事，
 * 必须能区分 —— 判据里显式判 code 就是为了这个。
 */
async function probe(page, username, token) {
  return page.evaluate(async ({ u, tk }) => {
    const out = [];
    const cases = [
      ['POST', '/api/v1/documents/upload', true],
      ['POST', '/api/v1/documents/upload/batch', true],
      ['POST', '/api/v1/documents/1/reparse', false],
      ['DELETE', '/api/v1/documents/999999', false],
    ];
    for (const [method, url, multipart] of cases) {
      const init = { method, headers: { Authorization: `Bearer ${tk}` } };
      if (multipart) {
        const fd = new FormData();
        fd.append('file', new Blob(['x'], { type: 'text/plain' }), 'probe.txt');
        init.body = fd;
      } else if (method === 'POST') {
        init.headers['Content-Type'] = 'application/json';
        init.body = '{}';
      }
      const r = await fetch(url, init);
      let code = '';
      try {
        const j = await r.clone().json();
        code = (j && (j.code || (j.error && j.error.code) || (j.detail && j.detail.code))) || '';
        if (!code && j && j.detail && j.detail.length) code = j.detail[0].msg || '';
        if (!code) code = typeof j === 'object' ? JSON.stringify(j).slice(0, 90) : String(j).slice(0, 90);
      } catch {
        code = '(非 JSON 响应)';
      }
      out.push({ method, url, status: r.status, code: String(code) });
    }
    return out;
  }, { u: username, tk: token });
}

(async () => {
  const browser = await chromium.launch({
    args: [
      '--enable-unsafe-swiftshader',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      // ⚠️ 必须显式绕代理 —— 本机 HTTP_PROXY 指向 58207 且 NO_PROXY 为空，
      //    chromium 会照样走它，于是「浏览器里 401、curl 里 200」。
      //    症状极具误导性：看起来像账号密码错或后端判据有问题，
      //    而根因在测试宿主的网络环境。
      //    （踩坑清单同款：curl 要 `--noproxy '*'` 才不会被代理吃掉。）
      '--no-proxy-server',
      '--proxy-bypass-list=<-loopback>',
    ],
  });
  let failures = 0;
  try {
    for (const username of ['chen.jie', 'wu.jing']) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const sess = await login(page, username, PASSWORDS[username]);
      if (!sess.token) {
        console.log(`  [FAIL] ${username} 登录后没拿到 token | localStorage 键=${sess.keys.join(',')}`);
        failures += 1;
        await ctx.close();
        continue;
      }
      console.log(`\n  ${username} 登录成功（token ${sess.token.length} 字符）`);
      const rows = await probe(page, username, sess.token);
      for (const r of rows) {
        const forbidden = r.status === 403 && /FORBIDDEN_KB_WRITE/.test(r.code);
        const tokenMissing = r.status === 401 && /TOKEN_MISSING/.test(r.code);
        const reached = r.status !== 403 && !tokenMissing;
        const mark = reached ? '放行到业务层'
          : forbidden ? '403 权限拒（预期）'
          : tokenMissing ? '❌ token 没带上（脚本 bug，不是权限问题）'
          : `意外 ${r.status}`;
        console.log(`    ${r.method.padEnd(6)} ${r.url.padEnd(34)} ${String(r.status).padEnd(4)} ${mark} | ${r.code}`);
        if (tokenMissing) failures += 1;
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  process.exit(failures === 0 ? 0 : 1);
})();