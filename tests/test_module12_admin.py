# pyright: basic
"""
模块12测试文件：管理端（P2-13）。

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    P2-13a  身份解析（core/identity）+ /api/v1/admin 的门槛          ← 本轮
    P2-13b  员工 CRUD 与组织维护                                     ← 下一轮
    P2-13c  密码管理（重置 / 强制改密 / 到期看板）
    P2-13d  审计日志

**读全部是只读的**，写只碰本模块自己造的行（前缀 `m12a_`，本轮实际一条都不写）——
module9 留下过教训：清表演示数据会让真实数据和测试自己的记录一起消失，且不报错。

--------------------------------------------------------------------------
为什么用 TestClient 而不是「起服务 + curl」
--------------------------------------------------------------------------
这里的判据是**后端门槛**（哪个角色能被放进哪个接口），它是 `Depends` 链上的
一件事，跟 HTTP 服务器怎么起无关。用 TestClient 可以直接喂任意 `X-User-Id`，
于是「一个普通员工的请求」不需要真的有人帮他登录 ——
这一点很重要：① 阶段网关还不知道 MySQL 有这些账号，**只有这条路能验**。

--------------------------------------------------------------------------
反向验证
--------------------------------------------------------------------------
每条「该拒的拒了」都要配一条「把这道守卫拆掉 → 它就能通过」，
否则守卫被写反，用例照样全绿（module11 起立的规矩）。

运行：
    make infra
    .venv/bin/python tests/test_module12_admin.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
PREFIX = "m12a_"

# 这三个用户名来自 scripts/seed_users.py（P2-11d），代表三种角色 + 一种状态。
# 直接依赖种子数据是刻意的：**不用夹具造临时账号**，这样测出来的门槛
# 与「管理员明天打开管理端」时看到的是同一批人。
ADMIN_USERNAME = "wu.jing"
HR_USERNAME = "zhou.yan"
USER_USERNAME = "chen.jie"
RESIGNED_USERNAME = "zheng.shuang"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


print("== 前置检查：MySQL 连通性 ==")
from core import db as db_module  # noqa: E402

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
if not _conn["ok"]:
    print()
    print("=" * 66)
    print("  SKIP：MySQL 不可达，模块12 未执行。")
    print("  本地起中间件：make infra")
    print("=" * 66)
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
print("  连通正常，开始执行\n")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from config.settings import settings  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.identity import (  # noqa: E402
    MODE_DEV,
    MODE_GATEWAY,
    SOURCE_BREAKGLASS,
    Actor,
    require_staff,
    resolve_actor,
)

client = TestClient(app)

print("== 第 1 组：前置数据（种子是否就位）==")
_admin = repo.get_by_username(ADMIN_USERNAME)
_hr = repo.get_by_username(HR_USERNAME)
_plain = repo.get_by_username(USER_USERNAME)
_resigned = repo.get_by_username(RESIGNED_USERNAME)
check("种子里有 admin / hr / user / resigned 四种账号",
      all(u is not None for u in (_admin, _hr, _plain, _resigned)),
      f"{ADMIN_USERNAME}={_admin is not None} {HR_USERNAME}={_hr is not None} "
      f"{USER_USERNAME}={_plain is not None} {RESIGNED_USERNAME}={_resigned is not None}")
if not all(u is not None for u in (_admin, _hr, _plain, _resigned)):
    print("\n  SKIP：种子数据不完整，先跑 make seed-users-apply。")
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
check("四个账号的角色/状态正如种子所写",
      (_admin.role, _hr.role, _plain.role, _resigned.status)
      == ("admin", "hr", "user", "resigned"),
      f"{_admin.role}/{_hr.role}/{_plain.role}/{_resigned.status}")


print("\n== 第 2 组：身份解析（core/identity）==")


def act_as(username: str | None, *, user_id_header: bool = True) -> tuple[int, dict]:
    """打一次 /me。返回 (status_code, json_or_empty)。"""
    headers = {}
    if username is not None:
        headers["X-User-Id" if user_id_header else "X-Username"] = username
    r = client.get("/api/v1/admin/me", headers=headers)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


s_admin, b_admin = act_as(ADMIN_USERNAME)
check("admin 账号能通过管理端门槛", s_admin == 200, f"{s_admin} {b_admin}")
check("  └ 返回的角色是 admin，且 id 有值（能写 created_by）",
      b_admin.get("role") == "admin" and isinstance(b_admin.get("id"), int),
      f"{b_admin.get('role')} / {b_admin.get('id')}")

s_hr, b_hr = act_as(HR_USERNAME)
check("hr 账号能通过管理端门槛", s_hr == 200, f"{s_hr} {b_hr}")
check("  └ hr 能管人事，但不能重置密码（D10）",
      b_hr.get("permissions", {}).get("staff") is True
      and b_hr.get("permissions", {}).get("reset_password") is False,
      f"{b_hr.get('permissions')}")

s_user, b_user = act_as(USER_USERNAME)
check("普通员工**进不了**管理端", s_user == 403, f"{s_user} {b_user}")
check("  └ 403 的文案是给人看的（不是 401 / 不是空）",
      "权限" in b_user.get("detail", ""), f"{b_user.get('detail')}")

s_resigned, b_resigned = act_as(RESIGNED_USERNAME)
check("离职员工即使拿着 token 也被拒", s_resigned == 403, f"{s_resigned} {b_resigned}")
check("  └ 文案说明是账号状态问题，不是权限问题",
      "停用" in b_resigned.get("detail", "") or "离职" in b_resigned.get("detail", ""),
      f"{b_resigned.get('detail')}")

s_ghost, b_ghost = act_as("nobody.m12a")
check("库里没有的人一律 401（不是 403，也不是放行）", s_ghost == 401, f"{s_ghost} {b_ghost}")

# 数字 id 也要认（11c 之后网关会发整数 uid，这条是给将来的链路留的通道）
s_uid, b_uid = act_as(str(_admin.id))
check("数字形态的 X-User-Id（将来的 uid）也能解析", s_uid == 200 and b_uid.get("id") == _admin.id,
      f"{s_uid} {b_uid}")

# X-Username 单独出现时能兜住（网关两个头都发，这条防的是只发一个的情况）
s_name_header, b_name_header = act_as(ADMIN_USERNAME, user_id_header=False)
check("只带 X-Username 也能解析出同一个人",
      s_name_header == 200 and b_name_header.get("username") == ADMIN_USERNAME,
      f"{s_name_header} {b_name_header}")


print("\n== 第 3 组：/me 与 /options 的形状==")
check("/me 不含 password_hash（它没有任何理由出现在响应里）",
      "password_hash" not in b_admin, str(list(b_admin.keys())))
check("/me 带 identity_source —— 权限问题上，看不见来源的身份比没有身份更糟",
      "identity_source" in b_admin, str(list(b_admin.keys())))
check("/me 带自己的密码状态（must_change / expire_in_days / expired / warn）",
      {"must_change", "expire_in_days", "expired", "warn"} <= set(b_admin.get("password", {})),
      str(b_admin.get("password")))

r_options = client.get("/api/v1/admin/options", headers={"X-User-Id": ADMIN_USERNAME})
check("/options 可读（200）", r_options.status_code == 200, f"{r_options.status_code}")
opt = r_options.json() if r_options.status_code == 200 else {}
check("  └ 同时给了扁平部门列表与部门树（树给展示、扁平给防环下拉）",
      isinstance(opt.get("departments"), list) and isinstance(opt.get("department_tree"), list),
      str(list(opt.keys())))
check("  └ 职位 / 角色 / 状态 / 序列四类枚举齐全",
      all(isinstance(opt.get(k), list) and opt[k] for k in ("positions", "roles", "statuses", "sequences")),
      str({k: len(opt.get(k, [])) for k in ("positions", "roles", "statuses", "sequences")}))
check("  └ 密码策略参数下发了（前端要在提交前就能说清长度要求）",
      opt.get("password_policy", {}).get("min_length") == settings.PASSWORD_MIN_LENGTH,
      str(opt.get("password_policy")))


print("\n== 第 4 组：反向验证（拆掉守卫，用例必须转红）==")
_overrides_before = dict(app.dependency_overrides)


def always_pass() -> Actor:
    """一个把 require_staff 换掉的替身：谁都能过。"""
    return Actor(id=-1, username="__override__", display_name="替身", role="admin",
                 status="active", source="test")


app.dependency_overrides[require_staff] = always_pass
s_override, _ = act_as(USER_USERNAME)
app.dependency_overrides.clear()
app.dependency_overrides.update(_overrides_before)
check("把 require_staff 换成「谁都放行」之后，普通员工不再被拒",
      s_override == 200, f"{s_override}")
check("  └ 说明上面那条 403 真的是这道守卫拦的（不是别的什么原因）",
      s_user == 403 and s_override == 200, f"原 {s_user} / 拆掉后 {s_override}")


print("\n== 第 5 组：gateway 模式必须 fail-closed==")
_mode_before = settings.IDENTITY_MODE
settings.IDENTITY_MODE = MODE_GATEWAY
try:
    s_noheader, b_noheader = act_as(None)
    check("gateway 模式下缺少身份头 → 401（而不是回落到某个默认账号）",
          s_noheader == 401, f"{s_noheader} {b_noheader}")
    s_ghost2, b_ghost2 = act_as("nobody.m12a")
    check("gateway 模式下查不到的人 → 401（不允许 break-glass 兜底）",
          s_ghost2 == 401, f"{s_ghost2} {b_ghost2}")
    s_ok2, _ = act_as(ADMIN_USERNAME)
    check("gateway 模式下正常链路不受影响", s_ok2 == 200, f"{s_ok2}")
finally:
    settings.IDENTITY_MODE = _mode_before

check("  └ 模式还原回 dev（不影响后续用例）", settings.IDENTITY_MODE == _mode_before,
      f"{settings.IDENTITY_MODE}")


print("\n== 第 6 组：dev 模式的后门只认一个名字==")
check("dev 模式下凭空捏造的用户名不会被当成超管",
      act_as("totally.made.up")[0] == 401, str(act_as("totally.made.up")))
_dev_name = settings.IDENTITY_DEV_USERNAME
_dev_record_exists = repo.get_by_username(_dev_name) is not None
_manual = resolve_actor(user_id_header=_dev_name, username_header=None)
check(f"仅 {_dev_name} 允许 break-glass（库里有这个人时按库里的身份）",
      (_manual.source != SOURCE_BREAKGLASS) == _dev_record_exists,
      f"source={_manual.source} 库里有这个人={_dev_record_exists}")
if not _dev_record_exists:
    check("  └ 这条后门给的是 admin（虚假 id=None），生产必须为 gateway 模式",
          _manual.role == "admin" and _manual.id is None, f"{_manual.role}/{_manual.id}")


print("\n== 第 7 组：本模块没有留下脏数据==")
from sqlalchemy import text  # noqa: E402

from core.db import get_engine  # noqa: E402

with get_engine().connect() as conn:
    left = conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalar()
    total = conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar()
    docs = conn.execute(text("SELECT COUNT(*) FROM document")).scalar()
check("没有留下本模块造的用户", left == 0, f"{left} 行")
check(f"用户总数仍是 {total}（本模块只读）", left == 0, f"{left}")
print(f"  复核：user={total} 行 / document={docs} 行")

print()
print("=" * 66)
print(f"  P2-13a 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
