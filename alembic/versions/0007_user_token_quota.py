"""P2-15：Token 月度额度 `user.token_quota_monthly`（谁用超了、差多少）

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07

--------------------------------------------------------------------------
这一列为什么存在
--------------------------------------------------------------------------
P2-15 要回答的问题是「谁把token 用超了」。而**判定需要两个输入**：

    已用量  —— 已经在库里（`chat_message.usage_*`，P0-1b 就有了）
    额度    —— 此前**完全没有**

没有额度，「超了」这个词就没有参照系。
`TOKEN_QUOTA_DEFAULT_MONTHLY` 放`.env`（全站默认，改一次生效），
但**个人额度必须落在库里** —— 有人是重度用户，有人只是来问一句的，
用同一个数对所有人既不公平也没法解释。

--------------------------------------------------------------------------
为什么**不**做成一张 `token_quota` 表（一行一人一月）
--------------------------------------------------------------------------
看起来更「正规」（能存历史、能按月调整），但本项**刻意不做**：

  1. **历史用量不需要存** —— 它能从 `chat_message` 明细**重新算出来**
     （`core/quota_repo.py` 的 `SUM()`）。而「存一份算得出来的数据」意味着
     有了两份真相，且必须处理它们不一致的情形
     （明细被删了而汇总还在、或者某次口径改了要重算历史）。
  2. **额度调整历史已经由 `audit_log` 承担**（15e 要求「改别人的额度落审计」），
     再造一张带时间轴的表就是第二份历史。
  3. 「按月不同额度」这个需求**还没出现过**。真出现了再加表 ——
     加一列的迁移是分钟级的，而**加错了表结构之后要回头改的成本高得多**。

代价说清楚：**同一套额度标准适用所有月份**。
若将来要「旺季给临时额度」，需要 `token_quota` 表 + 一次迁移。

--------------------------------------------------------------------------
为什么列是 BIGINT 而不是 INT
--------------------------------------------------------------------------
`INT` 最多约 21亿。够吗？够—— 单人单月用 21 亿 token 大约是
几万次长文档问答，而本系统的问答上下文里带着检索到的切片，
单次通常在千级。**但这个上限不该由「我猜够用」来定**：
溢出的表现是 `BIGINT` 溢出报错（`OverflowError` 或 MySQL 报错），
而那会在**提交答案的路上**炸开 —— 也就是说，一次用量异常会让用户看到
一个与「token 太大」毫无关系的错误。`BIGINT` 的成本是每行多 4 字节。

--------------------------------------------------------------------------
为什么 server_default 是"0" 而不是 NULL
--------------------------------------------------------------------------
`quota_policy.effective_quota()` 把「≤ 0」统一当作**不限**
（理由见那个函数的 docstring：两边都用 0 才能只留一条判据）。
所以列是 `NOT NULL DEFAULT 0`，而不是 `NULL` 表示「用全局默认」。

⚠️ 计划书原文写的是「`NULL` = 用全局默认」。**这里改成了 0** ——
两者语义相同，但 0 少一个分支：`NULL` 需要 `IS NULL` 检查，
而 `.env` 的值本来就是整数（0 表示不限）。两种编码混用时
总会有一处写成另一种，于是「个人额度是 0」与「没设个人额度」分不清。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# ⚠️ **不**在库里建 CHECK 约束（与 kb_role 的取舍不同）：
# 取值校验在 `core/quota_policy.py` 与写入口（admin_service.set_token_quota）。
# 库里的 CHECK 会在 `admin_service` 之外多出一处判定 ——
# 而那处没法被 `tests/test_module16_quota.py` 的规则测试覆盖到。
# 负数额度在语义上等价于「不限」，所以即使有脏数据也不会算错钱，
# 只是会被`effective_quota` 当成不限 —— 这是可接受的降级，不是错误。


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "token_quota_monthly",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            # ⚠️ 这段注释必须与 `core/schema.py` 里 user.token_quota_monthly
            # 列的注释**逐字相同**，否则 `compare_metadata()` 会报
            # `modify_comment` —— 那是 P2-11a 立下的「结构与视图必须一致」
            # 守卫，diff 必须为 0。两处各写一份的原因见 schema.py 文件头。
            comment=(
                "月度token 额度(P2-15)：0=不限（用全局默认 TOKEN_QUOTA_DEFAULT_MONTHLY）；"
                "计费口径=输入+输出，不含cache_read（服务商侧 prompt 缓存，单价不同，混入会算错钱）"
            ),
        ),
    )
    # 索引：管理端看板要「找超额的人」，而这是**进入看板第一屏**的查询。
    # 不建索引的话退化成全表扫 —— 单次不致命，但它每次进管理端都跑一遍。
    op.create_index("idx_user_token_quota", "user", ["token_quota_monthly"])


def downgrade() -> None:
    op.drop_index("idx_user_token_quota", table_name="user")
    op.drop_column("user", "token_quota_monthly")
