/**
 * 反向代理 —— 网关的最后一站：把通过鉴权的请求原样转给后端 FastAPI。
 *
 * ---------------------------------------------------------------------------
 * 为什么不用 `app.use(createProxyMiddleware(...))`（最省事的写法）
 * ---------------------------------------------------------------------------
 * Express 中间件**跑在 Nest 守卫之前**。如果代理挂在中间件层，
 * 请求会先被转发到后端、之后守卫才判断有没有 token —— 等于鉴权完全失效。
 * 实测表现是「没带 token 也能正常问答」，而且日志一切正常，极难发现。
 *
 * 所以代理注册成 **Controller 里的通配路由**：
 *   中间件（requestId）→ 守卫（JWT + 限流）→ 拦截器 → 路由 handler（代理转发）
 * 只有走到 handler 才转发，鉴权一定是已经过了的。
 *
 * ---------------------------------------------------------------------------
 * 三个容易踩的坑（都已处理）
 * ---------------------------------------------------------------------------
 * 1. **body 被 body-parser 吃掉**
 *    Nest 启动时默认挂 express.json()，请求体已被解析成 req.body，
 *    原始流已经消费完 —— 直接代理会转发一个空 body（后端收到空请求体，
 *    表现为「问答参数校验失败 422」，而且只在 POST 上出现）。
 *    解法：用官方提供的 `fixRequestBody` 在转发前把 req.body 重新写进 proxyReq。
 *    文件上传的 multipart 不走 body-parser，流没被动过，照常 pipe。
 *
 * 2. **流式响应被缓冲**
 *    NDJSON 逐 token 输出依赖代理边收边转。http-proxy-middleware 默认就是
 *    pipe 不缓冲，但必须保证 `selfHandleResponse: false`（自己接管响应就会
 *    先把整个响应收完再发，逐字效果立刻消失）。另外转发请求里显式带
 *    `Accept-Encoding: identity` 更稳：让后端不要压缩，中间少一层 gzip 缓冲。
 *
 * 3. **超时太短把长回答掐断**
 *    本地 9B 模型单轮可以跑到 30~80 秒，代理默认超时会直接 504。
 *    所以 proxyTimeout 走配置（默认 180s），要大于后端链路本身的超时。
 */
import {
  All,
  Controller,
  Inject,
  Logger,
  Next,
  Req,
  Res,
} from '@nestjs/common';
import { NextFunction, Request, Response } from 'express';
import { createProxyMiddleware, fixRequestBody, RequestHandler } from 'http-proxy-middleware';
import { gatewayConfig, GatewayConfig } from '../config/configuration';
import { AuthenticatedUser } from '../auth/strategies/jwt.strategy';
import { Public } from '../auth/decorators/public.decorator';

/**
 * 免 token 放行的上游健康检查路径（PLAN-v2.0.0 P0-2 白名单）。
 *
 * 为什么单独放行这两个：上线后接 Uptime 类监控 / 负载均衡探活，
 * 它们拿不到 token，但又必须能判断「整条链路（网关 + 后端）是否活着」。
 * 只探网关自己（`/api/health`）区分不出「后端挂了」—— 那才是最常发生的故障。
 *
 * 安全性：这两个路径只返回 `{status,name,version}`，不含任何用户数据或密钥；
 * 且是**精确路径**而非前缀通配，不会顺带打开 `/api/v1/system/*` 下的其他接口
 * （`smoke-test.sh` 有反向断言盯着这一点）。
 */
export const PUBLIC_HEALTH_PATHS = ['api/v1/system/health', 'api/v1/qa/health'] as const;

/** 挂在 req 上的额外字段（本文件自己加的，做类型标注方便读写）。 */
type ProxiedRequest = Request & {
  user?: AuthenticatedUser;
  requestId?: string;
  proxyStartedAt?: number;
};

@Controller()
export class ProxyController {
  private readonly logger = new Logger('Proxy');
  private readonly proxy: RequestHandler;

  constructor(@Inject(gatewayConfig.KEY) private readonly config: GatewayConfig) {
    this.proxy = createProxyMiddleware({
      target: config.backendUrl,
      changeOrigin: true,
      ws: false,
      // 带上 X-Forwarded-For/Host/Proto，后端日志与限流才看得到真实来源
      xfwd: true,
      timeout: config.proxyTimeoutMs,
      proxyTimeout: config.proxyTimeoutMs,
      // 保持默认的「代理自己 pipe 响应」：这是流式不被缓冲的前提
      selfHandleResponse: false,
      on: {
        proxyReq: (proxyReq, req, res) => {
          const incoming = req as ProxiedRequest;

          /**
           * ⚠️ 这里的语句顺序是**不能调换**的，曾经踩过：
           *
           * `fixRequestBody` 在重写完请求体后会调用 `proxyReq.end()` —— 那一刻
           * 请求头就已经发出去了。此后任何 `proxyReq.setHeader(...)` 都会抛
           * `ERR_HTTP_HEADERS_SENT`，而这个异常发生在 EventEmitter 回调里、
           * **没有任何地方 catch**，于是它直接打挂整个 Node 进程
           * （症状：只要有一个 POST 请求过来，网关就整个消失，日志里一行
           *   "Cannot set headers after they are sent"）。
           *
           * 所以：所有 setHeader 必须在 fixRequestBody 之前完成。
           * 另外整个回调包了 try/catch —— 这个位置抛异常的成本太高
           * （一条请求换一个进程），宁可降级成「这次转发不带附加头」也不能崩。
           */
          try {
            // ① 把身份信息注入下游请求头。
            //    后端因此不需要自己解析 JWT：它只要信任「这个头是网关加的」。
            //    ⚠️ 这意味着后端**必须只在内网可达**（compose 里不暴露 8000 端口），
            //    否则绕过网关直接调用就能伪造 X-User-Id。
            if (incoming.user) {
              proxyReq.setHeader('X-User-Id', incoming.user.userId);
              // 用户名可能含中文，HTTP 头只允许 ASCII，编码后再传（下游自行 decodeURIComponent）
              proxyReq.setHeader('X-Username', encodeURIComponent(incoming.user.username));
            }
            if (incoming.requestId) proxyReq.setHeader('X-Request-Id', incoming.requestId);

            // ② 不压缩：NDJSON 逐字输出最怕中间多一层 gzip 缓冲（见文件头坑 #2）
            proxyReq.setHeader('Accept-Encoding', 'identity');

            // ③ 最后才重写请求体（这一步会 end 掉 proxyReq，必须在所有 setHeader 之后）
            fixRequestBody(proxyReq, req as Request);
          } catch (e) {
            this.logger.error(
              `注入转发请求头/请求体失败（本次转发继续，但下游可能拿不到身份信息）| path=${incoming.originalUrl} err=${(e as Error).message}`,
            );
          }
        },

        proxyRes: (proxyRes, req) => {
          const incoming = req as ProxiedRequest;
          const elapsed = incoming.proxyStartedAt ? Date.now() - incoming.proxyStartedAt : -1;
          const line = `${incoming.method} ${incoming.originalUrl} -> ${proxyRes.statusCode} ${elapsed}ms`;
          // 4xx/5xx 用 warn 打出来，正常流式响应会有几百个 200，只记 debug 不刷日志
          if ((proxyRes.statusCode ?? 200) >= 400) {
            this.logger.warn(`${line} | requestId=${incoming.requestId ?? '-'}`);
          } else {
            this.logger.debug(line);
          }
        },

        error: (err, req, res) => {
          const incoming = req as ProxiedRequest;
          this.logger.error(
            `代理转发失败 | path=${incoming.originalUrl} target=${this.config.backendUrl} err=${err.message}`,
            err.stack,
          );
          // 已开始发响应（流式吐了一半）时改不了状态码，只能断开
          if (res && 'headersSent' in res && (res as Response).headersSent) {
            (res as Response).end();
            return;
          }
          if (res && 'writeHead' in res) {
            const response = res as Response;
            const timedOut = /timeout/i.test(err.message);
            response.status(timedOut ? 504 : 502).json({
              error: {
                code: timedOut ? 'UPSTREAM_TIMEOUT' : 'UPSTREAM_UNAVAILABLE',
                message: timedOut
                  ? '问答服务响应超时，请稍后重试'
                  : '问答服务暂时不可用，请稍后重试',
                detail: err.message,
                requestId: incoming.requestId ?? '',
                timestamp: new Date().toISOString(),
                path: incoming.originalUrl,
              },
            });
          }
        },
      },
    });

    this.logger.log(`反向代理已就绪 | /api/v1/* -> ${config.backendUrl}（超时 ${config.proxyTimeoutMs}ms）`);
  }

  /**
   * 公开健康检查：白名单放行的两个上游探活路径，免 token 转发。
   *
   * ⚠️ **必须声明在下面那个通配路由之前**。
   * Express 按注册顺序匹配，Nest 按方法声明顺序注册 —— 具体路径先注册才能
   * 抢在 `api/v1/*` 前面命中。顺序反了不会造成安全漏洞（只是回落到通配路由
   * 被守卫拒成 401，fail-closed），但白名单就静默失效了。
   * 冒烟脚本 `smoke-test.sh` 里有断言盯着这一点，别只靠肉眼。
   *
   * 这里复用同一个 `this.proxy`：转发逻辑（流式 pipe、超时、错误体）完全一致，
   * 与通配路由的**唯一差别**就是 `@Public()` 这一个装饰器。
   */
  @Public()
  @All([...PUBLIC_HEALTH_PATHS])
  handlePublicHealth(
    @Req() req: ProxiedRequest,
    @Res() res: Response,
    @Next() next: NextFunction,
  ): void {
    req.proxyStartedAt = Date.now();
    this.proxy(req, res, next);
  }

  /**
   * 通配路由：`/api/v1/` 下的一切请求（含问诊、文档、系统配置，以及
   * 除上面两个健康检查之外的其余接口——它们都需要 token）。
   *
   * 只代理 `/api/v1/*` 而不是 `*`：登录（/api/auth/*）与网关自身健康检查
   * （/api/health）属于网关的职责，不能被转发到后端去。
   */
  @All('api/v1/*')
  handle(@Req() req: ProxiedRequest, @Res() res: Response, @Next() next: NextFunction): void {
    req.proxyStartedAt = Date.now();
    this.proxy(req, res, next);
  }
}
