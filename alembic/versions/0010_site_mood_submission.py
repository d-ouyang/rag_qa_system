"""site-mood：念安站心情收集表 site_mood_submission（独立于 RAG 业务的公开小表）

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14

--------------------------------------------------------------------------
这张表为什么存在
--------------------------------------------------------------------------
nianan.site（个人站，静态部署）底部有个「Are you happy today」表单，
提交 {email, mood}。此前前端 submitMoodNote() 是空实现——数据没有去处。
本迁移在 rag_qa 库里为它开辟独立的落点：复用同一家 MySQL 实例，
但与 RAG 业务表（user/session/document/...）**无任何外键关联**。

--------------------------------------------------------------------------
边界（与 P3-6 quota_usage_monthly 同源的取舍）
--------------------------------------------------------------------------
    · 公开提交口 POST /api/v1/mood/submit：免登录，但被 Redis 双闸守护
      （per-IP 固定窗口限流 + 邮箱冷却去重），闸门参数见 config/settings.py；
    · 查看口 GET /api/v1/mood/admin：独立静态 token（MOOD_ADMIN_TOKEN）
      常量时间比对，与 RAG 的 JWT / INTERNAL_SHARED_SECRET 刻意互不复用——
      这个功能独立，密钥泄露不殃及主系统；
    · email 明文存储（用户 2026-10-14 拍板）。属个人信息：它**绝不**出现在
      任何公开返回里，admin 返回体刻意不带 ip 列。
--------------------------------------------------------------------------
为什么 email 不做唯一约束
--------------------------------------------------------------------------
去重是「冷却窗口」语义（10 分钟内算重复，过期允许再次提交），
不是「全库唯一」语义。唯一约束会把冷却语义锁死在 DDL 里，
调窗口就得改表；冷却窗口放在 Redis，调参改 .env 就行。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "site_mood_submission",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False,
                  comment="访客邮箱（明文存储，属个人信息；仅 mood/admin 口可见，绝不进任何公开返回"),
        sa.Column("mood", sa.Numeric(3, 2), nullable=False,
                  comment="心情值：-1.00 低落 ~ 1.00 开心（前端滑条，步长 0.05）"),
        sa.Column("mood_word", sa.String(16), nullable=False, server_default="",
                  comment="派生标签：Sad/Neutral/Happy（阈值 ±0.2，与前端 moodWord 一致）"),
        sa.Column("page_url", sa.String(512), nullable=False, server_default="",
                  comment="提交时所在页面（取 Referer；总是首页，留着便于将来多页复用）"),
        sa.Column("ip", sa.String(45), nullable=True,
                  comment="来源 IP（XFF 第一跳，45 字符是 IPv6 最长表示）；仅限流与排查用，管理页不展示"),
        sa.Column("user_agent", sa.String(512), nullable=False, server_default="",
                  comment="浏览器 UA（超长截断），辅助识别爬虫"),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("0"),
                  comment="0=未读 1=已跟进；运营态标记，仅 admin 口读写"),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False,
                  comment="提交时间；由应用显式写入（now_db() 口径），不依赖 DB 时区"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_site_mood_created", "created_at"),
        sa.Index("idx_site_mood_email", "email"),
        comment="念安站心情收集（site-mood）：公开提交 + token 保护的独立小表，与 RAG 业务无外键关联",
        **TABLE_KW,
    )


def downgrade() -> None:
    op.drop_table("site_mood_submission")
