/**
 * 额度预检服务（v4.1.0-p2.1 / Phase ②1）——quota 判定逻辑外移网关。
 *
 * ---------------------------------------------------------------------------
 * 为什么判定要搬进网关（而非留在 FastAPI）
 * ---------------------------------------------------------------------------
 * RAG 的鉴权已经 fail-closed 在网关侧（JWT + 路径授权都发生在这里），
 * 但额度预检此前在 FastAPI（`api/routes/qa.py::_quota_precheck`）——
 * **唯一收口点必须与鉴权同一个门**：能绕过网关直连后端的人（内网、调试端口、
 * 将来的新服务）不该同时绕过额度。判定搬进来之后：
 *   · 网关：判定（本服务，查库 + can_ask 语义）；
 *   · FastAPI：只留「查余额」（/quota/me，管理端横幅用）与「接 429」。
 *
 * ---------------------------------------------------------------------------
 * 口径镜像（一个字都不能差，差一个就是第二本账）
 * ---------------------------------------------------------------------------
 * 本文件是 `core/quota_repo.py` + `core/quota_policy.py` 的 TS 翻译，逐条对应：
 *   · usage 聚合 = usage_by_user 的 CTE 两段聚合（消息按会话收敛后再 SUM，
 *     cache_read 取会话级整值不 SUM、requests 只数 assistant——本判定用不到
 *     requests/cache_read，但保留列以与原口径逐字对齐）；
 *   · billable = input + output（**不含 cache_read**，刻意，见 quota_policy）；
 *   · effective_quota = 个人覆盖值 > 0 ? 覆盖值 : 全局默认（判据是 ≤0 不是 None）；
 *   · can_ask = 额度 ≤0 不限放行；已用 < 限量放行；否则拦截。
 *     **不做单轮 token 预估**（宽限单语义：提问时没超就放行，超了体现在下一问）。
 * 那边口径一变，这里必须跟着变 —— 改 quota_policy.py 的人要同时改本文件。
 *
 * ---------------------------------------------------------------------------
 * 失败语义：fail-open（与鉴权的 fail-closed 刻意不同）
 * ---------------------------------------------------------------------------
 * 查库抖动/超时 → 放行 + error 日志。理由：quota 是**成本护栏**不是**安全门**，
 * 为一次 DB 抖动把全员问答挡在门外（500/429 风暴）不划算；而鉴权失效的代价是
 * 越权，两者性质不同。这个取舍写在判定路径上，改之前先想清楚。
 */
import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import mysql from 'mysql2/promise';

/** 与 FastAPI 侧 429 错误体形状逐字段一致（前端按 code 特化，不解析文案）。 */
export interface QuotaPrecheckResult {
  allowed: boolean;
  used: number;
  effectiveQuota: number;
  period: string;
  message: string;
}

const BLOCK_MESSAGE = '本月 token 额度已用完，请联系管理员调整。';

@Injectable()
export class QuotaService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(QuotaService.name);
  private pool: mysql.Pool | null = null;

  constructor(private readonly config: ConfigService) {}

  /** block 档开关（TOKEN_QUOTA_OVER_ACTION=block 才拦；notify 档一行不拦）。
   *  用 getter：类字段初始化器早于参数属性赋值，直接初始化会拿到 undefined。 */
  private get overAction(): string {
    return this.config.get<string>('TOKEN_QUOTA_OVER_ACTION', 'notify');
  }
  private get defaultQuota(): number {
    return Number(this.config.get<string>('TOKEN_QUOTA_DEFAULT_MONTHLY', '100000'));
  }
  private get periodStartDay(): number {
    return Number(this.config.get<string>('TOKEN_QUOTA_PERIOD_START_DAY', '1'));
  }

  /** 需要预检的路径（POST 提问入口；非 block 档整体短路）。 */
  shouldCheck(path: string, method: string): boolean {
    if (this.overAction !== 'block') return false;
    const p = (path || '').split('?')[0].replace(/\/+$/, '');
    // ⚠️ 路径是 /api/v1/qa/ask（qa router 前缀 /api/v1/qa）——p3.1 真链路验证
    // 抓出此处曾误写成 office 的 /api/v1/ask 导致判定永不触发。
    return (
      method === 'POST' && (p === '/api/v1/qa/ask' || p === '/api/v1/qa/ask/stream')
    );
  }

  /** 账期左闭右开区间（period_bounds 的 TS 逐字翻译）：start 恒为「本月 startDay」，
   *  end = 下月 startDay（start_day=1 时 = 次月 1 号）。
   *  ⚠️ 上游语义照搬：当月的 startDay 还没到时，区间整体指向未来（查 0 用量 →
   *  放行）——这是 period_bounds 的既有行为，将来改上游时两边一起改。 */
  periodBounds(now = new Date()): { start: string; endExclusive: string } {
    const startDay = this.periodStartDay;
    const y = now.getUTCFullYear();
    const m = now.getUTCMonth() + 1; // 1..12
    const endY = m === 12 ? y + 1 : y;
    const endM = m === 12 ? 1 : m + 1;
    const pad = (n: number) => String(n).padStart(2, '0');
    return {
      start: `${y}-${pad(m)}-${pad(startDay)} 00:00:00`,
      endExclusive: `${endY}-${pad(endM)}-${pad(startDay)} 00:00:00`,
    };
  }

  private periodLabel(): string {
    const [datePart] = this.periodBounds().start.split(' ');
    return datePart.slice(0, 7); // YYYY-MM
  }

  /** can_ask 的 TS 翻译（纯函数）。 */
  canAsk(used: number, quota: number): { allowed: boolean; message: string } {
    if (quota <= 0) return { allowed: true, message: '' }; // 额度 ≤0 = 不限
    if (used < quota) return { allowed: true, message: '' };
    return { allowed: false, message: BLOCK_MESSAGE };
  }

  /** effective_quota 的 TS 翻译：个人覆盖值 > 0 生效，否则全局默认。 */
  effectiveQuota(quotaOverride: number): number {
    return quotaOverride > 0 ? quotaOverride : this.defaultQuota;
  }

  /** usage_by_user（单用户）的 CTE 两段聚合 TS 翻译——口径与 Python 侧逐字对应。 */
  private async usageForUser(
    userId: number,
  ): Promise<{ input: number; output: number; quotaOverride: number } | null> {
    const { start, endExclusive } = this.periodBounds();
    const sql = `
      WITH sess AS (
        SELECT s.id AS sid,
               s.user_id AS user_id,
               COALESCE(SUM(m.usage_input_token), 0)  AS input_tokens,
               COALESCE(SUM(m.usage_output_token), 0) AS output_tokens,
               COALESCE(SUM(m.role = 'assistant'), 0)   AS requests,
               COALESCE(s.usage_cache_read_token, 0)  AS cache_read
        FROM chat_message m
        JOIN session s ON s.id = m.session_id
        WHERE m.create_time >= ? AND m.create_time < ?
        GROUP BY s.id, s.user_id
      )
      SELECT
        u.id AS user_id,
        u.token_quota_monthly AS quota_override,
        COALESCE(SUM(sess.input_tokens), 0)  AS input_tokens,
        COALESCE(SUM(sess.output_tokens), 0) AS output_tokens
      FROM sess
      JOIN \`user\` u ON u.id = sess.user_id
      WHERE sess.user_id = ?
      GROUP BY u.id, u.token_quota_monthly`;
    const [rows] = await this.pool!.query<mysql.RowDataPacket[]>(sql, [
      start,
      endExclusive,
      userId,
    ]);
    if (!rows.length) return null; // 没有用量 → billable 0（effective 仍按 override/default）
    return {
      input: Number(rows[0].input_tokens),
      output: Number(rows[0].output_tokens),
      quotaOverride: Number(rows[0].quota_override),
    };
  }

  async precheck(userId: number): Promise<QuotaPrecheckResult> {
    try {
      const usage = await this.usageForUser(userId);
      const effective = this.effectiveQuota(usage?.quotaOverride ?? 0);
      const used = usage ? usage.input + usage.output : 0; // billable：不含 cache_read
      const { allowed, message } = this.canAsk(used, effective);
      return { allowed, used, effectiveQuota: effective, period: this.periodLabel(), message };
    } catch (err) {
      // fail-open：见文件头「失败语义」。error 级必须可见（与 quota 不吞异常的纪律对齐）。
      this.logger.error(`额度预检查库失败（fail-open 放行）| userId=${userId} err=${err}`);
      return { allowed: true, used: 0, effectiveQuota: 0, period: this.periodLabel(), message: '' };
    }
  }

  private ensurePool(): mysql.Pool {
    if (!this.pool) {
      this.pool = mysql.createPool({
        host: this.config.get<string>('MYSQL_HOST', 'localhost'),
        port: Number(this.config.get<string>('MYSQL_PORT', '3306')),
        user: this.config.get<string>('MYSQL_USER', 'rag'),
        password: this.config.get<string>('MYSQL_PASSWORD', ''),
        database: this.config.get<string>('MYSQL_DATABASE', 'rag_qa'),
        charset: 'utf8mb4',
        connectionLimit: 4,
        enableKeepAlive: true,
      });
    }
    return this.pool;
  }

  onModuleInit(): void {
    this.ensurePool();
  }

  onModuleDestroy(): void {
    void this.pool?.end();
    this.pool = null;
  }
}
