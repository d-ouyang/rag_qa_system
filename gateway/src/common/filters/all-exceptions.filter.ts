/**
 * 全局异常过滤器 —— 把所有错误收敛成**同一个 JSON 形状**。
 *
 * 统一后的响应体：
 *   {
 *     "error": {
 *       "code": "TOKEN_EXPIRED",        // 机器可判定的稳定标识（前端按它分支）
 *       "message": "登录已过期，请重新登录",  // 给人看的中文提示（可直接弹 toast）
 *       "detail": "...",                 // 可选：原始技术细节（开发期排障用）
 *       "requestId": "uuid",             // 与日志、响应头 X-Request-Id 一致
 *       "timestamp": "2026-09-23T...",
 *       "path": "/api/v1/qa/ask"
 *     }
 *   }
 *
 * 为什么必须统一（而不是让各处抛什么返回什么）：
 * 前端只写一套错误处理代码 —— 拿到 401 就跳登录、拿到 429 就提示「操作太频繁」、
 * 其他就展示 message。不统一的话每个接口的错误体都可能不一样，
 * 前端只能靠 HTTP 状态码猜，最后就是一堆 `if (status === 401)` 散落在各处。
 *
 * 注意边界：**只处理网关自己产生的错误**。后端 FastAPI 已经写好响应体的
 * （如 4xx/5xx 的 `{"detail": "..."}`）由代理原样透传，不在这里二次包装 ——
 * 因为要改写它们就得缓冲整个响应体，会把 NDJSON 流式问答一起搞坏。
 * 前端因此要兼容两种错误体，parseError 就是这么写的。
 */
import {
  ArgumentsHost,
  Catch,
  ExceptionFilter,
  HttpException,
  HttpStatus,
  Logger,
} from '@nestjs/common';
import { Request, Response } from 'express';

interface NormalizedError {
  code: string;
  message: string;
  detail?: unknown;
}

/** HTTP 状态码 → 默认错误码与文案（抛错方没给 code 时的兜底）。 */
const DEFAULT_ERRORS: Record<number, NormalizedError> = {
  [HttpStatus.BAD_REQUEST]: { code: 'BAD_REQUEST', message: '请求参数有误' },
  [HttpStatus.UNAUTHORIZED]: { code: 'UNAUTHORIZED', message: '登录已过期或未登录，请重新登录' },
  [HttpStatus.FORBIDDEN]: { code: 'FORBIDDEN', message: '没有访问权限' },
  [HttpStatus.NOT_FOUND]: { code: 'NOT_FOUND', message: '请求的资源不存在' },
  [HttpStatus.PAYLOAD_TOO_LARGE]: { code: 'PAYLOAD_TOO_LARGE', message: '请求体过大' },
  [HttpStatus.TOO_MANY_REQUESTS]: { code: 'TOO_MANY_REQUESTS', message: '操作太频繁，请稍后再试' },
  [HttpStatus.BAD_GATEWAY]: { code: 'UPSTREAM_UNAVAILABLE', message: '问答服务暂时不可用，请稍后重试' },
  [HttpStatus.SERVICE_UNAVAILABLE]: { code: 'SERVICE_UNAVAILABLE', message: '服务暂时不可用，请稍后重试' },
  [HttpStatus.GATEWAY_TIMEOUT]: { code: 'UPSTREAM_TIMEOUT', message: '问答服务响应超时，请稍后重试' },
  [HttpStatus.INTERNAL_SERVER_ERROR]: { code: 'INTERNAL_ERROR', message: '服务内部错误' },
};

@Catch()
export class AllExceptionsFilter implements ExceptionFilter {
  private readonly logger = new Logger('ExceptionFilter');

  catch(exception: unknown, host: ArgumentsHost): void {
    const ctx = host.switchToHttp();
    const res = ctx.getResponse<Response>();
    const req = ctx.getRequest<Request & { requestId?: string; user?: { username?: string } }>();

    // 响应已经开始（比如流式问答吐了一半才出错）：此时改状态码/写 JSON 都会
    // 直接抛 ERR_HTTP_HEADERS_SENT，只能记录日志后断开连接。
    if (res.headersSent) {
      this.logger.error(
        `响应已开始发送后才出错，只能中断连接 | path=${req.originalUrl} err=${describe(exception)}`,
      );
      res.end();
      return;
    }

    const status = exception instanceof HttpException ? exception.getStatus() : HttpStatus.INTERNAL_SERVER_ERROR;
    const normalized = this.normalize(exception, status);
    const requestId = res.getHeader('X-Request-Id')?.toString() ?? req.requestId ?? '';

    if (status >= 500) {
      this.logger.error(
        `${req.method} ${req.originalUrl} -> ${status} ${normalized.code} | requestId=${requestId} user=${req.user?.username ?? '-'} err=${describe(exception)}`,
        exception instanceof Error ? exception.stack : undefined,
      );
    } else if (status !== HttpStatus.UNAUTHORIZED) {
      // 401 不打日志：扫描器/未登录用户会把它刷爆，而它本身不是异常信号
      this.logger.warn(
        `${req.method} ${req.originalUrl} -> ${status} ${normalized.code} | requestId=${requestId}`,
      );
    }

    res.status(status).json({
      error: {
        code: normalized.code,
        message: normalized.message,
        ...(normalized.detail !== undefined ? { detail: normalized.detail } : {}),
        requestId,
        timestamp: new Date().toISOString(),
        path: req.originalUrl,
      },
    });
  }

  /**
   * 把任意异常归一化成 {code, message, detail}。
   *
   * 三种输入形态都要照顾到：
   *   1. 我们主动抛的对象形异常（UnauthorizedException({code, message})）→ 直接用；
   *   2. ValidationPipe 的 400（message 是字符串数组）→ 拼接成一句人话；
   *   3. 完全没预期的异常（TypeError 等）→ 500 + 固定文案。
   *      注意生产环境**不把原始 message 透出**：异常信息经常带内部路径、
   *      变量值甚至连接串，属于信息泄露。开发环境则保留以便排查。
   */
  private normalize(exception: unknown, status: number): NormalizedError {
    const fallback = DEFAULT_ERRORS[status] ?? {
      code: `HTTP_${status}`,
      message: '请求处理失败',
    };

    if (exception instanceof HttpException) {
      const response = exception.getResponse();
      if (typeof response === 'string') {
        return { ...fallback, message: this.humanize(response, fallback.message) };
      }
      if (response && typeof response === 'object') {
        const body = response as Record<string, unknown>;
        const rawMessage = body.message;
        // 校验类异常（ValidationPipe / 限流）的 message 是数组：
        // 全部拼起来会把 toast 撑成三行（用户只需要知道怎么改），
        // 所以这里**只取第一条做提示**，剩下的挪进 detail 供排查。
        if (Array.isArray(rawMessage)) {
          const items = rawMessage.map((m) => String(m));
          return {
            code: typeof body.code === 'string' ? body.code : fallback.code,
            message: this.pickBestMessage(items, fallback.message),
            detail: items.length > 1 ? items.slice(1) : body.detail,
          };
        }
        const message =
          typeof rawMessage === 'string' ? this.humanize(rawMessage, fallback.message) : fallback.message;
        return {
          code: typeof body.code === 'string' ? body.code : fallback.code,
          message,
          detail: body.detail,
        };
      }
      return fallback;
    }

    const isProduction = (process.env.NODE_ENV ?? 'development') === 'production';
    return {
      ...fallback,
      detail: isProduction ? undefined : describe(exception),
    };
  }

  /**
   * 把 Nest 内置异常的英文原文换成给用户看的中文文案。
   *
   * 典型来源：`ThrottlerException` 的 message 就是 "ThrottlerException: Too Many Requests"，
   * 直接透出去用户会看到一行英文类名 —— 既看不懂，也暴露了技术栈。
   * 判据是「形如 XxxException: ...」这样的异常类名前缀，命中就用兜底中文。
   */
  private humanize(message: string, fallback: string): string {
    if (/^[A-Za-z]*Exception\b/.test(message.trim())) return fallback;
    return message;
  }

  /**
   * 从一个字段的多条校验错误里挑一条给用户看。
   *
   * 为什么不能简单取第一条：字段缺失（比如整个 password 没传）时，
   * class-validator 会把「类型不对」「长度超限」全都报一遍，而它们的顺序
   * 取决于装饰器 metadata 的遍历顺序 —— 实际取到的是「密码不能超过 128 个字符」，
   * 对「没填密码」的人来说这是误导。
   * 按「可操作性」排序：先告诉他字段必填，再谈格式与长度。
   */
  private pickBestMessage(items: string[], fallback: string): string {
    const required = items.find((m) => m.includes('不能为空'));
    return required ?? this.humanize(items[0] ?? '', fallback);
  }
}

function describe(exception: unknown): string {
  if (exception instanceof Error) return `${exception.name}: ${exception.message}`;
  return String(exception);
}
