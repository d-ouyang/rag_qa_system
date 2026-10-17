"""qa_trace 加依据校验列：grounded / unsupported（v4.0.0 P4-6）

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-17

P4-6 Groundedness：答案生成后做「论断 vs 资料」依据比对，结果随轮镜像进
本表（q041 幻觉治理——阈值挡不住「擦边相关但无答案」，只有校验答案本身
才挡得住）。grounded=NULL = 未校验（校验关闭 / 判定失败 fail-open）。
列注释与 core/schema.py 逐字相同（compare_metadata 守着）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qa_trace",
        sa.Column("grounded", sa.Boolean(), nullable=True,
                  comment="依据校验结果（P4-6）：答案论断能否在上下文找到依据；NULL=未校验/校验关闭/判定失败（fail-open）"),
    )
    op.add_column(
        "qa_trace",
        sa.Column("unsupported", sa.JSON(), nullable=True,
                  comment="未被资料支持的论断清单（P4-6）；grounded=false 时非空"),
    )


def downgrade() -> None:
    op.drop_column("qa_trace", "unsupported")
    op.drop_column("qa_trace", "grounded")
