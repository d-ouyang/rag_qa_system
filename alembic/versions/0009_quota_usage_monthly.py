"""P3-6：月度用量快照表 quota_usage_monthly（归档审计用，判定路径不读它）

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-12

--------------------------------------------------------------------------
这张表为什么存在（P3-1 改变了前提）
--------------------------------------------------------------------------
拦截（P3-1）上线后，「这个月用了多少」不再是看板上的一行数字，
而是**执法依据** —— 被拦的员工与管理者之间可能就它发生争议。

争议需要证据，而当前的证据只有一份：`chat_message.usage_*` 明细。
明细的问题是它**寄居在聊天记录里**：将来一旦做聊天记录的清理 / 归档
（几乎必然会做，chat_message 是全库增长最快的表），
用量历史会跟着聊天记录一起消失 —— 那时「他上个月到底用没超」就死无对证。

所以把「每个账期每人用了多少」定期抄一份进自己的表，只增不删。

--------------------------------------------------------------------------
⚠️ 它是**第二份真相** —— 刻意的，且边界划死
--------------------------------------------------------------------------
2026-10-07 的 bug（见 `quota_repo.usage_by_user` 里「两段聚合」的注释）证明过
「存一份算得出来的数据」的代价：两份事实迟早不一致，不一致时无法判决。

所以本表的边界是：

    · **判定路径绝不读它** —— 预检（P3-1）与看板（P2-15de）仍走
      `quota_repo` 实时聚合，明细在，实时算永远是准的；
    · 它只服务**归档对账**：明细被清理后，「历史用量」只有这里有；
    · 数值来源就是 `usage_by_user()` 本身（同一段 CTE），
      两份事实在写入瞬间**逐位相等**，对账是恒等式而不是近似。

--------------------------------------------------------------------------
为什么 user_id 不加外键（同 audit_log 的取舍）
--------------------------------------------------------------------------
人可以被删（离职清理），但「他 10 月用了 8 万 token」是已发生的事实，
不随账号消失。加了外键 + CASCADE，删人会连带抹掉用量档案 ——
那正是这张表要防的事。

--------------------------------------------------------------------------
为什么用 upsert（ON DUPLICATE KEY UPDATE）而不是 append
--------------------------------------------------------------------------
beat 每天跑一次、只算**当前账期**：今天的数字覆盖昨天写的（同一主键），
历史账期**永不触碰**。「快照不回退」由此保证：chat_message 删掉几条记录后，
当天重算只会**变少**而不会消失 —— 覆盖写发生在**当期**，
已过去的账期没有任何写入路径（见 core/quota_snapshot.py 的 period 防御）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "quota_usage_monthly",
        sa.Column("user_id", sa.BigInteger(), nullable=False,
                  comment="员工 user.id；不加外键：人删了，历史用量仍是审计事实（同 audit_log 的取舍）"),
        sa.Column("period", sa.String(7), nullable=False,
                  comment="账期 'YYYY-MM'（自然月）；口径=quota_repo.period_bounds（start_day=1）"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0",
                  comment="当期输入 token 合计；口径=usage_by_user（消息按会话收敛后再 SUM，防重复计数）"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0",
                  comment="当期输出 token 合计；口径同 input_tokens"),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0",
                  comment="当期 assistant 消息条数（user 消息不是一次请求）"),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False,
                  comment="本行最近一次覆盖写时间（每日低峰 beat 重算当期；历史账期无写入路径）"),
        sa.PrimaryKeyConstraint("user_id", "period"),
        # 对账要「按账期拉全表」（快照 vs 实时聚合逐人对比），主键帮不上 —— 单独建
        sa.Index("idx_quota_monthly_period", "period"),
        comment=(
            "月度用量快照（P3-6）：归档审计用；"
            "判定路径不读它——预检与看板仍走 quota_repo 实时聚合（chat_message 明细在，实时算永远准）"
        ),
        **TABLE_KW,
    )


def downgrade() -> None:
    op.drop_table("quota_usage_monthly")
