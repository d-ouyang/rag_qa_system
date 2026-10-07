/**
 * 验证：网关对「客户端自带的身份头」是覆写还是放行？
 *
 * ⚠️ **12b 起这个脚本的 B 行描述的是一个「已经修掉」的形态，不是现状。**
 * 它仍然故意保留那个写法（`strip: false`）来演示「不剥离会怎样」，
 * 于是输出里 B 行永远显示「穿透到后端」。**别把 B 行读成当前状态** ——
 * 现状是「所有场景都剥离」，也就是 D 行那种。
 * 判断现状看两处：① `proxy.controller.ts` 里那段 `for (const h of INBOUND_IDENTITY_HEADERS)`
 * 是无条件执行的；② `tests/test_module14_trust_boundary.py` 的第 3 组断言它在注入之前。
 *
 * 复刻 proxy.controller.ts 的写法（条件式 setHeader），三种场景各发一次：
 *   A 受保护路径 + 网关注入了身份   → 客户端伪造头应被覆写
 *   B 免鉴权白名单路径（incoming.user 为空）→ 伪造头会不会原样送到后端？
 *   C 同 B，但网关先 removeHeader   → 验证修法是否有效
 *
 * 用真实的 http-proxy-middleware@3.0.7（与网关同版本）+ 真实 http 目标服务。
 */
const http = require('node:http')
const { createProxyMiddleware } = require('http-proxy-middleware')

const TARGET_PORT = 0 // 用系统分配的空闲端口，避免撞端口（9311 已被占用过）
let TARGET_ACTUAL = 0

const received = []
const target = http.createServer((req, res) => {
  received.push({ url: req.url, xUserId: req.headers['x-user-id'] ?? null, xRole: req.headers['x-role'] ?? null })
  res.writeHead(200, { 'content-type': 'application/json' })
  res.end('{"ok":true}')
})

// 复刻网关的 on.proxyReq 逻辑；strip=true 时模拟"修复后"的写法
function makeProxy({ strip }) {
  return createProxyMiddleware({
    target: `http://127.0.0.1:${TARGET_ACTUAL}`,
    changeOrigin: true,
    on: {
      proxyReq: (proxyReq, req) => {
        if (strip) {
          proxyReq.removeHeader('x-user-id')
          proxyReq.removeHeader('x-role')
        }
        const incoming = req
        if (incoming.user) {
          proxyReq.setHeader('X-User-Id', incoming.user.userId)
          proxyReq.setHeader('X-Role', incoming.user.role)
        }
      },
    },
  })
}

function call(port, { path, user, forged }) {
  return new Promise((resolve) => {
    const headers = {}
    if (forged) {
      headers['X-User-Id'] = forged.id
      headers['X-Role'] = forged.role
    }
    const req = http.request({ host: '127.0.0.1', port, path, method: 'GET', headers }, (res) => {
      res.resume()
      res.on('end', resolve)
    })
    // 模拟守卫：只有受保护路径才把 user 挂上去
    if (user) req.user = user
    req.end()
  })
}

/** 用一个"手工调用中间件"的 http server 承载，模拟守卫把 req.user 挂好后再进 handler */
function serve(proxy, port) {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      // 真实网关里，这一步之前已经跑过 JwtAuthGuard：非白名单路径必有 req.user
      const isPublic = req.url === '/api/v1/qa/health'
      if (!isPublic) {
        const fakeUser = { userId: '7', role: 'user' }
        Object.defineProperty(req, 'user', { value: fakeUser, configurable: true })
      }
      proxy(req, res, () => {
        res.writeHead(404)
        res.end()
      })
    })
    server.listen(port, '127.0.0.1', () => resolve(server))
  })
}

;(async () => {
  await new Promise((r) => {
    target.listen(TARGET_PORT, '127.0.0.1', () => {
      TARGET_ACTUAL = target.address().port
      r()
    })
  })

  // ---------- 现状（与网关一致：只覆写，不剥离） ----------
  const serverA = await serve(makeProxy({ strip: false }), 0)
  const portA = serverA.address().port
  const FORGED = { id: '999', role: 'admin' }

  received.length = 0
  await call(portA, { path: '/api/v1/qa/sessions', forged: FORGED })
  const protectedCase = received.at(-1)

  received.length = 0
  await call(portA, { path: '/api/v1/qa/health', forged: FORGED })
  const publicCase = received.at(-1)

  serverA.close()

  // ---------- 修法（先剥离再注入） ----------
  const serverB = await serve(makeProxy({ strip: true }), 0)
  const portB = serverB.address().port
  received.length = 0
  await call(portB, { path: '/api/v1/qa/health', forged: FORGED })
  const publicFixed = received.at(-1)

  received.length = 0
  await call(portB, { path: '/api/v1/qa/sessions', forged: FORGED })
  const protectedFixed = received.at(-1)
  serverB.close()
  target.close()

  const show = (n, c) => `${n.padEnd(38)} 后端收到 X-User-Id=${JSON.stringify(c.xUserId)}  X-Role=${JSON.stringify(c.xRole)}`
  // B 行是「**假如不剥离**」的对照，不是现状 —— 12b 之后网关无条件剥离。
  console.log(show('A 受保护路径（有身份，不剥离）', protectedCase))
  console.log(show('B 白名单路径（假如不剥离 ← 对照）', publicCase))
  console.log(show('C 白名单路径（剥离后）', publicFixed))
  console.log(show('D 受保护路径（剥离后 = 12b 现状）', protectedFixed))
  console.log('')
  console.log(`对照 B：不剥离的话，白名单路径的客户端伪造头${publicCase.xUserId === '999' ? '**穿透到后端**（这就是 12b 要修的缺口）' : '被拦住了'}`)
  console.log(`结论 C：剥离后伪造头${publicFixed.xUserId === null ? '已消失 ✅' : '仍在 ❌'}`)
  console.log(`结论 D：受保护路径由网关注入，伪造值${protectedFixed.xUserId === '7' ? '被覆写为真实身份（剥离不影响正常链路）✅' : '异常❌'}`)
  console.log('')
  console.log('⚠️ B 行仍显示「穿透」是**故意的** —— 它是「不剥离」的对照，用来证明修法确实有必要。')
  console.log('   现状看 D 行与 C 行：两种路径的伪造头都不存在。')
})()
