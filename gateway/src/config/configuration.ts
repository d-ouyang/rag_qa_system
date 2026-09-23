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
  };
}

export const gatewayConfig = registerAs('gateway', loadGatewayConfig);
