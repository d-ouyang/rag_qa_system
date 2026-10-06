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
为什么往返前只灌 **1 个**员工（而不是 2 个）
--------------------------------------------------------------------------
`employee_no` 在迁移里带 `server_default=""`。ADD COLUMN 会给存量行填空串，
而紧接着要建唯一键 `uk_user_employee_no` —— **两行空串会撞唯一键**。

所以这张迁移的真实前提是「user 表为空或至多 1 行」（真实场景就是空的，
账号在 P2-11c 之前一直走 `.env`）。本脚本灌 1 行，既验证了「非空表上
ADD COLUMN 不报错」，又不越到那个已知限制外面去。
那个限制本身写在迁移文件头里，不是靠脚本藏起来的。

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
check("【已知差异】往返后旧行的 employee_no 被填成空串，需人工回填工号",
      _u.employee_no == "", f"实际 {_u.employee_no!r}")
check("【已知差异】仓储层拒绝新号复用空串工号（create_user 的必填校验）",
      _user_rejects_empty_employee_no())

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
section("第 5 组：清理")
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
check("user 表里没有留下任何残留行（含空串工号那种漏网的）", total_user == 0,
      f"user 表还有 {total_user} 行")

print()
print("=" * 66)
print(f"  P2-11a 验收结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)