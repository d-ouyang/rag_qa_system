# pyright: basic
"""
P2-10a 测试：显式 MySQL 探针与聚合运维总览。

    .venv/bin/python tests/test_module17_observability.py

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    api/routes/system.py   GET /system/db       MySQL 探针（check_connection + 池水位 + 延迟）
    api/routes/system.py   GET /system/overview 聚合总览（db/queue/worker/memory/documents + overall）

--------------------------------------------------------------------------
设计取舍：为什么判定规则用 mock 而不是真环境
--------------------------------------------------------------------------
本模块的核心价值不在「探针能跑通」（那只是结构问题），而在 **overall 判定
规则** —— 它是「跨接口拼告警规则没人维护」这个老问题的收口，规则错了
监控就永远报错或永远沉默。

判定规则要在六种确定性场景下验证：

    db挂            → down   （真相源挂了，别的都无从谈起）
    broker挂        → degraded
    积压+worker死    → degraded
    积压+worker死+跳过探测 → 不降级（探测没做就不能拿它定罪）
    无积压+worker死  → ok     （没任务时 worker 闲着不是故障）
    redis内存 critical → degraded

这些场景在真实环境里**凑不齐**：你不能为了测「down」把本地 MySQL 停掉，
也不能等 Redis 真的写到 critical。所以判定规则全部走 mock 注入 ——
`unittest.mock.patch` 直接替换 `api.routes.system` 命名空间里的依赖函数，
输入是确定的，输出就必须是确定的。这是**验证不变量**，不是概率断言。

真实调用（第 1、2 组）只验**结构不变量**：键恒定出现、类型恒定、
永不抛异常 —— 「运维接口必须永不 5xx」这条铁律的可测形态。
"""
import sys
import unittest.mock as mock
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402

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


def section(title: str) -> None:
    print(f"\n== {title} ==")


# 导入放在 sys.path 之后。只 import 路由函数本身，不起 uvicorn。
from api.routes.system import db_status, system_overview  # noqa: E402


def _fake_queue(ok: bool = True, depth: int = 0, unacked: int = 0, detail: str = "") -> dict[str, Any]:
    return {"queue": "celery", "ok": ok, "depth": depth, "unacked": unacked, "detail": detail}


def _fake_worker(ok: bool, workers: list[str] | None = None, detail: str = "") -> dict[str, Any]:
    return {"ok": ok, "workers": workers or [], "detail": detail}


def _fake_memory(level: str | None = None, backend: str = "mysql") -> dict[str, Any]:
    """memory_report 的最小形态。level=None 表示非 redis 后端（无 redis_memory 键）。"""
    report: dict[str, Any] = {
        "backend": backend,
        "max_turns": 12,
        "ttl_seconds": 86400,
        "max_session_bytes": 65536,
        "degrade_on_error": True,
        "store": {},
        "health": {"ok": True, "detail": ""},
    }
    if level is not None:
        report["redis_memory"] = {"level": level, "used": 1, "peak": 1, "max": 100, "advice": ""}
    return report


# ===========================================================================
section("第 1 组  GET /system/db —— 结构不变量（真实调用）")
# ===========================================================================
_db = db_status()
check("db_status 不抛异常且有 db 键", isinstance(_db, dict) and "db" in _db, f"keys={list(_db) if isinstance(_db, dict) else type(_db)}")
_inner = _db.get("db", {})
check("db.backend 恒为字符串", isinstance(_inner.get("backend"), str), f"got {_inner.get('backend')!r}")
check("db.ok 恒为布尔", isinstance(_inner.get("ok"), bool), f"got {_inner.get('ok')!r}")
check("db.detail 恒为字符串（失败原因必须可读）", isinstance(_inner.get("detail"), str), f"got {_inner.get('detail')!r}")
check("db.latency_ms 是数字", isinstance(_inner.get("latency_ms"), (int, float)), f"got {_inner.get('latency_ms')!r}")
check("db.pool.status 存在", "status" in (_inner.get("pool") or {}), f"pool={_inner.get('pool')!r}")
check("checked_at 存在", isinstance(_db.get("checked_at"), str), f"got {_db.get('checked_at')!r}")

# ===========================================================================
section("第 2 组  GET /system/overview —— 结构不变量（真实调用，worker 探测开启）")
# ===========================================================================
_ov = system_overview(worker=True)
check("overview 不抛异常且有 overall 键", isinstance(_ov, dict) and "overall" in _ov, f"keys={list(_ov) if isinstance(_ov, dict) else type(_ov)}")
_status = (_ov.get("overall") or {}).get("status")
check(f"overall.status ∈ ok/degraded/down（实际 {_status!r}）", _status in ("ok", "degraded", "down"), f"got {_status!r}")
check("overall.reasons 是列表", isinstance((_ov.get("overall") or {}).get("reasons"), list), f"got {(_ov.get('overall') or {}).get('reasons')!r}")
for key in ("db", "queue", "worker", "documents", "memory", "worker_checked", "elapsed_ms", "generated_at"):
    check(f"overview 含 {key} 键", key in _ov, f"missing {key}")
check("worker_checked=True 时 worker.ok 是布尔", isinstance((_ov.get("worker") or {}).get("ok"), bool), f"got {(_ov.get('worker') or {}).get('ok')!r}")

# ===========================================================================
section("第 3 组  GET /system/overview —— ?worker=false 跳过阻塞探测")
# ===========================================================================
_ov_skip = system_overview(worker=False)
check("worker_checked=False", _ov_skip.get("worker_checked") is False, f"got {_ov_skip.get('worker_checked')!r}")
_w = _ov_skip.get("worker") or {}
check("worker.ok=None（不参与判定）", _w.get("ok") is None, f"got {_w.get('ok')!r}")
check("worker.detail 说明跳过原因", "跳过" in str(_w.get("detail", "")), f"got {_w.get('detail')!r}")

# ===========================================================================
section("第 4 组  overall 判定规则 —— 六种确定性场景（mock 注入）")
# ===========================================================================
def _scenario(name: str, expect_status: str, expect_reason_keyword: str | None = None, **kw: Any) -> None:
    """注入一套确定性依赖，断言 overall 判级与降级原因。"""
    db_ret = (
        {"backend": "mysql", "ok": True, "detail": "mysql:3306/qa"}
        if kw.get("db_ok", True)
        else {"backend": "mysql", "ok": False, "detail": "mysql:3306/qa → OperationalError: refused"}
    )
    try:
        with (
            mock.patch("api.routes.system.check_connection", return_value=db_ret),
            mock.patch("api.routes.system.queue_depth",
                       return_value=_fake_queue(kw.get("queue_ok", True), kw.get("depth", 0), kw.get("unacked", 0))),
            mock.patch("api.routes.system.worker_alive",
                       return_value=_fake_worker(kw.get("worker_ok", True), detail=kw.get("worker_detail", ""))),
            mock.patch("api.routes.system.repo.counts_by_status",
                       return_value=kw.get("docs") or {"pending": 0, "parsing": 0, "success": 3, "fail": 0}),
            mock.patch("api.routes.system.get_memory_manager") as mm,
        ):
            mm.return_value.memory_report.return_value = _fake_memory(kw.get("mem_level"), kw.get("mem_backend", "mysql"))
            ov = system_overview(worker=kw.get("worker_probe", True))
        status = ov["overall"]["status"]
        reasons = " | ".join(ov["overall"]["reasons"])
        ok = status == expect_status and (expect_reason_keyword is None or expect_reason_keyword in reasons)
        check(f"{name} → {expect_status}" + (f"（原因含「{expect_reason_keyword}」）" if expect_reason_keyword else ""),
              ok, f"got status={status!r} reasons={reasons!r}")
    except Exception as e:  # noqa: BLE001
        check(f"{name} → {expect_status}", False, f"抛了异常 {type(e).__name__}: {e}")


# 场景 1：MySQL 挂 → down（即使 broker 也挂，down 优先）
_scenario("场景1 MySQL不可达", "down", db_ok=False, queue_ok=False)

# 场景 2：broker 挂（db 正常）→ degraded，原因提到 broker
_scenario("场景2 Redis broker不可达", "degraded", "broker", queue_ok=False, worker_ok=True)

# 场景 3：有积压 + worker 死 → degraded，原因提到积压
_scenario("场景3 积压+worker死", "degraded", "积压", depth=5, unacked=2, worker_ok=False)

# 场景 4：有积压 + worker 死 + 跳过探测 → 不降级（没探测就不能定罪）
_scenario("场景4 积压+worker死但跳过探测", "ok", worker_probe=False, depth=5, unacked=2, worker_ok=False)

# 场景 5：无积压 + worker 死 → ok（闲着不是故障）
_scenario("场景5 无积压+worker死", "ok", depth=0, unacked=0, worker_ok=False)

# 场景 6：redis 内存 critical → degraded，原因提到内存
_scenario("场景6 redis内存critical", "degraded", "内存", mem_level="critical", mem_backend="redis")

# 场景 7（对照）：全部正常 → ok 且 reasons 为空
_scenario("场景7 全部正常", "ok")

# ===========================================================================
section("第 5 组  结构一致性 —— overview 的 db 块与 /db 同构")
# ===========================================================================
check("overview.db 与 /db 的键集合一致",
      set(_ov.get("db", {}).keys()) == set(_db.get("db", {}).keys()),
      f"overview={sorted(_ov.get('db', {}).keys())} vs /db={sorted(_db.get('db', {}).keys())}")
check("overview.memory.backend 来自 memory_report",
      isinstance((_ov.get("memory") or {}).get("backend"), str), f"got {(_ov.get('memory') or {}).get('backend')!r}")

# ===========================================================================
print(f"\n{'=' * 60}")
print(f"P2-10a 观测探针测试：PASS={PASS} FAIL={FAIL}")
print("=" * 60)
if FAIL:
    sys.exit(1)
print("\n✅ 全部通过 —— /db 与 /overview 结构恒定，overall 判定规则七种场景全部符合预期。")
