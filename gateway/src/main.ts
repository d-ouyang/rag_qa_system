/**
 * 网关启动入口。
 *
 * 启动顺序里有三件事值得留意（都在下面注释里）：
 *   ① body-parser 关掉再手动挂 —— 让「请求体怎么被解析」显式可控；
 *   ② CORS 必须在守卫之前生效，否则浏览器的 OPTIONS 预检会被 401 拦掉；
 *   ③ 启动时打印关键配置摘要（不含任何密钥），便于确认「起的是不是想要的那个实例」。
 */
import 'reflect-metadata';
import { Logger, ValidationPipe } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { NestFactory } from '@nestjs/core';
import { NestExpressApplication } from '@nestjs/platform-express';
import { json, urlencoded } from 'express';
import { AppModule } from './app.module';
import { GatewayConfig } from './config/configuration';

/**
 * 进程级异常兜底。
 *
 * 为什么网关选择「记录并继续」而不是像 Node 默认那样直接退出：
 * 网关是**无状态转发服务**，不持有会话/缓存，单个请求的异常不会让内存里的
 * 状态变得不可信；而它又是全站唯一入口，崩掉一次就是「所有人问答中断」。
 * 之前就吃过一次亏：代理回调里一个 ERR_HTTP_HEADERS_SENT 没被捕获，
 * 一个普通 POST 请求就把整个进程带走了（进程消失、容器重启、在途请求全断）。
 *
 * 代价要说清楚：如果将来网关开始持有状态（例如把限流计数放进程内存），
 * 这个兜底就成了「带着损坏状态继续服务」的帮凶，那时应该改成记录后退出、
 * 由编排层拉起新实例。当前选择是明确的可用性优先。
 */
process.on('uncaughtException', (err) => {
  const logger = new Logger('Uncaught');
  logger.error(`未捕获异常（进程继续运行）：${err.message}`);
  if (err.stack) logger.error(err.stack);
});

process.on('unhandledRejection', (reason) => {
  const logger = new Logger('UnhandledRejection');
  const detail = reason instanceof Error ? `${reason.message}\n${reason.stack ?? ''}` : String(reason);
  logger.error(`未处理的 Promise 拒绝（进程继续运行）：${detail}`);
});

async function bootstrap(): Promise<void> {
  const logger = new Logger('Bootstrap');

  const app = await NestFactory.create<NestExpressApplication>(AppModule, {
    // 关掉 Nest 默认的 body parser，改在下面手动挂（原因见 ①）
    bodyParser: false,
    // 日志级别从环境变量控制，生产可以调成 log/warn/error
    logger: ['log', 'warn', 'error'],
  });

  const config = app.get(ConfigService).getOrThrow<GatewayConfig>('gateway');

  // ① 显式挂 body parser
  //    为什么不用 Nest 默认的：默认 json limit 是 100kb，一旦有人粘贴长文本提问
  //    就会吃 413，而错误信息只说「请求体过大」，排查时很难联想到是网关而不是后端
  //    的限制。显式写在这里，改动一处、可解释一处。
  //    另外这两个 parser 只解析 json / urlencoded —— multipart（文件上传）不碰，
  //    原始流保持可读，代理才能把它完整转发给后端。
  app.use(json({ limit: '4mb' }));
  app.use(urlencoded({ extended: true, limit: '4mb' }));

  // ② CORS。浏览器发跨域请求前会先发 OPTIONS 预检，而预检**不带 Authorization**，
  //    如果让它走到 JWT 守卫就会被 401 拦掉，表现为「前端怎么都登录不上，控制台
  //    只有一条 CORS 报错」。cors 中间件会在路由之前直接响应 OPTIONS（返回 204），
  //    所以这里开启它同时也是在保护预检。
  app.enableCors({
    origin: config.allowedOrigins,
    credentials: true,
    allowedHeaders: ['Content-Type', 'Authorization', 'X-Request-Id'],
    exposedHeaders: ['X-Request-Id'],
    maxAge: 600,
  });

  /**
   * trust proxy 控制 `req.ip` 是否信任 X-Forwarded-For。
   *
   * ⚠️ 这是个安全开关，不能默认打开：如果网关直接暴露在公网，
   * 任何人都能自己伪造 X-Forwarded-For 来伪装 IP，从而绕过按 IP 的登录限流。
   * 只有当网关**确实**躲在 Nginx 后面，且 Nginx 用 `$remote_addr` 覆盖（而不是
   * 追加）该头时，才应该打开。
   */
  if (process.env.GATEWAY_TRUST_PROXY === 'true') {
    app.set('trust proxy', true);
    logger.log('已启用 trust proxy：req.ip 采信 X-Forwarded-For（仅在 Nginx 前置且覆盖该头时安全）');
  }

  // 请求参数校验：白名单模式（未声明的字段直接剔除），并把校验错误交给全局过滤器统一格式化
  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      forbidNonWhitelisted: true,
      transform: true,
    }),
  );

  // 收到 SIGTERM 时先停止接收新请求再退出（容器滚动更新时不会掐断在途请求）
  app.enableShutdownHooks();

  await app.listen(config.port, '0.0.0.0');

  // ③ 启动摘要：刻意只打印「有无配置」而不打印配置值本身
  logger.log(`网关已启动 | http://0.0.0.0:${config.port} env=${config.isProduction ? 'production' : 'development'}`);
  logger.log(`转发规则 | /api/v1/* -> ${config.backendUrl}（代理超时 ${config.proxyTimeoutMs}ms）`);
  logger.log(
    `鉴权 | 用户表来源=${config.users.source} 账号数=${config.users.map.size} token有效期=${config.jwtExpiresIn}`,
  );
  logger.log(
    `限流 | ${config.throttleLimit} 次 / ${Math.round(config.throttleTtlMs / 1000)}s（按用户，未登录按 IP）；登录接口 8 次/分钟`,
  );
  logger.log(`CORS | 允许来源=${config.allowedOrigins.join(' ')}`);
}

bootstrap().catch((err) => {
  // 配置缺失（如生产没设 JWT 密钥）会走到这里：打印人话后以非 0 退出，
  // 让容器编排判定启动失败，而不是「起来了但没防护」。
  const logger = new Logger('Bootstrap');
  logger.error(`网关启动失败：${err instanceof Error ? err.message : String(err)}`);
  if (err instanceof Error && err.stack) logger.error(err.stack);
  process.exit(1);
});
