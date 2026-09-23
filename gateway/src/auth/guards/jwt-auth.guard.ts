/**
 * 全局 JWT 守卫 —— 默认拦截一切请求，只有 `@Public()` 标记的路由放行。
 *
 * 关键点：401 的响应体必须和网关其他错误**同构**（`{error: {code, message, ...}}`），
 * 否则前端要为「鉴权失败」和「其他失败」写两套解析逻辑。
 * 所以这里不抛裸的 UnauthorizedException，而是带上 code/message 的对象。
 */
import { ExecutionContext, Injectable, UnauthorizedException } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { AuthGuard } from '@nestjs/passport';
import { IS_PUBLIC_KEY } from '../decorators/public.decorator';
import { AuthenticatedUser } from '../strategies/jwt.strategy';

@Injectable()
export class JwtAuthGuard extends AuthGuard('jwt') {
  constructor(private readonly reflector: Reflector) {
    super();
  }

  canActivate(context: ExecutionContext) {
    const isPublic = this.reflector.getAllAndOverride<boolean>(IS_PUBLIC_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (isPublic) return true;
    return super.canActivate(context);
  }

  /**
   * 覆写 handleRequest 是为了控制错误语义与响应体形状。
   *
   * 区分「没带 token」和「token 过期/无效」——前端处理方式不同：
   * 前者（用户没登录）应该直接跳登录页；后者（会话过期）还该提示一句
   * 「登录已过期，请重新登录」，否则用户会以为自己根本没登录过。
   */
  handleRequest<TUser = AuthenticatedUser>(
    err: unknown,
    user: TUser | false,
    info: { message?: string; name?: string } | undefined,
  ): TUser {
    if (err || !user) {
      const infoName = info?.name ?? '';
      const expired = infoName === 'TokenExpiredError';
      const missing = infoName === 'Error' && (info?.message ?? '').includes('No auth token');
      throw new UnauthorizedException({
        code: expired ? 'TOKEN_EXPIRED' : missing ? 'TOKEN_MISSING' : 'UNAUTHORIZED',
        message: expired
          ? '登录已过期，请重新登录'
          : missing
            ? '未登录，请先登录'
            : '登录状态无效，请重新登录',
        detail: info?.message,
      });
    }
    return user;
  }
}
