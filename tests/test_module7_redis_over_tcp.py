# pyright: basic
"""
模块7测试文件：Redis 会话存储的「准真实」联调 —— 走真 socket，而不是进程内假对象。

---------------------------------------------------------------------------
为什么已经有 module6 了还要单独一个模块
---------------------------------------------------------------------------
module6 用 `fakeredis.FakeRedis` 在**本进程内**模拟 Redis，验的是「语义对不对」，
那块它做得很扎实。但它有一个结构性盲区：

    数据活在测试进程的内存里 → **永远验不出「进程重启后数据还在不在」**。

而这恰恰是 P0-1 存在的全部理由（把 MemoryManager 从内存 dict 换成 Redis）。
「语义一致」和「重启不丢数据」是两件事，前者不蕴含后者。

本模块用 `fakeredis.TcpFakeServer` 起一个**真的监听端口的服务**，
再用**真的 redis-py 客户端**经 TCP 连上去，于是：

  · 连接池、pipeline、Lua 脚本、TTL 全部走真实网络路径（而不是直接调 Python 方法）；
  · **换一个客户端实例 == 换一个进程** → 可以真正断言「重启后数据还在」；
  · 仍然不需要本机装 Redis，CI 也能跑。

---------------------------------------------------------------------------
覆盖点
---------------------------------------------------------------------------
1. 真实 redis-py 客户端经 TCP 读写（走连接池路径）
2. 新客户端（等价进程重启）能读到旧客户端写的数据        ← 核心
3. MemoryManager + Redis：换一个 Manager 实例仍拿得到历史 ← 核心
4. HASH field 结构 / TTL / ZSET 索引在真实连接下同样成立，并摘除幽灵成员
5. 分布式锁的 Lua 释放脚本在真实连接下可用（并发读改写不丢数据）
6. 内存预警在「INFO 读不到」时降级为 unknown 且**不阻断写入**
   —— 监控组件绝不该成为把应用写死的原因
7. 连接不可达时读返回 None、不抛异常，问答链路不中断

运行：
    .venv/bin/python tests/test_module7_redis_over_tcp.py
"""
import socket
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

# 同样不启后台巡检线程（否则它可能去连真实的 localhost:6379 刷日志）
_ORIGINAL_MONITOR_ENABLED = settings.REDIS_MEMORY_MONITOR_ENABLED
settings.REDIS_MEMORY_MONITOR_ENABLED = False

HOST = "127.0.0.1"
PORT = 6399
TTL = 3


def check(name: str, condition: bool, detail: str = "") -> None:
    """统一的断言输出：通过/失败计数并打印。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def wait_port_open(host: str, port: int, timeout: float = 5.0) -> bool:
    """等服务真的开始监听（serve_forever 是异步起来的，不能立刻 connect）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# 起一个真的 TCP 服务 + 真的 redis-py 客户端
# --------------------------------------------------------------------------- #
print("\n== 第 0 组：启动 TCP 假 Redis 服务（真 socket） ==")

from fakeredis import TcpFakeServer  # noqa: E402

import redis  # noqa: E402

server = TcpFakeServer((HOST, PORT), server_type="redis")
# ⚠️ 必须显式开：ThreadingMixIn 的 handler 线程默认是**非守护**的，
# 而 redis-py 的连接池会一直攥着 socket 不放 → 每个连接一个非守护线程
# → 脚本跑到最后也退不出去，进程挂在那里不结束（踩过）。
server.daemon_threads = True
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()

listening = wait_port_open(HOST, PORT)
check(f"假 Redis 服务已在 {HOST}:{PORT} 监听（走真实 TCP）", listening)
if not listening:
    print("\n服务没起来，后续用例无法执行。")
    sys.exit(1)


def new_client() -> "redis.Redis":
    """
    造一个**全新的** redis-py 客户端（含独立连接池）。

    注意这里没有任何东西指向 fakeredis 的 Python 对象 —— 只有 host/port。
    所以第二个客户端拿到数据这件事，等价于「另一个进程连上同一个 Redis」。
    """
    return redis.Redis(
        host=HOST,
        port=PORT,
        db=0,
        decode_responses=False,  # 与生产客户端配置一致
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=30,
    )


from core.redis_store import RedisSessionStore  # noqa: E402
from core.session_store import SessionSnapshot  # noqa: E402

client_a = new_client()
check("真实 redis-py 客户端能 PING 通（真网络往返）", client_a.ping() is True)

store_a = RedisSessionStore(client=client_a, ttl_seconds=TTL)


# --------------------------------------------------------------------------- #
print("\n== 第 1 组：经真实连接读写（HASH 结构 / TTL / 索引） ==")

store_a.save(
    "tcp-1",
    SessionSnapshot(
        messages=[
            {"role": "user", "content": "上海的社保基数怎么算？"},
            {"role": "assistant", "content": "以上年度全口径城镇单位就业人员平均工资为基准。"},
        ],
        exchange_meta=[{"intent": "policy_consult", "elapsed_ms": 88.5}],
        session_meta={"pinned": True, "title": "社保"},
        usage={"input_tokens": 12, "output_tokens": 34, "cache_read_tokens": 0, "requests": 1},
    ),
)

loaded = store_a.load("tcp-1")
check("经 TCP 写入后能读回", loaded is not None)
check(
    "中文内容经 TCP 往返无乱码",
    loaded is not None and loaded.messages[0]["content"] == "上海的社保基数怎么算？",
    f"实际 {loaded.messages[0]['content'] if loaded else None}",
)
check(
    "元数据经 TCP 往返一致",
    loaded is not None and loaded.exchange_meta[0]["intent"] == "policy_consult",
)
check("置顶标记经 TCP 往返一致", loaded is not None and loaded.session_meta["pinned"] is True)

session_key = f"{settings.REDIS_KEY_PREFIX}:session:tcp-1"
index_key = f"{settings.REDIS_KEY_PREFIX}:sessions"

raw = client_a.hgetall(session_key)
field_names = sorted(k.decode() if isinstance(k, bytes) else k for k in raw)
check(
    "服务端确实是 HASH 的 5 个 field（不是一条大 JSON）",
    field_names == ["exchange_meta", "last_active", "messages", "session_meta", "usage"],
    f"实际 {field_names}",
)
check("TTL 真的落在服务端（读 TTL 命令）", client_a.ttl(session_key) > 0, f"实际 {client_a.ttl(session_key)}")
check("索引 ZSET 存在于服务端", client_a.exists(index_key) == 1)

# 索引残留清理
client_a.zadd(index_key, {"ghost-tcp": time.time()})
ids = store_a.list_ids()
check("list_ids 摘除索引中的幽灵成员", "ghost-tcp" not in ids, f"实际 {ids}")
check("幽灵成员被顺手从 ZSET 删除", client_a.zscore(index_key, "ghost-tcp") is None)


# --------------------------------------------------------------------------- #
print("\n== 第 2 组：换一个客户端实例 == 换一个进程（重启不丢数据） ==")

# 关键用例：客户端 B 与客户端 A 之间没有任何 Python 对象共享，
# 只有「同一个 host:port」。它读得到数据，就说明数据在服务端而不在进程里。
client_b = new_client()
store_b = RedisSessionStore(client=client_b, ttl_seconds=TTL)

check("新进程（新客户端 + 新连接池）能读到旧数据", store_b.exists("tcp-1") is True)
survived = store_b.load("tcp-1")
check(
    "新进程读回的历史内容与服务端一致",
    survived is not None and survived.messages[1]["content"].startswith("以上年度全口径"),
    f"实际 {survived.messages if survived else None}",
)
check("新进程读回的用量统计一致", survived is not None and survived.usage["requests"] == 1)
check("新进程也能列出会话索引", store_b.list_ids()[:1] == ["tcp-1"], f"实际 {store_b.list_ids()}")


# --------------------------------------------------------------------------- #
print("\n== 第 3 组：MemoryManager + Redis —— 换 Manager 实例仍拿到历史（P0-1 验收） ==")

from core.memory_manager import MemoryManager  # noqa: E402

# ① 第一个 Manager：显式注入走 TCP 的 Store
manager_1 = MemoryManager(max_turns=10, ttl_seconds=TTL, store=RedisSessionStore(client=new_client(), ttl_seconds=TTL))
manager_1.add_exchange("mm-1", "公司年假怎么算？", "入职满一年可享 5 天带薪年假。", meta={"intent": "policy_consult"})
manager_1.add_exchange("mm-1", "那病假呢？", "病假按当地最低工资标准的 80% 计发。")
manager_1.update_session_meta("mm-1", title="假期制度", pinned=True)
# 用量由独立入口累加（add_exchange 不管 token 统计，这是接口分层，不是 bug）
manager_1.add_usage("mm-1", input_tokens=12, output_tokens=34)

check("第一个 Manager 写入后能读到 4 条消息", len(manager_1.get_messages("mm-1")) == 4)

# ② 模拟「后端重启」：全新 Manager、全新 Store、全新客户端
#
#    ⚠️ 这里**不再走工厂**。原来这段会设 `settings.MEMORY_BACKEND = "redis"`
#    让 build_session_store 装配一个 Redis Store，顺带验证工厂路径。
#    2026-09-23 架构调整后 Redis 已退出会话真相源：
#      · `MEMORY_BACKEND` 只剩 memory | mysql；
#      · 工厂遇到 "redis" 会**直接抛 ValueError**，而不是静默回退到内存版
#        （静默回退在生产上等于「重启即丢全部会话」，日志里只有一行 WARNING，
#          比启动失败坏得多 —— 详见 core/session_store.py 的 build_session_store）。
#    工厂对退役取值的报错行为由 tests/test_module8 断言；本模块只测 Redis 自身语义，
#    所以改成显式注入。
manager_2 = MemoryManager(
    max_turns=10, ttl_seconds=TTL, store=RedisSessionStore(client=new_client(), ttl_seconds=TTL)
)

check("重启后新的 Manager 报出 redis 后端", manager_2.store.name == "redis", f"实际 {manager_2.store.name}")
msgs = manager_2.get_messages("mm-1")
check("★ 重启后历史会话仍在（4 条消息）", len(msgs) == 4, f"实际 {len(msgs)}")
check(
    "★ 重启后历史内容正确",
    len(msgs) == 4 and msgs[0].content == "公司年假怎么算？" and msgs[2].content == "那病假呢？",
    f"实际 {[m.content for m in msgs]}",
)
sessions = {s["session_id"]: s for s in manager_2.list_sessions()}
check("★ 重启后置顶与标题仍在", sessions.get("mm-1", {}).get("pinned") is True, f"实际 {sessions.get('mm-1')}")
check("★ 重启后标题仍在", sessions.get("mm-1", {}).get("title") == "假期制度")
check(
    "★ 重启后用量统计仍在",
    sessions.get("mm-1", {}).get("usage", {}).get("requests") == 1,
    f"实际 {sessions.get('mm-1')}",
)
check(
    "★ 重启后 token 明细仍在",
    manager_2.get_usage("mm-1").get("output_tokens") == 34,
    f"实际 {manager_2.get_usage('mm-1')}",
)

report = manager_2.memory_report()
check("memory_report 报出后端为 redis", report.get("backend") == "redis", f"实际 {report.get('backend')}")
check("memory_report 带 store 分区", isinstance(report.get("store"), dict))


# --------------------------------------------------------------------------- #
print("\n== 第 4 组：分布式锁的 Lua 释放在真实连接下可用（并发读改写不丢数据） ==")

from core.session_store import SessionSnapshot as _Snap  # noqa: E402

concurrent_store = RedisSessionStore(client=new_client(), ttl_seconds=3600)
concurrent_store.save("tcp-concurrent", _Snap(messages=[]))


def _worker(tag: str) -> None:
    """一轮「读 → 改 → 写」，没锁就会互相覆盖。"""
    with concurrent_store.session_lock("tcp-concurrent"):
        snap = concurrent_store.load("tcp-concurrent")
        snap.messages.append({"role": "user", "content": tag})
        concurrent_store.save("tcp-concurrent", snap)


threads = [threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

final = concurrent_store.load("tcp-concurrent")
check(
    "5 个线程并发读改写，5 条一条不丢（Lua 锁在真连接下生效）",
    final is not None and len(final.messages) == 5,
    f"实际 {len(final.messages) if final else None}",
)
check(
    "并发写入内容完整（不是互相覆盖）",
    final is not None and {m["content"] for m in final.messages} == {f"w{i}" for i in range(5)},
    f"实际 {[m['content'] for m in final.messages] if final else None}",
)


# --------------------------------------------------------------------------- #
print("\n== 第 5 组：内存预警的降级 —— 监控不该把应用搞挂 ==")

from core.redis_monitor import RedisMemoryMonitor  # noqa: E402
from core.redis_store import get_runtime_state  # noqa: E402

monitor = RedisMemoryMonitor(client=new_client())
status = monitor.inspect()
check("inspect 在真实连接下不抛异常", isinstance(status, dict))
check("返回结果含 level", "level" in status, f"实际 keys={sorted(status)}")
check(
    "level 取值合法",
    status.get("level") in {"ok", "warn", "critical", "fatal", "unknown"},
    f"实际 {status.get('level')}",
)
check("返回结果含 thresholds（供接口展示阈值）", isinstance(status.get("thresholds"), dict))
check("返回结果含建议列表", isinstance(status.get("advice"), list))

# 核心安全性质：即使 INFO 读不到（level=unknown），也不能拦写入
get_runtime_state().unblock_writes()
acted = monitor.check_and_act()
check("check_and_act 在真实连接下不抛异常", isinstance(acted, dict))
if status.get("level") == "unknown":
    print(f"        （本题 INFO 不可读：{status.get('reason')}）")
    check("★ INFO 不可读时**不**进入只读保护（监控失效不等于拒绝服务）", get_runtime_state().is_write_blocked()[0] is False)
    check(
        "★ unknown 状态下 MemoryManager 仍能正常写入",
        manager_2.add_exchange("mm-unknown", "q", "a") is None,
    )
    # 契约：字段形状不随连通性变化（level / reason / thresholds / advice / checked_at 恒在）
    check("★ unknown 时 thresholds 仍在（纯配置，不依赖 Redis）", isinstance(status.get("thresholds"), dict))
    check(
        "★ unknown 时给出可执行建议（Redis 不可达怎么查）",
        any("Redis 不可达" in a for a in status.get("advice", [])),
        f"实际 {status.get('advice')}",
    )
    # 反向断言：unknown 是「没读到」而不是「读到了且正常」，绝不能套用 ok 的话术
    check(
        "★ unknown 不说「水位正常，无需处理」（那会掩盖故障）",
        not any("无需处理" in a for a in status.get("advice", [])),
        f"实际 {status.get('advice')}",
    )


# --------------------------------------------------------------------------- #
print("\n== 第 6 组：连接不可达时的降级（问答链路不中断） ==")

# 指到一个没人监听的端口
dead_store = RedisSessionStore(client=new_client(), ttl_seconds=TTL)
dead_store._client = redis.Redis(host=HOST, port=1, socket_timeout=0.3, socket_connect_timeout=0.3)  # type: ignore[assignment]

degraded = dead_store.load("anything")
check("连接不可达时读返回 None（按无历史处理，不抛异常）", degraded is None)
check("连接不可达时 exists 返回 False", dead_store.exists("anything") is False)
check("连接不可达时 health 报 ok=False", dead_store.health().get("ok") is False, f"实际 {dead_store.health()}")

try:
    dead_store.save("anything", _Snap(messages=[{"role": "user", "content": "x"}]))
    check("连接不可达时 save 不抛异常（degrade_on_error，默认吞掉）", True)
except Exception as exc:  # pragma: no cover
    check("连接不可达时 save 不抛异常（degrade_on_error，默认吞掉）", False, f"抛出 {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
print("\n== 收尾 ==")
# 先把客户端连接池都关掉（不然服务端 handler 线程还在等命令，进程迟迟不退），
# 再关服务。顺序反了也能靠 daemon_threads 兜住，但显式关更干净。
for _c in (client_a, client_b, manager_2.store._client):  # type: ignore[attr-defined]
    try:
        _c.close()
    except Exception:
        pass

try:
    server.shutdown()
    server.server_close()
    check("TCP 假服务已关闭", True)
except Exception as exc:  # pragma: no cover
    check("TCP 假服务已关闭", False, f"{type(exc).__name__}: {exc}")
finally:
    settings.REDIS_MEMORY_MONITOR_ENABLED = _ORIGINAL_MONITOR_ENABLED

print("\n==================================================")
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
