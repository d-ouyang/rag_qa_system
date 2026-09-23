import { MiddlewareConsumer, Module, NestModule } from '@nestjs/common';
import { APP_FILTER, APP_GUARD } from '@nestjs/core';
import { ConfigModule, ConfigService } from '@nestjs/config';
import { ThrottlerModule } from '@nestjs/throttler';

import { gatewayConfig, GatewayConfig } from './config/configuration';
import { AuthModule } from './auth/auth.module';
import { ProxyModule } from './proxy/proxy.module';
import { HealthModule } from './health/health.module';
import { JwtAuthGuard } from './auth/guards/jwt-auth.guard';
import { UserThrottlerGuard } from './auth/guards/user-throttler.guard';
import { AllExceptionsFilter } from './common/filters/all-exceptions.filter';
import { RequestIdMiddleware } from './common/middlewares/request-id.middleware';

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      load: [gatewayConfig],
      envFilePath: ['.env'],
    }),
    ThrottlerModule.forRootAsync({
      imports: [ConfigModule],
      inject: [ConfigService],
      useFactory: (configService: ConfigService) => {
        const config = configService.getOrThrow<GatewayConfig>('gateway');
        return [
          {
            name: 'default',
            ttl: config.throttleTtlMs,
            limit: config.throttleLimit,
          },
        ];
      },
    }),
    AuthModule,
    HealthModule,
    // 代理模块放最后：Nest 按 imports 顺序注册路由，通配路由最后注册才不会
    // 抢在具体路由前面把请求截走。
    ProxyModule,
  ],
  providers: [
    // ⚠️ 顺序即执行顺序：先鉴权（确定用户身份），再按用户限流。
    // 反过来的话限流只能按 IP 算，一是误伤同网段用户，二是多用户共用一个出口 IP 时
    // 会被互相拖累。
    { provide: APP_GUARD, useClass: JwtAuthGuard },
    { provide: APP_GUARD, useClass: UserThrottlerGuard },
    // 全局异常过滤器：所有错误收敛成统一响应体（见 common/filters 的说明）
    { provide: APP_FILTER, useClass: AllExceptionsFilter },
  ],
})
export class AppModule implements NestModule {
  configure(consumer: MiddlewareConsumer): void {
    // 中间件跑在守卫之前，所以 requestId 在鉴权失败时也已经存在 ——
    // 401 的响应体里同样带得出 requestId。
    consumer.apply(RequestIdMiddleware).forRoutes('*');
  }
}
