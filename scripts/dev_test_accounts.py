#!/usr/bin/env python
"""
给你手动测「会话隔离」用的一对**固定密码**账号。

为什么需要这个脚本（不是多余功夫）：
- 种子脚本给每个人的都是**随机**临时密码，只显示一次 → 你不可能知道
- 验收脚本每次跑完会把密码换成**新的随机值** → 连开发者自己都进不去
- 所以「能不能自己手动测」这件事本身被挡住了 —— 这不是隔离的问题，是**凭据问题**

所以这里显式造两个**固定密码、平级、都是普通员工**的账号：
- 固定密码的好处：你能自己反复登录，不用每次叫人
- 刻意不给 admin/admin/hr：**要验的恰恰是「权限再高也看不到别人的问答」**，
  用管理员会让「他权限更高所以能看到」成为一个合理解释
- ⚠️ 两个账号都是 must_change_password=0，否则每次登录都被导去改密页，
  你根本进不到问答界面（那是「员工自助改密入口还不存在」导致的，见 11c/13c）

⚠️ **这是本机开发用的账号，不要带到生产**。
    生产形态下密码由管理员在管理端重置发放（明文只显示一次）。

用法：
    .venv/bin/python scripts/dev_test_accounts.py            # 干跑，只报告
    .venv/bin/python scripts/dev_test_accounts.py --apply    # 真设
    .venv/bin/python scripts/dev_test_accounts.py --reset    # 改回随机临时密码 + 强制改密
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import text  # noqa: E402

from core import password_policy as policy  # noqa: E402
from core.db import get_engine  # noqa: E402

# 固定密码。强度按 11b 的策略走（长度 12，含大小写 + 数字 + 符号，无易混字符）
TEST_ACCOUNTS = [
    # (登录名, 固定密码)
    ("chen.jie", "DevTest2026!Aa"),# 陈杰 —— 平级员工
    ("zhao.min", "DevTest2026!Bb"),      # 赵敏 —— 平级员工
]


def report(apply_: bool, reset: bool) -> None:
    print("=" * 68)
    print("  会话隔离手动测试账号")
    print("=" * 68)
    mode = "真设（--apply）" if apply_ else ("改回随机（--reset）" if reset else "干跑（不改数据）")
    print(f"  模式：{mode}")
    print()
    with get_engine().connect() as c:
        for username, pwd in TEST_ACCOUNTS:
            row = c.execute(
                text(
                    "SELECT id, role, status, must_change_password FROM `user` WHERE username=:u"
                ),
                {"u": username},
            ).first()
            if row is None:
                print(f"  ✗ {username:12} 不在库里 —— 先跑 make seed-users-apply")
                continue
            oid, role, status, must_change = row
            print(f"  · {username:12} id={oid} role={role:5} status={status}")
            print(f"    密码：{pwd if not reset else '（改为随机临时密码，你看不到）'}")
    print()
    if not apply_ and not reset:
        print("  这是干跑。确认无误后加 --apply。")
    print()


def do_apply() -> None:
    for username, pwd in TEST_ACCOUNTS:
        # validate_strength 返回**违规项列表**（空 = 通过），不是 bool/对象。
        # 逐条打出来而不是只说「不合法」—— 与管理端同一口径。
        violations = policy.validate_strength(pwd, username=username)
        if violations:
            raise SystemExit(f"密码不符合 11b 策略（{username}）：" + "；".join(violations))
        with get_engine().begin() as c:
            exists = c.execute(
                text("SELECT id FROM `user` WHERE username=:u"), {"u": username}
            ).scalar()
            if exists is None:
                raise SystemExit(f"账号不存在：{username}（先跑 make seed-users-apply）")
            # must_change_password=0：否则登录后被导去改密页，进不到问答界面
            c.execute(
                text(
                    "UPDATE `user` SET password_hash=:h, must_change_password=0,"
                    " failed_login_count=0, locked_until=NULL WHERE username=:u"
                ),
                {"h": policy.hash_password(pwd), "u": username},
            )
    print(f"  ✓ 已设 {len(TEST_ACCOUNTS)} 个账号的固定密码（must_change_password=0）")


def do_reset() -> None:
    with get_engine().begin() as c:
        for username, _ in TEST_ACCOUNTS:
            c.execute(
                text(
                    "UPDATE `user` SET password_hash=:h, must_change_password=1,"
                    " failed_login_count=0, locked_until=NULL WHERE username=:u"
                ),
                {
                    "h": policy.hash_password(policy.generate_temporary_password()),
                    "u": username,
                },
            )
    print("  ✓ 已改回随机临时密码 + 强制改密（恢复到种子形态）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真设固定密码")
    ap.add_argument("--reset", action="store_true", help="改回随机临时密码 + 强制改密")
    args = ap.parse_args()

    if args.apply and args.reset:
        raise SystemExit("--apply 与 --reset 不能同时给")

    report(args.apply, args.reset)
    if args.apply:
        do_apply()
    elif args.reset:
        do_reset()