/**
 * 鉴权接口。
 *
 *   POST /api/auth/login   登录换 token（@Public + 严格限流）
 *   GET  /api/auth/me      校验当前 token 并返回用户信息
 *   POST /api/auth/logout  前端主动登出的「对端」（无状态，仅作语义完整）
 */
import { Body, Controller, Get, HttpCode, Post, Req } from '@nestjs/common';
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
   */
  @Public()
  @Throttle({ default: { limit: 8, ttl: 60_000 } })
  @HttpCode(200)
  @Post('login')
  login(@Body() dto: LoginDto): Promise<LoginResult> {
    return this.authService.login(dto);
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
   */
  @HttpCode(200)
  @Post('logout')
  logout(@Req() req: { user: AuthenticatedUser }) {
    return { ok: true, username: req.user?.username ?? null };
  }
}
