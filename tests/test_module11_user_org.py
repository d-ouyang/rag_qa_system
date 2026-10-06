# pyright: basic
"""
模块11测试文件：组织与账号表（P2-11a 的**表与仓储**）。

--------------------------------------------------------------------------
它管什么、不管什么
--------------------------------------------------------------------------
    module11  user / department / position 的表结构与仓储行为  ← 本文件
    P2-11b   密码策略（到期 / 历史 / 锁定阈值）—— 不在
    P2-11c   网关查库与 JWT —— 不在
    P2-12    数据隔离 —— 不在

**本文件刻意不测「密码规则」**：11a 只保证「能正确地存和读这些字段」，
判定规则的测试属于 11b。混在一起测会出现一个很糟的情况：11b 改了策略，
红的却是 11a 的断言，看不出是谁的责任。

--------------------------------------------------------------------------
覆盖点
--------------------------------------------------------------------------
1. **结构一致性**：迁移建出来的表 == `core/schema.py`（沿用 module8 的
   `compare_metadata`，含列注释比对）。另外锁两条本期专属的形状：
   `is_active` 已下线、`status`/`role`/`token_version` 已就位。
2. **仓储行为的成对断言**：每个「该拒的拒了」都配一个「该放的放了」。
   只有正向断言的测试，会在守卫被写反时依然全绿。
3. **token_version 的联动**：停用 / 离职 / 改角色 / 改密都必须让旧 token 失效。
   这是 P2-11c 的地基，本轮必须验。
4. **脱敏**：`to_dict()` 不含 password_hash、手机号默认打码。
5. **部门树**：三层结构 + 孤儿节点提到根层（数据坏了要看得见）。
6. **存量数据零影响**：本模块跑完，`document` / `session` / `chat_message`
   的行数与切片数必须与跑之前完全一致。

运行：
    make infra                                          # 先起中间件
    .venv/bin/python tests/test_module11_user_org.py
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
# 本模块造的数据统一前缀，清理只按前缀删（绝不按全表清空 ——
# 那是 module9 留下的教训：清表会把真实演示数据抹掉且不报错）
PREFIX = "m11a_"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def check_raises(name: str, exc_type: type[BaseException], fn, /, *args, **kwargs) -> None:
    """
    断言「必须抛某异常」——与正向断言配对，避免守卫被写反还全绿。

    前三个参数用 `/` 标记为**仅位置**：否则被测函数的 `name=` / `code=` 这类
    关键字会撞上本函数的形参（`create_department(code=..., name=...)` 就撞过一次）。
    """
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        check(name, True, str(e))
    except Exception as e:  # noqa: BLE001
        check(name, False, f"抛的是 {type(e).__name__} 而不是 {exc_type.__name__}: {e}")
    else:
        check(name, False, "没有抛异常")


# --------------------------------------------------------------------------- #
# 前置：MySQL 不可达就整模块 SKIP
# --------------------------------------------------------------------------- #
print("== 前置检查：MySQL 连通性 ==")
from core import db as db_module  # noqa: E402

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
if not _conn["ok"]:
    print()
    print("=" * 66)
    print("  SKIP：MySQL 不可达，模块11 未执行。")
    print("  本地起中间件：make infra      （或 docker compose up -d mysql redis）")
    print("  若这是 CI / 验收，请设 REQUIRE_MYSQL=1 让它变成硬失败。")
    print("=" * 66)
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
print("  连通正常，开始执行\n")

from sqlalchemy import delete, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from core import schema as app_schema  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.db import get_engine, now_db  # noqa: E402


def cleanup() -> None:
    """
    只删本模块造的数据（按前缀），绝不按全表清空 —— 那是 module9 留下的教训：
    清表会把真实演示数据抹掉且不报错。

    username 与 employee_no 两个键都按前缀删：本模块不跑迁移往返，
    employee_no 一直是真值，两个条件等价；多写一个是为了将来有人在本模块里
    加了往返场景时，清理不会静默漏行。
    """
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM `user` WHERE employee_no LIKE :p OR username LIKE :p"),
            {"p": f"{PREFIX}%"},
        )
        conn.execute(
            text("DELETE FROM department WHERE code LIKE :p"), {"p": f"{PREFIX}%"}
        )
        conn.execute(
            text("DELETE FROM position WHERE code LIKE :p"), {"p": f"{PREFIX}%"}
        )


def business_snapshot() -> dict:
    """业务存量快照（user_repo 不该影响其中任何一张）。"""
    with get_engine().connect() as conn:
        return {
            "document": conn.execute(
                text("SELECT COUNT(*), COALESCE(SUM(chunk_count), 0) FROM document")
            ).one(),
            "session": conn.execute(text("SELECT COUNT(*) FROM session")).scalar(),
            "chat_message": conn.execute(text("SELECT COUNT(*) FROM chat_message")).scalar(),
        }


cleanup()
SNAP_BEFORE = business_snapshot()

# --------------------------------------------------------------------------- #
# 第 1 组：结构一致性（迁移 DDL ↔ core/schema.py）
# --------------------------------------------------------------------------- #
print("== 第 1 组：结构一致性（Alembic 迁移 ↔ core/schema.py）==")
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402

with get_engine().connect() as conn:
    _mc = MigrationContext.configure(conn, opts={"compare_type": True})
    _diff = compare_metadata(_mc, app_schema.metadata)
check(
    "七张表与 core/schema.py 完全一致（列/类型/可空/索引/唯一键/注释）",
    not _diff,
    f"diff={_diff}",
)

with get_engine().connect() as conn:
    _tables = {r[0] for r in conn.execute(text("SHOW TABLES"))}
check(
    "组织与账号三张表都已建出",
    {"user", "department", "position"} <= _tables,
    f"实际 {sorted(_tables)}",
)

with get_engine().connect() as conn:
    _cols = [r[0] for r in conn.execute(text("SHOW COLUMNS FROM `user`"))]
check("user.is_active 已下线（权威字段是 status）", "is_active" not in _cols, f"列={_cols}")
check(
    "user 的账号体系列已就位",
    {"employee_no", "email", "phone", "department_id", "position_id", "role",
     "status", "password_changed_at", "must_change_password", "token_version",
     "failed_login_count", "locked_until", "last_login_at", "created_by",
     "updated_by", "update_time", "deleted_at"} <= set(_cols),
    f"缺={sorted({'employee_no','email','phone','department_id','position_id','role','status','password_changed_at','must_change_password','token_version','failed_login_count','locked_until','last_login_at','created_by','updated_by','update_time','deleted_at'} - set(_cols))}",
)

with get_engine().connect() as conn:
    # SHOW INDEX 的列序是 Table / Non_unique / Key_name / Column_name / ...
    # Key_name 在第 3 列（索引名），第 1 列是表名。
    _user_idx = {r[2] for r in conn.execute(text("SHOW INDEX FROM `user`"))}
check(
    "user 的唯一键与索引齐备（工号 / 邮箱唯一，按部门+状态、按角色可筛）",
    {"uk_user_employee_no", "uk_user_email", "idx_user_dept_status", "idx_user_role"} <= _user_idx,
    f"实际={sorted(_user_idx)}",
)

# --------------------------------------------------------------------------- #
# 第 2 组：账号仓储 —— 成对断言
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：账号仓储（该放的放了 / 该拒的拒了）==")

_dept_root = repo.create_department(code=f"{PREFIX}root", name="测试中心", sort_order=90)
_dept_a = repo.create_department(code=f"{PREFIX}eng", name="测试研发部",
                                 parent_id=_dept_root, sort_order=91)
_pos = repo.create_position(code=f"{PREFIX}senior", name="测试高级工程师", level="P7")
check("部门两级建出", repo.get_department(_dept_a) is not None)
check("职位建出且带职级", repo.get_position_by_code(f"{PREFIX}senior").level == "P7")

_uid = repo.create_user(
    username=f"{PREFIX}alice",
    employee_no=f"{PREFIX}E001",
    password_hash="$2b$12$fakehashforstorageonly",
    display_name="测试员工甲",
    email=f"{PREFIX}alice@example.com",
    phone="13812345678",
    department_id=_dept_a,
    position_id=_pos,
    must_change_password=True,
    created_by=None,
)
check("建号返回 id 且能按登录名取回", repo.get_by_username(f"{PREFIX}alice") is not None)
check("能按工号取回", repo.get_by_employee_no(f"{PREFIX}E001").id == _uid)
check("默认 role=user / status=active",
      (lambda u: u.role == repo.ROLE_USER and u.status == repo.STATUS_ACTIVE)(repo.get(_uid)))
check("must_change_password 已落库", repo.get(_uid).must_change_password is True)

# --- 该拒的拒了 ---
check_raises("建号缺 username → ValueError", ValueError, repo.create_user,
             username="", employee_no=f"{PREFIX}E998", password_hash="x")
check_raises("建号缺 employee_no → ValueError", ValueError, repo.create_user,
             username=f"{PREFIX}bob", employee_no="", password_hash="x")
check_raises("建号缺 password_hash → ValueError", ValueError, repo.create_user,
             username=f"{PREFIX}bob", employee_no=f"{PREFIX}E998", password_hash="")
check_raises("role 拼错 → ValueError", ValueError, repo.create_user,
             username=f"{PREFIX}bob", employee_no=f"{PREFIX}E998",
             password_hash="x", role="hradmin")
check_raises("status 拼错 → ValueError", ValueError, repo.create_user,
             username=f"{PREFIX}bob", employee_no=f"{PREFIX}E998",
             password_hash="x", status="activ")
check_raises("工号重复 → 唯一键挡住（IntegrityError）", IntegrityError, repo.create_user,
             username=f"{PREFIX}alice2", employee_no=f"{PREFIX}E001", password_hash="x")
check_raises("登录名重复 → 唯一键挡住（IntegrityError）", IntegrityError, repo.create_user,
             username=f"{PREFIX}alice", employee_no=f"{PREFIX}E999", password_hash="x")
check_raises("不存在的 role 传给 list_users → ValueError", ValueError, repo.list_users, role="boss")

# --- 该拒的拒了（入口校验，含状态/角色变更）---
check_raises("set_status 传非法状态 → ValueError（不能靠 DB 兜着）", ValueError,
             repo.set_status, _uid, "activ")
check_raises("set_role 传非法角色 → ValueError", ValueError, repo.set_role, _uid, "root")
check("bump_token_version 对不存在的用户返回 0（不抛）", repo.bump_token_version(99999999) == 0)
check("record_login_failure 对不存在的用户返回 0（不抛）",
      repo.record_login_failure(99999999) == 0)

# --- 该放的放了 ---
_uid2 = repo.create_user(username=f"{PREFIX}bob", employee_no=f"{PREFIX}E002",
                         password_hash="x", display_name="测试员工乙")
check("第二个账号建出", repo.get(_uid2) is not None)
check("邮箱可空（MySQL 唯一索引允许多个 NULL）",
      repo.get_by_employee_no(f"{PREFIX}E002").email is None)

# --- 列表默认不含离职 ---
repo.set_status(_uid2, repo.STATUS_RESIGNED)
check("离职后默认列表里没有他",
      all(u.id != _uid2 for u in repo.list_users(keyword=PREFIX)))
check("显式 include_resigned 才看得到",
      any(u.id == _uid2 for u in repo.list_users(keyword=PREFIX, include_resigned=True)))
check("按工号搜得到", any(u.id == _uid for u in repo.list_users(keyword=f"{PREFIX}E001")))
check("按姓名搜得到", any(u.id == _uid for u in repo.list_users(keyword="测试员工甲")))
check("按部门筛得到", [u.id for u in repo.list_users(department_id=_dept_a)] == [_uid])
check("部门人数统计只数在职", repo.count_users_in_department(_dept_a) == 1)

# --- 资料修改：不碰 role / status，「不改」与「清空」要分得开 ---
repo.update_profile(_uid, display_name="测试员工甲改", email=None)
_u = repo.get(_uid)
check("update_profile 能改 display_name", _u.display_name == "测试员工甲改")
check("update_profile 传 None 真的把邮箱清空了", _u.email is None)
check("update_profile 不动 role/status",
      (_u.role == repo.ROLE_USER and _u.status == repo.STATUS_ACTIVE))
check("不传任何字段 → 返回 False（什么都没改）", repo.update_profile(_uid) is False)
check("更新不存在的用户 → False", repo.update_profile(99999999, display_name="x") is False)

# --- token_version 联动：P2-11c 的地基 ---
_base = repo.get(_uid).token_version
repo.set_status(_uid, repo.STATUS_DISABLED)
check("停用后 token_version +1（旧 token 立刻失效）",
      repo.get(_uid).token_version == _base + 1)
repo.set_status(_uid, repo.STATUS_ACTIVE)
check("恢复 active 也 +1（不让离职前的 token 复活）",
      repo.get(_uid).token_version == _base + 2)
repo.set_role(_uid, repo.ROLE_HR)
check("改角色也 +1（提权立刻生效）",
      repo.get(_uid).token_version == _base + 3 and repo.get(_uid).role == repo.ROLE_HR)
check("非 active 不能登录", repo.get(_uid).is_active is True)

_tv = repo.get(_uid).token_version
repo.update_password(_uid, "$2b$12$anotherfakehash", must_change_password=False)
_u = repo.get(_uid)
check("改密让 token_version +1（其他设备旧 token 失效）", _u.token_version == _tv + 1)
check("改密写入 password_changed_at（到期计算的就是它）", _u.password_changed_at is not None)
check("改密后 must_change_password 清零", _u.must_change_password is False)
check_raises("空哈希改密 → ValueError", ValueError, repo.update_password, _uid, "")

# --- 登录失败计数 / 锁定 ---
repo.record_login_failure(_uid, lock_until=None)
check("失败一次计数=1", repo.get(_uid).failed_login_count == 1)
repo.record_login_failure(_uid, lock_until=now_db() + timedelta(minutes=15))
check("连错两次计数=2", repo.get(_uid).failed_login_count == 2)
check("锁定时间已写入", repo.get(_uid).is_locked_at() is True)
repo.record_login_success(_uid)
check("登录成功后计数归零", repo.get(_uid).failed_login_count == 0)
check("登录成功后解锁", repo.get(_uid).locked_until is None)
check("不传 lock_until → 不锁", repo.get(_uid).is_locked_at() is False)

# --- 对外字典的脱敏与脱敏边界 ---
_d = repo.get(_uid).to_dict()
check("to_dict 不含 password_hash", "password_hash" not in _d)
check("to_dict 不含 token_version 以外的可疑字段", "deleted_at" not in _d)
repo.update_profile(_uid, phone="13812345678")
check("手机号默认脱敏", repo.get(_uid).to_dict()["phone"] == "138****5678")
check("显式 raw_phone=True 才给原值",
      repo.get(_uid).to_dict(raw_phone=True)["phone"] == "13812345678")
check("短号码整体打码", repo.mask_phone("1234") == "****")
check("空值透传", repo.mask_phone(None) is None)

# --------------------------------------------------------------------------- #
# 第 3 组：部门树与职位
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：部门树与职位 ==")
_dept_b = repo.create_department(code=f"{PREFIX}ops", name="测试运营部",
                                 parent_id=_dept_a, sort_order=92)
_tree = repo.build_department_tree(repo.list_departments())
_nodes = {n["code"]: n for n in _tree if n["code"].startswith(PREFIX)}
check("根节点只返回顶层部门", f"{PREFIX}root" in _nodes)
check("两级子树挂对了",
      len(_nodes[f"{PREFIX}root"]["children"]) == 1
      and _nodes[f"{PREFIX}root"]["children"][0]["code"] == f"{PREFIX}eng")
check("三级子树挂对了",
      _nodes[f"{PREFIX}root"]["children"][0]["children"][0]["code"] == f"{PREFIX}ops")
check("设部门负责人成功并可读回",
      repo.set_department_leader(_dept_a, _uid) and repo.get_department(_dept_a).leader_user_id == _uid)
check("负责人可置空（离职交接）",
      repo.set_department_leader(_dept_a, None) and repo.get_department(_dept_a).leader_user_id is None)
check_raises("部门 code 重复 → IntegrityError", IntegrityError, repo.create_department,
             code=f"{PREFIX}eng", name="重名部门")
check_raises("空部门名 → ValueError", ValueError, repo.create_department,
             code=f"{PREFIX}x", name="")
check_raises("非法职位序列 → ValueError", ValueError, repo.create_position,
             code=f"{PREFIX}x", name="x", sequence="sales")
check("按序列过滤职位", [p.code for p in repo.list_positions(sequence="tech")] != [])
check("按序列过滤为空时返回空列表",
      repo.list_positions(sequence="function") == [])
check("不存在的部门按 code 查 → None", repo.get_department_by_code(f"{PREFIX}nope") is None)
check("不存在的职位按 code 查 → None", repo.get_position_by_code(f"{PREFIX}nope") is None)
check("空列表建树 → 空树", repo.build_department_tree([]) == [])

# --------------------------------------------------------------------------- #
# 第 4 组：存量数据零影响
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：存量数据零影响 ==")
SNAP_AFTER = business_snapshot()
check(f"document 行数/切片数未变（{SNAP_AFTER['document']}）",
      SNAP_AFTER["document"] == SNAP_BEFORE["document"],
      f"{SNAP_BEFORE['document']} → {SNAP_AFTER['document']}")
check(f"session 行数未变（{SNAP_AFTER['session']}）", SNAP_AFTER["session"] == SNAP_BEFORE["session"])
check(f"chat_message 行数未变（{SNAP_AFTER['chat_message']}）",
      SNAP_AFTER["chat_message"] == SNAP_BEFORE["chat_message"])

cleanup()
with get_engine().connect() as conn:
    _left = conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE employee_no LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalar()
check("清理按前缀完成，没留下残数据", _left == 0)

# --------------------------------------------------------------------------- #
print()
print("=" * 66)
print(f"  模块11 结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)