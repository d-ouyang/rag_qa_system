# pyright: basic
"""
模块6测试文件：验证会话存储抽象层（core/session_store.py）、Redis 存储实现
（core/redis_store.py）与 Redis 内存预警（core/redis_monitor.py）。

运行：
    .venv/bin/python tests/test_module6_session_store.py     （或 make test）

覆盖点：
1. 存储层语义一致性：MemorySessionStore 与 RedisSessionStore 跑同一套断言
   （两个后端行为必须一致，否则「换后端」就是换了个 bug）
2. Redis 特有行为：TTL 真的落在 Redis 上、索引 ZSET、pipeline 原子写、
   跨线程互斥锁、写入失败（OOM）时的降级
3. MemoryManager 接 Redis 后端：读写/裁剪/截断/元数据/用量/会话列表
4. 内存预警：三级水位分级、fatal 触发只读保护、水位回落自动解除
5. 故障降级：Redis 读失败不抛异常（按「无历史」处理，问答链路不中断）

说明：
    · 用 fakeredis 模拟 Redis（需要 lupa 支持 Lua，锁释放脚本要用）；
      不需要本机真的装 Redis，CI 也能跑；
    · 全程不联网、不调模型。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging
from config.settings import settings

setup_logging()

PASS = 0
FAIL = 0

# 测试不启动后台巡检线程（否则会反复尝试连真实的 localhost:6379 刷日志）
_ORIGINAL_MONITOR_ENABLED = settings.REDIS_MEMORY_MONITOR_ENABLED
settings.REDIS_MEMORY_MONITOR_ENABLED = False

# 测试用的短 TTL，避免要等 6 小时才能验证过期
TEST_TTL = 1


def check(name: str, condition: bool, detail: str = "") -> None:
    """统一的断言输出：通过/失败计数并打印。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def make_fake_redis():
    """构造一个 fakeredis 实例（bytes 模式，与生产客户端配置一致）。"""
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=False)


# --------------------------------------------------------------------------- #
# 第 1 组：存储层语义一致性（两个后端跑同一套断言）
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：存储层语义一致性（memory / redis 双跑） ==")

from core.session_store import MemorySessionStore, SessionSnapshot
from core.redis_store import RedisSessionStore
# 存储契约抽到 tests/store_contract.py —— module8 的 MySQL 后端跑的是**同一个函数**，
# 这样「换后端行为一致」由代码保证，而不是靠两份手写断言凑巧写得像。
from store_contract import exercise_store


memory_store = MemorySessionStore(ttl_seconds=TEST_TTL)
exercise_store(memory_store, "memory", check)

fake_client = make_fake_redis()
redis_store = RedisSessionStore(client=fake_client, ttl_seconds=TEST_TTL)
exercise_store(redis_store, "redis", check)

# TTL 过期：两个后端都应「过期即读不到」（内存版惰性、Redis 版由 TTL 自动清）
memory_store.save("exp", SessionSnapshot(messages=[{"role": "user", "content": "x"}]))
redis_store.save("exp", SessionSnapshot(messages=[{"role": "user", "content": "x"}]))
time.sleep(TEST_TTL + 0.2)
check("[memory] TTL 过期后读不到", memory_store.load("exp") is None)
check("[redis] TTL 过期后读不到（由 Redis 自动回收）", redis_store.load("exp") is None)
check("[memory] purge_expired 能清过期会话", memory_store.purge_expired() >= 0)


# --------------------------------------------------------------------------- #
# 第 2 组：Redis 特有行为
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：Redis 特有行为 ==")

client2 = make_fake_redis()
store2 = RedisSessionStore(client=client2, ttl_seconds=3600)
store2.save("key-check", SessionSnapshot(messages=[{"role": "user", "content": "hello"}]))

# key 设计：会话 key 与索引 key 都带统一前缀，便于运维识别归属
check(
    "会话 key 带统一前缀 rag:session:",
    client2.exists(f"{settings.REDIS_KEY_PREFIX}:session:key-check") == 1,
)
check(
    "索引 ZSET key 存在 rag:sessions",
    client2.exists(f"{settings.REDIS_KEY_PREFIX}:sessions") == 1,
)
check("会话 key 设置了 TTL", client2.ttl(f"{settings.REDIS_KEY_PREFIX}:session:key-check") > 0)

# 一次写要落成一个 HASH 的多个 field（而不是一条大 JSON 字符串）
fields = client2.hgetall(f"{settings.REDIS_KEY_PREFIX}:session:key-check")
field_names = sorted(k.decode() if isinstance(k, bytes) else k for k in fields)
check(
    "会话落成 HASH 的 5 个 field",
    field_names == ["exchange_meta", "last_active", "messages", "session_meta", "usage"],
    f"实际 {field_names}",
)

# 读命中会续期（touch=True），读列表式调用不续期（touch=False）
client2.expire(f"{settings.REDIS_KEY_PREFIX}:session:key-check", 5)
store2.load("key-check")
check("读命中会刷新 TTL（touch=True）", client2.ttl(f"{settings.REDIS_KEY_PREFIX}:session:key-check") > 5)

# 只读式读取：把 TTL 压到很短，touch=False 读取后不应续期
client2.expire(f"{settings.REDIS_KEY_PREFIX}:session:key-check", 5)
before = client2.ttl(f"{settings.REDIS_KEY_PREFIX}:session:key-check")
snap = store2.load("key-check", touch=False)
after = client2.ttl(f"{settings.REDIS_KEY_PREFIX}:session:key-check")
check("touch=False 的读不续期（会话列表不刷 TTL）", snap is not None and after <= before, f"{before}→{after}")

# 索引残留清理：手动往索引里塞一个不存在的成员，list_ids 应该把它摘掉
client2.zadd(f"{settings.REDIS_KEY_PREFIX}:sessions", {"ghost": time.time()})
ids = store2.list_ids()
check("list_ids 过滤索引里的幽灵成员", "ghost" not in ids, f"实际 {ids}")
check(
    "幽灵成员被顺手从索引摘除",
    client2.zscore(f"{settings.REDIS_KEY_PREFIX}:sessions", "ghost") is None,
)

# 会话级锁：5 个线程并发对同一会话「读改写」，锁生效则一条都不该丢
from core.session_store import SessionSnapshot as _Snap

lock_client = make_fake_redis()
lock_store = RedisSessionStore(client=lock_client, ttl_seconds=3600)
lock_store.save("concurrent", _Snap(messages=[]))


def _worker(tag: str) -> None:
    with lock_store.session_lock("concurrent"):
        snapshot = lock_store.load("concurrent")
        snapshot.messages.append({"role": "user", "content": tag})
        # 放大竞争窗口：没有锁的话这里必然互相覆盖
        time.sleep(0.03)
        lock_store.save("concurrent", snapshot)


threads = [threading.Thread(target=_worker, args=(f"q{i}",)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
final = lock_store.load("concurrent")
check(
    "并发写入不丢数据（分布式锁生效）",
    final is not None and len(final.messages) == 5,
    f"实际 {len(final.messages) if final else 0} 条",
)

# 写入失败（OOM）→ 降级不抛异常 + 进入只读保护
import redis as _redis_mod

from core.redis_store import get_runtime_state


class _OomPipeline:
    """模拟 Redis 内存满：任何写操作在 EXEC 时被拒。"""

    def __init__(self, err: Exception) -> None:
        self._err = err

    def __getattr__(self, name: str):
        def _stub(*args, **kwargs):
            return self

        return _stub

    def execute(self):
        raise self._err


class _OomRedis:
    """模拟一个已经写满的 Redis（所有 pipeline 写操作都报 OOM）。"""

    def __init__(self, err: Exception) -> None:
        self._err = err

    def pipeline(self, transaction: bool = True) -> _OomPipeline:
        return _OomPipeline(self._err)

    def hgetall(self, key):
        return {}

    def zcard(self, key):
        return 0


get_runtime_state().unblock_writes()
oom_err = _redis_mod.exceptions.ResponseError(
    "OOM command not allowed when used memory > 'maxmemory'."
)
oom_store = RedisSessionStore(client=_OomRedis(oom_err), ttl_seconds=3600)
raised = False
try:
    oom_store.save("x", SessionSnapshot(messages=[{"role": "user", "content": "y"}]))
except Exception:
    raised = True
check("OOM 写入失败不抛异常（降级，可用性优先）", raised is False)
blocked, reason = get_runtime_state().is_write_blocked()
check("OOM 后自动进入只读保护", blocked is True, f"实际 {reason}")
check("只读保护记录原因", "内存" in reason, f"实际 {reason}")
check("写失败被计数（供健康检查暴露）", get_runtime_state().snapshot()["write_failures"] >= 1)
get_runtime_state().unblock_writes()
check("解除只读保护后恢复可写", get_runtime_state().is_write_blocked()[0] is False)


# --------------------------------------------------------------------------- #
# 第 3 组：MemoryManager 接 Redis 后端
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：MemoryManager 使用 Redis 后端 ==")

from core.memory_manager import MemoryManager

mm_client = make_fake_redis()
mm_store = RedisSessionStore(client=mm_client, ttl_seconds=3600)
mm = MemoryManager(max_turns=3, ttl_seconds=3600, store=mm_store)

mm.add_exchange("s1", "问题A1", "回答A1", meta={"intent": "knowledge_query", "ts": 1.0})
mm.add_exchange("s2", "问题B1", "回答B1")
check("会话隔离：s1 只有自己消息", len(mm.get_messages("s1")) == 2)
check("会话计数正确", mm.session_count() == 2)
check("消息角色交替", [m.type for m in mm.get_messages("s1")] == ["human", "ai"])
check("历史消息是 LangChain Message 对象", mm.get_messages("s1")[0].content == "问题A1")

# 窗口裁剪：max_turns=3 → 最多 6 条消息
for i in range(2, 5):
    mm.add_exchange("s1", f"问题A{i}", f"回答A{i}")
messages = mm.get_messages("s1")
check("窗口裁剪：只保留最近 3 轮（6 条）", len(messages) == 6, f"实际 {len(messages)} 条")
check("裁剪后最早的一轮是 A2", messages[0].content == "问题A2", f"实际 {messages[0].content}")
check("元数据随消息同步裁剪", len(mm.get_exchange_meta("s1")) == 3, f"实际 {len(mm.get_exchange_meta('s1'))}")

# 读不存在的会话不再隐式创建（v2.0.0 修正：避免幽灵会话）
before_count = mm.session_count()
check("读不存在的会话返回空列表", mm.get_messages("ghost-session") == [])
check("读不存在的会话不会创建它", mm.session_count() == before_count)
check("对不存在的会话 update_session_meta 返回 None", mm.update_session_meta("ghost-session") is None)
check("get_session_meta 无记录返回默认值", mm.get_session_meta("ghost-session") == {"pinned": False})

# 元数据 / 截断 / 用量
meta = mm.update_session_meta("s1", title="报销流程", pinned=True)
check("更新元数据返回最新值", meta is not None and meta["pinned"] is True and meta["title"] == "报销流程")
check("元数据持久化能读回", mm.get_session_meta("s1")["title"] == "报销流程")

mm.truncate_session("s1", 2)
check("截断到 2 条消息", len(mm.get_messages("s1")) == 2, f"实际 {len(mm.get_messages('s1'))}")
check("截断同步裁元数据", len(mm.get_exchange_meta("s1")) == 1)
check("截断不存在的会话返回 False", mm.truncate_session("ghost-session", 0) is False)

mm.add_usage("s1", input_tokens=10, output_tokens=20, cache_read_tokens=5)
mm.add_usage("s1", input_tokens=1, output_tokens=2)
usage = mm.get_usage("s1")
check("token 用量累加正确", usage["input_tokens"] == 11 and usage["requests"] == 2, f"实际 {usage}")
check("用量读到的是副本（外部改动不写回）", (usage.update({"requests": 99}) or mm.get_usage("s1")["requests"] == 2))

# 会话列表：不因「翻列表」而给全部会话续命
listed = mm.list_sessions()
check("list_sessions 返回全部会话", len(listed) == 2, f"实际 {len(listed)}")
check("list_sessions 带置顶与标题", any(i["pinned"] for i in listed))
check("会话列表按活跃倒序", listed[0]["session_id"] in ("s1", "s2"))

# 清空会话
check("清空存在的会话返回 True", mm.clear_session("s2") is True)
check("清空不存在的会话返回 False", mm.clear_session("s2") is False)
check("清空后 Redis key 已删除", mm_client.exists(f"{settings.REDIS_KEY_PREFIX}:session:s2") == 0)
check("清空后索引里也没有它", mm_client.zscore(f"{settings.REDIS_KEY_PREFIX}:sessions", "s2") is None)

# 读失败（Redis 断开）→ 按无历史处理，不抛异常
class _BrokenRedis:
    def hgetall(self, key):
        raise _redis_mod.exceptions.ConnectionError("connection refused")

    def pipeline(self, transaction: bool = True):
        raise _redis_mod.exceptions.ConnectionError("connection refused")


broken_store = RedisSessionStore(client=_BrokenRedis(), ttl_seconds=3600)
broken_mm = MemoryManager(max_turns=3, ttl_seconds=3600, store=broken_store)
crashed = False
try:
    check("Redis 断开时读历史返回空（不炸问答）", broken_mm.get_messages("any") == [])
    check("Redis 断开时 list_sessions 返回空", broken_mm.list_sessions() == [])
except Exception as e:  # pragma: no cover
    crashed = True
    print(f"    异常：{type(e).__name__}: {e}")
check("Redis 断开时记忆层不向上抛异常", crashed is False)
check("Redis 断开时 health 如实报 not ok", broken_store.health().get("ok") is False)

# 内存预警报告接口
report = mm.memory_report()
check("memory_report 报出后端为 redis", report["backend"] == "redis")
check("memory_report 含 store 统计", report["store"]["session_count"] == 1)
check("memory_report 含 Redis 内存段", "redis_memory" in report)


# --------------------------------------------------------------------------- #
# 第 3B 组：轮元数据与轮严格对齐（回归：_normalize_meta 的调用位置）
# --------------------------------------------------------------------------- #
print("\n== 第 3B 组：轮元数据对齐（回归） ==")

from core.session_store import MemorySessionStore  # noqa: E402


def _run_meta_alignment(store, label: str) -> None:
    """
    同一套断言在两个后端上各跑一遍 —— 换后端不该换出不同的 bug。

    为什么这组断言值得单独写：这个错位从 p0.1 就潜伏着（当时只在文档里记了一笔），
    三个子版本的模块测试全绿，直到真实会话刷新页面才现形。原因是
    **单轮断言看不出错位** —— 必须「连写 3 轮，且中间故意有一轮不带 meta」才能暴露。
    所以这里每一步都写死轮号，不许用「长度对不对」这种能蒙混过关的断言。
    """
    m = MemoryManager(max_turns=10, ttl_seconds=3600, store=store)
    sid = f"align-{label}"
    m.add_exchange(sid, "q1", "a1", meta={"intent": "i1", "sources": ["c1"]})
    m.add_exchange(sid, "q2", "a2")                 # 中间轮故意不带 meta
    m.add_exchange(sid, "q3", "a3", meta={"intent": "i3", "sources": ["c3"]})

    metas = m.get_exchange_meta(sid)
    msgs = m.get_messages(sid)
    check(f"[{label}] 消息条数 == 6（3 轮）", len(msgs) == 6, f"实际 {len(msgs)} 条")
    check(f"[{label}] 元数据条数 == 轮数", len(metas) == 3, f"实际 {len(metas)} 条：{metas}")
    check(
        f"[{label}] 第 1 轮元数据还在（不被后面的轮顶掉）",
        len(metas) > 0 and metas[0].get("intent") == "i1",
        f"实际 {metas[:1]}",
    )
    check(
        f"[{label}] 中间无 meta 的轮是空占位，不是错位",
        len(metas) > 1 and metas[1] == {},
        f"实际 {metas[1:2]}",
    )
    check(
        f"[{label}] 第 3 轮元数据落在正确下标",
        len(metas) > 2 and metas[2].get("intent") == "i3",
        f"实际 {metas[2:3]}",
    )

    # 直接照抄历史接口的回填方式（api/routes/qa.py：`turn = 消息下标 // 2`）
    # —— 复用线上同一段索引逻辑，避免「测试对了、线上还是错的」
    backfilled = [metas[i // 2] if i // 2 < len(metas) else {} for i in range(len(msgs))]
    hits = [len(b.get("sources") or []) for b in backfilled]
    check(
        f"[{label}] 按 轮号=下标//2 回填后第 1 轮拿到自己的引用",
        (backfilled[1].get("sources") or []) == ["c1"],
        f"实际 {backfilled[1]}",
    )
    check(
        f"[{label}] 回填后第 3 轮拿到自己的引用",
        (backfilled[5].get("sources") or []) == ["c3"],
        f"实际 {backfilled[5]}",
    )
    check(
        f"[{label}] 引用只落在它该在的轮上（中间轮为空占位）",
        hits == [1, 1, 0, 0, 1, 1],
        f"实际 {hits}",
    )

    # 连续更多轮也不该漂移：再写两轮，前 3 轮的元数据必须原地不动
    m.add_exchange(sid, "q4", "a4", meta={"intent": "i4"})
    m.add_exchange(sid, "q5", "a5", meta={"intent": "i5"})
    metas5 = m.get_exchange_meta(sid)
    check(
        f"[{label}] 续写 2 轮后前 3 轮元数据原地不动",
        [d.get("intent") for d in metas5[:3]] == ["i1", None, "i3"],
        f"实际 {metas5}",
    )
    check(
        f"[{label}] 续写后仍严格等长",
        len(metas5) == len(m.get_messages(sid)) // 2 == 5,
        f"实际元数据 {len(metas5)} 条",
    )


_run_meta_alignment(MemorySessionStore(ttl_seconds=3600), "memory")
_run_meta_alignment(RedisSessionStore(client=make_fake_redis(), ttl_seconds=3600), "redis")


# --------------------------------------------------------------------------- #
# 第 4 组：Redis 内存预警分级
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：Redis 内存预警 ==")

from core.redis_monitor import RedisMemoryMonitor, human_bytes

check("human_bytes 格式化 MB", human_bytes(1024 * 1024) == "1.00MB", f"实际 {human_bytes(1024 * 1024)}")
check("human_bytes 处理 None", human_bytes(None) == "未知")


class _FakeInfoRedis:
    """只服务于内存预警的假客户端：可控地返回 INFO memory。"""

    def __init__(self, used: int, maxmemory: int, policy: str = "noeviction") -> None:
        self._used = used
        self._maxmemory = maxmemory
        self._policy = policy

    def info(self, section: str = "default"):
        return {
            "used_memory": self._used,
            "used_memory_peak": self._used,
            "used_memory_rss": self._used,
            "maxmemory": self._maxmemory,
            "maxmemory_policy": self._policy,
            "mem_fragmentation_ratio": "1.05",
        }

    def zcard(self, key: str) -> int:
        return 7


MB = 1024 * 1024
_orig_thresholds = (
    settings.REDIS_MEMORY_WARN_RATIO,
    settings.REDIS_MEMORY_CRITICAL_RATIO,
    settings.REDIS_MEMORY_FATAL_RATIO,
    settings.REDIS_MEMORY_ASSUMED_MAX_MB,
    settings.REDIS_MEMORY_FATAL_READONLY,
)

try:
    settings.REDIS_MEMORY_WARN_RATIO = 0.70
    settings.REDIS_MEMORY_CRITICAL_RATIO = 0.85
    settings.REDIS_MEMORY_FATAL_RATIO = 0.95
    settings.REDIS_MEMORY_ASSUMED_MAX_MB = 128
    settings.REDIS_MEMORY_FATAL_READONLY = True

    # 各级水位：128MB 上限下的 50 / 75 / 90 / 97 MB
    cases = [
        (64 * MB, "ok"),
        (96 * MB, "warn"),
        (115 * MB, "critical"),
        (125 * MB, "fatal"),
    ]
    for used, expected in cases:
        monitor = RedisMemoryMonitor(client=_FakeInfoRedis(used, 128 * MB))
        status = monitor.inspect()
        check(
            f"水位分级：{used // MB}MB/128MB -> {expected}",
            status["level"] == expected,
            f"实际 {status['level']}（ratio={status['used_ratio']}）",
        )

    status = RedisMemoryMonitor(client=_FakeInfoRedis(64 * MB, 128 * MB)).inspect()
    check("水位计算正确（64/128=50%）", abs(status["used_ratio"] - 0.5) < 0.001, f"实际 {status['used_ratio']}")
    check("水位基准标注为 maxmemory", status["ratio_basis"] == "maxmemory")
    check("报告本应用会话数", status["app_session_count"] == 7)
    check("报告 maxmemory_policy（便于确认驱逐策略）", status["maxmemory_policy"] == "noeviction")
    check("水位正常时给出无需处理的建议", any("无需处理" in a for a in status["advice"]))

    # 未设 maxmemory → 用假定容量折算，并如实标注 basis=assumed
    assumed = RedisMemoryMonitor(client=_FakeInfoRedis(100 * MB, 0)).inspect()
    check("未设 maxmemory 时用假定容量折算", assumed["ratio_basis"] == "assumed")
    check("未设 maxmemory 时上限标注为未设置", "未设置" in assumed["maxmemory_human"])

    # fatal → 自动只读保护；回落 → 自动解除
    get_runtime_state().unblock_writes()
    fatal_monitor = RedisMemoryMonitor(client=_FakeInfoRedis(125 * MB, 128 * MB))
    fatal_status = fatal_monitor.check_and_act()
    check("fatal 水位触发只读保护", get_runtime_state().is_write_blocked()[0] is True)
    check("fatal 状态写回运行态（供接口展示）", get_runtime_state().get_memory_status() is not None)

    recovered_monitor = RedisMemoryMonitor(client=_FakeInfoRedis(64 * MB, 128 * MB))
    recovered_monitor.check_and_act()
    check("水位回落自动解除只读保护", get_runtime_state().is_write_blocked()[0] is False)
    check("回落分级为 ok", recovered_monitor.inspect()["level"] == "ok")

    # critical 会触发清理回调（回收索引残留）
    calls = []
    crit_monitor = RedisMemoryMonitor(client=_FakeInfoRedis(115 * MB, 128 * MB))
    crit_monitor.set_cleanup_hook(lambda: (calls.append(1), 3)[1])
    crit_status = crit_monitor.check_and_act()
    check("critical 水位触发清理动作", calls == [1])
    check("清理数量写回巡检结果", crit_status.get("purged_index_entries") == 3)

    # 关掉只读保护开关：fatal 也只告警，不拦写入
    settings.REDIS_MEMORY_FATAL_READONLY = False
    get_runtime_state().unblock_writes()
    RedisMemoryMonitor(client=_FakeInfoRedis(125 * MB, 128 * MB)).check_and_act()
    check("关闭只读开关后 fatal 不拦写入", get_runtime_state().is_write_blocked()[0] is False)

    # Redis 不可达 → level=unknown，且不抛异常
    class _DeadRedis:
        def info(self, section: str = "default"):
            raise _redis_mod.exceptions.ConnectionError("boom")

    dead = RedisMemoryMonitor(client=_DeadRedis()).inspect()
    check("Redis 不可达时分级为 unknown", dead["level"] == "unknown")
    check("unknown 状态带原因说明", "无法读取" in dead.get("reason", ""))
finally:
    (
        settings.REDIS_MEMORY_WARN_RATIO,
        settings.REDIS_MEMORY_CRITICAL_RATIO,
        settings.REDIS_MEMORY_FATAL_RATIO,
        settings.REDIS_MEMORY_ASSUMED_MAX_MB,
        settings.REDIS_MEMORY_FATAL_READONLY,
    ) = _orig_thresholds
    get_runtime_state().unblock_writes()
    settings.REDIS_MEMORY_MONITOR_ENABLED = _ORIGINAL_MONITOR_ENABLED


# --------------------------------------------------------------------------- #
print("\n==================================================")
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
