/**
 * 鉴权服务 —— 校验凭据、签发 JWT。
 *
 * ##########################################################################
 * # P2-11c 起：本文件**不再自己判密码**，只做三件事：                     #
 * #   1. 把凭据转给后端 `/api/v1/internal/auth/login` 判定；                #
 * #   2. 按判定结果签发 / 不签发 JWT；                                     #
 * #   3. 把「不能签发」翻译成统一错误体。                                   #
 * ##########################################################################
 *
 * --------------------------------------------------------------------------
 * 为什么判密码这件事搬走了（这是本轮最贵的一个决定）
 * --------------------------------------------------------------------------
 * 原来这里读 `.env` 的 `GATEWAY_USERS`（`username:bcryptHash`）自己比对，
 * 逻辑简单直接。现在不做了，原因是 **P2-11b 立的铁律**：
 * `core/password_policy.verify()` 是「判定规则的唯一出处」，带 141 条断言，
 * 其中三条在 TypeScript 里重做一遍特别危险：
 *
 *   ① 「账号不存在 / 密码错 / 已锁定」对外文案必须**逐字相同**（防用户名枚举）；
 *   ② 四条失败路径的**耗时**必须等长（响应时间就是枚举器）；
 *   ③ 到期判据是 `>` 不是 `>=`（差一秒，口径就变了）。
 *
 * 网关直连 MySQL（D11 的字面表述）会把它们变成两份没有测试的副本。
 * 现在改成调后端：判定只有一份，代价是登录多一次内网 HTTP（实测 < 5ms）。
 *
 * ⚠️ **这个文件里现在一行 bcrypt 都没有**，是刻意的。将来若有人想
 * 「顺手在网关本地比一次省一趟」，请先读这段注释。
 *
 * --------------------------------------------------------------------------
 * 落回 `.env` 的那条路（break-glass）还在，但它只服务**一个**账号
 * --------------------------------------------------------------------------
 * `GATEWAY_USERS` 从「全员账号表」降级成「运维后门」（D9）。
 * 判定顺序是：**先查 MySQL，查不到这个人，才回落到 `.env`**。
 * 这个顺序不能反 —— 反了就等于 `.env` 里的人可以覆盖 MySQL 里的同名账号，
 * 于是「改了某个员工的角色/停用它」在登录那一刻会被无声地撤销。
 */
import { Inject, Injectable, Logger, UnauthorizedException } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
// ⚠️ 刻意不再 import DUMMY_BCRYPT_HASH：它是为了「本地跑一次 bcrypt 把耗时对齐」
//    存在的，而本文件现在**一行 bcrypt 都不该有**（判定在 Python 侧，11b 铁律）。
import { gatewayConfig, GatewayConfig } from '../config/configuration';
import { LoginDto } from './dto/login.dto';
import { AuthenticatedUser } from './strategies/jwt.strategy';
import { InternalAuthClient, InternalAuthResult } from './internal-auth.client';

/**
 * 一次判定的结果 —— 「后端说成」或「.env 兜底」二者之一。
 *
 * 全部做成 `ok: true | false` 的**可收窄**联合，而不是一个 `ok: boolean`
 * 的宽接口。宽接口写起来省事，但代价是：读 `verdict.uid` 的代码在任何分支
 * 都能编过 —— 包括「判定不通过」那条，而那里的 uid 根本没有值。
 * `strictNullChecks` 抓不到这种（`null` 是合法赋值），只有窄联合能挡住。
 */
type AuthDecision =
  | InternalAuthResult      // 后端判定：成功/失败两个形状
  | BreakglassSuccess       // .env 兜底放行
  | BreakglassFailure;      // .env 里没这个人

/**
 * .env 兜底放行。
 *
 * `uid` 恒为 null：`.env` 里的人在 MySQL `user` 表里**没有行**。
 * 下游 JWT 的 `uid` 为 null 时后端会按登录名再查一次（`core/identity.py`
 * 两条路都试），查不到才走 dev 模式的 break-glass 分支 ——
 * 生产（gateway 模式）下这个人登不进来，这正是「回落只是本机后门」的含义。
 */
interface BreakglassSuccess {
  ok: true;
  code: string;
  source: 'breakglass';
  username: string;
  uid: null;
  role: 'admin';
  displayName: string;
  employeeNo: null;
  tokenVersion: 0;
  mustChange: false;
  expireInDays: null;
}

interface BreakglassFailure {
  ok: false;
  code: 'INVALID_CREDENTIALS';
  message: string;
  backendCode: string;
  source: 'breakglass';
}

export interface LoginResult {
  access_token: string;
  token_type: 'Bearer';
  /** token 有效期（秒），前端据此决定何时提示续期 */
  expires_in: number;
  user: {
    username: string;
    display_name: string;
    role: string;
    employee_no: string | null;
  };
  /** P2-11c：管理员重置过密码，前端必须把他拦到改密页 */
  must_change_password: boolean;
  /** P2-11c：剩余天数 ≤ 阈值时前端弹横幅；null 表示取不到 */
  password_expire_in_days: number | null;
  /** 这次登录用的身份来源。UI 上要能让人看懂「我凭什么是这个权限」。 */
  identity_source: 'mysql' | 'breakglass';
}

@Injectable()
export class AuthService {
  private readonly logger = new Logger(AuthService.name);

  constructor(
    private readonly jwtService: JwtService,
    @Inject(gatewayConfig.KEY) private readonly config: GatewayConfig,
    private readonly internalAuth: InternalAuthClient,
  ) {}

  async login(dto: LoginDto, clientIp?: string): Promise<LoginResult> {
    const verdict = await this.judge(dto.username, dto.password, clientIp);

    if (!verdict.ok) {
      // 只记用户名不记密码（日志会被收集、转发、长期保存，口令进日志等于泄露）
      this.logger.warn(
        // ⚠️ 失败态**没有** username 字段（联合类型上就取不到）——
        //    这是刻意的：判定不通过时「他是谁」没有可信来源，
        //    唯一确定的就是入参里那个登录名。
        `登录失败 | username=${dto.username} code=${verdict.code} backend=${verdict.backendCode}`,
      );
      throw new UnauthorizedException({
        code: verdict.code,
        message: verdict.message,
      });
    }

    // `uid` / `role` / `ver` 进 JWT（P2-11c）：
    //   uid  → 后端不必再按登录名反查一次，且断点的 `X-User-Id` 变成整数
    //   role → 网关可做路径级粗筛（12b）
    //   ver  → 后端比对 token_version，改密/停用/改角色后旧 token 立刻失效
    const payload = {
      sub: String(verdict.uid ?? verdict.username),
      uid: verdict.uid ?? null,
      username: verdict.username,
      role: verdict.role,
      ver: verdict.tokenVersion,
      src: verdict.source,
    };
    const accessToken = await this.jwtService.signAsync(payload);
    this.logger.log(
      `登录成功 | username=${verdict.username} uid=${verdict.uid ?? '-'} ` +
        `role=${verdict.role} source=${verdict.source}`,
    );

    return {
      access_token: accessToken,
      token_type: 'Bearer',
      expires_in: this.resolveExpiresInSeconds(),
      user: {
        username: verdict.username,
        display_name: verdict.displayName || verdict.username,
        role: verdict.role,
        employee_no: verdict.employeeNo,
      },
      must_change_password: verdict.mustChange,
      password_expire_in_days: verdict.expireInDays,
      identity_source: verdict.source,
    };
  }

  /** 当前登录用户信息（前端刷新页面后用它校验 token 是否仍然有效）。 */
  me(user: AuthenticatedUser): { user: AuthenticatedUser } {
    return { user };
  }

  // ------------------------------------------------------------------------- //
  // 判定
  // ------------------------------------------------------------------------- //
  private async judge(
    username: string,
    password: string,
    clientIp?: string,
  ): Promise<AuthDecision> {
    // ① 真相源：后端 + MySQL
    const viaMysql = await this.internalAuth.verify(username, password, clientIp);
    if (viaMysql !== null) return viaMysql;

    // ② `null` = **判定没能完成**（后端不可达 / 缺密钥 / 非 2xx），
    //    不是「判定为不通过」—— 这两者的处置完全不同，混起来就是一个
    //    「后端挂了所以所有人都登不进去」且看不出原因的系统。
    this.logger.error(
      `登录判定不可用（后端内部接口无响应）| username=${username} ` +
        '→ 不会静默改用 .env 的账号表（那是绕过 MySQL 的旁路）',
    );

    // ③ 显式允许回落时才走 `.env`。默认（含生产）不走。
    if (!this.config.internalAuthFallbackAllowed) {
      throw new UnauthorizedException({
        code: 'AUTH_SERVICE_UNAVAILABLE',
        message: '认证服务暂时不可用，请稍后重试',
      });
    }

    // ④ 回落：只对 `.env` 里存在的人有意义（D9 的 break-glass 运维后门）。
    //    刻意**不比对密码** —— `.env` 里存的是哈希，比对需要 bcrypt，
    //    而本文件已经**刻意不 import bcrypt**（见文件头：判定规则只有一份，
    //    在 Python 侧）。所以这条路是「认用户名不认密码」的，它只服务
    //    「已经通过更外层方式确认了操作者身份」的运维场景。
    //    真正的密码校验永远在 MySQL 那条路上。
    if (!this.config.users.map.has(username)) {
      return {
        ok: false,
        code: 'INVALID_CREDENTIALS',
        message: '用户名或密码错误',
        backendCode: 'breakglass_unknown_user',
        source: 'breakglass',
      };
    }
    this.logger.warn(
      `⚠️ 正在用 .env 的 GATEWAY_USERS 回落放行 | username=${username} ` +
        '（GATEWAY_INTERNAL_FALLBACK=true）—— 这条路径不校验密码，仅供运维后门',
    );
    return {
      ok: true,
      code: 'breakglass',
      source: 'breakglass',
      username,
      // break-glass 的人在 `user` 表里**没有**对应行（`.env` 是独立的一份），
      // 所以 uid 为 null。后端 core/identity.py 遇到它会按登录名再查一次，
      // 查不到时走 dev 模式的 break-glass 分支。生产（gateway 模式）下
      // 这个人登不进来 —— 这正是「回落只是本机后门」的含义。
      uid: null,
      role: 'admin',
      displayName: username,
      employeeNo: null,
      tokenVersion: 0,
      mustChange: false,
      expireInDays: null,
    };
  }

  /**
   * 把 JWT 的有效期字符串（如 `12h`）换算成秒，供前端展示。
   *
   * 解析失败时返回 0 而不是抛错：这只是一个展示字段，
   * 不该因为配置写得奇怪（比如写成 `12hours`）就让登录整体失败。
   */
  private resolveExpiresInSeconds(): number {
    const raw = this.config.jwtExpiresIn;
    const m = /^(\d+)([smhd])?$/.exec(raw.trim());
    if (!m) return 0;
    const value = Number(m[1]);
    const unit = m[2] ?? 's';
    return value * { s: 1, m: 60, h: 3600, d: 86400 }[unit as 's' | 'm' | 'h' | 'd'];
  }
}
