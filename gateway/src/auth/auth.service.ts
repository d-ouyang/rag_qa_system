/**
 * 鉴权服务 —— 校验凭据、签发 JWT。
 *
 * 三个刻意的设计点：
 *
 * 1. **用户不存在的分支也跑一次 bcrypt 比对**
 *    bcrypt 是故意慢的（约 100ms）。如果不存在的用户立刻返回，
 *    攻击者就能用响应时间区分「用户名不存在」和「密码错误」，
 *    从而先枚举出有效用户名再暴力破解密码。所以给不存在的用户也喂一个
 *    哑 hash 做一次等长比对，把两条路径的耗时抹平。
 *
 * 2. **只返回 access token，不做服务端会话**
 *    JWT 是无状态的，登出由前端丢弃 token 实现。代价是「签发后无法立刻吊销」，
 *    对单体小用户量的系统可以接受（等真需要吊销时引入 token 版本号/黑名单）。
 *    这个取舍写在这里，而不是让别人以为「忘了做登出」。
 *
 * 3. **失败一律返回同一个错误文案**
 *    「用户名或密码错误」不区分是哪个错，避免给撞库的人提供反馈信号。
 */
import { Inject, Injectable, Logger, UnauthorizedException } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import * as bcrypt from 'bcryptjs';
import { gatewayConfig, GatewayConfig, DUMMY_BCRYPT_HASH } from '../config/configuration';
import { LoginDto } from './dto/login.dto';
import { AuthenticatedUser } from './strategies/jwt.strategy';

export interface LoginResult {
  access_token: string;
  token_type: 'Bearer';
  /** token 有效期（秒），前端据此决定何时提示续期 */
  expires_in: number;
  user: { username: string };
}

@Injectable()
export class AuthService {
  private readonly logger = new Logger(AuthService.name);

  constructor(
    private readonly jwtService: JwtService,
    @Inject(gatewayConfig.KEY) private readonly config: GatewayConfig,
  ) {}

  async login(dto: LoginDto): Promise<LoginResult> {
    const hash = this.config.users.map.get(dto.username);
    const matched = await bcrypt.compare(dto.password, hash ?? DUMMY_BCRYPT_HASH);

    if (!hash || !matched) {
      // 只记用户名不记密码（日志会被收集、转发、长期保存，口令进日志等于泄露）
      this.logger.warn(`登录失败 | username=${dto.username} reason=${hash ? 'bad-password' : 'unknown-user'}`);
      throw new UnauthorizedException({
        code: 'INVALID_CREDENTIALS',
        message: '用户名或密码错误',
      });
    }

    const payload = { sub: dto.username, username: dto.username };
    const accessToken = await this.jwtService.signAsync(payload);
    this.logger.log(`登录成功 | username=${dto.username}`);

    return {
      access_token: accessToken,
      token_type: 'Bearer',
      expires_in: this.resolveExpiresInSeconds(),
      user: { username: dto.username },
    };
  }

  /** 当前登录用户信息（前端刷新页面后用它校验 token 是否仍然有效）。 */
  me(user: AuthenticatedUser): { user: AuthenticatedUser } {
    return { user };
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
