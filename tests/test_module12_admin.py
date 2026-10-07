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

# 「种子还在」的完整名单 —— 用来做收尾断言。
# ⚠️ 这里**刻意不用** `SELECT COUNT(*) FROM user == 10`：
# 那是拿「全表总数」当不变量，而全表是**共享状态**——任何一个并发跑着的
# 脚本（比如 module11 正在造自己的账号）都会把它顶上去，
# 于是本模块明明全绿、却因为别人的数据变红（实测并行跑时就是这样）。
# 不变量要说「这 10 个人还在」：既不受别人新增影响，也更严格（认名字，不认数量）。
SEED_USERNAMES = (
    "wu.jing", "zhou.yan", "chen.jie", "zhao.min", "sun.lei",
    "li.na", "zhang.wei", "wang.fang", "xu.hao", "zheng.shuang",
)


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
from sqlalchemy import text  # noqa: E402

from api.main import app  # noqa: E402

# --------------------------------------------------------------------------- #
# 审计基线（P2-13d）
# --------------------------------------------------------------------------- #
# 本脚本的动作现在**都会落审计**，所以它必须像清理 user / department 那样清理审计 ——
# 否则 `make test` 每跑一次就在真审计表里堆一批 m12a_tmp 的垃圾，
# 而审计表恰恰是「出事那天要靠它」的那张表，不能被测试数据淹没。
#
# 记基线而不是按业务键删：审计行的 target_label 会被对象改名带着变
# （改过的员工标签是「m12a_tmp（改过名字）」），按前缀 LIKE 会漏。
# 表可能还不存在（迁移 0005 没跑），那时记 None，收尾也不清。
try:
    with db_module.get_engine().connect() as _c:
        AUDIT_BASELINE_MAX_ID = int(
            _c.execute(text("SELECT COALESCE(MAX(id), 0) FROM audit_log")).scalar() or 0
        )
except Exception:  # noqa: BLE001 - 表不存在属于「还没迁移」，不是错误
    AUDIT_BASELINE_MAX_ID = None
from config.settings import settings  # noqa: E402
from core import password_policy as policy  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.db import get_engine, now_db  # noqa: E402
from core.identity import (  # noqa: E402
    MODE_DEV,
    MODE_GATEWAY,
    SOURCE_BREAKGLASS,
    Actor,
    require_admin,
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


print("\n== 第 7 组：员工 CRUD（P2-13b）==")

# 本模块造的人统一前缀，清理只按前缀删
TMP_USER = f"{PREFIX}tmp"
TMP_EMP = f"{PREFIX}E1"
TMP_DEPT = f"{PREFIX}dept"
TMP_POS = f"{PREFIX}pos"


def cleanup_fixtures() -> None:
    """删掉本模块造的所有东西。**只按业务键删**，绝不按全表清空。"""
    with get_engine().begin() as conn:
        conn.execute(text(
            "DELETE p FROM user_password_history p JOIN `user` u ON u.id = p.user_id "
            "WHERE u.username LIKE :p"), {"p": f"{PREFIX}%"})
        conn.execute(text("UPDATE `user` SET department_id = NULL WHERE username LIKE :p"),
                     {"p": f"{PREFIX}%"})
        conn.execute(text("UPDATE `user` SET position_id = NULL WHERE position_id IN "
                          "(SELECT id FROM position WHERE code LIKE :p)"), {"p": f"{PREFIX}%"})
        conn.execute(text("DELETE FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
        conn.execute(text("DELETE FROM department WHERE code LIKE :p"), {"p": f"{PREFIX}%"})
        conn.execute(text("DELETE FROM position WHERE code LIKE :p"), {"p": f"{PREFIX}%"})


cleanup_fixtures()

_h = {"X-User-Id": ADMIN_USERNAME}
_r = client.post("/api/v1/admin/users", json={
    "username": TMP_USER, "employee_no": TMP_EMP, "display_name": "模块12临时员工",
    "department_id": _admin.department_id,
}, headers=_h)
check("管理员能建号（201）", _r.status_code == 201, f"{_r.status_code} {_r.text[:120]}")
_created = _r.json() if _r.status_code == 201 else {}
_tmp_pwd = _created.get("temporary_password", "")
check("  └ 响应里带一个一次性临时密码", bool(_tmp_pwd), str(list(_created.keys())))
check("  └ 新员工 must_change_password=1（首次登录必须改密）",
      _created.get("user", {}).get("must_change_password") is True,
      str(_created.get("user", {}).get("must_change_password")))
check("  └ 临时密码通过了强度校验（不是随便拼的一串）",
      not policy.validate_strength(_tmp_pwd, username=TMP_USER, employee_no=TMP_EMP),
      str(policy.validate_strength(_tmp_pwd, username=TMP_USER, employee_no=TMP_EMP)))
check("  └ 明文密码不在响应体的其它位置重复出现，也不含 password_hash",
      "password_hash" not in _created.get("user", {}), str(list(_created.get("user", {}))))
_tmp_id = _created.get("user", {}).get("id")
with get_engine().connect() as conn:
    _hist = conn.execute(text("SELECT COUNT(*) FROM user_password_history WHERE user_id=:i"),
                         {"i": _tmp_id}).scalar()
check("  └ 建号写了一条改密历史（所以他首次改密不能用同一个密码）", _hist == 1, f"{_hist}")

# ---- 唯一键冲突必须是人话 ----
_r2 = client.post("/api/v1/admin/users", json={
    "username": TMP_USER, "employee_no": f"{PREFIX}E2",
}, headers=_h)
check("重复登录名 → 409，且报错是中文人话（不是数据库原文）",
      _r2.status_code == 409 and "登录名" in _r2.json().get("detail", ""),
      f"{_r2.status_code} {_r2.json().get('detail')}")
_r3 = client.post("/api/v1/admin/users", json={
    "username": f"{PREFIX}other", "employee_no": TMP_EMP,
}, headers=_h)
check("重复工号 → 409，同样是人话",
      _r3.status_code == 409 and "工号" in _r3.json().get("detail", ""),
      f"{_r3.status_code} {_r3.json().get('detail')}")
_r3b = client.post("/api/v1/admin/users", json={
    "username": f"{PREFIX}other", "employee_no": f"{PREFIX}E3",
    "email": _admin.email,
}, headers=_h)
check("重复邮箱 → 409（三个唯一键都要翻得出来）",
      _r3b.status_code == 409 and "邮箱" in _r3b.json().get("detail", ""),
      f"{_r3b.status_code} {_r3b.json().get('detail')}")

# ---- 改资料 / 状态 / 角色 ----
_r4 = client.patch(f"/api/v1/admin/users/{_tmp_id}", json={"display_name": "改过名字"},
                   headers=_h)
check("改资料生效", _r4.status_code == 200 and _r4.json().get("display_name") == "改过名字",
      f"{_r4.status_code} {_r4.text[:100]}")

_r5 = client.patch(f"/api/v1/admin/users/{_tmp_id}/status", json={"status": "disabled"},
                   headers=_h)
check("停用生效，且 token_version 变大了（他手上的 token 立刻失效）",
      _r5.status_code == 200 and _r5.json().get("status") == "disabled"
      and _r5.json().get("token_version", 0) >= 1,
      f"{_r5.status_code} {_r5.json()}")
_r5b = client.patch(f"/api/v1/admin/users/{_tmp_id}/status", json={"status": "resigned"},
                    headers=_h)
check("离职走 status=resigned —— **不删行**（他名下还有会话与文档）",
      _r5b.status_code == 200 and _r5b.json().get("status") == "resigned",
      f"{_r5b.status_code}")
with get_engine().connect() as conn:
    _still = conn.execute(text("SELECT COUNT(*) FROM `user` WHERE id=:i"), {"i": _tmp_id}).scalar()
check("  └ 行还在库里（离职 ≠ 删除）", _still == 1, f"{_still}")

_r6 = client.patch(f"/api/v1/admin/users/{_admin.id}/status", json={"status": "disabled"},
                   headers=_h)
check("不能停用自己（否则一把就把自己关在门外）", _r6.status_code == 403,
      f"{_r6.status_code} {_r6.text[:80]}")
_r6b = client.patch(f"/api/v1/admin/users/{_admin.id}/role", json={"role": "user"}, headers=_h)
check("不能给自己降权", _r6b.status_code == 403, f"{_r6b.status_code}")

# hr 能改资料、不能改角色
_hr_h = {"X-User-Id": HR_USERNAME}
check("hr 能改员工资料（人事的本职）",
      client.patch(f"/api/v1/admin/users/{_tmp_id}", json={"phone": "13800000000"},
                   headers=_hr_h).status_code == 200)
check("hr **不能**改角色（只有系统管理员能）",
      client.patch(f"/api/v1/admin/users/{_tmp_id}/role", json={"role": "admin"},
                   headers=_hr_h).status_code == 403)

# ---- 部门 / 职位 ----
_r7 = client.post("/api/v1/admin/departments", json={
    "code": TMP_DEPT, "name": "模块12临时部门", "parent_id": None}, headers=_h)
check("新建部门成功", _r7.status_code == 201, f"{_r7.status_code} {_r7.text[:100]}")
_tmp_dept_id = _r7.json().get("id")
_r8 = client.post("/api/v1/admin/departments", json={
    "code": f"{TMP_DEPT}2", "name": "子部门", "parent_id": _tmp_dept_id}, headers=_h)
_sub_id = _r8.json().get("id")
_r9 = client.patch(f"/api/v1/admin/departments/{_tmp_dept_id}", json={"parent_id": _sub_id},
                   headers=_h)
check("**防环**：把上级设成自己的子孙被拒",
      _r9.status_code == 400 and "环" in _r9.json().get("detail", ""),
      f"{_r9.status_code} {_r9.json().get('detail')}")
_r9b = client.patch(f"/api/v1/admin/departments/{_tmp_dept_id}", json={"parent_id": _tmp_dept_id},
                    headers=_h)
check("  └ 把上级设成自己也被拒", _r9b.status_code == 400, f"{_r9b.status_code}")

_r10 = client.delete(f"/api/v1/admin/departments/{_tmp_dept_id}", headers=_h)
check("有子部门时不允许删（否则下级的 parent_id 会悬空）",
      _r10.status_code == 400 and "下级" in _r10.json().get("detail", ""),
      f"{_r10.status_code} {_r10.json().get('detail')}")
client.delete(f"/api/v1/admin/departments/{_sub_id}", headers=_h)
_r10b = client.delete(f"/api/v1/admin/departments/{_tmp_dept_id}", headers=_h)
check("  └ 子部门移走后可以删", _r10b.status_code == 200, f"{_r10b.status_code}")

_r11 = client.post("/api/v1/admin/positions", json={
    "code": TMP_POS, "name": "模块12临时职位", "level": "P9", "sequence": "tech"}, headers=_h)
check("新建职位成功", _r11.status_code == 201, f"{_r11.status_code} {_r11.text[:100]}")
_tmp_pos_id = _r11.json().get("id")
client.patch(f"/api/v1/admin/users/{_tmp_id}", json={"position_id": _tmp_pos_id}, headers=_h)
_r12 = client.delete(f"/api/v1/admin/positions/{_tmp_pos_id}", headers=_h)
check("有人挂着的职位不能删（否则那个人的职位会悬空）",
      _r12.status_code == 400 and "挂着" in _r12.json().get("detail", ""),
      f"{_r12.status_code} {_r12.json().get('detail')}")
client.patch(f"/api/v1/admin/users/{_tmp_id}", json={"position_id": None}, headers=_h)
check("  └ 人员挪走后可以删",
      client.delete(f"/api/v1/admin/positions/{_tmp_pos_id}", headers=_h).status_code == 200)


print("\n== 第 8 组：密码管理（P2-13c）==")
_r13 = client.post(f"/api/v1/admin/users/{_admin.id}/password/reset", headers=_h)
check("不能重置自己的密码（那是主应用「我的账号」的事）",
      _r13.status_code == 403, f"{_r13.status_code} {_r13.text[:80]}")
_r14 = client.post(f"/api/v1/admin/users/{_plain.id}/password/reset", headers=_hr_h)
check("hr **不能**重置他人密码（D10）", _r14.status_code == 403, f"{_r14.status_code}")

# 反向验证：把 require_admin 换成「谁都放行」，hr 那条 403 必须转 200
_overrides_before = dict(app.dependency_overrides)
app.dependency_overrides[require_admin] = lambda: Actor(
    id=-1, username="__override__", display_name="替身", role="admin",
    status="active", source="test")
_r14b = client.post(f"/api/v1/admin/users/{_plain.id}/password/reset", headers=_hr_h)
app.dependency_overrides.clear()
app.dependency_overrides.update(_overrides_before)
check("反向验证：拆掉 require_admin 后 hr 能重置 → 证明 403 来自这道守卫",
      _r14b.status_code == 200, f"{_r14b.status_code} {_r14b.text[:80]}")

# 用临时员工验重置（不碰种子员工的密码）
client.patch(f"/api/v1/admin/users/{_tmp_id}/status", json={"status": "active"}, headers=_h)
_before = repo.get(_tmp_id)
_r15 = client.post(f"/api/v1/admin/users/{_tmp_id}/password/reset", headers=_h)
check("管理员能重置密码（200）", _r15.status_code == 200, f"{_r15.status_code} {_r15.text[:100]}")
_new_pwd = _r15.json().get("temporary_password", "")
_after = repo.get(_tmp_id)
check("  └ 重置后 must_change_password=1", _after.must_change_password is True,
      f"{_after.must_change_password}")
check("  └ 重置会 token_version +1（其他设备上的旧 token 立刻失效）",
      _after.token_version > _before.token_version,
      f"{_before.token_version} → {_after.token_version}")
check("  └ 新密码 ≠ 旧密码，且通过了强度校验",
      bool(_new_pwd) and _new_pwd != _tmp_pwd
      and not policy.validate_strength(_new_pwd, username=TMP_USER, employee_no=TMP_EMP),
      f"{_new_pwd}")
with get_engine().connect() as conn:
    _hist2 = conn.execute(text("SELECT COUNT(*) FROM user_password_history WHERE user_id=:i"),
                          {"i": _tmp_id}).scalar()
check("  └ 又写了一条改密历史", _hist2 == 2, f"{_hist2}")

# 离职员工不签发
client.patch(f"/api/v1/admin/users/{_tmp_id}/status", json={"status": "resigned"}, headers=_h)
_r16 = client.post(f"/api/v1/admin/users/{_tmp_id}/password/reset", headers=_h)
check("已离职的员工不签发新密码（给他发了也没用，还会污染看板）",
      _r16.status_code == 400, f"{_r16.status_code} {_r16.text[:80]}")

# 强制改密开关
_r17 = client.patch(f"/api/v1/admin/users/{_tmp_id}/password/must-change",
                    json={"must_change": True}, headers=_h)
check("强制改密开关可以打开", _r17.status_code == 200, f"{_r17.status_code}")
_mc_before = repo.get(_tmp_id).password_changed_at
client.patch(f"/api/v1/admin/users/{_tmp_id}/password/must-change",
             json={"must_change": False}, headers=_h)
check("  └ 这个开关**不给密码续期**（password_changed_at 不动）",
      repo.get(_tmp_id).password_changed_at == _mc_before,
      f"{_mc_before} → {repo.get(_tmp_id).password_changed_at}")


print("\n== 第 9 组：到期看板的数字与 SQL 对得上 ==")
_board = client.get("/api/v1/admin/password/board", headers=_h)
check("看板可读（200）", _board.status_code == 200, f"{_board.status_code}")
b = _board.json() if _board.status_code == 200 else {}
counts = b.get("counts", {})
_now = now_db()
_expire_secs = settings.PASSWORD_EXPIRE_DAYS * 86400
_warn_floor = (settings.PASSWORD_EXPIRE_DAYS - settings.PASSWORD_EXPIRE_WARN_DAYS) * 86400
with get_engine().connect() as conn:
    rows = conn.execute(text(f"""
        SELECT CASE
                 WHEN locked_until IS NOT NULL AND locked_until > :now THEN 'locked'
                 WHEN must_change_password = 1 THEN 'must_change'
                 WHEN password_changed_at IS NULL
                      OR TIMESTAMPDIFF(SECOND, password_changed_at, :now) > :expire THEN 'expired'
                 WHEN TIMESTAMPDIFF(SECOND, password_changed_at, :now) >= :warn_floor THEN 'expiring'
                 WHEN last_login_at IS NULL
                      OR TIMESTAMPDIFF(DAY, last_login_at, :now) >= :stale THEN 'stale'
                 ELSE 'ok' END AS bucket, COUNT(*) AS n
        FROM `user` WHERE status = 'active' GROUP BY bucket
    """), {"now": _now, "expire": _expire_secs, "warn_floor": _warn_floor, "stale": 90}).all()
sql_counts = {r[0]: int(r[1]) for r in rows}
check("五个桶的人数与 SQL 逐桶一致（服务层的判定口径 == SQL 口径）",
      counts.get("locked", 0) == sql_counts.get("locked", 0)
      and counts.get("must_change", 0) == sql_counts.get("must_change", 0)
      and counts.get("expired", 0) == sql_counts.get("expired", 0)
      and counts.get("expiring_soon", 0) == sql_counts.get("expiring", 0)
      and counts.get("stale_login", 0) == sql_counts.get("stale", 0),
      f"看板={counts} / SQL={sql_counts}")
_buckets_all = sum(counts.get(k, 0) for k in
                   ("must_change", "expiring_soon", "expired", "stale_login", "locked"))
_active_total = sum(sql_counts.values())
check("五个桶**互不包含**：加总不超过在职人数（一个人只落一个桶）",
      _buckets_all <= _active_total, f"五桶合计 {_buckets_all} / 在职 {_active_total}")
check("看板只统计在职的人（离职/停用本来就登不进来，进去只会干扰判断）",
      all(str(u.get("status")) == "active"
          for bucket in ("must_change", "expiring_soon", "expired", "stale_login", "locked")
          for u in b.get(bucket, [])),
      "发现非 active 的人")

print("\n== 第 10 组：清理与零残留==")
cleanup_fixtures()
with get_engine().connect() as conn:
    left_user = conn.execute(text("SELECT COUNT(*) FROM `user` WHERE username LIKE :p"),
                             {"p": f"{PREFIX}%"}).scalar()
    left_dept = conn.execute(text("SELECT COUNT(*) FROM department WHERE code LIKE :p"),
                             {"p": f"{PREFIX}%"}).scalar()
    left_pos = conn.execute(text("SELECT COUNT(*) FROM position WHERE code LIKE :p"),
                            {"p": f"{PREFIX}%"}).scalar()
    total = conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar()
    docs = conn.execute(text("SELECT COUNT(*) FROM document")).scalar()
    # 逐个绑定而不是塞一个 tuple：MySQL 驱动不认「Python tuple」这个类型，
    # 而 expanding bindparam 又要多 import 一层 —— 名单是写死的常量，拼占位符最直白。
    slots = ", ".join(f":n{i}" for i in range(len(SEED_USERNAMES)))
    params = {f"n{i}": name for i, name in enumerate(SEED_USERNAMES)}
    present = {
        row[0]
        for row in conn.execute(
            text(f"SELECT username FROM `user` WHERE username IN ({slots})"), params
        )
    }
check("本模块造的用户已清干净", left_user == 0, f"{left_user} 行")
check("本模块造的部门已清干净", left_dept == 0, f"{left_dept} 行")
check("本模块造的职位已清干净", left_pos == 0, f"{left_pos} 行")
missing = [n for n in SEED_USERNAMES if n not in present]
check(f"种子员工一个不少（{len(SEED_USERNAMES)} 人）", not missing, f"缺 {missing}")
print(f"  复核：user={total} 行 / document={docs} 行")

# ---- 审计也要清（P2-13d）----
# 这一段本身就是「测试自己删除审计」—— 与 audit_repo 声称的「不可改不可删」不矛盾：
# 那条约束的对象是**应用层接口**，这里是测试用 SQL 清理自己造的夹具行。
if AUDIT_BASELINE_MAX_ID is not None:
    with get_engine().connect() as conn:
        made_audit = int(conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE id > :b"),
            {"b": AUDIT_BASELINE_MAX_ID},
        ).scalar() or 0)
        conn.execute(text("DELETE FROM audit_log WHERE id > :b"), {"b": AUDIT_BASELINE_MAX_ID})
        conn.commit()
    with get_engine().connect() as conn:
        left_audit = int(conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE id > :b"),
            {"b": AUDIT_BASELINE_MAX_ID},
        ).scalar() or 0)
    # 两条都要：删之前得确实验证过「本模块确实造了审计」，
    # 否则清理逻辑坏掉（永远删 0 行）也会显示全绿 —— 那是第 49 条坑的形状。
    check("本模块的动作确实落了审计（否则下面的清理断言在空转）",
          made_audit > 0, f"{made_audit} 行")
    check("本模块造的审计行已清干净（否则真审计会被测试数据淹没）",
          left_audit == 0, f"{left_audit} 行")

print()
print("=" * 66)
print(f"  P2-13 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
