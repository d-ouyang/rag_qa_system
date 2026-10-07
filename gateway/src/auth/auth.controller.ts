/**
 * 鉴权接口。
 *
 *   POST /api/auth/login   登录换 token（@Public + 严格限流）
 *   GET  /api/auth/me      校验当前 token 并返回用户信息
 *   POST /api/auth/logout  前端主动登出的「对端」（无状态，仅作语义完整）
 */
import { Body, Controller, Get, Headers, HttpCode, Post, Req } from '@nestjs/common';
import { Throttle } from '@nestjs/throttler';
import { AuthService, LoginResult } from './auth.service';
import { LoginDto } from './dto/login.dto';
import { Public } from './decorators/public.decorator';
import { AuthenticatedUser } from './strategies/jwt.strategy';

@Controller('api/auth')
export class AuthController {
  constructor(private readonly authService: AuthService) {}

  /**
   * 登录。
   *
   * 限流刻意比全局严得多且写死（8 次/分钟/IP）：登录是唯一能用「试」来攻破的接口，
   * 8 次/分钟下暴力破解 8 位密码需要天文数字的时间。做成可配置项没意义 ——
   * 没人会把它调得更宽松，而调松了就等于把闸门拆了。
   *
   * ⚠️ 11c 之后这层限流**变得更关键**：后端的密码策略里有「连错 5 次锁 15 分钟」，
   * 但那是**按账号**的 —— 同一 IP 换 20 个账号各试 5 次就是 100 次，
   * IP 限流是唯一拦住这件事的东西。所以不要把它调宽。
   */
  @Public()
  @Throttle({ default: { limit: 8, ttl: 60_000 } })
  @HttpCode(200)
  @Post('login')
  login(
    @Body() dto: LoginDto,
    @Headers('x-forwarded-for') forwardedFor?: string,
    @Req() req?: { ip?: string; socket?: { remoteAddress?: string } },
  ): Promise<LoginResult> {
    return this.authService.login(dto, clientIpOf(forwardedFor, req));
  }

  /** 当前用户：前端启动时用它验证本地 token 是否还有效（无效会拿到 401）。 */
  @Get('me')
  me(@Req() req: { user: AuthenticatedUser }) {
    return this.authService.me(req.user);
  }

  /**
   * 登出。
   *
   * JWT 无状态，服务端没有会话可销毁，这里返回 200 只是让前端有个明确的
   * 「登出成功」信号（前端负责丢弃本地 token）。保留这个接口而不是让前端
   * 自己清缓存，是为了将来加 token 黑名单/版本号时接口不用改。
   *
   * P2-11c 起它还会**顺带落一条审计**（`auth.logout`）—— 由后端记，
   * 因为审计表在 MySQL 那一侧，gateway 自己不写。
   */
  @HttpCode(200)
  @Post('logout')
  logout(@Req() req: { user?: AuthenticatedUser }): { ok: true; username: string | null } {
    return { ok: true, username: req.user?.username ?? null };
  }
}

/**
 * 取本次登录的来源 IP（转给后端供审计落库）。
 *
 * 优先 `X-Forwarded-For` 的**第一段**：`req.ip` 在网关后面拿到的是
 * 反代（Nginx）的地址，本地全是 127.0.0.1，记它等于没记。
 *
 * ⚠️ 敢信 XFF 的前提与后端那一侧同源：它只由前置反代设置。
 * 哪天有人把网关直接暴露出去、前面没有反代，这个头就是客户端自由填的，
 * 审计里的 IP 就只是「一个自称来自某地的人」。
 */
function clientIpOf(
  forwardedFor: string | undefined,
  req: { ip?: string; socket?: { remoteAddress?: string } } | undefined,
): string | undefined {
  const first = (forwardedFor ?? '').split(',')[0]?.trim();
  if (first) return first;
  return req?.ip ?? req?.socket?.remoteAddress ?? undefined;
}
