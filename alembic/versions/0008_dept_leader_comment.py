"""订正 `department.leader_user_id` 的列注释（P2-14 作废 D13 时漏改迁移）

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11

--------------------------------------------------------------------------
这是一个**只改注释**的迁移，为什么值得单独一个版本
--------------------------------------------------------------------------
2026-10-08 作废了 P2-14 的 D13（「部门负责人默认是他那个部门共享知识库的 writer」），
当时只改了 `core/schema.py` 的列注释，**没有**同步已经应用过的迁移 0003。

于是 `compare_metadata()` 从那天起就一直是红的，表现为
`make test-sandbox` 里 `test_module8_mysql_session_store` 恒定 82/1，
而**这个失败会中断整轮沙箱回归**（脚本 `set -e` 遇错即停），
后面 9/10/11/12/13/15/18 全部模块**根本没有跑** ——
也就是说：一条注释漂移，让整个回归门禁失效了三个版本。

教训（比这次修复本身值钱）：
    **回归脚本在第一个失败处停下，会让「只有一条失败」变成「只剩一条被执行」。**
    一个已知的、与当前改动无关的红灯，代价不是「多一条红」，
    而是后面所有断言的覆盖全部静默消失。

--------------------------------------------------------------------------
为什么是改迁移而不是改 schema.py
--------------------------------------------------------------------------
两边谁对？**schema.py 那版是对的** —— 它描述的是现实（写权限由 `user.kb_role`
授予，见 `core/kb_acl.py`），0003 那版描述的是一个已被作废的旧设计。
把 schema.py 改回旧注释会让「结构与视图一致」这条守卫
反过来保护一个错误的事实。

为什么不直接改 0003：它已经在本地与服务器上应用过了，
改一个已应用的迁移对已存在的库**完全无效**（alembic 只认版本号），
只会让「全新安装」与「老库升级」走向分叉。正确做法永远是**新加一个迁移**。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_NEW_COMMENT = (
    "部门负责人 user.id；知识库是全公司共用的，写权限改由 user.kb_role 授予"
    "（见 core/kb_acl.py），本列不再隐含「负责人默认能写」"
    "——2026-10-08 原 D13 已作废"
)


def upgrade() -> None:
    # ⚠️ MySQL 改注释必须带**完整列类型**（`MODIFY COLUMN` 的语义），
    # 只写 comment 会把列类型一起改掉 —— 所以这里用 existing_type 原样回填。
    op.alter_column(
        "department",
        "leader_user_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        comment=_NEW_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        "department",
        "leader_user_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        comment="部门负责人 user.id；按 P2-14 的 D13，默认是该部门共享知识库的 writer",
    )
