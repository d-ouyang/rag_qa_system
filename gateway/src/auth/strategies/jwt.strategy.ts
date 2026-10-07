/**
 * JWT 策略（passport-jwt）。
 *
 * 约定：token 从 `Authorization: Bearer <token>` 头里取。
 * 为什么不支持 query 参数传 token（`?token=xxx`）：
 * URL 会进浏览器历史、Nginx access log、Referer 头，等于把凭据写进日志。
 * 唯一例外是 NDJSON 流式请求 —— 但它用的是 fetch + POST，能带自定义头，
 * 不需要靠 query 传（这也是当初选 NDJSON 而不是 EventSource 的原因之一）。
 */
import { Injectable } from '@nestjs/common';
import { PassportStrategy } from '@nestjs/passport';
// ⚠️ ExtractJwt / Strategy 来自 `passport-jwt`，**不是** `@nestjs/passport`
//    （后者只导出 PassportStrategy）。写错包名 tsc 会直接报「无此导出成员」，
//    而运行时是 `Strategy is not a constructor` —— 编译期能抓住，别当运行时问题查。
import { ExtractJwt, Strategy } from 'passport-jwt';
import { Inject } from '@nestjs/common';
import { gatewayConfig, GatewayConfig } from '../../config/configuration';
// ⚠️ 从 internal-auth.client 引入 KbRole / normalizeKbRole，而不是在 jwt.strategy
// 里重写一份五档清单 —— 与 `INBOUND_IDENTITY_HEADERS` 同一个理由（跨语言
// 清单要单一出处；跨文件清单要单一模块）。
import { KbRole, normalizeKbRole } from '../internal-auth.client';

export interface JwtPayload {
  /**
   * subject。
   *
   * ⚠️ **P2-11c 之前它是登录名字符串，11c 之后它是 `user.id` 的十进制串。**
   * 理由：整数 id 才是稳定的用户标识（登录名理论上可改）。
   *
   * 保留成字符串是因为 JWT 的 `sub` 按规范必须是字符串。11c 之前签的是
   * 登录名，所以这个字段的**语义变了** —— 旧 token 的 `sub` 是名字、
   * 新的是数字。后果：一个 11c 之前签发的 token（最长 12 小时有效期）
   * 在 11c 之后仍能通过校验（`uid` 为 null → 走兼容分支，见 `validate`）。
   * 换句话说：**升级后有一个最长 12 小时的窗口，旧 token 里的身份不含 uid**。
   * 要立刻失效就把 `GATEWAY_JWT_EXPIRES_IN` 临时调短再改回去。
   */
  sub: string;
  /** `user.id`；11c 之前签发的 token 里没有这个字段（null）。 */
  uid?: number | null;
  username: string;
  /** `user.role`（admin / hr / user）。11c 之前没有。 */
  role?: string;
  /**
   * `user.kb_role`（none / ops / qa / dev / superadmin）。**P2-14b 起才有**，
   * 之前签发的 token 里没有这个字段。
   *
   * ⚠️ 缺失时**必须当`none` 处理**（fail-closed），不能当放行：
   * 一个 `kb_role=ops` 的人在他 token 签发后被降级成 `none`，
   * 他手里的旧 token 仍带着 `ops`。反过来按 `none` 处理只损失
   * 「最长 12 小时内要重登一次」，而按 `ops` 处理是真漏洞。
   * 兜底在 `core/kb_acl.normalize()` 与 `isKbRoleNone()` 两处各做一次。
   */
  kbRole?: string;
  /** `token_version` —— 改密 / 停用 / 改角色会让它 +1，后端据此判旧 token 失效。 */
  ver?: number;
  /** 身份来源：mysql（正常）| breakglass（.env 回落）。 */
  src?: string;
  iat?: number;
  exp?: number;
}

/**
 * 挂在 req.user 上的对象（下游请求头也从这里取）。
 *
 * `userId` 是**字符串**而不是 number：它要原样进 HTTP 头，
 * 而 `X-User-Id` 的值是文本；在这里就转成数字的话，null（break-glass）
 * 与数字之间要做类型 gymnastics，而 HTTP 头根本不在乎。
 */
export interface AuthenticatedUser {
  userId: string;
  username: string;
  /** 11c 起：整数 id；break-glass 或旧 token 时为 null。 */
  uid: number | null;
  role: string;
  /**
   * 知识库写权限（P2-14b）。**必填**（不是可选）——
   * 缺失时 `validate()` 会填 `'none'`，所以下游读它不用判空。
   * 做成必填而不是 `kbRole?: string`：可选字段会逼每个调用点写
   * `user.kbRole ?? 'none'`，而那行一旦漏掉就是「undefined 被当成有权限」
   * 或「undefined 让判据整个失效」，两种都不报错。
   */
  kbRole: KbRole;
  /** token_version；后端比对它判断这个 token 是否已被改密/停用作废。 */
  tokenVersion: number;
  source: string;
}

@Injectable()
export class JwtStrategy extends PassportStrategy(Strategy, 'jwt') {
  constructor(@Inject(gatewayConfig.KEY) config: GatewayConfig) {
    super({
      jwtFromRequest: ExtractJwt.fromAuthHeaderAsBearerToken(),
      // 过期必须由服务端判定：token 的 exp 是签发时写死的，客户端时间不可信
      ignoreExpiration: false,
      secretOrKey: config.jwtSecret,
    });
  }

  /**
   * 校验通过后 passport 会把返回值挂到 `req.user`。
   *
   * 11c 起**这里仍然不查库** —— `status` / `token_version` 的比对放在后端
   * （它本来就要查库），网关只负责把 `ver` 原样传给下游。
   * 这是设计规格 §8.1 建议的方案 3：零额外往返，吊销依赖后端参与。
   *
   * ⚠️ 代价要说清：**前端**（5173 / 5174）在 11c 之后仍能拿着一个已被
   * 改密/停用作废的 token 调网关自己的接口（`/api/auth/me`、限流计数），
   * 因为网关不查库。它能过网关，但过不了后端 —— 而所有真正的业务接口
   * 都在后端。要让 `/api/auth/me` 也立刻失效，得让网关查一次库
   * （设计规格 §8.1 的方案 1/2），代价是每个请求多一次 DB 往返。
   * 选方案 3 是因为**真正需要失效的地方都在后端**。
   */
  async validate(payload: JwtPayload): Promise<AuthenticatedUser> {
    // 11c 兼容分支：旧 token 没有 uid。此时 `sub` 是登录名，
    // 仍能用（后端 identity 会按数字/登录名两条路都试，见 core/identity.py）。
    const uid = typeof payload.uid === 'number' ? payload.uid : null;
    return {
      // 旧 token 的 sub 是登录名（不能当 id 用），新 token 的是数字 id
      userId: uid !== null ? String(uid) : payload.sub,
      username: payload.username ?? payload.sub,
      uid,
      role: payload.role ?? '',
      // ⚠️ 缺失（P2-14b 之前签发的 token）或不认识 → **一律none**。
      // 见 JwtPayload.kbRole 的注释：fail-closed 方向必须是「进不去」。
      kbRole: normalizeKbRole(payload.kbRole),
      tokenVersion: typeof payload.ver === 'number' ? payload.ver : 0,
      source: payload.src ?? 'legacy',
    };
  }
}
