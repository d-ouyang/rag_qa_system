# pyright: basic
"""
模块8测试文件：MySQL 会话存储（P0-1 的**生产真相源**实现）。

--------------------------------------------------------------------------
它和 module6 / module7 的分工
--------------------------------------------------------------------------
    module6  memory / fakeredis  → 「语义对不对」（存储契约）
    module7  fakeredis over TCP  → 「重启后数据还在不在」（进程内假对象验不出这个）
    module8  **真 MySQL**        → 「真相源这一层立不立得住」
                                    （行级 diff、事务锁、软过期、SQL 可查、与 memory 后端逐字对齐）

--------------------------------------------------------------------------
覆盖点
--------------------------------------------------------------------------
1. **结构一致性**：Alembic 迁移建出来的表 == `core/schema.py` 的定义。
   用 Alembic 自己的比较器（`compare_metadata`）而不是手写「列名相等」——
   列类型、可空性、索引、唯一键、列注释的漂移它都能发现。
   （这条断言存在的理由：迁移是冻结的 DDL 快照，schema.py 是活的查询视图，
     两份刻意分开，就必须有东西盯着它们别分叉。）
2. **存储契约**：直接复用 `tests/store_contract.py` 里**同一个** `exercise_store`，
   与 module6 的 memory / redis 后端跑一模一样的断言。
3. **MySQL 特有**：行级 diff 真的落成了行（不是把整条会话塞进一个 blob）；
   轮元数据挂在 assistant 行上；用户行的 `turn_meta` 是 SQL NULL。
4. **重启不丢**：销毁连接池、换全新 Engine/Store/Manager 实例（≈ 换进程）后，
   会话列表 / 历史 / 置顶 / 标题 / 用量 / 每轮元数据全在。
5. **FOR UPDATE**：多线程对同一会话 load-modify-save，一轮都不丢。
6. **TTL 软过期**：load 返回 None，但**行仍在库里**；purge_expired 只打归档标记。
7. **无孤儿行**：删会话时消息一并清掉（没有 FK 级联，靠应用层保证）。
8. **与 memory 后端 parity**：同一串操作跑两遍，结果逐字段相等。
9. **MySQL 不可达时整模块 SKIP**，不把「没起容器」误报成失败。
   想让它在 CI / 验收里变成硬失败，设 `REQUIRE_MYSQL=1`。

运行：
    make infra                                          # 先起中间件
    .venv/bin/python tests/test_module8_mysql_session_store.py
"""
import json
import os
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


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


# --------------------------------------------------------------------------- #
# 前置：MySQL 不可达就整模块 SKIP
# --------------------------------------------------------------------------- #
print("== 前置检查：MySQL 连通性 ==")
from core import db as db_module
from core.schema import chat_message_table as MSG
from core.schema import session_table as SESS

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
if not _conn["ok"]:
    print()
    print("=" * 66)
    print("  SKIP：MySQL 不可达，模块8 未执行。")
    print("  本地起中间件：make infra      （或 docker compose up -d mysql redis）")
    print("  若这是 CI / 验收，请设 REQUIRE_MYSQL=1 让它变成硬失败。")
    print("=" * 66)
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
print("  连通正常，开始执行\n")

from sqlalchemy import delete, text  # noqa: E402

from core.db import get_engine  # noqa: E402
from core.memory_manager import MemoryManager, reset_memory_manager  # noqa: E402
from core.mysql_store import MySQLSessionStore  # noqa: E402
from core.session_store import MemorySessionStore, SessionSnapshot, build_session_store  # noqa: E402
from store_contract import exercise_store  # noqa: E402


def wipe() -> None:
    """清空两张业务表。先删消息再删会话（没有 FK 级联，顺序得自己保证）。"""
    with get_engine().begin() as conn:
        conn.execute(delete(MSG))
        conn.execute(delete(SESS))


def count_messages(session_id: str) -> int:
    with get_engine().connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM chat_message WHERE session_id = :s"), {"s": session_id}
        ).scalar() or 0)


def session_row(session_id: str):
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT * FROM session WHERE id = :s"), {"s": session_id}
        ).mappings().first()


# --------------------------------------------------------------------------- #
# 第 1 组：结构一致性（迁移 DDL ↔ core/schema.py）
# --------------------------------------------------------------------------- #
print("== 第 1 组：结构一致性（Alembic 迁移 ↔ core/schema.py）==")
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402

from core import schema as app_schema  # noqa: E402

with get_engine().connect() as _conn:
    _mc = MigrationContext.configure(_conn, opts={"compare_type": True})
    _diff = compare_metadata(_mc, app_schema.metadata)
check(
    "迁移建出的表与 core/schema.py 完全一致（列/类型/可空/索引/唯一键/注释）",
    not _diff,
    f"diff={_diff}",
)

with get_engine().connect() as _conn:
    _tables = {r[0] for r in _conn.execute(text("SHOW TABLES"))}
check(
    "五张业务表都已建出",
    {"user", "folder", "session", "chat_message", "document"} <= _tables,
    f"实际 {sorted(_tables)}",
)

# --------------------------------------------------------------------------- #
# 第 2 组：存储契约（与 memory / redis 后端跑同一个函数）
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：存储契约（复用 tests/store_contract.py）==")
wipe()
# TTL 给足：契约不测过期（那在第 6 组），但如果 MySQL 每次操作都要一个来回，
# TTL=1~2 秒就可能让「stats 报出会话数」在慢机器上偶发红 —— 闪断比不测更糟。
mysql_store = MySQLSessionStore(ttl_seconds=60)
exercise_store(mysql_store, "mysql", check)

# --------------------------------------------------------------------------- #
# 第 3 组：MySQL 特有 —— 行级 diff 与字段映射
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：MySQL 特有 —— 行级落库 ==")
wipe()
store = MySQLSessionStore(ttl_seconds=3600)

msgs = []
metas = []
for i in range(3):
    msgs += [
        {"role": "user", "content": f"问题{i}"},
        {"role": "assistant", "content": f"回答{i}"},
    ]
    metas.append({
        "intent": f"intent{i}",
        "elapsed_ms": 100.0 + i,
        "sources": [{"index": 1, "source": f"doc{i}.md", "snippet": f"片段{i}"}],
        "usage": {"input_tokens": i + 1, "output_tokens": i + 2},
    })
store.save("m1", SessionSnapshot(
    messages=msgs, exchange_meta=metas,
    session_meta={"pinned": True, "title": "行级验证"},
    usage={"input_tokens": 6, "output_tokens": 9, "cache_read_tokens": 2, "requests": 3},
))

check("3 轮 = 6 行消息（不是整条塞一个字段）", count_messages("m1") == 6, f"实际 {count_messages('m1')}")
with get_engine().connect() as conn:
    roles = [r[0] for r in conn.execute(text(
        "SELECT role FROM chat_message WHERE session_id='m1' ORDER BY seq"))]
check("role 按 seq 交替", roles == ["user", "assistant"] * 3, f"实际 {roles}")
with get_engine().connect() as conn:
    nulls = [r[0] for r in conn.execute(text(
        "SELECT seq FROM chat_message WHERE session_id='m1' AND turn_meta IS NULL ORDER BY seq"))]
check("turn_meta 为 SQL NULL 的正是 user 行（seq 0/2/4）", nulls == [0, 2, 4], f"实际 {nulls}")
with get_engine().connect() as conn:
    r = conn.execute(text(
        "SELECT ref_ids, usage_input_token, JSON_UNQUOTE(JSON_EXTRACT(turn_meta,'$.intent')) "
        "FROM chat_message WHERE session_id='m1' AND seq=5")).first()
check("assistant 行投影出 usage 明细", r is not None and r[1] == 3, f"实际 {r}")
check("turn_meta 可用 SQL 直接查 JSON 字段", r is not None and r[2] == "intent2", f"实际 {r}")
row = session_row("m1")
check("session 汇总列落库", row is not None and row["usage_requests"] == 3, f"实际 {dict(row) if row else None}")
check("session 标题/置顶落库", row is not None and row["title"] == "行级验证" and bool(row["is_pinned"]))
with get_engine().connect() as conn:
    _fsp = conn.execute(text(
        "SELECT DATETIME_PRECISION FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=:db AND TABLE_NAME='session' AND COLUMN_NAME='last_active_at'"),
        {"db": settings.MYSQL_DATABASE}).scalar()
# 秒精度会让「0.4 秒的间隔」被截断成 0，TTL 判定（now - last_active > ttl）直接判错。
check("last_active_at 精度为微秒（DATETIME(6)）", _fsp == 6, f"实际 DATETIME_PRECISION={_fsp}")

# 截断：库里真的少了行，而不是留一堆「逻辑已删」的脏数据
store.save("m1", SessionSnapshot(messages=msgs[:4], exchange_meta=metas[:2]))
check("截断后库里只剩 4 行（DELETE 真的执行了）", count_messages("m1") == 4, f"实际 {count_messages('m1')}")
# 编辑重发：中间的 user 消息被 UPDATE
edited = list(msgs[:4])
edited[2] = {"role": "user", "content": "问题1-改"}
store.save("m1", SessionSnapshot(messages=edited, exchange_meta=metas[:2]))
check("编辑重发后内容已 UPDATE", (store.load("m1") or SessionSnapshot()).messages[2]["content"] == "问题1-改")

# --------------------------------------------------------------------------- #
# 第 4 组：重启不丢（换连接池 + 换 Manager 实例）
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：重启不丢 ==")
wipe()
mm = MemoryManager(max_turns=10, ttl_seconds=3600, store=MySQLSessionStore(ttl_seconds=3600))
mm.add_exchange("boot", "重启前的提问", "重启前的回答", meta={"intent": "before", "elapsed_ms": 42.5})
mm.add_usage("boot", 11, 22, 3)
mm.update_session_meta("boot", title="重启验证", pinned=True)

# 销毁连接池 + 重置单例：等价于「换一个进程」
db_module.dispose_engine()
reset_memory_manager()
mm2 = MemoryManager(max_turns=10, ttl_seconds=3600, store=MySQLSessionStore(ttl_seconds=3600))

check("重启后历史消息还在", [m.content for m in mm2.get_messages("boot")] == ["重启前的提问", "重启前的回答"])
check("重启后轮元数据还在", mm2.get_exchange_meta("boot")[0].get("intent") == "before",
      f"实际 {mm2.get_exchange_meta('boot')}")
check("重启后耗时（浮点）无损", mm2.get_exchange_meta("boot")[0].get("elapsed_ms") == 42.5)
check("重启后置顶/标题还在", mm2.get_session_meta("boot") == {"pinned": True, "title": "重启验证"},
      f"实际 {mm2.get_session_meta('boot')}")
check("重启后用量还在", mm2.get_usage("boot") ==
      {"input_tokens": 11, "output_tokens": 22, "cache_read_tokens": 3, "requests": 1},
      f"实际 {mm2.get_usage('boot')}")
check("重启后会话列表还能列出来", [i["session_id"] for i in mm2.list_sessions()] == ["boot"])

# --------------------------------------------------------------------------- #
# 第 5 组：并发不丢轮（SELECT ... FOR UPDATE）
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：并发不丢轮（FOR UPDATE）==")
wipe()
cc = MySQLSessionStore(ttl_seconds=3600)
cc.save("cc", SessionSnapshot(messages=[]))
ROUNDS = 12


def _hammer(i: int) -> None:
    with cc.session_lock("cc"):
        snap = cc.load("cc") or SessionSnapshot()
        snap.messages.append({"role": "user", "content": f"t{i}"})
        snap.messages.append({"role": "assistant", "content": f"r{i}"})
        snap.last_active = time.time()
        cc.save("cc", snap)


_threads = [threading.Thread(target=_hammer, args=(i,)) for i in range(ROUNDS)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
final = cc.load("cc")
check(f"并发 {ROUNDS} 轮一轮不丢", final is not None and len(final.messages) == ROUNDS * 2,
      f"实际 {len(final.messages) if final else None}")
check("并发下标无重复（没有两次写入互相覆盖）",
      final is not None and len({m["content"] for m in final.messages}) == ROUNDS * 2)

# --------------------------------------------------------------------------- #
# 第 6 组：TTL 软过期 + 归档
# --------------------------------------------------------------------------- #
print("\n== 第 6 组：TTL 软过期 ==")
wipe()
ttl_store = MySQLSessionStore(ttl_seconds=1)
ttl_store.save("exp", SessionSnapshot(messages=[{"role": "user", "content": "x"}]))
time.sleep(1.3)
check("过期后 load 返回 None", ttl_store.load("exp") is None)
check("过期后 exists 为 False", ttl_store.exists("exp") is False)
check("过期后不在会话列表里", "exp" not in ttl_store.list_ids())
check("⚠️ 过期后行仍在库里（没被物理删）", session_row("exp") is not None)
archived = ttl_store.purge_expired()
check("purge_expired 报出归档数", archived >= 1, f"实际 {archived}")
row = session_row("exp")
check("purge_expired 只打归档标记 is_archived=1", row is not None and bool(row["is_archived"]),
      f"实际 {dict(row) if row else None}")
check("归档后消息行仍在（会话是永久资产）", count_messages("exp") == 1)

# --------------------------------------------------------------------------- #
# 第 7 组：删除不留孤儿行
# --------------------------------------------------------------------------- #
print("\n== 第 7 组：删除不留孤儿行 ==")
wipe()
dd = MySQLSessionStore(ttl_seconds=3600)
dd.save("d1", SessionSnapshot(messages=[{"role": "user", "content": "a"},
                                        {"role": "assistant", "content": "b"}]))
check("删除前有 2 行消息", count_messages("d1") == 2)
check("delete 返回 True", dd.delete("d1") is True)
check("删除后消息 0 行（无孤儿）", count_messages("d1") == 0, f"实际 {count_messages('d1')}")
check("delete 幂等（第二次返回 False）", dd.delete("d1") is False)

# --------------------------------------------------------------------------- #
# 第 8 组：与 memory 后端行为 parity
# --------------------------------------------------------------------------- #
print("\n== 第 8 组：与 memory 后端 parity ==")


def run_scenario(manager: MemoryManager) -> dict:
    """同一串操作。两个后端跑完必须逐字段相等 —— 否则「换后端 = 换了个 bug」。"""
    manager.add_exchange("p", "q1", "a1", meta={"intent": "i1"})
    manager.add_usage("p", 1, 2, 0)
    manager.add_exchange("p", "q2", "a2", meta={"intent": "i2"})
    manager.add_usage("p", 3, 4, 1)
    manager.update_session_meta("p", title="T", pinned=True)
    manager.add_exchange("p2", "x", "y")
    manager.truncate_session("p", keep_messages=3)
    return {
        "msgs": [m.content for m in manager.get_messages("p")],
        "metas": manager.get_exchange_meta("p"),
        "meta": manager.get_session_meta("p"),
        "usage": manager.get_usage("p"),
        # last_active 是时间戳、两后端必然不同，不参与比对；只比顺序语义
        "ids": manager.store.list_ids(),
        "count": manager.session_count(),
        "session_ids": sorted(i["session_id"] for i in manager.list_sessions()),
        "message_count": sorted(i["message_count"] for i in manager.list_sessions()),
    }


wipe()
mysql_result = run_scenario(MemoryManager(max_turns=10, ttl_seconds=3600,
                                         store=MySQLSessionStore(ttl_seconds=3600)))
memory_result = run_scenario(MemoryManager(max_turns=10, ttl_seconds=3600,
                                           store=MemorySessionStore(ttl_seconds=3600)))
check("parity：两边操作序列后的结果完全一致", mysql_result == memory_result,
      f"\n    mysql = {json.dumps(mysql_result, ensure_ascii=False, default=str)}"
      f"\n    memory= {json.dumps(memory_result, ensure_ascii=False, default=str)}")
for _key in ("msgs", "metas", "meta", "usage", "ids", "count", "session_ids", "message_count"):
    check(f"parity 细分：{_key}", mysql_result[_key] == memory_result[_key],
          f"mysql={mysql_result[_key]!r} memory={memory_result[_key]!r}")

# --------------------------------------------------------------------------- #
# 第 9 组：工厂装配路径（build_session_store）
# --------------------------------------------------------------------------- #
print("\n== 第 9 组：工厂装配路径 ==")
_orig_backend = settings.MEMORY_BACKEND
try:
    settings.MEMORY_BACKEND = "memory"
    check("memory → MemorySessionStore", isinstance(build_session_store(ttl_seconds=60), MemorySessionStore))

    settings.MEMORY_BACKEND = "mysql"
    check("mysql → MySQLSessionStore", isinstance(build_session_store(ttl_seconds=60), MySQLSessionStore))

    # 退役取值必须**响亮地失败**。若这里静默回退成内存版，生产上等于
    # 「重启即丢全部会话」，而日志里只有一行 WARNING —— 静默数据丢失比启动失败坏得多。
    settings.MEMORY_BACKEND = "redis"
    try:
        build_session_store(ttl_seconds=60)
        check("redis（已退役）必须抛错，而不是静默回退内存版", False, "居然没抛")
    except ValueError as exc:
        check("redis（已退役）必须抛错，而不是静默回退内存版", "已废弃" in str(exc), f"实际 {exc}")

    settings.MEMORY_BACKEND = "sqlite"
    try:
        build_session_store(ttl_seconds=60)
        check("未知取值必须抛错，而不是静默回退内存版", False, "居然没抛")
    except ValueError as exc:
        check("未知取值必须抛错，而不是静默回退内存版", "未知" in str(exc), f"实际 {exc}")
finally:
    settings.MEMORY_BACKEND = _orig_backend

# --------------------------------------------------------------------------- #
print("\n" + "=" * 50)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 50)
wipe()
sys.exit(1 if FAIL else 0)
