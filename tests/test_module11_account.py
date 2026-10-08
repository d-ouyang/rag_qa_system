# pyright: basic
"""
模块11测试文件：账号体系（P2-11a 表与仓储 + P2-11b 密码策略）。

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
6. **密码策略**（P2-11b）：到期边界（正好到期 / 差一秒）、历史不可复用、
   连续失败锁定、cost 10 老哈希渐进升级；「账号不存在 / 密码错 / 已锁定」
   三者对外文案必须逐字相同，且四条失败路径耗时同量级。
   **这一组必须钉死时钟**（`now=`），否则夹具会在不同运行时刻给出不同答案。
7. **改密历史仓储**：写入 / 取最近 N 条 / 裁剪到保留条数。
8. **存量数据零影响**：本模块跑完，`document` / `session` / `chat_message`
   的行数与切片数必须与跑之前完全一致。

运行：
    make infra                                          # 先起中间件
    .venv/bin/python tests/test_module11_account.py
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

import time  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

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
        # 先清改密历史再清 user —— 没有外键级联，但留着悬空的历史行更难查
        conn.execute(
            text("DELETE p FROM user_password_history p JOIN `user` u ON u.id = p.user_id "
                 "WHERE u.employee_no LIKE :p OR u.username LIKE :p"),
            {"p": f"{PREFIX}%"},
        )
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
check("按序列过滤只返回该序列下的职位",
      all(p.sequence == "tech" for p in repo.list_positions(sequence="tech"))
      and repo.get_position_by_code(f"{PREFIX}senior") is not None)
# ⚠️ 原来这里写的是「按序列过滤为空时返回空列表」，用sequence="function" 断言全表为空。
# 那是**在种子数据出现之前才成立的隐含前提** —— P2-11d 把 6 个职位（含 function 序列）
# 灌进库之后，这条断言红了，而它本来就不该关心别人有没有数据。
# 教训与踩坑清单第 49 条同源：**断言的判据不能依赖「这张表本来是空的」。**
check("按序列过滤的判据不依赖表是否为空（本模块造的职位在结果里）",
      f"{PREFIX}senior" in [p.code for p in repo.list_positions(sequence="tech")])
check("不存在于本模块的职位按 code 查 → None",
      repo.get_position_by_code(f"{PREFIX}nope") is None)
check("不存在的部门按 code 查 → None", repo.get_department_by_code(f"{PREFIX}nope") is None)
check("空列表建树 → 空树", repo.build_department_tree([]) == [])

# --------------------------------------------------------------------------- #
# 第 4 组：密码策略（P2-11b）
# --------------------------------------------------------------------------- #
# ⚠️ 这一组**必须把时钟钉死**：password_policy 的每个判定都接受 now= 参数，
# 而「锁定未到期」「密码刚过期」这类夹具一旦用真实时钟，同一份夹具会在
# 不同的运行时刻给出不同答案 —— 表现就是「本地绿、CI 红」的间歇性失败。
print("\n== 第 4 组：密码策略 ==")
from core import password_policy as policy  # noqa: E402

# 测试用的固定时钟：所有判定都传它，绝不依赖 datetime.now()
NOW = datetime(2026, 10, 6, 23, 0, 0)

# cost 12 的哈希本机一次约 165ms，所以**全程只造一次**并复用；
# 其余「只关心能不能判重」的地方用 cost 4 的快哈希（cost 值不影响判定语义）。
_HASH_C12 = policy.hash_password("Abcdefghij1")
_HASH_C10 = policy.hash_password("Abcdefghij1", cost=10)


def fast_hash(password: str) -> str:
    """只为「跑得快」存在的哈希。**不要**用它断言 needs_rehash。"""
    return policy.hash_password(password, cost=4)


def mk_user(**kw):
    """造一个 UserRecord 做 verify 的夹具。"""
    base = dict(
        id=1, username="p11b", employee_no="p11b_E001", display_name="策略夹具",
        email=None, phone=None, gender=None, department_id=None, position_id=None,
        role=repo.ROLE_USER, kb_role=repo.KB_ROLE_NONE,
        # P2-15b 加的列：夹具默认 0（不限）。⚠️ 加 UserRecord 字段时
        # 各测试模块自己的构造点也要跟着补 —— 本文件当时没跑到这条路径，
        # 到 15d 全量回归才炸出来（TypeError 停在夹具构造，不是断言红）。
        token_quota_monthly=0,
        status=repo.STATUS_ACTIVE, password_hash=_HASH_C12,
        password_changed_at=NOW - timedelta(days=1), must_change_password=False,
        token_version=0, failed_login_count=0, locked_until=None, last_login_at=None,
        created_by=None, updated_by=None, create_time=NOW, update_time=NOW, deleted_at=None,
    )
    base.update(kw)
    return repo.UserRecord(**base)


# --- 5.1 到期边界（正好到期 / 差一秒）---
check("正好 90 天 → 仍算有效（判据是严格大于）",
      policy.is_expired(NOW - timedelta(days=90), now=NOW) is False)
check("超过 90 天 1 秒 → 已过期",
      policy.is_expired(NOW - timedelta(days=90) - timedelta(seconds=1), now=NOW) is True)
check("89 天 → 未过期", policy.is_expired(NOW - timedelta(days=89), now=NOW) is False)
check("password_changed_at 为 NULL → 按已过期处理（fail-closed）",
      policy.is_expired(None, now=NOW) is True)
check("expire_days=0 → 显式关闭到期策略",
      policy.is_expired(NOW - timedelta(days=9999), now=NOW, expire_days=0) is False)
check("剩余天数向上取整：85 天前改密 → 剩 5 天",
      policy.expire_in_days(NOW - timedelta(days=85), now=NOW) == 5)
check("已过期时剩余天数为负",
      policy.expire_in_days(NOW - timedelta(days=100), now=NOW) == -10)
check("剩余 5 天 → 该弹到期提醒", policy.should_warn_expire(NOW - timedelta(days=85), now=NOW) is True)
check("剩余 30 天 → 不弹", policy.should_warn_expire(NOW - timedelta(days=30), now=NOW) is False)
check("已过期 → 不弹提醒（那是拦截不是提醒）",
      policy.should_warn_expire(NOW - timedelta(days=200), now=NOW) is False)

# --- 5.2 历史不可复用 ---
_H1, _H2 = fast_hash("HistoryOne1!"), fast_hash("HistoryTwo2!")
check("命中历史 → 判为不可复用", policy.hits_history("HistoryOne1!", [_H1, _H2]) is True)
check("未命中 → 放行", policy.hits_history("BrandNew3!", [_H1, _H2]) is False)
check("历史为空 → 放行", policy.hits_history("Anything1!", []) is False)
check("limit=0 → 相当于关闭历史检查", policy.hits_history("HistoryOne1!", [_H1], limit=0) is False)
check("列表里有坏哈希不会打断判定（只跳过那一条）",
      policy.hits_history("HistoryTwo2!", ["garbage-not-bcrypt", _H1, _H2]) is True)
check("limit=1 时只看第一条：第二条的明文不算命中",
      policy.hits_history("HistoryTwo2!", [_H1, _H2], limit=1) is False)
check("limit=2 时第二条能命中",
      policy.hits_history("HistoryTwo2!", [_H1, _H2], limit=2) is True)

# --- 5.3 失败锁定 ---
check("连错 4 次 → 不锁", policy.should_lock(4) is False)
check("连错 5 次 → 锁", policy.should_lock(5) is True)
check("锁定阈值可配：max_failures=3 时3 次就锁", policy.should_lock(3, max_failures=3) is True)
check("锁定截止 = now + 15 分钟",
      policy.lock_deadline(now=NOW) == NOW + timedelta(minutes=15))
check("锁定时长可配", policy.lock_deadline(now=NOW, lock_minutes=5) == NOW + timedelta(minutes=5))

# --- 5.4 cost 10 老哈希渐进升级 ---
check("读出 cost12 = 12", policy.hash_cost(_HASH_C12) == 12)
check("读出 cost10 = 10", policy.hash_cost(_HASH_C10) == 10)
check("非 bcrypt 串读 cost → None（不抛）", policy.hash_cost("not-a-hash") is None)
check("cost12 的哈希不需要 rehash", policy.needs_rehash(_HASH_C12) is False)
check("cost10 的哈希需要 rehash（登录成功后顺带升级）", policy.needs_rehash(_HASH_C10) is True)
check("坏串不需要 rehash（别在登录路径上把用户踢去改密）", policy.needs_rehash("not-a-hash") is False)
check("verify 在密码正确时给出 rehash 提示",
      policy.verify(mk_user(password_hash=_HASH_C10), "Abcdefghij1", now=NOW).rehash is True)
check("verify 在已是目标 cost 时不提示 rehash",
      policy.verify(mk_user(), "Abcdefghij1", now=NOW).rehash is False)

# --- 5.5 强度校验 ---
check("合格密码无违规项", policy.validate_strength("Abcdefghij1") == [])
check("空密码被拒", policy.validate_strength("") == ["密码不能为空"])
check("太短被拒", any("长度不足" in v for v in policy.validate_strength("Ab1!")))
check("纯数字被拒", any("纯数字" in v for v in policy.validate_strength("12345678901")))
check("单一字符类被拒", any("两类" in v for v in policy.validate_strength("abcdefghij")))
check("与登录名相同被拒",
      any("登录名" in v for v in policy.validate_strength("P11B", username="p11b")))
check("与工号相同被拒",
      any("工号" in v for v in policy.validate_strength("p11bE001", employee_no="p11bE001")))
check("超过 72 字节被拒（bcrypt 只取前 72 字节，超出部分静默忽略）",
      any("72 字节" in v for v in policy.validate_strength("中" * 30)))
check("违规项是列表而不是布尔（管理端要逐条展示）",
      isinstance(policy.validate_strength("123"), list))

# --- 5.6 临时密码 ---
_tps = {policy.generate_temporary_password() for _ in range(10)}
check("临时密码不会重复", len(_tps) == 10)
check("临时密码必然通过强度校验",
      all(policy.validate_strength(p) == [] for p in _tps))
# 断言「候选池」而不是「抽 24 个字符的样本」—— 后者是概率性的：
# 单个 24 位样本含易混字符的概率约 28%，也就是说**这条断言 15 遍里会漏 13 遍**。
# （第一版就栽在这儿：小写位能抽到 `l`、大写位能抽到 `I`/`O`，
#   而测试只抽一个样本，13 次里只红 2 次，差点被当成「偶发」放过去。）
check("候选池本身就不含易混字符（确定性断言）",
      not (set(policy._TEMP_LOWER + policy._TEMP_UPPER + policy._TEMP_DIGIT)
           & policy._TEMP_CONFUABLES))
check("生成 200 个临时密码都不含易混字符 0/O/1/l/I（抄错会被当成密码错误）",
      not set("".join(policy.generate_temporary_password(length=24) for _ in range(200)))
      & policy._TEMP_CONFUABLES)
check("临时密码长度可配但不低于 10",
      len(policy.generate_temporary_password(length=20)) == 20
      and len(policy.generate_temporary_password(length=4)) >= 10)

# --- 5.7 verify：四条失败路径的文案必须完全一致 ---
_codes = {
    "not_found": policy.verify(None, "Abcdefghij1", now=NOW),
    "wrong_password": policy.verify(mk_user(), "WrongPass123", now=NOW),
    "locked": policy.verify(mk_user(locked_until=NOW + timedelta(minutes=5)), "Abcdefghij1", now=NOW),
}
check("账号不存在 / 密码错 / 已锁定 三者的对外文案逐字相同（防用户名枚举）",
      len({o.message for o in _codes.values()}) == 1,
      f"实际={[o.message for o in _codes.values()]}")
check("三者 code 仍然可区分（给日志与监控看）",
      len({o.code for o in _codes.values()}) == 3)
check("锁定未到期 → 不放行", _codes["locked"].ok is False)
check("锁定期刚过 → 放行",
      policy.verify(mk_user(locked_until=NOW - timedelta(seconds=1)), "Abcdefghij1", now=NOW).code == "ok")
check("停用/离职 → 单独文案（用户要看得懂，否则只会去找客服）",
      policy.verify(mk_user(status=repo.STATUS_RESIGNED), "Abcdefghij1", now=NOW).code == "inactive")
check("强制改密 → 放行但打标",
      policy.verify(mk_user(must_change_password=True), "Abcdefghij1", now=NOW).code == "must_change")
check("密码过期 → 不放行（到期后除改密与登出一律 403）",
      policy.verify(mk_user(password_changed_at=NOW - timedelta(days=200)), "Abcdefghij1", now=NOW).ok is False)
check("过期结果的 detail 带剩余天数",
      policy.verify(mk_user(password_changed_at=NOW - timedelta(days=200)), "Abcdefghij1",
                    now=NOW).detail.get("expire_in_days") == -110)
check("正常登录 → ok 且无多余文案",
      policy.verify(mk_user(), "Abcdefghij1", now=NOW).message == "")

# --- 5.8 四条失败路径的耗时必须是同一量级 ---
_t = {}
for _label, _u, _c in (("not_found", None, "Abcdefghij1"),
                       ("wrong_password", mk_user(), "WrongPass123"),
                       ("locked", mk_user(locked_until=NOW + timedelta(minutes=5)), "Abcdefghij1"),
                       ("inactive", mk_user(status=repo.STATUS_RESIGNED), "Abcdefghij1")):
    _s = time.time(); policy.verify(_u, _c, now=NOW); _t[_label] = time.time() - _s
_mn, _mx = min(_t.values()), max(_t.values())
# 上界取 _mx * 3 + 0.02：bcrypt 自身有 165ms 抖动，纯计时断言必须留足余量，
# 否则这是一条会随机红的断言（比没有断言更糟）。真正的防线是「代码里四条路径
# 都调了 _burn」，计时断言只负责发现「有人把某条路径的 _burn 删了」。
check(f"四条失败路径耗时同量级（最慢/最快 = {_mx/_mn:.2f}）", _mx < _mn * 3 + 0.02,
      f"{_t}")

# --------------------------------------------------------------------------- #
# 第 5 组：改密历史的仓储（真 DB）
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：改密历史仓储 ==")
_uid3 = repo.create_user(username=f"{PREFIX}carol", employee_no=f"{PREFIX}E003",
                         password_hash=fast_hash("InitialPass1!"), display_name="测试员工丙")
# ⚠️ 明文与哈希要分成两个列表。曾经把哈希直接当 candidate 传给 hits_history，
# 于是「不命中」那条断言因为「拿哈希跟哈希比永远不相等」而**假通过** ——
# 空断言比没有断言更危险（坑35）。所以这里保留 _pws 供判重使用。
_pws = [f"Rotate{i}Pass{i}!" for i in range(1, 7)]
_hashes = [fast_hash(p) for p in _pws]
for _i, _h in enumerate(_hashes):
    repo.add_password_history(_uid3, _h, changed_by_user_id=None,
                              now=NOW - timedelta(days=10 - _i))
check("写入 6 条历史", repo.count_password_history(_uid3) == 6)
_recent = repo.list_recent_password_hashes(_uid3, 3)
check("取最近 3 条且按时间倒序", len(_recent) == 3 and _recent[0] == _hashes[-1])
check("最近 3 条之外的更早记录取不到", _hashes[0] not in _recent)
check("判重：命中最近 5 条里的最新一条（传的是**明文**）",
      policy.hits_history(_pws[-1], repo.list_recent_password_hashes(_uid3, 5)) is True)
check("判重：更早的第 6 条查不到 —— 深度就是策略本身",
      policy.hits_history(_pws[0], repo.list_recent_password_hashes(_uid3, 5)) is False)
check("反向验证上一条不是空断言：显式放宽深度到 10 就能命中更早那条",
      policy.hits_history(_pws[0], repo.list_recent_password_hashes(_uid3, 10), limit=10) is True)
check("深度上限是硬的：不传 limit 时按 PASSWORD_HISTORY_KEEP 封顶（传 10 条也只查 5 条）",
      policy.hits_history(_pws[0], repo.list_recent_password_hashes(_uid3, 10)) is False)
check("新密码（不在历史里）放行",
      policy.hits_history("NeverUsed9!", repo.list_recent_password_hashes(_uid3, 5)) is False)
check("裁剪到保留 5 条", repo.prune_password_history(_uid3, 5) == 1)
check("裁剪后剩 5 条", repo.count_password_history(_uid3) == 5)
check("裁剪保留的是最近 5 条（最旧那条已被裁掉）",
      _hashes[0] not in repo.list_recent_password_hashes(_uid3, 10))
check("裁剪到 1 条", repo.prune_password_history(_uid3, 1) == 4)
check("再裁到 1 条时无事可做", repo.prune_password_history(_uid3, 1) == 0)
check_raises("空哈希不能进历史", ValueError, repo.add_password_history, _uid3, "")

# --------------------------------------------------------------------------- #
# 第 6 组：存量数据未受影响
# --------------------------------------------------------------------------- #
print("\n== 第 6 组：存量数据零影响 ==")
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