#!/usr/bin/env python3
"""
模拟员工种子数据 —— 把 user / department / position 三张表填成一个「像样的小公司」。

    .venv/bin/python scripts/seed_users.py                            # 干跑：只报告要做什么
    .venv/bin/python scripts/seed_users.py --apply                    # 真做（可反复跑）
    .venv/bin/python scripts/seed_users.py --apply --reset-password    # 给所有人换一个新临时密码
    .venv/bin/python scripts/seed_users.py --apply --reset            # 先删掉这批人再重建
    .venv/bin/python scripts/seed_users.py --apply --credentials-file .seed-credentials.csv

--------------------------------------------------------------------------
为什么默认是干跑
--------------------------------------------------------------------------
沿用 `scripts/reindex.py` 的规矩：**会写库的命令不该在敲错时直接生效。**
这个脚本写的是「账号 + 密码」，参数敲错跑一次 undesire 的种子，
真��会被塞进一批临时密码。所以默认只打印计划，`--apply` 才落库。

--------------------------------------------------------------------------
幂等靠「按业务键 upsert」，不靠「先查再插」
--------------------------------------------------------------------------
朴素的幂等是「先 SELECT 看在不在，不在才 INSERT」—— 两个进程同时跑会插出两条。
这里改成按业务键 upsert：

    部门 按 code ｜ 职位 按 code ｜ 员工 按 username（唯一键）

两边同时跑时，第二次会撞唯一键，于是最坏结果是「一人一条」，而不是「两条同名人」。
这与 `core/document_repo` 用原子 UPDATE 抢任务是同一个思路。

--------------------------------------------------------------------------
重跑绝对不能动密码 —— 本脚本最容易写错的地方
--------------------------------------------------------------------------
想象一下：种子跑完，某员工自己改了密码；一周后有人为了「刷新一下数据」重跑脚本。
如果脚本每次都重置密码，这个员工会被打回临时密码，**而且他自己不知道**。

所以重跑只补/ 修正资料（姓名、邮箱、手机号、部门、职位），以及：

    · **不碰** `password_hash`、`must_change_password`、`status`
    · 换密码只有两条显式路径：`--reset-password`，或管理员在管理端重置（P2-13b）
    · `status` 是运行时状态：管理员停用了某员工，重跑不该把他悄悄恢复。
      所以它只在**首次创建**时写入。

--------------------------------------------------------------------------
密码怎么发出去
--------------------------------------------------------------------------
每个员工一个**随机**临时密码（`core/password_policy.generate_temporary_password`），并且：

    · `must_change_password = 1`  → 首登必须改密
    · 写一条 `user_password_history` → 他第一次改密时**不能用同一个密码**
    · **只显示一次**：打印到控制台，或显式 `--credentials-file` 写文件

⚠️ 脚本一律用 `print` 而**不走 logging**，所以临时密码**不会进 `app.log`**
（`LOG_FILE` 是开着的，日志会被翻阅、备份、同步）。

`--credentials-file` 会先检查该路径**是否已被 git 跟踪**，被跟踪就拒绝写 ——
一个把密码提交进仓库的凭据文件，比没有这个文件糟得多。

--------------------------------------------------------------------------
和 break-glass 超管的关系（别搞混）
--------------------------------------------------------------------------
种子里的 `role=admin`（吴静）是**企业管理员**：管人事、重置员工密码。
它**不是** PLAN §11 D9 那个 break-glass 超管 —— 那个留在 `.env` 的 `GATEWAY_USERS`
里，专门给「连不上数据库时进得去」用。两者都叫 admin，但用途完全不同。
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

from sqlalchemy import text  # noqa: E402

from core import password_policy as policy  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.db import check_connection, get_engine, now_db  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent

# --------------------------------------------------------------------------- #
# 种子数据
#
# 刻意用 `example.com`：这些邮箱会出现在管理端界面上，用真实域名会让人以为
# 它能收信（然后奇怪为什么 HR 的离职邮件没来）。
# code / employee_no 都是稳定键 —— 改了它们，这个脚本就从「修正原来那批」
# 变成「另建一批人」了。
# --------------------------------------------------------------------------- #
DEPARTMENTS = [
    # (code, name, parent_code, sort_order)
    ("HQ", "总部职能中心", None, 1),
    ("OPS", "门店运营部", "HQ", 2),
    ("TECH", "技术中心", "HQ", 3),
]

POSITIONS = [
    # (code, name, level, sequence)
    ("P5", "专员", "P5", repo.SEQUENCE_FUNCTION),
    ("P6", "高级专员", "P6", repo.SEQUENCE_FUNCTION),
    ("P7", "工程师", "P7", repo.SEQUENCE_TECH),
    ("P8", "资深工程师", "P8", repo.SEQUENCE_TECH),
    ("M1", "经理", "M1", repo.SEQUENCE_MANAGEMENT),
    ("M2", "高级经理", "M2", repo.SEQUENCE_MANAGEMENT),
]

EMPLOYEES = [
    # (username, employee_no, display_name, dept, position, role, status, phone)
    ("wu.jing", "G0001", "吴静", "HQ", "M2", repo.ROLE_ADMIN, repo.STATUS_ACTIVE, "13800000001"),
    ("zhou.yan", "G0002", "周妍", "HQ", "P5", repo.ROLE_HR, repo.STATUS_ACTIVE, "13800000002"),
    ("chen.jie", "G0003", "陈杰", "OPS", "M1", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000003"),
    ("zhao.min", "G0004", "赵敏", "OPS", "M1", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000004"),
    ("sun.lei", "G0005", "孙磊", "OPS", "P6", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000005"),
    ("li.na", "G0006", "李娜", "TECH", "P8", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000006"),
    ("zhang.wei", "G0007", "张伟", "TECH", "P7", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000007"),
    ("wang.fang", "G0008", "王芳", "TECH", "P7", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000008"),
    ("xu.hao", "G0009", "徐浩", "TECH", "P6", repo.ROLE_USER, repo.STATUS_ACTIVE, "13800000009"),
    # 留一个「已离职」：让管理端一进去就有「离职 ≠ 删行」的样本可看。
    # 这是**创建时**写进去的；脚本重跑不会把别人改过的 status 改回来。
    ("zheng.shuang", "G0010", "郑爽", "OPS", "P6", repo.ROLE_USER, repo.STATUS_RESIGNED, "13800000010"),
]

# 部门负责人 → 按 P2-14 的 D13，部门共享知识库的默认 writer 就是他
LEADERS = {"HQ": "wu.jing", "OPS": "chen.jie", "TECH": "li.na"}


def _hr(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * 4}")


def _in_clause(values: list[str]) -> str:
    """把一组业务键拼成 SQL 的 IN 列表。

    为什么手拼而不用绑定参数：这些值全部来自本文件顶部的常量表，**不是外部输入**。
    用绑定参数反而要把参数塞进 `.bindparams()` 再 `text()`，可读性更差。
    凡是来自外部的输入，一律不许走这个函数。
    """
    return ", ".join(f"'{v}'" for v in values)


def _git_tracked(path: Path) -> bool:
    """该路径是否已被 git 跟踪。是则拒绝往里写凭据。"""
    try:
        if subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=str(REPO_ROOT), capture_output=True,
        ).returncode == 0:
            return False  # 已被 ignore → 安全
        return subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=str(REPO_ROOT), capture_output=True,
        ).returncode == 0
    except (OSError, ValueError):  # pragma: no cover
        return False


def _wipe() -> None:
    """
    只删「种子数据自己」的那几条，绝不全表清空。

    全表清空会把真人在库里建的东西抹掉且不报错 —— 那是 module9 留下的教训。
    这里按业务键精确删：部门 code / 职位 code / 员工 username。
    """
    names = _in_clause([e[0] for e in EMPLOYEES])
    codes_p = _in_clause([p[0] for p in POSITIONS])
    codes_d = _in_clause([d[0] for d in DEPARTMENTS])
    with get_engine().begin() as conn:
        # 顺序有讲究：改密历史 → 员工 → 职位 → 部门。
        # 没有 FK 级联，所以顺序得自己保证；而部门是员工的 parent_id，父节点要后删。
        conn.execute(text(
            "DELETE p FROM user_password_history p JOIN `user` u ON u.id = p.user_id "
            f"WHERE u.username IN ({names})"
        ))
        conn.execute(text(f"DELETE FROM `user` WHERE username IN ({names})"))
        conn.execute(text(f"DELETE FROM position WHERE code IN ({codes_p})"))
        conn.execute(text(f"DELETE FROM department WHERE code IN ({codes_d})"))
    print(f"  已清除 {len(EMPLOYEES)} 名员工（含改密历史）/ {len(POSITIONS)} 个职位 / "
          f"{len(DEPARTMENTS)} 个部门")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="模拟员工种子数据（默认干跑；--apply 才落库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="真写库（默认只报告计划）")
    parser.add_argument(
        "--reset", action="store_true",
        help="先删掉这批种子数据再重建（只按业务键删，不碰别人的数据）",
    )
    parser.add_argument(
        "--reset-password", action="store_true",
        help="给所有人换一个新随机临时密码（会 token_version+1，即吊销他们手上的 token）",
    )
    parser.add_argument(
        "--credentials-file", type=Path, default=None,
        help="把临时密码写到这个文件（只写一次；路径若被 git 跟踪则拒绝）",
    )
    args = parser.parse_args()

    print("=" * 74)
    print("模拟员工种子数据 —— 部门 3 / 职位 6 / 员工 10（含 1 名已离职）")
    if not args.apply:
        print("** 干跑模式 ** 什么都不会写。加 --apply 才会落库。")
    print("=" * 74)

    if not check_connection()["ok"]:
        print("\n❌ MySQL 不可达。make infra 起中间件后重跑。")
        return 1

    if args.reset:
        if not args.apply:
            print("\n⚠️ --reset 只与 --apply 一起用（干跑模式下删东西没有意义）。")
            return 1
        _hr("第 0 组：清除旧的种子数据")
        _wipe()

    # ------------------------------------------------------------------ #
    _hr("第 1 组：部门")
    dept_ids: dict[str, int] = {}
    for code, name, parent, sort in DEPARTMENTS:
        existing = repo.get_department_by_code(code)
        if existing:
            dept_ids[code] = existing.id
            print(f"  已存在 {code:5} {name}（id={existing.id}）→ 保持不变")
            continue
        print(f"  待创建 {code:5} {name}（上级 {parent or '—'}）")
        if args.apply:
            dept_ids[code] = repo.create_department(
                code=code, name=name,
                parent_id=dept_ids.get(parent) if parent else None,
                sort_order=sort,
            )
    # 兜底：父节点若这次没被创建（例如本来就存在），上面那轮拿不到它的 id，
    # 这里补齐并修正父子关系。**只在 --apply 下做** —— 干跑时什么都没建，
    # 去查当然查不到，那不是「需要兜底」，那是「还没到那一步」。
    if args.apply:
        for code, _name, parent, _sort in DEPARTMENTS:
            if code not in dept_ids:
                dept_ids[code] = repo.get_department_by_code(code).id
            if not parent:
                continue
            current = repo.get_department(dept_ids[code])
            if current is not None and current.parent_id is None:
                repo.set_department_parent(dept_ids[code], dept_ids[parent])

    # ------------------------------------------------------------------ #
    _hr("第 2 组：职位")
    pos_ids: dict[str, int] = {}
    for code, name, level, seq in POSITIONS:
        existing = repo.get_position_by_code(code)
        if existing:
            pos_ids[code] = existing.id
            print(f"  已存在 {code:4} {name}（职级 {existing.level}）→ 保持不变")
            continue
        print(f"  待创建 {code:4} {name}（职级 {level} / {seq}）")
        if args.apply:
            pos_ids[code] = repo.create_position(code=code, name=name, level=level, sequence=seq)
    if args.apply:
        for code, _n, _l, _s in POSITIONS:
            if code not in pos_ids:
                pos_ids[code] = repo.get_position_by_code(code).id

    # ------------------------------------------------------------------ #
    _hr("第 3 组：员工")
    issued: list[tuple[str, str, str, str]] = []
    for uname, emp_no, name, dept, pos, role, status, phone in EMPLOYEES:
        existing = repo.get_by_username(uname)
        if existing and not args.reset_password:
            if args.apply:
                # 只同步资料；密码、must_change_password、status 一律不碰
                repo.update_profile(
                    existing.id, display_name=name, phone=phone,
                    department_id=dept_ids[dept], position_id=pos_ids[pos],
                )
            print(f"  已存在 {uname:14} {name} → 只同步资料，**不动密码、不动状态**")
            continue
        tmp = policy.generate_temporary_password()
        # 已离职的员工**不签发凭据**。他在 verify() 里会先被 status 拦掉、
        # 本来就登不进来，但若给他 must_change_password=1，
        # 将来 P2-13c 的「待首次改密」看板就会把一个离职的人算进去 ——
        # 那种数字会让看板一上来就少一条可解释的记录。
        issue_credential = status != repo.STATUS_RESIGNED
        print(f"  {'待重置密码' if existing else '待创建  '} {uname:14} {name}"
              f"（{emp_no} / {role} / {status}）"
              f"{'' if issue_credential else '  ← 已离职，不签发凭据'}")
        if not args.apply:
            continue
        violations = policy.validate_strength(tmp, username=uname, employee_no=emp_no)
        if violations:  # pragma: no cover - generate_temporary_password 保证不会发生
            raise SystemExit(f"生成的临时密码没通过强度校验：{violations}")
        target = existing.id if existing else repo.create_user(
            username=uname, employee_no=emp_no,
            password_hash=policy.hash_password(tmp),
            display_name=name,
            email=f"{uname}@example.com",
            phone=phone,
            department_id=dept_ids[dept], position_id=pos_ids[pos],
            role=role, status=status,
            must_change_password=issue_credential,
            password_changed_at=now_db() if issue_credential else None,
        )
        if existing:
            repo.update_password(target, policy.hash_password(tmp), must_change_password=issue_credential)
        if issue_credential:
            repo.add_password_history(target, policy.hash_password(tmp))
            repo.prune_password_history(target, 5)
            issued.append((uname, emp_no, name, tmp))

    # ------------------------------------------------------------------ #
    _hr("第 4 组：部门负责人")
    for dept_code, leader in LEADERS.items():
        print(f"  {dept_code:5} 负责人 = {leader}")
        if args.apply:
            repo.set_department_leader(dept_ids[dept_code], repo.get_by_username(leader).id)

    if not args.apply:
        print("\n" + "=" * 74)
        print("干跑结束。确认无误后加 --apply。")
        print("=" * 74)
        return 0

    # ------------------------------------------------------------------ #
    _hr("临时密码（只显示这一次）")
    if not issued:
        print("  （没有新签发 / 重置的密码 —— 重跑不动密码是刻意的）")
    else:
        print(f"  {'登录名':16}{'工号':8}{'姓名':10}临时密码")
        for uname, emp_no, name, tmp in issued:
            print(f"  {uname:16}{emp_no:8}{name:10}{tmp}")
        print("\n  ⚠️ 这一屏只出现这一次。它们已进user_password_history，")
        print("     且每个人的 must_change_password=1 —— 首次登录必须改密。")
        print("  ⚠️ 脚本走 print 不走 logging，所以这些密码不会进 app.log。")

    if args.credentials_file:
        path = args.credentials_file
        if _git_tracked(path):
            print(f"\n❌ 拒绝写入 {path}：它已被 git 跟踪，密码进仓库等于泄漏。")
            print("   请换一个被 .gitignore 忽略的路径（或先把它加进 .gitignore）。")
            return 1
        path.write_text(
            "username,employee_no,display_name,temporary_password\n"
            + "".join(f"{u},{e},{n},{p}\n" for u, e, n, p in issued),
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        print(f"\n✅ 凭据已写入 {path}（权限 600）")

    # ------------------------------------------------------------------ #
    _hr("结果")
    with get_engine().connect() as conn:
        for table in ("department", "position", "user"):
            n = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
            print(f"  {table:12} 共 {n} 行")
        forced = conn.execute(
            text("SELECT COUNT(*) FROM `user` WHERE must_change_password=1")
        ).scalar()
        hist = conn.execute(text("SELECT COUNT(*) FROM user_password_history")).scalar()
        print(f"  待首次改密：{forced} 人")
        print(f"  改密历史：{hist} 条")
    print("\n" + "=" * 74)
    print("完成。下一个动作：P2-11c 把网关改成查这张表（现在登录仍走 .env）。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())