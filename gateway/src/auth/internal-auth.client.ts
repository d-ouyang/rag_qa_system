/**
 * 内部认证客户端 —— 网关调后端 `/api/v1/internal/auth/*`（P2-11c）。
 *
 * ##########################################################################
 * # 这个类的返回值刻意是 `InternalAuthResult | null`：                    #
 * #   非 null = 判定**跑完了**（成或不成都是判定跑完了）                   #
 * #   null     = 判定**没跑成**（后端不可达 / 缺密钥 / 5xx / 解析失败）      #
 * ##########################################################################
 * 把它和「判定为不通过」分开，是本文件唯一重要的事情。
 *
 * 混起来的后果（真实会发生，不是假想）：后端挂了一小时，
 * 所有人都收到「用户名或密码错误」。用户会去问「我密码没改啊」，
 * 排查的人会去查密码策略与 bcrypt cost —— 而真因是网关连不上后端。
 * 这类「错误信息把排查引向反方向」的问题，代价远大于多写一个类型。
 *
 * --------------------------------------------------------------------------
 * ⚠️ 为什么用 `node:http` 而不是全局 `fetch`（这一段是踩出来的，别改回去）
 * --------------------------------------------------------------------------
 * Node 的全局 `fetch`（undici 实现）**会读 `HTTP_PROXY` / `http_proxy`
 * 环境变量**，而本项目对后端的调用是**同机内网**（本机 `127.0.0.1`，
 * compose 网络里的 `backend`）。于是：
 *
 *   · 开发机常年挂着 ClashX / 机场客户端（`export http_proxy=...`）→ 必踩；
 *   · 很多服务器默认也 export 了代理变量 → 生产同样会踩；
 *   · 症状 = `fetch failed` + `cause: ECONNREFUSED`，**没有状态码、没有响应体**，
 *     而「ECONNREFUSED」指的是**代理端口**，排查时极易往「后端没起」上想 ——
 *     其实后端活得好好的（本轮就真的绕了一圈）。
 *
 * 实测确认过（2026-10-07，Node 22.22）：`node:http` 完全不读代理环境变量，
 * 直连目标 host。`NO_PROXY` 也不要用 —— Node 22 的 undici 默认**不尊重**它。
 *
 * 代价：`node:http` 的 API 比 fetch 啰嗦（自己拼 JSON、自己收分块）。
 * 这个代价是值的：它换来「不依赖任何代理环境配置」，也就是本机、测试机、
 * 生产机的行为**完全一致**。要改回 fetch 之前，先在**挂了代理的 shell** 里实测一遍。
 */
import { Inject, Injectable, Logger } from '@nestjs/common';
import * as http from 'node:http';
import { gatewayConfig, GatewayConfig } from '../config/configuration';

/** 对外失败 code。**这是网关自己的口径**，不是后端内部 code（见 `mapFailureCode`）。 */
export type AuthFailureCode =
  | 'INVALID_CREDENTIALS'
  | 'PASSWORD_EXPIRED'
  | 'ACCOUNT_INACTIVE';

/**
 * 知识库写权限的五档 —— **与后端 `core/kb_acl.py` 的 `KB_ROLES` 一一对应**。
 *
 * ⚠️ 两份清单分别在两个语言里，**没有任何东西会校验它们是否还对得上**
 * —— 与 `INBOUND_IDENTITY_HEADERS` 面临完全一样的问题（见那个文件的文件头）。
 * `tests/test_module14_trust_boundary.py` 会读后端 `core/kb_acl.py` 的源码比对，
 * 少一档就红。往两边加档时**两边都要改**。
 *
 * `unknown` 不是一档权限，而是「值不认识」的形状：后端给了
 * `normalize()` 不认识的东西时落到这里，而它的行为是**当只读**。
 * 单独设一个值而不是收窄成 `never`，是为了让「不认识」在类型上可见 ——
 * `switch` 里漏掉它会编译报错（`noFallthroughCasesInSwitch` 之外再加一条断言）。
 */
export const KB_ROLES = ['none', 'ops', 'qa', 'dev', 'superadmin', 'unknown'] as const;
export type KbRole = (typeof KB_ROLES)[number];

/**
 * 把后端给的 `kb_role` 规整成 `KbRole`。
 *
 * **不认识的一律降级成 `unknown`（其行为 = 只读）**，与后端 `kb_acl.normalize()`
 * 的方向一致。两端都fail-closed 是刻意的：**任何一端的解析出意外值，
 * 结果都是「谁也进不去」而不是「谁都能进」**。
 */
export function normalizeKbRole(raw: unknown): KbRole {
  const v = typeof raw === 'string' ? raw.trim().toLowerCase() : '';
  return (KB_ROLES as readonly string[]).includes(v) ? (v as KbRole) : 'unknown';
}

/**
 * 判定**通过**。
 *
 * ⚠️ 刻意做成「通过」与「不通过」两个不同形状（而不是一个 `ok: boolean` + 全字段）——
 * 不通过时 `uid` / `role` / `tokenVersion` / `displayName` **一个都没有意义**。
 * 合成一个形状就得给它们填 `''` / `0` / `null`，而调用方必然会去读其中某个
 * （读到一个 `role: ''` 之后按「不是 admin」还是「空角色」分支，行为完全不同）。
 * 让「不通过」类型上就**没有**这些字段，读它的代码会编译不过 —— 靠类型挡住，
 * 不靠注释提醒。
 */
export interface InternalAuthSuccess {
  ok: true;
  code: string;
  source: 'mysql';
  username: string;
  uid: number;
  role: string;
  /**
   * 知识库写权限（P2-14b）。**五档之一**，由后端归一化后给出。
   *
   * ⚠️ 类型是**字面联合**而不是 `string`：它会一路进 JWT 并被
   * `decideKbWritePath()` 用来做放行判断，写成 `string` 的话
   * 任何拼错的档位都编得过 —— 而那等于「网关误放行」。
   * 取不到时后端给的是 `'none'`（fail-closed）。
   */
  kbRole: KbRole;
  displayName: string;
  employeeNo: string | null;
  tokenVersion: number;
  mustChange: boolean;
  expireInDays: number | null;
}

/** 判定**跑完了但不通过**。 */
export interface InternalAuthFailure {
  ok: false;
  code: AuthFailureCode;
  message: string;
  /** 后端内部的 code（记日志用，不发给客户端） */
  backendCode: string;
  source: 'mysql';
}

export type InternalAuthResult = InternalAuthSuccess | InternalAuthFailure;

/** 判定要多久算异常。bcrypt cost 12 单次 165~210ms（实测 M 系列），8s 是它的 30 倍余量。 */
const TIMEOUT_MS = 8_000;
/** 响应体上限。判定结果最多几百字节；1MB 是「后端返回了别的东西」的兜底。 */
const MAX_BODY_BYTES = 1024 * 1024;

@Injectable()
export class InternalAuthClient {
  private readonly logger = new Logger(InternalAuthClient.name);

  constructor(@Inject(gatewayConfig.KEY) private readonly config: GatewayConfig) {}

  /**
   * 校验凭据。
   *
   * @returns 判定跑完了 → `InternalAuthResult`（`ok` 可能为 false）
   *          判定没跑成 → `null`（调用方**不得**当成「密码错」）
   */
  async verify(
    username: string,
    password: string,
    clientIp?: string,
  ): Promise<InternalAuthResult | null> {
    if (!this.config.internalToken) {
      this.logger.error('未配置 INTERNAL_SHARED_SECRET，无法调用后端判定（登录将不可用）');
      return null;
    }

    let target: URL;
    try {
      target = new URL(
        this.config.internalAuthPath,
        this.config.backendUrl.replace(/\/+$/, ''),
      );
    } catch {
      this.logger.error(
        `内部接口地址不合法 | backendUrl=${this.config.backendUrl} ` +
          `path=${this.config.internalAuthPath}`,
      );
      return null;
    }

    const body = Buffer.from(JSON.stringify({ username, password }), 'utf8');
    const headers: Record<string, string> = {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': String(body.length),
      'X-Internal-Token': this.config.internalToken,
    };
    // 审计里的来源 IP 取 XFF 第一段，所以这里要把原始来源传下去。
    if (clientIp) headers['X-Forwarded-For'] = clientIp;

    let res: http.IncomingMessage;
    let raw: Buffer;
    try {
      ({ res, raw } = await this.request(target, 'POST', headers, body));
    } catch (e) {
      const err = e as NodeJS.ErrnoException;
      this.logger.error(
        `调用后端判定失败 | ${err.code ?? err.message} | ${target.origin}${target.pathname}`,
      );
      return null;
    }

    // 401 = 密钥不对（配置问题）；5xx = 后端炸了。两种都不是「密码错」。
    if (res.statusCode !== 200) {
      this.logger.error(
        `后端判定接口返回异常 | status=${res.statusCode} ` +
          `${res.statusCode === 401 ? '（共享密钥不匹配 —— 检查 INTERNAL_SHARED_SECRET）' : ''}`,
      );
      return null;
    }

    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(raw.toString('utf8')) as Record<string, unknown>;
    } catch {
      this.logger.error(
        `后端判定返回的不是 JSON | 前 120 字节=${raw.toString('utf8').slice(0, 120)}`,
      );
      return null;
    }
    return this.normalize(parsed, username);
  }

  /** 一次 `node:http` 请求。刻意用最朴素的形态 —— 见文件头「为什么不用 fetch」。 */
  private request(
    target: URL,
    method: string,
    headers: Record<string, string>,
    body: Buffer,
  ): Promise<{ res: http.IncomingMessage; raw: Buffer }> {
    return new Promise((resolve, reject) => {
      const req = http.request(
        {
          protocol: target.protocol,
          hostname: target.hostname,
          port: target.port || (target.protocol === 'https:' ? 443 : 80),
          path: `${target.pathname}${target.search}`,
          method,
          headers,
          // 不设 agent：默认 globalAgent 不读代理环境变量（已实测）。
          timeout: TIMEOUT_MS,
        },
        (res) => {
          const chunks: Buffer[] = [];
          let size = 0;
          res.on('data', (c: Buffer) => {
            size += c.length;
            if (size > MAX_BODY_BYTES) {
              req.destroy();
              reject(new Error(`响应体超过 ${MAX_BODY_BYTES} 字节`));
              return;
            }
            chunks.push(c);
          });
          res.on('end', () => resolve({ res, raw: Buffer.concat(chunks) }));
          res.on('error', reject);
        },
      );

      req.on('timeout', () => {
        req.destroy();
        const e = new Error(`超时（${TIMEOUT_MS}ms）`) as NodeJS.ErrnoException;
        e.code = 'ETIMEDOUT';
        reject(e);
      });
      req.on('error', reject);
      req.end(body);
    });
  }

  /**
   * 把后端的 body 规整成 `InternalAuthResult`。
   *
   * ⚠️ **这里做了一次口径翻译（后端 code → 网关 code）**，不是原样透传。
   * 因为后端的 code 是给后端日志看的（`wrong_password` / `not_found` /
   * `locked`），直接透传出去等于把「这个账号存在、密码错了」
   * 明文回给客户端 —— 那就是用户名枚举器。
   *
   * 所以对外只有三种 code：
   *   INVALID_CREDENTIALS —— 密码类的一切（不存在 / 错 / 锁定），前端无从区分；
   *   PASSWORD_EXPIRED     —— 密码**是对的**，只是到期了。前端要把用户导去改密，
   *                          而不是让他重新输一遍（他没输错，凭什么让他重输）。
   *   ACCOUNT_INACTIVE     —— 停用/离职，单独文案（沿用 11b 的取舍）。
   */
  private normalize(
    body: Record<string, unknown>,
    fallbackUsername: string,
  ): InternalAuthResult | null {
    const code = String(body.code ?? '');

    // ⚠️ `ok` 缺失**不是**「失败」，而是「我不知道这是什么」。
    // 直接落进成功分支会拿一个没有 uid 的空壳去签 token —— 那是最糟的结局
    // （放行了一个无法吊销、无法追溯的身份）。所以先判形状。
    if (typeof body.ok !== 'boolean') {
      this.logger.error(
        `后端判定返回体形状不对（缺 ok 字段）| keys=${Object.keys(body).join(',') || '(空)'}`,
      );
      return null;
    }

    if (body.ok === false) {
      const mapped = mapFailureCode(code);
      if (!mapped) {
        // 拿到一个不认识的 code：宁可当成「密码错」也不要把它当成功放行。
        this.logger.warn(`后端返回未登记的失败 code=${code}，按密码错处理`);
      }
      return {
        ok: false,
        code: mapped ?? 'INVALID_CREDENTIALS',
        message:
          mapped === 'ACCOUNT_INACTIVE'
            ? '账号已停用或已离职，请联系管理员'
            : mapped === 'PASSWORD_EXPIRED'
              ? '密码已过期，请修改密码后继续使用'
              : '用户名或密码错误',
        backendCode: code,
        source: 'mysql',
      };
    }

    const user = (body.user ?? {}) as Record<string, unknown>;
    const username = String(user.username ?? fallbackUsername);
    const uid = typeof user.id === 'number' ? user.id : null;
    if (!username || uid === null) {
      // 没有 uid 就没法做 token_version 比对（改密后旧 token 不会失效）。
      // 这种情况**必须**当失败 —— 放行一个无法吊销的身份，是比登不进去严重得多的问题。
      this.logger.error(`后端判定成功但没给 uid | username=${username}，按失败处理`);
      return null;
    }

    return {
      ok: true,
      code,
      source: 'mysql',
      username,
      uid,
      role: String(user.role ?? 'user'),
      kbRole: normalizeKbRole(user.kb_role),
      displayName: String(user.display_name ?? username),
      employeeNo: typeof user.employee_no === 'string' ? user.employee_no : null,
      tokenVersion: typeof user.token_version === 'number' ? user.token_version : 0,
      mustChange: body.must_change === true,
      expireInDays: typeof body.expire_in_days === 'number' ? body.expire_in_days : null,
    };
  }
}

/**
 * 后端内部 code → 对外 code。
 *
 * ⚠️ `not_found` / `wrong_password` / `locked` 三者**必须映射到同一个值**。
 * 分开映射就等于在响应体里附送一个用户名枚举器 —— 前端不显示它，
 * 但它在 network 面板里，谁都能看。
 */
export function mapFailureCode(backendCode: string): AuthFailureCode | null {
  switch (backendCode) {
    case 'not_found':
    case 'wrong_password':
    case 'locked':
      return 'INVALID_CREDENTIALS';
    case 'expired':
      return 'PASSWORD_EXPIRED';
    case 'inactive':
      return 'ACCOUNT_INACTIVE';
    default:
      return null;
  }
}
