/**
 * 请求 ID 中间件 —— 给每个请求打一个可追踪的 id。
 *
 * 为什么必须单开一个中间件而不是在 filter 里随手生成：
 * 「线上报错 → 用户截图 → 你翻日志」这条链路里，唯一能把前端那一次点击
 * 和服务器里那一条错误对上的东西就是这个 id。所以它必须在**请求最开始**
 * 就确定，并原样回写响应头，前端出错时能直接展示给用户（让用户报障时报这个号）。
 *
 * 若上游（Nginx / 前一个网关）已带了 X-Request-Id，沿用而不覆盖 ——
 * 全链路同一个 id 才有意义。
 */
import { Injectable, NestMiddleware } from '@nestjs/common';
import { randomUUID } from 'node:crypto';
import { NextFunction, Request, Response } from 'express';

@Injectable()
export class RequestIdMiddleware implements NestMiddleware {
  use(req: Request, res: Response, next: NextFunction): void {
    const incoming = req.headers['x-request-id'];
    const requestId = typeof incoming === 'string' && incoming.trim() ? incoming.trim() : randomUUID();

    req.headers['x-request-id'] = requestId;
    (req as Request & { requestId?: string }).requestId = requestId;
    res.setHeader('X-Request-Id', requestId);
    next();
  }
}
