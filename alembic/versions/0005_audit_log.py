"""P2-13d：审计日志表（谁 / 何时 / 对谁 / 做了什么 / 从哪个 IP）

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07

--------------------------------------------------------------------------
这张表回答的是哪个问题
--------------------------------------------------------------------------
「出事了，谁动过这个人？」

人事与密码操作的特点是**后果不可逆且不可自查**：把一个离职员工的账号悄悄复职、
把别人的密码重置掉、给某个人降级——做的人记得很清楚，被做的人不会知道，
而事后没有任何痕迹可以回答「上周三到底谁把陈杰的密码重置了」。

所以这张表**只追加**：写下即定案。

--------------------------------------------------------------------------
为什么「不可改不可删」要在**表结构**上做，而不是靠约定
--------------------------------------------------------------------------
靠约定（「谁都不许改审计表」）是最常见的做法，也是最容易失效的做法：
一个紧急修复就能把一行 UPDATE 掉，且没有任何东西会报警。

这里的做法是**结构上不给改的机会**：

    · 仓储层 `core/audit_repo.py` 只有 `record()` 与 `list_logs()`，**没有
      update / delete**；
    · 表里**没有** `update_time`、**没有** `deleted_at` —— 没有这些列，
      「顺手更新一下」「软删掉」在 SQL 层面就不成立；
    · 唯一的 DDL 入口是迁移，而迁移里只有 `drop_table`（downgrade）。

日志本身**不做归档、不做清理**：审计的价值随时间上升，只增不减是刻意的。
代价要说清楚：这是一张只涨的表，长期跑要预留磁盘与备份空间
（生产上线时按 §0.6 的备份项一起处理）。

--------------------------------------------------------------------------
为什么冗余存 actor_username / actor_role / target_label，而不建外键去 join user
--------------------------------------------------------------------------
三个理由，第一个是**必须**，后两个是**划算**：

1. **break-glass 超管在 `user` 表里没有行。** `.env` 里那个 `admin`（运维后门）
   走的是 `identity_source=breakglass`，它不是员工账号 —— 外键根本挂不上。
   审计任何「谁做的」，都必须允许操作人没有 user.id。
2. **人事会改名、离职会换状态。** 日志要在半年后还能读懂「当时是周妍动的手」，
   而 `display_name` 可能已经改了、那个人可能已经离职。
3. **审计查询不该被 join 拖慢。** 它是出事时第一个要查的东西，宁可多存几行字符串。

`actor_user_id` 仍然留着 —— 它让「这个人一共动过多少次」可以走索引，
但它**可空**（理由同 0004 的 `changed_by_user_id`：不用 0 或 -1 去占位）。

--------------------------------------------------------------------------
detail 为什么是 TEXT 而不是 MySQL JSON 列
--------------------------------------------------------------------------
三个理由，前两个是实际的：

1. **MySQL 的 JSON 列不能有 DEFAULT**（11a 建表时踩过：`server_default` 建不上），
   而审计要能记「这次动作没有附加信息」这种最普通的情况。
2. **JSON 列的取值校验在应用层之前** —— 塞进去的字符串格式不受控，
   查询时反而要处理「这一行解析失败」。
3. 文本 + `json.dumps(ensure_ascii=False)` 出来的结果**逐字节可测**，
   断言里能直接写「detail 里不该出现明文密码」这种判据。

敏感信息的防线在**应用层**（`audit_repo.record()` 会拒绝带
password / token / 哈希特征的键与值），不在这一层。

--------------------------------------------------------------------------
不建外键
--------------------------------------------------------------------------
沿用 0001 / 0003 / 0004 的决定：`user` 行不会删（离职走 `status=resigned`），
所以引用不会变成孤儿；真要清理也由应用层显式做。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True,
                  comment="操作人 user.id；break-glass 超管没有 user 行，此时为空"),
        sa.Column("actor_username", sa.String(64), nullable=False,
                  comment="操作人登录名（冗余存一份：超管不在 user 表、人也会改名）"),
        sa.Column("actor_role", sa.String(16), nullable=True,
                  comment="操作当时的系统角色；事后权限变了，日志仍按当时记"),
        sa.Column("action", sa.String(48), nullable=False,
                  comment="动作标识，如 user.create / user.status.change / password.reset"),
        sa.Column("target_type", sa.String(32), nullable=False,
                  comment="对象类型：user / department / position / auth"),
        sa.Column("target_id", sa.BigInteger(), nullable=True, comment="对象 id；无法定位时为空"),
        sa.Column("target_label", sa.String(160), nullable=True,
                  comment="对象可读标签（如 chen.jie（陈杰））；冗余，防对象改名后读不懂"),
        sa.Column("detail", sa.Text(), nullable=True,
                  comment="附加信息（JSON 文本）；**绝不含密码明文与哈希**，写入前应用层会拒绝"),
        sa.Column("ip", sa.String(45), nullable=True,
                  comment="来源 IP；45 字符是 IPv6 的最长表示（含 IPv4 映射）"),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False,
                  comment="发生时间；由应用显式写入，不依赖 DB 时区"),
        sa.PrimaryKeyConstraint("id"),
        # 四个索引对应四种查法：时间线（出事先看这个）、按人（这人动过多少次）、
        # 按对象（这个人的全部痕迹）、按动作（一次批量操作的全过程）
        sa.Index("idx_audit_created", "created_at"),
        sa.Index("idx_audit_actor", "actor_user_id", "created_at"),
        sa.Index("idx_audit_target", "target_type", "target_id"),
        sa.Index("idx_audit_action", "action"),
        comment="审计日志（P2-13d；只追加，无 update_time / deleted_at —— 改不了也删不掉）",
        **TABLE_KW,
    )


def downgrade() -> None:
    # 整表删掉 = 审计历史全丢，且**不可再生**（没有任何地方会重新生成这些行）。
    # 这正是「日志不可改不可删」这条规矩的代价：想销毁它只能销毁整个库。
    # 所以这条迁移只应该出现在「开发期回滚」与「测试库重建」里。
    op.drop_table("audit_log")