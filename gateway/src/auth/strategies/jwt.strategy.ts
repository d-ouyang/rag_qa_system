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
import { ExtractJwt, Strategy } from 'passport-jwt';
import { Inject } from '@nestjs/common';
import { gatewayConfig, GatewayConfig } from '../../config/configuration';

export interface JwtPayload {
  /** 用户名（subject） */
  sub: string;
  username: string;
  iat?: number;
  exp?: number;
}

/** 挂在 req.user 上的对象（下游请求头也从这里取）。 */
export interface AuthenticatedUser {
  userId: string;
  username: string;
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
   * 这里不做数据库查询 —— 用户表在进程内存里（来自环境变量配置），
   * O(1) 查表。等用户体系变复杂（改密码/踢下线/权限）再引入持久化，
   * 那时这里就是「查库 + 校验 token 版本号」的位置。
   */
  async validate(payload: JwtPayload): Promise<AuthenticatedUser> {
    return { userId: payload.sub, username: payload.username };
  }
}
