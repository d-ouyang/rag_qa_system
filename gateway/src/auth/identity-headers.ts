/**
 * 身份头的**唯一**清单 —— 网关与后端两侧都从这里取（后端经 Python 的
 * `core/identity.py` 镜像同一份，见那个文件的 `IDENTITY_HEADERS`）。
 *
 * ---------------------------------------------------------------------------
 * 为什么要有这个文件（而不是在两处各写一份字符串数组）
 * ---------------------------------------------------------------------------
 * 13d 踩过的坑在这里原样重演了一次：后端审计写 `{"from","to"}`、前端读
 * `detail.status`，两边各自都自洽，于是明细列整列 `—`，后端全绿、接口 200、
 * 不报错。**两个语言各自写一份「字符串字面量清单」，没有任何东西会校验它们
 * 是否还对得上** —— 只有「谁在读」变了这一件事，而那正是编译器管不到的地方。
 *
 * 这里的做法是「共享一份 + 正扫对方源码」：
 *   · 剥离与注入只用 `INBOUND_IDENTITY_HEADERS`；
 *   · `tests/test_module14_trust_boundary.py` 会读本文件与 `proxy.controller.ts`
 *     的源码，确认注入的头**全部**在这个清单里（漏一个就红）。
 * 为什么不是「抽成 npm 包给后端用」：那是把一个字符串数组变成跨语言构建依赖，
 * 代价远大于收益，而收益只是省掉一次 grep。
 */

/**
 * 客户端**可以随便填**、而我们**无条件删掉**的头。
 *
 * ⚠️ 为什么必须「无条件」而不是「有身份时才删」（这是 12b 的核心）：
 * 11c 之前的写法是条件式注入 —— `if (incoming.user) { setHeader(...) }`。
 * 对受保护路径没问题（守卫保证了 `incoming.user` 存在，伪造值会被覆写）。
 * 但**白名单路径**（`@Public()` 的两个健康检查）上守卫不跑、`incoming.user`
 * 是 undefined，整段注入被跳过 → 客户端自带的 `X-User-Id: 999` **原样转发到后端**。
 * 已用同版本的 `http-proxy-middleware@3.0.7` 实测过
 * （复现脚本 `gateway/scripts/probe-header-strip.cjs`，结论 B 行）。
 *
 * 12b 施工前的实测量化（本机，2026-10-08）：
 *   白名单路径 + `X-User-Id: 440`（真管理员 id）→ 后端按admin 处理请求。
 * 也就是说这不是「理论上的隐患」，是一条**当时就能用的**身份伪造路径。
 *
 * `x-internal-auth` 也在清单里：它是 12b 新加的「网关证明」，
 * **同样不能允许客户端自带** —— 否则任何人都能自己声明「我是网关」。
 *
 * ⚠️ **`x-role` 与 `x-user-role` 都在清单里，而实际注入的只有 `x-user-role`。**
 * 这不是冗余，是 12b 施工时对上的一个规格与实现不一致：
 * 设计规格 §5.1.2 与复现脚本 `probe-header-strip.cjs` 里写的都是 `X-Role`，
 * 而 11c 真正注入的是 `X-User-Role`。两个名字都剥掉的代价是零，
 * 收益是**不管将来哪份文档/哪段代码读哪个名字，都读不到客户端填的值**。
 * 只剥其中一个的话，另一个就成了后门 —— 而「后端读不读这个头」这件事
 * 是会变的（12d 就会读role）。
 */
export const INBOUND_IDENTITY_HEADERS = [
  'x-user-id',
  'x-username',
  'x-user-role',
  // ↑ 实际注入的（11c 起）
  'x-role',
  // ↓ 规格 §5.1.2 与复现脚本里写的那个名字。**不注入，但照样要剥**（见上）。
  'x-dept-id',
  'x-token-version',
  // ↓ P2-14b 新增：知识库写权限。与 `x-role` 同理 —— **不注入也可以，
  //   但必须剥**，因为客户端能自己填一个 `superadmin`。
  //   ⚠️ 后端目前**不读**这个头（它的 `kb_role` 从库里实时读，见
  //   `core/identity.py`），但「后端将来会不会读它」是会变的
  //   —— 与 `x-dept-id` 当初的理由一模一样。剥的代价是零。
  'x-user-kb-role',
  'x-identity-source',
  'x-internal-auth',
] as const;

/** 网关转发时**注入**给后端的头（与 `AuthenticatedUser` 的字段一一对应）。 */
export interface ForwardedIdentity {
  userId: string;
  username: string;
  role: string;
  tokenVersion: number;
  source: string;
  /**
   * ⚠️ **刻意没有 `kbRole`**（P2-14b 的决定），尽管 `INBOUND_IDENTITY_HEADERS`
   * 里剥了 `x-user-kb-role`。理由：**会造出第二个真相源**。
   *
   * 后端的 `kb_role` 从 **MySQL 实时读**（`core/identity.py::_make_actor`），
   * 所以改授权**立刻生效**、不需要重登（14a 实测已确认）。
   * 若这里再注入一个来自 JWT 的 `kb_role`，同一个字段就有两个来源：
   * JWT 里是签发时的快照、库里是当前值。两者不一致时，
   * 「谁说了算」会变成一个需要回答的问题 —— 而权限系统里
   * 「两个真相源」意味着**在某些时刻它就是错的，且不报错**。
   *
   * 代价要说清：**网关的粗筛用的是 JWT 里的快照**，所以
   * 「管理员降级某人的 `kb_role`」之后，那个人手里的旧 token
   * 仍能在网关过这道门，直到 token 过期（最长 12h）。
   * 但**后端会拒**（它读的是库里的当前值）—— 两层都在，粗筛那道只是
   * 「省一次后端往返」，不是唯一防线。
   * 要让网关立刻知道，改授权时 `token_version+1` 强制重登即可
   * （`user_repo.set_kb_role()` 已经这么做了，14f 接上管理端后即生效）。
   */
}

/**
 * 把真实身份写进下游请求头。
 *
 * 刻意做成一个**返回要设的头**的纯函数，而不是就地 `setHeader` 若干次：
 * 这样「注入了哪些头」可以在测试里直接断言，不必真的起一个代理去抓包。
 * 顺序无关紧要（`setHeader` 是覆盖式的），但**必须在剥离之后调用**。
 */
export function buildForwardIdentityHeaders(
  user: ForwardedIdentity,
  internalAuthToken: string,
): Record<string, string> {
  const headers: Record<string, string> = {
    'X-User-Id': user.userId,
    // 用户名可能含中文，HTTP 头只允许 ASCII，编码后再传（下游自行 decodeURIComponent）
    'X-Username': encodeURIComponent(user.username),
    'X-User-Role': user.role || 'user',
    // 缺了 ver，改密后旧 token 在后端眼里仍然有效 —— 那是漏掉就不会报错的失效
    'X-Token-Version': String(user.tokenVersion ?? 0),
    'X-Identity-Source': user.source || 'legacy',
  };
  // ⚠️ 12b新增：**即使没有身份也要带这个头**（白名单路径就是这种情况）。
  // 后端在 gateway 模式下要靠它区分「这是网关转发的」与「有人直连 8000」——
  // 缺它一律 401，所以少了它等于白名单路径在后端那边全部被拒。
  // 见 docs/设计规格-用户权限与管理端.md §5.1.3 的 B 方案（纵深）。
  if (internalAuthToken) {
    headers['X-Internal-Auth'] = internalAuthToken;
  }
  return headers;
}