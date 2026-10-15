# pyright: basic
"""
模块22测试文件：念安站心情收集接口（site-mood，公开小模块）。

--------------------------------------------------------------------------
它与相邻模块的分工
--------------------------------------------------------------------------
    module8   真 MySQL  → RAG 业务真相源（会话/消息）
    module11  账号体系   → 结构一致性的 compare_metadata 断言就在那里
    module22  site-mood → 「公开小接口立不立得住」：
                          双闸（限流/去重）的顺序与语义、admin token 的
                          fail-closed、返回体不泄 PII。

覆盖点
--------------------------------------------------------------------------
1.  **结构一致性**：Alembic 迁移建出来的库 == `core/schema.py`（整库 diff 为 0，
    复用 module11 的比较器用法）。跑本模块前必须 `make db-upgrade`。
2.  **submit 正常路径**：合法邮箱 + 合法 mood → 200 `{ok:true, id}`。
3.  **邮箱冷却**：同邮箱（含大小写变体）窗口内第二次提交 → `{ok:false,
    reason:"duplicate"}`，库里的行数不增加。
3.5 **库内去重（p4.2）**：删掉 Redis 冷却位模拟「冷却已过期」，表里已有
    同邮箱的提交仍按 duplicate 拒绝且不落新行——去重判定落在库上。
4.  **per-IP 限流**：第 4 次提交（同一 XFF IP）→ `{ok:false, reason:"rate_limited"}`。
    刻意让去重与限流在同一条请求序列里先后触发——两者都真跑过，而不是各自单测。
5.  **参数边界**：非法邮箱 → 400；mood 越界 → 422。
6.  **admin fail-closed**：MOOD_ADMIN_TOKEN 为空 → 503；错误 token → 401；
    正确 token → 200。返回体**不含 ip 字段**（存它是为了限流，不是给人看的）。
7.  **清理**：测试自建的行（*@moodtest.local）与 Redis 键（rag:mood:*）跑完即删，
    不污染本地库与本地 Redis。

运行：
    make infra                                          # 先起中间件
    .venv/bin/alembic upgrade head                      # 先把迁移打到最新
    .venv/bin/python tests/test_module22_mood.py
"""
import random
import sys
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
# 前置：MySQL / Redis 不可达就整模块 SKIP
# --------------------------------------------------------------------------- #
print("== 前置检查：MySQL / Redis 连通性 ==")
from core import db as db_module

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
_mysql_ok = _conn["ok"]

_redis_ok = False
try:
    from core.redis_store import get_redis_client

    get_redis_client().ping()
    _redis_ok = True
except Exception as e:  # noqa: BLE001
    print(f"  Redis 不可达：{type(e).__name__}: {e}")

if not (_mysql_ok and _redis_ok):
    print()
    print("=" * 66)
    print("  SKIP：MySQL 或 Redis 不可达，模块22 未执行。")
    print("  本地起中间件：make infra      （或 docker compose up -d mysql redis）")
    if not _redis_ok:
        print("  （submit 的双闸依赖 Redis；admin 也读 MySQL）")
    print("=" * 66)
    sys.exit(0)

from fastapi.testclient import TestClient
from sqlalchemy import text

from api.main import app
from core.schema import site_mood_submission_table as MOOD

client = TestClient(app)

# 每次运行用独立 XFF IP：不与之前运行/真实流量的限流计数器互相污染。
TEST_IP = f"203.0.113.{random.randint(2, 254)}"
TEST_IP_2 = f"203.0.113.{random.randint(2, 254)}"
while TEST_IP_2 == TEST_IP:
    TEST_IP_2 = f"203.0.113.{random.randint(2, 254)}"
XFF = {"X-Forwarded-For": TEST_IP}
XFF2 = {"X-Forwarded-For": TEST_IP_2}
DOMAIN = "moodtest.local"


def cleanup() -> None:
    """删掉本模块造的行与键。跑前跑后各一遍：跑前防上一次残留干扰计数。"""
    with db_module.session_scope() as session:
        session.execute(
            text("DELETE FROM site_mood_submission WHERE email LIKE :p"),
            {"p": f"%@{DOMAIN}"},
        )
    client_redis = get_redis_client()
    prefix = (settings.REDIS_KEY_PREFIX or "rag").rstrip(":")
    for pattern in (f"{prefix}:mood:ratelimit:*", f"{prefix}:mood:emaildup:*"):
        keys = list(client_redis.scan_iter(match=pattern))
        if keys:
            client_redis.delete(*keys)


cleanup()
_original_admin_token = settings.MOOD_ADMIN_TOKEN

try:
    # ----------------------------------------------------------------------- #
    # 第 1 组：结构一致性（迁移 DDL ↔ core/schema.py）
    # ----------------------------------------------------------------------- #
    print("== 第 1 组：结构一致性 ==")
    from alembic.autogenerate import compare_metadata  # noqa: E402
    from alembic.migration import MigrationContext  # noqa: E402
    from core.schema import metadata as app_schema_metadata  # noqa: E402

    with db_module.get_engine().connect() as conn:
        _tables = {r[0] for r in conn.execute(text("SHOW TABLES"))}
    check(
        "site_mood_submission 表已建出（先跑 alembic upgrade head）",
        "site_mood_submission" in _tables,
        f"实际表={sorted(_tables)}",
    )
    with db_module.get_engine().connect() as conn:
        _mc = MigrationContext.configure(conn, opts={"compare_type": True})
        _diff = compare_metadata(_mc, app_schema_metadata)
    check(
        "整库与 core/schema.py 完全一致（列/类型/可空/索引/注释）",
        not _diff,
        f"diff={_diff}",
    )

    # ----------------------------------------------------------------------- #
    # 第 2 组：submit —— 正常路径 / 双闸顺序 / 参数边界
    # ----------------------------------------------------------------------- #
    print("== 第 2 组：submit 双闸 ==")

    def count_rows() -> int:
        with db_module.get_engine().connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM site_mood_submission WHERE email LIKE :p"),
                    {"p": f"%@{DOMAIN}"},
                ).scalar()
            )

    _before = count_rows()

    r1 = client.post(
        "/api/v1/mood/submit",
        json={"email": f"happy@{DOMAIN}", "mood": 0.5},
        headers=XFF,
    )
    check("合法提交 → 200", r1.status_code == 200, f"status={r1.status_code} body={r1.text}")
    j1 = r1.json()
    check("合法提交 → ok=true 且有 id", j1.get("ok") is True and isinstance(j1.get("id"), int), f"body={j1}")

    r2 = client.post(
        "/api/v1/mood/submit",
        json={"email": f"HAPPY@{DOMAIN}", "mood": -1.0},
        headers=XFF,
    )
    j2 = r2.json()
    check(
        "同邮箱大小写变体 → duplicate（冷却大小写不敏感）",
        r2.status_code == 200 and j2.get("ok") is False and j2.get("reason") == "duplicate",
        f"status={r2.status_code} body={j2}",
    )
    check("duplicate 不落库", count_rows() == _before + 1, f"rows={count_rows()} 期望 {_before + 1}")

    r3 = client.post(
        "/api/v1/mood/submit",
        json={"email": f"sad@{DOMAIN}", "mood": -0.5},
        headers=XFF,
    )
    j3 = r3.json()
    check("第二个不同邮箱 → ok=true（同 IP 第 3 次仍在限内）", j3.get("ok") is True, f"body={j3}")

    r4 = client.post(
        "/api/v1/mood/submit",
        json={"email": f"third@{DOMAIN}", "mood": 0.0},
        headers=XFF,
    )
    j4 = r4.json()
    check(
        "同 IP 第 4 次 → rate_limited（限流在最外层，被去重拒绝的尝试也计数）",
        r4.status_code == 200 and j4.get("ok") is False and j4.get("reason") == "rate_limited",
        f"status={r4.status_code} body={j4}",
    )
    check("rate_limited 不落库", count_rows() == _before + 2, f"rows={count_rows()} 期望 {_before + 2}")

    r5 = client.post(
        "/api/v1/mood/submit",
        json={"email": "not-an-email", "mood": 0.0},
        headers=XFF,
    )
    check("非法邮箱 → 400（参数校验不消耗限流额度）", r5.status_code == 400, f"status={r5.status_code}")

    r6 = client.post(
        "/api/v1/mood/submit",
        json={"email": f"x@{DOMAIN}", "mood": 1.5},
        headers=XFF,
    )
    check("mood 越界（1.5）→ 422", r6.status_code == 422, f"status={r6.status_code}")

    # ----------------------------------------------------------------------- #
    # 第 3 组：mood_word 派生 + 库里字段
    # ----------------------------------------------------------------------- #
    print("== 第 3 组：落库字段 ==")
    with db_module.get_engine().connect() as conn:
        _rows = conn.execute(
            text(
                "SELECT email, mood, mood_word, ip FROM site_mood_submission "
                "WHERE email LIKE :p ORDER BY id"
            ),
            {"p": f"%@{DOMAIN}"},
        ).all()
    _by_email = {r[0]: r for r in _rows}
    _happy = _by_email.get(f"happy@{DOMAIN}")
    _sad = _by_email.get(f"sad@{DOMAIN}")
    check("mood=0.5 → 派生 Happy", _happy is not None and _happy[2] == "Happy", f"row={_happy}")
    check("mood=-0.5 → 派生 Sad", _sad is not None and _sad[2] == "Sad", f"row={_sad}")
    check("ip 落库用的是 XFF 第一跳", _happy is not None and _happy[3] == TEST_IP, f"ip={_happy[3] if _happy else None}")

    # 第二个 IP 不受第一个 IP 的限流影响
    r7 = client.post(
        "/api/v1/mood/submit",
        json={"email": f"other-ip@{DOMAIN}", "mood": 0.0},
        headers=XFF2,
    )
    check("另一个 IP → 正常通过（限流按 IP 隔离）", r7.json().get("ok") is True, f"body={r7.json()}")

    # ----------------------------------------------------------------------- #
    # 第 3.5 组：库内去重（p4.2）—— 冷却过期后，表里已有同邮箱也算重复
    # ----------------------------------------------------------------------- #
    print("== 第 3.5 组：库内去重 ==")
    from api.routes.mood import _email_dedup_key  # noqa: E402

    # 删掉 happy@ 的 Redis 冷却位，模拟「10 分钟冷却窗口已过期」
    get_redis_client().delete(_email_dedup_key(f"happy@{DOMAIN}"))
    _rows_before_dbdup = count_rows()
    r8b = client.post(
        "/api/v1/mood/submit",
        json={"email": f"happy@{DOMAIN}", "mood": -0.8},
        headers=XFF2,
    )
    j8b = r8b.json()
    check(
        "冷却位已删但库内已有同邮箱 → duplicate（去重判定落在库上）",
        r8b.status_code == 200 and j8b.get("ok") is False and j8b.get("reason") == "duplicate",
        f"status={r8b.status_code} body={j8b}",
    )
    check(
        "库内去重拒绝不落新行",
        count_rows() == _rows_before_dbdup,
        f"rows={count_rows()} 期望 {_rows_before_dbdup}",
    )

    # ----------------------------------------------------------------------- #
    # 第 4 组：admin —— fail-closed 三态 + 返回体不泄 PII
    # ----------------------------------------------------------------------- #
    print("== 第 4 组：admin 查看口 ==")
    r8 = client.get("/api/v1/mood/admin")
    check(
        "MOOD_ADMIN_TOKEN 为空 → 503（fail-closed，不是「不校验」）",
        r8.status_code == 503,
        f"status={r8.status_code}",
    )

    settings.MOOD_ADMIN_TOKEN = "module22-admin-token"
    try:
        r9 = client.get("/api/v1/mood/admin")
        check("已配 token 但未提供 → 401", r9.status_code == 401, f"status={r9.status_code}")

        r10 = client.get("/api/v1/mood/admin", params={"token": "wrong-token"})
        check("错误 token → 401", r10.status_code == 401, f"status={r10.status_code}")

        r11 = client.get("/api/v1/mood/admin", params={"token": "module22-admin-token", "limit": 50})
        check("正确 token → 200", r11.status_code == 200, f"status={r11.status_code}")
        j11 = r11.json()
        check(
            "admin 返回 total/returned/items 三段",
            isinstance(j11.get("total"), int) and isinstance(j11.get("items"), list),
            f"keys={sorted(j11.keys())}",
        )
        _leak = [k for k in (j11.get("items") or [{}])[0].keys() if k == "ip"]
        check("admin 返回体不含 ip 字段", not _leak, f"keys={sorted((j11.get('items') or [{}])[0].keys())}")
        check(
            "admin 能看到本模块造的行",
            j11.get("total", 0) >= 3,
            f"total={j11.get('total')}",
        )
    finally:
        settings.MOOD_ADMIN_TOKEN = _original_admin_token

finally:
    cleanup()
    settings.MOOD_ADMIN_TOKEN = _original_admin_token

print()
print("=" * 66)
print(f"  模块22 完成：PASS={PASS} FAIL={FAIL}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
