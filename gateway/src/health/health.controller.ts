/**
 * 网关自身的健康与元信息。
 *
 *   GET /api/health         网关进程探活（给负载均衡/监控用，无需 token）
 *
 * 注意这个路由**不能**被代理走（它不在 /api/v1 下），否则「网关是否活着」
 * 就变成了「后端是否活着」—— 探活会失去意义：后端挂了会把网关也判死，
 * 而网关其实是好的（这正是网关存在的价值之一）。
 */
import { Controller, Get, Inject } from '@nestjs/common';
import { gatewayConfig, GatewayConfig } from '../config/configuration';
import { Public } from '../auth/decorators/public.decorator';

@Controller('api')
export class HealthController {
  constructor(@Inject(gatewayConfig.KEY) private readonly config: GatewayConfig) {}

  @Public()
  @Get('health')
  health() {
    return {
      status: 'ok',
      service: 'rag-qa-gateway',
      version: process.env.npm_package_version ?? '2.0.0-p0.2',
      // 只报「用户表来源」与「后端地址」，绝不回显密钥、口令或哈希
      user_source: this.config.users.source,
      user_count: this.config.users.map.size,
      upstream: this.config.backendUrl,
      env: this.config.isProduction ? 'production' : 'development',
      uptime_seconds: Math.round(process.uptime()),
      timestamp: new Date().toISOString(),
    };
  }
}
