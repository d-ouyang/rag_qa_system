"""qa_trace 加 usage 归因列（v4.1.0-p2.3 / Phase ②3）

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-16

usage 归因（对齐 office B2）：每轮轨迹带上该轮 token 用量（input/output/
cache_read），qa_trace.id 自成 query_id——给定一条轨迹即得「谁(user_id)、
哪个会话、走了什么流程、花了多少 token」的完整归因。usage 真相源仍是
chat_message 的累计列，本表是**当轮镜像**（分析表自带数据，查询不用 join）。
列注释与 core/schema.py 逐字相同（compare_metadata 守着）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qa_trace",
        sa.Column("usage_input_tokens", sa.BigInteger(), nullable=True,
                  comment="该轮输入 token 用量镜像（P2-3 归因）；NULL=老数据/未记录"),
    )
    op.add_column(
        "qa_trace",
        sa.Column("usage_output_tokens", sa.BigInteger(), nullable=True,
                  comment="该轮输出 token 用量镜像（P2-3 归因）；NULL=老数据/未记录"),
    )
    op.add_column(
        "qa_trace",
        sa.Column("usage_cache_read_tokens", sa.BigInteger(), nullable=True,
                  comment="该轮缓存读 token 镜像（不参与 billable，仅展示）；NULL=老数据/未记录"),
    )


def downgrade() -> None:
    op.drop_column("qa_trace", "usage_cache_read_tokens")
    op.drop_column("qa_trace", "usage_output_tokens")
    op.drop_column("qa_trace", "usage_input_tokens")
