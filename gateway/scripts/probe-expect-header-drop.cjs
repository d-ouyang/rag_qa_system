/**
 * 复现并验证网关身份头注入的修法（P2-14f 期间发现的真缺陷）。
 *
 * ---------------------------------------------------------------------------
 * 缺陷是什么
 * ---------------------------------------------------------------------------
 * `http-proxy@1.18.1` 的 `lib/http-proxy/passes/web-incoming.js` 里：
 *
 *     proxyReq.on('socket', function (socket) {
 *       if (server && !proxyReq.getHeader('expect')) {
 *         server.emit('proxyReq', proxyReq, req, res, options);
 *       }
 *     });
 *
 * **`proxyReq` 事件在客户端带了 `Expect: 100-continue` 时根本不触发。**
 * 而 `proxy.controller.ts` 把「无条件剥离客户端伪造的身份头」与「注入真实身份」
 * 两件事**全都**放在这个事件的回调里 —— 于是这个头一出现，回调一次都不跑：
 *
 *   · 真实身份没注入 → 后端在 dev 模式回落 break-glass 超管（`kb_role=none`）
 *     → `require_kb_upload` 判拒 → 上传 403 `FORBIDDEN_KB_WRITE`；
 *     gateway 生产模式则是缺 `X-Internal-Auth` → 全站 401。
 *   · **更糟的是剥离也没执行** → 客户端自带的 `X-User-Id` 原样穿透到后端。
 *     dev 模式下这等于「伪造身份直接生效」（12b 当年量化的那条洞换了入口）。
 *
 * 为什么此前从没暴露：浏览器 `fetch`/`XHR` 默认不发 `Expect`，
 * 而 P2-12b 当时只测了「白名单路径 + 伪造头」这一个场景（走 `@Public()`，
 * 那条路没有 `Expect`）。两个条件都没被同时满足过。
 *
 * ---------------------------------------------------------------------------
 * 这个脚本干什么
 * ---------------------------------------------------------------------------
 * 用**真实的** `http-proxy-middleware@3.0.7`（与网关同版本）+ 真实 http 目标服务，
 * 对比四种场景下 `proxyReq` 回调是否执行、以及「改 `req.headers`」的修法是否有效：
 *
 *   A  GET，无 Expect                → 回调执行（基线）
 *   B  POST multipart，无 Expect     → 回调执行（所以「multipart 丢头」是误判）
 *   C  POST multipart，带 Expect     → **回调不执行，身份头全丢**（缺陷）
 *   D  同 C，但转发前从 req.headers 摘掉 expect → 回调恢复执行
 *   E  同 C，但转发前直接改 req.headers 注入身份 → 回调不执行也照样有身份头
 *
 * 用法：在 `gateway/` 目录下 `node scripts/probe-expect-header-drop.cjs`
 * 退出码 0 = 结论仍成立（改动没把缺陷改没）；非 0 = 结论被推翻，需要重新定位。
 */
const http = require('node:http');
const { createProxyMiddleware } = require('http-proxy-middleware');

const IDENTITY_HEADERS = ['x-user-id', 'x-username', 'x-user-role', 'x-token-version'];

/** 目标服务：回显它收到的身份头。 */
function startTarget(received) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const chunks = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        received.push({
          method: req.method,
          // 后端（uvicorn）会按 RFC 7231 把 Expect 当逐跳头删掉，
          // 所以「后端有没有收到 expect」不能当判据 —— 判据是身份头在不在。
          identity: IDENTITY_HEADERS.map((h) => req.headers[h] ?? null),
        });
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end('{"ok":true}');
      });
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

/**
 * 两种转发写法，**刻意并列**，因为这个脚本的价值就在于「对比」：
 *   'proxyReq' —— 12b~14e 期间的写法：剥离/注入放在 on.proxyReq 回调里
 *   'inbound'   —— 14f 修完后的写法：在转发前盖进入站 req.headers
 *
 * 两者共用同一个 target 与同一份身份数据，唯一差别就是「在哪儿改头」。
 * 如果哪天 http-proxy 换掉实现，这个对比会立刻失效并给出可读的结论，
 * 而不是让断言变成一句没人验证过的口头承诺。
 */
function makeProxy(port, strategy) {
  return createProxyMiddleware({
    target: `http://127.0.0.1:${port}`,
    changeOrigin: true,
    selfHandleResponse: false,
    on: {
      proxyReq: (proxyReq, req) => {
        if (strategy !== 'proxyReq') return;
        const incoming = req;
        for (const h of IDENTITY_HEADERS) proxyReq.removeHeader(h);
        if (incoming.user) {
          proxyReq.setHeader('X-User-Id', incoming.user.userId);
          proxyReq.setHeader('X-Username', incoming.user.username);
          proxyReq.setHeader('X-User-Role', incoming.user.role);
          proxyReq.setHeader('X-Token-Version', String(incoming.user.tokenVersion));
        }
      },
    },
  });
}

function serve(proxy, mode, forged) {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      // 模拟守卫：非白名单路径必有 req.user
      Object.defineProperty(req, 'user', {
        value: { userId: '442', username: 'chen.jie', role: 'user', tokenVersion: 105 },
        configurable: true,
      });
      // 模拟客户端伪造身份头（12b 要防的那件事）
      if (forged) {
        req.headers['x-user-id'] = '440';
        req.headers['x-username'] = 'wu.jing';
        req.headers['x-user-role'] = 'admin';
      }
      // ⚠️ **修法的真实位置在代理侧、转发之前**，不是客户端侧。
      // 客户端带 Expect 时它自己的头在 socket 事件时就发完了，改不动
      // （ERR_HTTP_HEADERS_SENT）；能在那之前改的只有**入站**的
      // `req.headers` —— `http-proxy` 的 `setupOutgoing` 里
      // `outgoing.headers = extend({}, req.headers)` 会把它整个复制过去，
      // 所以这里改了就一定到下游，与 proxyReq 事件是否触发无关。
      if (mode === 'inbound') {
        // 与 stampIdentityOnInbound() 一一对应：先无条件剥离，再注入真实身份，
        // 最后摘掉 expect（摘掉之后 proxyReq 事件才会触发，
        // fixRequestBody / Accept-Encoding 才有地方执行）。
        for (const h of IDENTITY_HEADERS) delete req.headers[h];
        req.headers['x-user-id'] = '442';
        req.headers['x-username'] = 'chen.jie';
        req.headers['x-user-role'] = 'user';
        req.headers['x-token-version'] = '105';
        delete req.headers.expect;
      }
      if (mode === 'strip-expect') {
        delete req.headers.expect;
      }
      proxy(req, res, () => {
        res.writeHead(404);
        res.end();
      });
    });
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

/**
 * @param mode 'plain' 什么都不做（复现缺陷）
 *             'strip-expect' 转发前从**入站** req.headers 摘掉 expect
 *             'inject-req-headers' 转发前直接改**入站** req.headers 注入身份
 */
function send(port, { path, method = 'GET', expect = false }) {
  return new Promise((resolve, reject) => {
    const headers = {};
    if (expect) headers.Expect = '100-continue';
    let body = null;
    if (method === 'POST') {
      const boundary = '----probeboundary';
      body = Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\nhello\r\n--${boundary}--\r\n`,
      );
      headers['Content-Type'] = `multipart/form-data; boundary=${boundary}`;
      headers['Content-Length'] = String(body.length);
    }
    const req = http.request({ host: '127.0.0.1', port, path, method, headers }, (res) => {
      res.resume();
      res.on('end', resolve);
    });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

(async () => {
  const received = [];
  const target = await startTarget(received);
  const tport = target.address().port;

  // mode 决定「在哪儿改头」；strategy 决定 proxyReq 回调里写不写（14f 后是空的）。
  const cases = [
    // ---- 旧写法（12b~14e）：剥离/注入在 on.proxyReq 里 ----
    ['A GET 无 Expect（基线）', { path: '/a' }, 'plain', 'proxyReq', false],
    ['B POST multipart 无 Expect', { path: '/b', method: 'POST' }, 'plain', 'proxyReq', false],
    ['C POST multipart 带 Expect ← 缺陷本体', { path: '/c', method: 'POST', expect: true }, 'plain', 'proxyReq', false],
    ['G 旧写法 + 伪造身份头 + 带 Expect ← 12b 那条洞的新入口',
      { path: '/g', method: 'POST', expect: true }, 'plain', 'proxyReq', true],
    // ---- 新写法（14f）：转发前盖进入站 req.headers ----
    ['D 入站盖头 + 带 Expect', { path: '/d', method: 'POST', expect: true }, 'inbound', 'inbound', false],
    ['E 入站盖头 + 无 Expect', { path: '/e', method: 'POST' }, 'inbound', 'inbound', false],
    ['H 入站盖头 + 伪造身份头 + 带 Expect ← 修好之后这里必须是真身份',
      { path: '/h', method: 'POST', expect: true }, 'inbound', 'inbound', true],
    // ---- 只摘 expect（证明「摘 expect」单独就够了，但那样就没纵深）----
    ['F 只摘 expect + 带 Expect', { path: '/f', method: 'POST', expect: true }, 'strip-expect', 'proxyReq', false],
  ];

  const results = [];
  for (const [label, opts, mode, strategy, forged] of cases) {
    const proxy = makeProxy(tport, strategy);
    const gw = await serve(proxy, mode, forged);
    const gport = gw.address().port;
    received.length = 0;
    await send(gport, opts);
    const r = received[0];
    const has = Boolean(r && r.identity.every((v) => v !== null));
    // 「真身份」= 下游看到的是 442/chen.jie，而不是伪造的 440/wu.jing
    const isReal = Boolean(r && r.identity[0] === '442' && r.identity[1] === 'chen.jie');
    results.push({ label, has, isReal });
    console.log(
      `${has ? '身份头在  ' : '身份头丢失'}| ${label} | ${r ? r.identity.join(',') : '(无请求到达)'}`,
    );
    gw.close();
  }
  target.close();

  const by = Object.fromEntries(results.map((r) => [r.label[0], r]));
  const checks = [
    ['A 基线必须有身份头', by.A.has === true],
    ['B multipart 本身不影响（此前「multipart 丢头」是误判）', by.B.has === true],
    ['C 旧写法 + 带 Expect → 身份头全丢（缺陷本体仍复现）', by.C.has === false],
    ['D 新写法 + 带 Expect → 身份头在（这就是修法）', by.D.has === true],
    ['E 新写法 + 无 Expect → 身份头在（没把正常路径弄坏）', by.E.has === true],
    ['F 只摘 expect 也能恢复（证明根因确实是 Expect）', by.F.has === true],
    ['G 旧写法 + 伪造头 + 带 Expect → 伪造身份真的穿透了（这就是 12b 那条洞的新入口）',
      // ⚠️ 判据只看 `isReal`，**不能**看 `has`：伪造头只填了三个，
      // x-token-version 没填，所以 `has` 必然是 false —— 用它判会得到
      // 「没穿透」这个**与事实相反**的结论。穿透与否只由「下游看到的是谁」决定。
      by.G.isReal === false],
    ['H 新写法 + 伪造头 + 带 Expect → 下游看到的是真身份（洞被关上）',
      by.H.has === true && by.H.isReal === true],
  ];
  console.log('');
  let failed = 0;
  for (const [label, ok] of checks) {
    console.log(`${ok ? 'PASS' : 'FAIL'} | ${label}`);
    if (!ok) failed += 1;
  }
  process.exit(failed === 0 ? 0 : 1);
})();