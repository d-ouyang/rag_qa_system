/**
 * 网关配置加载 —— 全部配置只从环境变量读，且关键项「生产缺失即启动失败」。
 *
 * 为什么要把「缺配置」从「运行时才炸」提前到「启动就炸」：
 * 网关是唯一入口，如果 JWT 密钥缺失却还能起来，那它就是用空密钥在签发 token，
 * 任何人都能自己伪造一个。这种「能启动但完全没防护」的状态比启动失败危险得多。
 */
import { registerAs } from '@nestjs/config';
import { Logger } from '@nestjs/common';
import { randomBytes } from 'node:crypto';

const logger = new Logger('GatewayConfig');

/** 开发环境兜底账号（密码 admin123），生产环境绝不会走到这里。 */
const DEV_DEFAULT_USERS =
  'admin:$2a$10$V7V4pJ6xSM4eTfNMXHYO9OiRtN3kUWKGijeEMJWJadsp/Ixt9N/UC';

/** 用于「用户名不存在时也跑一次 bcrypt 比对」的哑 hash，抹平时间差。 */
export const DUMMY_BCRYPT_HASH = '$2a$10$LBDmYKCh81Q5nfkaBKnAtuoaDzaPCdopG9fcAWsCaNJhkwohblNva';

export interface GatewayUserTable {
  /** username -> bcrypt hash */
  readonly map: Map<string, string>;
  /** 配置来源描述（日志与 /api/health 展示，不含任何口令） */
  readonly source: string;
}

export interface GatewayConfig {
  port: number;
  jwtSecret: string;
  jwtExpiresIn: string;
  users: GatewayUserTable;
  backendUrl: string;
  allowedOrigins: string[];
  throttleTtlMs: number;
  throttleLimit: number;
  proxyTimeoutMs: number;
  isProduction: boolean;
  /**
   * P2-11c：登录判定的真相源是 MySQL，网关**不再自己判密码**。
   * 这三个是网关调后端内部接口要用的东西。
   */
  internalAuthPath: string;
  internalToken: string;
  /** 内部接口的判定结果拿不到时，网关是否回落到 `.env` 的 GATEWAY_USERS。 */
  internalAuthFallbackAllowed: boolean;
}

function list(value: string | undefined, fallback: string[]): string[] {
  if (!value) return fallback;
  return value
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean);
}

/**
 * 解析 GATEWAY_USERS。
 *
 * 格式：`user1:$2b$10$....,user2:$2b$10$....`
 * **只接受 bcrypt hash，不接受明文**：环境变量会被 docker inspect、进程列表、
 * 监控采集看到，明文口令放在这里等于公开。用 `npm run hash -- <密码>` 生成。
 */
export function parseUsers(raw: string | undefined, isProduction: boolean): GatewayUserTable {
  const source = raw ? 'env:GATEWAY_USERS' : isProduction ? 'missing' : 'dev-default';
  if (!raw) {
    if (isProduction) {
      throw new Error(
        '生产环境必须配置 GATEWAY_USERS（格式：用户名:bcrypt哈希，多个用逗号分隔）。' +
          '生成哈希：cd gateway && npm run hash -- 你的密码',
      );
    }
    logger.warn(
      '未配置 GATEWAY_USERS，使用开发默认账号 admin / admin123（仅限本地调试，生产会直接启动失败）',
    );
    return { map: parseUserEntries(DEV_DEFAULT_USERS), source };
  }

  const map = parseUserEntries(raw);
  if (map.size === 0) {
    throw new Error('GATEWAY_USERS 解析后为空，请检查格式：用户名:bcrypt哈希[,用户名:哈希]');
  }
  return { map, source };
}

function parseUserEntries(raw: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const entry of raw.split(',')) {
    const trimmed = entry.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf(':');
    if (idx <= 0) {
      throw new Error(`GATEWAY_USERS 条目格式错误（应形如 admin:$2b$10$...）：${trimmed.slice(0, 12)}...`);
    }
    const username = trimmed.slice(0, idx).trim();
    const secret = trimmed.slice(idx + 1).trim();
    // bcrypt 的三种前缀都接受（$2a$/$2b$/$2y$）。明文直接报错，不给「先用着」的机会。
    if (!/^\$2[aby]\$/.test(secret)) {
      throw new Error(
        `GATEWAY_USERS 里 ${username} 的凭据不是 bcrypt 哈希（不允许明文口令）。` +
          '生成哈希：cd gateway && npm run hash -- 你的密码',
      );
    }
    map.set(username, secret);
  }
  return map;
}

export function loadGatewayConfig(): GatewayConfig {
  const isProduction = (process.env.NODE_ENV ?? 'development') === 'production';

  // JWT 密钥：生产必须显式配置；开发缺失时随机生成（重启即失效，会踢掉登录态，
  // 这个副作用本身就是提醒：赶紧去配一个固定密钥）。
  let jwtSecret = process.env.GATEWAY_JWT_SECRET ?? '';
  if (!jwtSecret) {
    if (isProduction) {
      throw new Error('生产环境必须配置 GATEWAY_JWT_SECRET（建议 openssl rand -hex 32 生成）');
    }
    jwtSecret = randomBytes(32).toString('hex');
    logger.warn('未配置 GATEWAY_JWT_SECRET，已生成临时随机密钥（本进程重启后所有登录态失效）');
  }

  return {
    port: Number(process.env.GATEWAY_PORT ?? 3000),
    jwtSecret,
    jwtExpiresIn: process.env.GATEWAY_JWT_EXPIRES_IN ?? '12h',
    users: parseUsers(process.env.GATEWAY_USERS, isProduction),
    // 后端 FastAPI 地址：容器里是 http://backend:8000，本机开发是 http://127.0.0.1:8000。
    // 用 127.0.0.1 而不是 localhost：Node 18+ 对 localhost 会优先解析成 IPv6 ::1，
    // 而后端若只监听 IPv4 就会连不上（这个坑在 Node 里非常常见）。
    backendUrl: process.env.GATEWAY_BACKEND_URL ?? 'http://127.0.0.1:8000',
    allowedOrigins: list(process.env.GATEWAY_ALLOWED_ORIGINS, [
      'http://localhost:5173',
      'http://127.0.0.1:5173',
    ]),
    throttleTtlMs: Number(process.env.GATEWAY_THROTTLE_TTL_MS ?? 60_000),
    throttleLimit: Number(process.env.GATEWAY_THROTTLE_LIMIT ?? 120),
    proxyTimeoutMs: Number(process.env.GATEWAY_PROXY_TIMEOUT_MS ?? 180_000),
    isProduction,
    // ---- P2-11c：登录判定走后端内部接口 ----
    // 判定规则（防枚举文案 / 耗时对齐 / 到期边界 / 锁定 / 强制改密）在
    // Python 侧只有一份，带 141 条断言。让网关再写一份 TypeScript 等于
    // 把这三条各复制到一个没有测试守着的地方，半年后必然静默漂移。
    // 代价是登录多一次内网 HTTP（实测 < 5ms），认下来。
    internalAuthPath: process.env.GATEWAY_INTERNAL_AUTH_PATH ?? '/api/v1/internal/auth/login',
    // 与后端的 INTERNAL_SHARED_SECRET 必须是同一个值。
    // 缺失时**不做兜底默认值**（见 loadInternalToken 的注释）。
    internalToken: loadInternalToken(isProduction),
    // 内部接口不可用时，是否允许回落到 .env 的 GATEWAY_USERS。
    //
    // ⚠️ 默认 false，生产也建议 false。回落看着「提高可用性」，实际是开了一条
    // 绕过 MySQL 的登录通道 —— 万一内部接口配错，回落会让**所有**账号都改用
    // .env 里那份陈旧数据判定，而且没有任何日志提示「我正走在回落路径上」。
    // 真正需要它的是 D9 的 break-glass 超管，那一条走 `users` 表而不是这里。
    internalAuthFallbackAllowed: process.env.GATEWAY_INTERNAL_FALLBACK === 'true',
  };
}

/**
 * 读内部共享密钥。
 *
 * 生产缺失直接抛错（fail-closed）：网关拿不到它就**没法做登录判定**，
 * 而「用空密钥去调内部接口」只会被后端 401 —— 症状是「登录全挂」，
 * 原因却是「这里少配了一个环境变量」，排查要跨两个进程。
 * 启动就失败能让这件事在第一眼解决。
 *
 * 开发环境允许为空：此时网关仍然启动，但 `internalAuthFallbackAllowed`
 * 默认 false，于是登录会明确报「认证服务不可用」而不是默默用陈旧数据。
 */
function loadInternalToken(isProduction: boolean): string {
  const token = process.env.INTERNAL_SHARED_SECRET ?? '';
  if (!token && isProduction) {
    throw new Error(
      '生产环境必须配置 INTERNAL_SHARED_SECRET（与后端同一个值）。' +
        '生成：python -c "import secrets; print(secrets.token_urlsafe(48))"',
    );
  }
  if (!token) {
    logger.warn(
      '未配置 INTERNAL_SHARED_SECRET：登录判定将不可用（这是开发环境的预期行为，' +
        '内部接口需要它）。如需回落请显式设 GATEWAY_INTERNAL_FALLBACK=true。',
    );
  }
  return token;
}

export const gatewayConfig = registerAs('gateway', loadGatewayConfig);
