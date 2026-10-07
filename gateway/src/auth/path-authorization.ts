/**
 * 路径级粗粒度授权 —— 网关这一层（设计规格 §5.1「网关能做的」那一栏）。
 *
 * ---------------------------------------------------------------------------
 * 为什么有它，以及为什么它**不是**后端 `require_staff` 的替代品
 * ---------------------------------------------------------------------------
 * 后端 13a 已经有 `require_staff`（实测：普通员工经网关打管理端是 403），
 * 所以这一层**在安全性上没有增量**。它买的是两件事：
 *
 *   ① **请求不进后端**。普通员工点管理端，在网关就被拒 —— 不占用后端的
 *      数据库连接、不进业务日志、不产生审计行（他本来也什么权限都没有）。
 *   ② **纵深**。设计规格 §5.2 第 13 行写明了理由：网关的路径规则是**粗筛**，
 *      而「裸跑 8000 调试」「忘配路径规则」两种情况都会让它整个不生效。
 *      两层各自解决不同的问题，所以不算重复实现。
 *
 * ---------------------------------------------------------------------------
 * 为什么角色集合是 `admin | hr` 而不是只有 `admin`
 * ---------------------------------------------------------------------------
 * 后端 `STAFF_ROLES = {admin, hr}`（D10：hr 能改员工资料，不能碰密码）。
 * 网关这一层若只放 admin，hr 会在网关上被拒，而后端那条 hr 分支永远走不到 ——
 * 于是「后端允许」与「网关允许」不一致，**表现为 hr 完全打不开管理端**
 * （一个只在 hr 账号下才现形的 bug）。粗筛必须比精确判据**更宽**：
 * 网关答「你可能是管人事的，放进去让后端判」，后端答「你到底能不能改这个字段」。
 * 反向（网关比后端严）才是 bug。
 */

/** 能进管理端的角色 —— 与后端 `core/identity.py` 的 `STAFF_ROLES` 对齐。 */
export const STAFF_ROLES = ['admin', 'hr'] as const;

/** 需要 `STAFF_ROLES` 之一的前缀（**精确前缀匹配**，不是子串匹配）。 */
export const STAFF_PATH_PREFIXES = ['/api/v1/admin/'] as const;

/**
 * ⚠️ **P2-14b 的一个刻意选择**：网关上 `kb_role` 的粗筛**只看 `none`**。
 *
 * 网关答的是「你**可能**有权限吗」，后端答「你**到底**有什么权限」。
 * 所以这一层**必须比后端更宽，不能更窄**（12b 已经踩过一次反面教材：
 * 网关只放 `admin` 会让 `hr` 在网关被拒，而后端那条 hr 分支永远走不到，
 * 症状是「只有 admin 账号能进管理端」—— 一个只在特定账号下才现形的 bug）。
 *
 * 于是这里的判据是 `kbRole !== 'none'` 就放行去打后端。
 * **代价**：一个 `kb_role` 是 `ops` 的人会顺利通过网关、然后被后端以
 * `reindex` 拒掉（`ops` 刻意不给重灌）。这是**误拒一次**，
 * 而漏放的代价是全公司知识库被改 —— 两害相权取轻。
 *
 * `unknown`（后端给了不认识的档位）在这里**当 none 处理**：
 * 网关不该比后端更懂 `kb_role`，它只负责「不是 none 就去问后端」。
 */
export function isKbRoleNone(kbRole: string | undefined): boolean {
  if (!kbRole) return true; // 老 token 没有这个字段 → fail-closed
  return kbRole === 'none' || kbRole === 'unknown';
}

/**
 * 知识库**写**路径（`kb_role` 管的那四条）。与后端
 * `api/routes/documents.py` 的四条写路由一一对应。
 *
 * ⚠️ 匹配的是**路径 + 方法**：同一个 `/api/v1/documents/upload` 用 GET 调
 * 不存在（但万一将来加了 `GET /documents/upload` 的预检接口），
 * 而 `DELETE /api/v1/documents/{doc_id}` 与 `GET /api/v1/documents/{doc_id}`
 * 共用路径前缀 —— 只看路径的话，一个 `kb_role=none` 的人连**看**文档列表都不行，
 * 而共用知识库是刻意的业务决策。
 *
 * 路径匹配用**正则**而不是前缀：前缀 `/api/v1/documents/upload` 会顺带
 * 匹配到 `upload/batch`（那确实也要拦，没问题），但
 * 前缀 `/api/v1/documents/` 会**误伤所有读接口**。所以只列四条精确的。
 */
const KB_WRITE_ROUTES: ReadonlyArray<{ method: string; pattern: RegExp }> = [
  { method: 'POST', pattern: /^\/api\/v1\/documents\/upload$/ },
  { method: 'POST', pattern: /^\/api\/v1\/documents\/upload\/batch$/ },
  // `{doc_id}` 是数字：只匹配数字段，避免将来出现
  // `/api/v1/documents/import` 这种路径被误判成「带 id 的文档」。
  { method: 'POST', pattern: /^\/api\/v1\/documents\/\d+\/reparse$/ },
  { method: 'DELETE', pattern: /^\/api\/v1\/documents\/\d+$/ },
];

/** 这条请求是不是一条「知识库写操作」。方法 + 路径都匹配才算。 */
export function isKbWriteRequest(path: string, method: string | undefined): boolean {
  if (!method) return false; // 没有方法（理论上不该发生）→ 当成读，放行给后端判
  const m = method.toUpperCase();
  return KB_WRITE_ROUTES.some((r) => r.method === m && r.pattern.test(path));
}

/** 无需 token 即可访问的上游探活路径 —— 与 `proxy.controller.ts` 的白名单同一份。 */
export const PUBLIC_PATHS = ['/api/v1/system/health', '/api/v1/qa/health'] as const;

/**
 * **绝对不许经网关转发**的前缀 —— 内部接口（设计规格 §5.2 第 5 行）。
 *
 * ⚠️ 12b 施工前实测（2026-10-08）：`POST /api/v1/internal/auth/login`
 * 经网关是**可达的**，带一个普通员工的合法 token 就能打到后端，
 * 后端回 401「内部接口鉴权失败」—— 也就是说**唯一的防线是那道共享密钥**。
 * 一旦那把密钥因为任何配置失误而泄漏/为空，这条路立刻变成「任意登录判定」。
 *
 * 为什么它归到「路径级授权」而不是「加进白名单的反面」：它要拒绝的是
 * **所有身份**（含 admin），所以不能写成「角色不够」—— 那会让一个 admin
 * 看到「需要 admin 权限」这种自相矛盾的提示。单独一条规则、单独一个错误码。
 *
 * 报什么码：这里刻意用404 而不是 403。403 等于确认「这条路径存在但你不许」，
 * 而内部接口**对任何调用者都不该存在** —— 用 404 让探测者分不清
 * 「路径不存在」与「路径存在但被封」，不给枚举留线索。
 */
export const NEVER_PROXIED_PREFIXES = ['/api/v1/internal/'] as const;

export interface PathDecision {
  allowed: boolean;
  /** 允许时为 undefined；拒绝时是一句给用户看的话（不泄露「为什么他不是 admin」）。 */
  message?: string;
  /** 拒绝原因代码，写进日志便于统计。 */
  code?: string;
}

/** 路径是否需要 staff 角色。注意用 `startsWith` 匹配**带尾斜杠**的前缀。 */
export function requiresStaff(path: string): boolean {
  return STAFF_PATH_PREFIXES.some((p) => path === p || path.startsWith(p));
}

export function isPublicPath(path: string): boolean {
  return PUBLIC_PATHS.includes(path as (typeof PUBLIC_PATHS)[number]);
}

/** 内部接口（`/api/v1/internal/*`）—— 任何身份都不许经网关转发。 */
export function isInternalPath(path: string): boolean {
  return NEVER_PROXIED_PREFIXES.some((p) => path === p || path.startsWith(p));
}

/**
 * 判断一个请求路径在这个登录者身份下能不能放行。
 *
 * ⚠️ **只回答「路径与身份是否匹配」，不回答「这个人是谁」** —— 后者由守卫做。
 * 所以调用点必须在守卫**之后**；放之前调用会让未登录请求拿到 403 而不是 401，
 * 而前端对这两者的处理不同（403 不会跳登录页，用户会卡在页面上）。
 *
 * @param path   请求路径（含 query 无妨，只按 pathname 语义匹配前缀）
 * @param user   登录者身份（未登录传undefined）。**刻意传整个对象而不是
 *               `role` / `kbRole` 两个字符串** —— 13d 踩过「两边各自传自己
 *               需要的字段」的后果：加第三个维度时（`kb_role` 就是），
 *               少传一个不会编译报错，只表现为「那道门忘了看这个字段」。
 *               传对象的话，调用点必须把整个 user 交出来。
 */
export function decidePath(
  path: string,
  user: { role?: string; kbRole?: string } | undefined,
  method?: string,
): PathDecision {
  // ⚠️ 内部接口**排在 staff 之前**：它对所有身份都拒绝，
  // 所以哪怕是 admin 也不该看到「需要管理员权限」这种提示。
  if (isInternalPath(path)) {
    return {
      allowed: false,
      code: 'NOT_FOUND',
      // 措辞刻意与真正的 404 路由一致：不承认这条路径存在。
      message: '请求的资源不存在',
    };
  }

  // ---- 知识库写路径（P2-14b）：在 staff 之前 ----
  // ⚠️ 排在 staff 之前是刻意的：一条 `POST /api/v1/documents/upload`
  // 对任何人都不是「管理端路径」，所以它永远不会走到下面的 staff 分支；
  // 反过来放后面则要靠「它恰好不是 admin 路径」这个巧合，脆弱。
  if (isKbWriteRequest(path, method) && isKbRoleNone(user?.kbRole)) {
    return {
      allowed: false,
      code: 'FORBIDDEN_KB_WRITE',
      // ⚠️ 文案**不含「你的 kb_role 是什么」** —— 与后端 11b 的防枚举同源。
      message: '没有权限修改知识库，请联系管理员开通',
    };
  }

  if (!requiresStaff(path)) return { allowed: true };

  if (user?.role && (STAFF_ROLES as readonly string[]).includes(user.role)) {
    return { allowed: true };
  }
  // ⚠️ 文案**不区分「不是 admin」与「不是 hr」**：那等于把角色体系
  // 摊给任何一个想试的人（他能通过反复试推出「admin 能进、hr 不能进」
  // 还是别的组合）。统一一句「需要管理员或人事权限」，与后端一致。
  return {
    allowed: false,
    code: 'FORBIDDEN_PATH',
    message: '需要管理员或人事权限才能访问该接口',
  };
}