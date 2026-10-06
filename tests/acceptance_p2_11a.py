# pyright: basic
"""
P2-11a 验收脚本：组织与账号表（迁移 0003）

--------------------------------------------------------------------------
它验的是单测**验不到**的那部分
--------------------------------------------------------------------------
`tests/test_module11_user_org.py` 验的是「迁移已经应用之后，结构对不对、
仓储行为对不对」。但有一类风险它碰不到：**迁移本身能不能安全地来回跑**。

    downgrade 0003 → 0002  （表结构真的能退回去吗？）
    upgrade   0002 → 0003  （在**有数据**的库上能重新升上去吗？）
    结构零漂移             （往返之后 compare_metadata 仍然是 0 吗？）
    存量数据零损失         （35 个文档 / 90 个切片 / 1 个会话 / 12 条消息还在吗？）

--------------------------------------------------------------------------
往返前会往表里塞 1 个员工；表里通常还有 P2-11d 的 10 个种子员工
--------------------------------------------------------------------------
这一条曾经是个「已知限制」：`employee_no` 带 `server_default=""`，
ADD COLUMN 给存量行填空串，而紧接着要建唯一键 —— **两行空串会撞唯一键**，
于是 0003 只在「user 表为空或至多 1 行」时能重放。

P2-11d 交付后种子数据把 10 个员工放进表里，这个限制第一次真的被触发：
迁移往返验收直接报 `1062 Duplicate entry ''`。修法是在迁移里加一步回填
（`employee_no = CONCAT('LEGACY-', id)`），不是在本脚本里绕开。

所以现在这条断言反过来成了回归防线：**往返之后，工号不能是空串，
也不能互相重复。**

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    make infra                                   # 先起中间件
    .venv/bin/python tests/acceptance_p2_11a.py   # 跑前不要有别的进程在写库
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
PREFIX = "p211a_"


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


from sqlalchemy import text  # noqa: E402

from core import db as db_module  # noqa: E402

if not db_module.check_connection()["ok"]:
    print("SKIP：MySQL 不可达。make infra 起中间件后重跑。")
    sys.exit(1)

from core import user_repo as repo  # noqa: E402
from core.db import get_engine  # noqa: E402


def one(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).one()


def cols(table: str) -> set[str]:
    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM `{table}`"))}


def tables() -> set[str]:
    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(text("SHOW TABLES"))}


def alembic(*args: str) -> None:
    subprocess.run([".venv/bin/alembic", *args], check=True)


def business() -> dict:
    return {
        "document_docs": one("SELECT COUNT(*) FROM document")[0],
        "document_chunks": one("SELECT COALESCE(SUM(chunk_count), 0) FROM document")[0],
        "session": one("SELECT COUNT(*) FROM session")[0],
        "chat_message": one("SELECT COUNT(*) FROM chat_message")[0],
    }


def _user_rejects_empty_employee_no() -> bool:
    """新建号时传空工号必须被拒（往返把老行冲成空串之后，这条守卫更重要）。"""
    try:
        repo.create_user(username=f"{PREFIX}probe", employee_no="", password_hash="x")
    except ValueError:
        return True
    return False


def cleanup() -> None:
    """
    清掉本脚本造的数据。

    ⚠️ 按 **username** 前缀删，不按 employee_no —— 迁移往返会把 employee_no
    冲成空串（ADD COLUMN 只能填默认值），按 employee_no 删会漏掉那一行。
    username 是唯一键且不受迁移影响，用它当清理锚点才靠得住。
    """
    existing = tables()
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
        if "department" in existing:
            conn.execute(text("DELETE FROM department WHERE code LIKE :p"), {"p": f"{PREFIX}%"})
        if "position" in existing:
            conn.execute(text("DELETE FROM position WHERE code LIKE :p"), {"p": f"{PREFIX}%"})


cleanup()

# --------------------------------------------------------------------------- #
section("第 1 组：起点状态")
# --------------------------------------------------------------------------- #
print(f"  业务存量：{business()}")
check("当前已在 0003（department 表存在）", "department" in tables())
check("is_active 已下线", "is_active" not in cols("user"))
BEFORE = business()


def others_rows() -> int:
    """本脚本之外的 user 行数（P2-11d 的 10 个种子员工）。

    为什么要单独数：本脚本第一版断言「user 表总行数为 0」——
    那是**在种子数据出现之前才成立的前提**。P2-11d 把 10 个员工灌进来之后，
    这条断言红了，而它本来就不该关心别人的数据。同一堂课今天已经咬第二次，
    于是这里改成「按业务键数自己的行」，而不是断言整表为空。
    """
    return one("SELECT COUNT(*) FROM `user` WHERE username NOT LIKE :p",
               {"p": f"{PREFIX}%"})[0]


OTHERS_BEFORE = others_rows()


# 由迁移 0003 / 0004 建出来的表 —— 往返时会被 DROP TABLE 删掉（含数据）。
# 验收脚本必须把它们连数据一起快照、往返后按原 id 写回，否则会留下悬空引用
# （员工的 department_id / position_id 指着那些被删掉的行）。
# 这里按表名 → 列清单写死，而不是动态扫information_schema：
# 动态扫在开发期方便，但一旦漏掉某张表就是静默的数据损失，而这里漏不起。
ROUNDTRIP_TABLES = {
    "position": ("id", "code", "name", "level", "sequence", "create_time"),
    "department": ("id", "code", "name", "parent_id", "leader_user_id", "sort_order", "create_time"),
    "user_password_history": ("id", "user_id", "password_hash", "changed_at", "changed_by_user_id"),
}


def snapshot_created_tables() -> dict[str, list[tuple]]:
    snap: dict[str, list[tuple]] = {}
    with get_engine().connect() as conn:
        for table, cols in ROUNDTRIP_TABLES.items():
            snap[table] = [tuple(r) for r in conn.execute(
                text(f"SELECT {', '.join(cols)} FROM `{table}` ORDER BY id"))]
    return snap


def restore_created_tables(snap: dict[str, list[tuple]]) -> int:
    n = 0
    with get_engine().begin() as conn:
        for table, cols in ROUNDTRIP_TABLES.items():
            for row in snap[table]:
                placeholders = ", ".join(f":c{i}" for i in range(len(cols)))
                conn.execute(text(
                    f"INSERT INTO `{table}` ({', '.join(cols)}) VALUES ({placeholders})"
                ), {f"c{i}": v for i, v in enumerate(row)})
                n += 1
    return n


OTHERS_BEFORE = others_rows()


def org_snapshot() -> tuple[list, list]:
    """部门与职位的全部行（含 id）。

    为什么必须快照：往返会 `DROP TABLE department / position`，
    **表里的数据跟着一起没了** —— 而员工的 `department_id` / `position_id`
    还指着那些 id，于是变成悬空引用（实测：重跑种子后靠「只同步资料」才修回来）。
    验收脚本不该留下这种状态，所以连数据一起快照、往返后按原 id 写回
    （保留 id 很重要：员工的引用靠它才能继续有效）。
    """
    with get_engine().connect() as conn:
        deps = conn.execute(text(
            "SELECT id, code, name, parent_id, leader_user_id, sort_order, create_time "
            "FROM department ORDER BY id"
        )).all()
        poss = conn.execute(text(
            "SELECT id, code, name, level, sequence, create_time FROM position ORDER BY id"
        )).all()
    return deps, poss


def restore_org(deps: list, poss: list) -> int:
    n = 0
    with get_engine().begin() as conn:
        for r in poss:
            conn.execute(text(
                "INSERT INTO position (id, code, name, level, sequence, create_time) "
                "VALUES (:id, :code, :name, :level, :seq, :ct)"
            ), {"id": r[0], "code": r[1], "name": r[2], "level": r[3], "seq": r[4], "ct": r[5]})
            n += 1
        for r in deps:
            conn.execute(text(
                "INSERT INTO department (id, code, name, parent_id, leader_user_id, sort_order, "
                "create_time) VALUES (:id, :code, :name, :pid, :lid, :so, :ct)"
            ), {"id": r[0], "code": r[1], "name": r[2], "pid": r[3], "lid": r[4], "so": r[5], "ct": r[6]})
            n += 1
    return n


def employee_no_snapshot() -> dict[int, str]:
    """往返前把所有工号按 id 记下来。

    为什么必须快照-还原：往返会把每行的 employee_no 冲成 `LEGACY-<id>`
    （0003 的回填），那**不是**真实工号。验收脚本不该把别人的工号留在库里 ——
    尤其 P2-11d 之后表里会有 10 个种子员工，他们的工号是有意义的业务数据。
    """
    with get_engine().connect() as conn:
        return {int(r[0]): r[1] for r in conn.execute(text("SELECT id, employee_no FROM `user`"))}


def restore_employee_no(snapshot: dict[int, str]) -> int:
    n = 0
    with get_engine().begin() as conn:
        for uid, emp_no in snapshot.items():
            n += conn.execute(
                text("UPDATE `user` SET employee_no = :e WHERE id = :i AND employee_no LIKE 'LEGACY-%'"),
                {"e": emp_no, "i": uid},
            ).rowcount
    return n

# 灌 1 个员工（为什么只灌 1 个，见文件头）
_uid = repo.create_user(
    username=f"{PREFIX}u1",
    employee_no=f"{PREFIX}E001",
    password_hash="$2b$12$acceptancefakehash",
    display_name="验收员工",
)
check("灌入 1 个员工成功", repo.get_by_username(f"{PREFIX}u1") is not None)
ROW_BEFORE = one("SELECT username, display_name, password_hash, create_time FROM `user` WHERE id=:i",
                 {"i": _uid})
print(f"  员工行（往返前）：{ROW_BEFORE}")
_rows_before = one("SELECT COUNT(*) FROM `user`")[0]
EMP_NO_SNAPSHOT = employee_no_snapshot()
CREATED_TABLES_SNAPSHOT = snapshot_created_tables()
print(f"  已快照 {len(EMP_NO_SNAPSHOT)} 行的工号，往返后原样写回")

# --------------------------------------------------------------------------- #
section("第 2 组：downgrade 到 0002")
# --------------------------------------------------------------------------- #
alembic("downgrade", "0002")
check("department 表已删除", "department" not in tables())
check("position 表已删除", "position" not in tables())
check("is_active 已回归", "is_active" in cols("user"))
check("新增列已删除", "employee_no" not in cols("user") and "token_version" not in cols("user"))
ROW_MID = one("SELECT username, display_name, password_hash, create_time FROM `user` WHERE id=:i",
              {"i": _uid})
check("员工行在回退后仍存在且原字段未变", ROW_MID == ROW_BEFORE, f"{ROW_MID}")
check("回退过程中业务存量未变", business() == BEFORE, f"{business()}")

# --------------------------------------------------------------------------- #
section("第 3 组：upgrade 回 head（非空表上重建）")
# --------------------------------------------------------------------------- #
alembic("upgrade", "head")
check("department 表重建", "department" in tables())
check("position 表重建", "position" in tables())
check("is_active 再次下线", "is_active" not in cols("user"))
check(
    "user 的 18 个新列全部回来了",
    {"employee_no", "email", "phone", "gender", "department_id", "position_id", "role",
     "status", "password_changed_at", "must_change_password", "token_version",
     "failed_login_count", "locked_until", "last_login_at", "created_by", "updated_by",
     "update_time", "deleted_at"} <= cols("user"),
)
_u = repo.get_by_username(f"{PREFIX}u1")
check("员工行仍然在，且 username/display_name/password_hash 未被改动",
      _u is not None and _u.display_name == ROW_BEFORE[1] and _u.password_hash == ROW_BEFORE[2])
check("新增列按默认值补齐：role=user / status=active / token_version=0",
      (_u.role == "user" and _u.status == "active" and _u.token_version == 0))

# ⚠️ 下面这条是**已知的预期差异**，不是缺陷，但它必须被显式钉住：
# ADD COLUMN 只能给存量行填默认值，**造不出真实工号**。所以往返之后这个人的
# employee_no 变成空串 —— 意味着「工号唯一且有值」这条业务约束在往返后
# 不再成立，必须人工/脚本回填。这一条写进迁移文件头的「已知限制」，
# 这里断言它确实发生，避免它悄悄发生。
check("【回归防线】往返后 employee_no 不是空串（0003 里加了 LEGACY-回填）",
      _u.employee_no != "", f"实际 {_u.employee_no!r}")
check(f"回填值是 LEGACY-<自己的 id>（本行 id={_uid}）",
      _u.employee_no == f"LEGACY-{_uid}", f"实际 {_u.employee_no!r}")
check("仓储层拒绝新号复用空串工号（create_user 的必填校验）",
      _user_rejects_empty_employee_no())

# ③ 往返之后：全表的 employee_no 必须互不重复（这正是以前会炸的地方）
with get_engine().connect() as conn:
    dup = conn.execute(
        text("SELECT employee_no, COUNT(*) c FROM `user` GROUP BY employee_no HAVING c > 1")
    ).all()
    blanks = conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE employee_no = ''")
    ).scalar()
    total = conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar()
check(f"往返后全表 {total} 行的工号互不重复（0003 的唯一键建得出来）",
      not dup, f"重复={dup}")
check("往返后没有空串工号", blanks == 0, f"空串 {blanks} 行")
check("往返过程没有丢员工", total == _rows_before, f"{_rows_before} → {total}")

# --------------------------------------------------------------------------- #
section("第 4 组：结构零漂移 + 存量数据")
# --------------------------------------------------------------------------- #
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402

from core import schema as app_schema  # noqa: E402

with get_engine().connect() as conn:
    diff = compare_metadata(
        MigrationContext.configure(conn, opts={"compare_type": True}), app_schema.metadata
    )
check("往返之后结构仍然零漂移", not diff, f"diff={diff}")

AFTER = business()
print(f"  业务存量：{AFTER}")
check(f"文档 {BEFORE['document_docs']} 个 / 切片 {BEFORE['document_chunks']} 个未变",
      (AFTER["document_docs"], AFTER["document_chunks"])
      == (BEFORE["document_docs"], BEFORE["document_chunks"]))
check(f"会话 {BEFORE['session']} 个未变", AFTER["session"] == BEFORE["session"])
check(f"消息 {BEFORE['chat_message']} 条未变", AFTER["chat_message"] == BEFORE["chat_message"])

# --------------------------------------------------------------------------- #
section("第 5 组：还原工号")
# --------------------------------------------------------------------------- #
_restored = restore_employee_no(EMP_NO_SNAPSHOT)
check(f"往返造成的 LEGACY- 工号已全部还原（{_restored} 行）", _restored == len(EMP_NO_SNAPSHOT),
      f"还原 {_restored} / 共 {len(EMP_NO_SNAPSHOT)}")
with get_engine().connect() as conn:
    _left_legacy = conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE employee_no LIKE 'LEGACY-%'")
    ).scalar()
check("库里没有残留的 LEGACY- 工号", _left_legacy == 0, f"残留 {_left_legacy} 行")

# 往返把 0003 / 0004 建的表整表 DROP 了 → 按原 id 写回
_created = sum(len(v) for v in CREATED_TABLES_SNAPSHOT.values())
print(f"  往返前这些表共 {_created} 行，往返后被 DROP TABLE 删空 → 按原 id 写回")
_back = restore_created_tables(CREATED_TABLES_SNAPSHOT)
check(f"迁移建的表数据已还原（{_back} 行）", _back == _created, f"写回 {_back} / 应写 {_created}")
with get_engine().connect() as conn:
    _dangling = conn.execute(text(
        "SELECT COUNT(*) FROM `user` u LEFT JOIN department d ON d.id = u.department_id "
        "WHERE u.department_id IS NOT NULL AND d.id IS NULL")).scalar()
    _counts = {t: conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
               for t in ROUNDTRIP_TABLES}
check("每张表的行数都回到往返前",
      _counts == {t: len(v) for t, v in CREATED_TABLES_SNAPSHOT.items()},
      f"实际 {_counts}")
check("没有员工的部门引用变成悬空", _dangling == 0, f"悬空 {_dangling} 行")

# --------------------------------------------------------------------------- #
section("第 6 组：清理")
# --------------------------------------------------------------------------- #
cleanup()
with get_engine().connect() as conn:
    # ⚠️ 判据必须**独立于** cleanup 用的 WHERE 条件。用同一条件验证「清干净了」
    # 是永远为真的假断言（第一版就踩了这个：cleanup 按 employee_no 删，
    # 校验也按 employee_no 查，于是残留行根本查不出来，测试却全绿）。
    left_user = conn.execute(text("SELECT COUNT(*) FROM `user` WHERE username LIKE :p"),
                             {"p": f"{PREFIX}%"}).scalar()
    left_dept = conn.execute(text("SELECT COUNT(*) FROM department WHERE code LIKE :p"),
                             {"p": f"{PREFIX}%"}).scalar()
    left_pos = conn.execute(text("SELECT COUNT(*) FROM position WHERE code LIKE :p"),
                            {"p": f"{PREFIX}%"}).scalar()
    total_user = conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar()
check("验收数据已清理干净", left_user == 0 and left_dept == 0 and left_pos == 0,
      f"{left_user}/{left_dept}/{left_pos}")
check("清理没有误伤业务数据", business() == BEFORE, f"{business()}")
with get_engine().connect() as conn:
    _mine = conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalar()
check(f"本脚本造的 {PREFIX}* 行已全部清掉", _mine == 0, f"还剩 {_mine} 行")
check(f"别人的行数未被本脚本改动（种子员工 {OTHERS_BEFORE} 人）",
      others_rows() == OTHERS_BEFORE, f"{OTHERS_BEFORE} → {others_rows()}")

print()
print("=" * 66)
print(f"  P2-11a 验收结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)