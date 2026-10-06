"""P2-11b：改密历史表（历史不可复用）

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06

--------------------------------------------------------------------------
这张表回答的是哪个问题
--------------------------------------------------------------------------
「员工不能复用自己最近用过的密码」是企业密码策略里最常见、也最容易被
口头答应、实际没落地的一条。原因很简单：它需要**历史**——而绝大多数系统
只存「当前这一个哈希」。当前哈希只能回答「对不对」，回答不了「是不是
刚用过」。

所以这张表存的是**历次改密的哈希**，判定时逐条 `bcrypt.compare`。

--------------------------------------------------------------------------
为什么逐条比对，而不是给密码算一个指纹来O(1) 判断
--------------------------------------------------------------------------
指纹方案（`HMAC-SHA256(secret, password)` 存一列）确实快得多，一次哈希
就能判重。否掉它的理由有两个：

1. **多一份需要小心保管的密钥**。指纹密钥一旦泄露，攻击者可以直接对
   候选口令批量比对；多一处秘密就多一处要轮转、要备份、不能进日志的地方。
2. **它只在「已存哈希」这一处引入新概念**，而收益只是把改密那次动作从
   ~0.85s 降到 ~0.17s —— 这个动作一天可能只发生一次。

代价（明写下来，别当没有）：改密时逐条比对。**本机实测 cost 12 单次约165ms**
（M 系列；换机器数字会变，这里只说明量级），保留 5 条约 0.85s。这个耗时只发生在
「改密」这一次动作上，不在登录路径上（登录只比对 1 条），所以可以接受。真嫌慢就把
`PASSWORD_HISTORY_KEEP` 调小，或把 `BCRYPT_COST` 降一档。

--------------------------------------------------------------------------
为什么保留「最近 5 条」而不是「全部历史」
--------------------------------------------------------------------------
全部历史有两个问题：一是无限增长，二是**并不能真的提高安全性** ——
能拿到的历史哈希越多，攻击者能离线验证的组合也越多，而这跟「你最近用过
哪几个」无关。NIST SP 800-63B 的建议也是限制历史深度而不是留全量。

所以：`changed_at` 之外没有归档机制，超出保留条数的旧记录由应用层裁掉
（`user_repo.prune_password_history`）。

--------------------------------------------------------------------------
为什么 `changed_by_user_id` 可空
--------------------------------------------------------------------------
两种改密来源都必须记：本人自助改密（没有管理员，这时为空）与管理员重置
（`changed_by_user_id` 是管理员 id）。留空表示「本人」，不用拿0 或者
负数去占位 —— 后者会变成一个能通过 FK 的假用户 id（与0001 里
`folder.user_id` 不用 0 是同一个理由）。

--------------------------------------------------------------------------
不建外键、也不建 `password_changed_at` 在这张表里的冗余列
--------------------------------------------------------------------------
不建 FK：沿用 0001/0003 的决定。`user` 行不会删（离职走 `status=resigned`），
所以历史不会变成孤儿；真要清理也由应用层显式做。

不冗余「这次改密的时间」到 `user` 表以外的地方：`user.password_changed_at`
已经记了最近一次改密时间，这张表记的是**每一次**的时间，两者语义不同，
不能互相替代也不要互相回填。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}
_TS = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "user_password_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="员工 user.id"),
        sa.Column(
            "password_hash", sa.String(255), nullable=False,
            comment="当时的 bcrypt 哈希；用于「历史不可复用」的逐条比对",
        ),
        sa.Column("changed_at", mysql.DATETIME(fsp=6), nullable=False, comment="这次改密发生的时间"),
        sa.Column(
            "changed_by_user_id", sa.BigInteger(), nullable=True,
            comment="操作人 user.id；管理员重置时是管理员 id，本人改密为空",
        ),
        sa.PrimaryKeyConstraint("id"),
        # 支持「取某个员工最近 N 条」—— 没有这个索引，每次改密都要全表扫
        sa.Index("idx_pwh_user_changed", "user_id", "changed_at"),
        comment="改密历史（P2-11b；只追加，超出保留条数的旧记录会被裁掉）",
        **TABLE_KW,
    )


def downgrade() -> None:
    # 整表删掉 = 历史全丢。代价记下来：downgrade 之后「不能复用最近密码」
    # 这条策略事实上失效，直到应用重新写满 5 条。不可再生，但也不是业务资产
    # （真正的资产是 user.password_hash 里的那一个）。
    op.drop_table("user_password_history")