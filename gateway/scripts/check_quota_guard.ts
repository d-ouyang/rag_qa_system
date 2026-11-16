/**
 * 额度预检判定测试（v4.1.0-p2.1）——跑法：npx -y tsx gateway/scripts/check_quota_guard.ts
 * 口径基准：core/quota_policy.py 的 can_ask / effective_quota / billable / period_bounds。
 * 失败任何断言 → 退出码 1（可挂 CI）。
 */
import assert from 'node:assert';

import { QuotaService } from '../src/quota/quota.service';

function makeService(over: Record<string, string> = {}): QuotaService {
  const config = { get: (key: string, def?: string) => (key in over ? over[key] : (def ?? '')) };
  return new QuotaService(config as never);
}

let failed = 0;
function check(name: string, fn: () => void): void {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`FAIL  ${name}: ${e}`);
  }
}

check('canAsk：额度 ≤0 = 不限，无论已用多少', () => {
  const svc = makeService();
  assert.equal(svc.canAsk(999999, 0).allowed, true);
  assert.equal(svc.canAsk(999999, -5).allowed, true);
});
check('canAsk：已用 < 限量放行；≥ 限量拦截并带文案', () => {
  const svc = makeService();
  assert.equal(svc.canAsk(99, 100).allowed, true);
  const denied = svc.canAsk(100, 100);
  assert.equal(denied.allowed, false);
  assert.ok(denied.message.includes('额度已用完'));
});
check('effectiveQuota：覆盖值 >0 生效；≤0 用全局默认', () => {
  const svc = makeService({ TOKEN_QUOTA_DEFAULT_MONTHLY: '100000' });
  assert.equal(svc.effectiveQuota(500), 500);
  assert.equal(svc.effectiveQuota(0), 100000);
  assert.equal(svc.effectiveQuota(-3), 100000);
});
check('periodBounds：start_day=1 当月整月（左闭右开）', () => {
  const svc = makeService();
  const b = svc.periodBounds(new Date(Date.UTC(2026, 10, 15)));
  assert.equal(b.start, '2026-11-01 00:00:00');
  assert.equal(b.endExclusive, '2026-12-01 00:00:00');
});
check('periodBounds：start_day=21 → start 恒为本月 21 号（上游语义照搬）', () => {
  const svc = makeService({ TOKEN_QUOTA_PERIOD_START_DAY: '21' });
  const b = svc.periodBounds(new Date(Date.UTC(2026, 10, 15))); // 2026-11-15
  assert.equal(b.start, '2026-11-21 00:00:00');
  assert.equal(b.endExclusive, '2026-12-21 00:00:00');
});
check('periodBounds：start_day=21 跨年末（12 月 → 次年 1 月）', () => {
  const svc = makeService({ TOKEN_QUOTA_PERIOD_START_DAY: '21' });
  const b = svc.periodBounds(new Date(Date.UTC(2026, 11, 25))); // 2026-12-25
  assert.equal(b.start, '2026-12-21 00:00:00');
  assert.equal(b.endExclusive, '2027-01-21 00:00:00');
});
check('shouldCheck：notify 档整体短路', () => {
  const svc = makeService({ TOKEN_QUOTA_OVER_ACTION: 'notify' });
  assert.equal(svc.shouldCheck('/api/v1/ask', 'POST'), false);
});
check('shouldCheck：block 档只拦 POST 的 ask 两入口', () => {
  const svc = makeService({ TOKEN_QUOTA_OVER_ACTION: 'block' });
  assert.equal(svc.shouldCheck('/api/v1/qa/ask', 'POST'), true);
  assert.equal(svc.shouldCheck('/api/v1/qa/ask/stream', 'POST'), true);
  assert.equal(svc.shouldCheck('/api/v1/qa/ask', 'GET'), false);
  assert.equal(svc.shouldCheck('/api/v1/documents', 'POST'), false);
});

async function asyncChecks(): Promise<void> {
  // precheck 主链路（usageForUser 打桩——mock 池路径等价）
  const svc = makeService({ TOKEN_QUOTA_OVER_ACTION: 'block' });
  (svc as unknown as { usageForUser: unknown }).usageForUser = async () => ({
    input: 600,
    output: 400,
    quotaOverride: 800, // 生效 800，已用 1000 → 拦
  });
  const denied = await svc.precheck(7);
  assert.equal(denied.allowed, false);
  assert.equal(denied.used, 1000);
  assert.equal(denied.effectiveQuota, 800);
  assert.match(denied.period, /^\d{4}-\d{2}$/);
  console.log('  ok  precheck：已用 ≥ 生效额度 → 429 形状结果');

  (svc as unknown as { usageForUser: unknown }).usageForUser = async () => null;
  const empty = await svc.precheck(7);
  assert.equal(empty.allowed, true);
  assert.equal(empty.used, 0);
  console.log('  ok  precheck：无用量记录 → billable 0 放行');

  (svc as unknown as { usageForUser: unknown }).usageForUser = async () => {
    throw new Error('db down');
  };
  const open = await svc.precheck(7);
  assert.equal(open.allowed, true); // 成本护栏 fail-open（见 service 头注释）
  console.log('  ok  precheck：查库失败 fail-open 放行');
}

async function main(): Promise<void> {
  await asyncChecks();
  console.log(failed === 0 ? '✅ quota guard 判定全部通过' : `❌ ${failed} 处失败`);
  process.exit(failed === 0 ? 0 : 1);
}

void main();
