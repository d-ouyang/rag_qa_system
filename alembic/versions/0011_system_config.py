"""system_config：运行时业务配置表（v4.0.0 P4-1 ConfigService 的真相源）

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16

--------------------------------------------------------------------------
这张表为什么存在
--------------------------------------------------------------------------
pydantic-settings 在进程启动那一刻冻结 `.env`：改任何业务开关都要
recreate 容器才生效。对基础设施配置（连接串 / 密钥 / 端口）这是应得的代价，
但对「检索开关 / 模型参数 / 缓存层」这类业务开关是纯运维摩擦。

P4-1 起业务开关的真相源迁到本表（core/config_service.py 是读写门面）：
    · 管理端（P4-2）改开关 → 落本表 → 进程内缓存即时更新，**无需重启**；
    · 请求链路只读进程内缓存（TTL 回源兜底），零额外 DB 开销；
    · 基础设施配置仍留 `.env` —— 两类配置的分界写在 settings.py
      CONFIG_BACKEND 的注释里，别把密钥/连接串也搬进来。

--------------------------------------------------------------------------
为什么 value 用 JSON 文本而不是按类型分列
--------------------------------------------------------------------------
开关类型会不断增加（bool/int/float/str/enum），分列等于每加一种类型就
改一次表；JSON 文本 + value_type 标注让「加一种类型」只是应用侧的事。
类型校验在应用层（set_flag 拒绝跨类型改写，get_flag 按 default 的类型守卫），
不依赖 DB 约束 —— 与项目「判定逻辑集中在 core/*.py」的既有约定一致。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("config_key", sa.String(128), nullable=False,
                  comment="配置键（如 query_rewrite）；不用裸 key 作列名——KEY 在 MySQL 关键字清单里，带前缀省掉反引号的麻烦"),
        sa.Column("config_value", sa.Text(), nullable=False,
                  comment="配置值，JSON 序列化（bool/int/float/str 原生类型）；不按类型分列，加类型不改表"),
        sa.Column("value_type", sa.String(16), nullable=False, server_default="str",
                  comment="值类型标注：bool/int/float/str；set_flag 拒绝跨类型改写，get_flag 按此守卫"),
        sa.Column("description", sa.String(255), nullable=False, server_default="",
                  comment="人类可读说明；管理端设置页展示，空串=未填"),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False,
                  comment="最后修改时间；由应用显式写入（now_db() 口径），不依赖 DB 时区"),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default="",
                  comment="最后修改人（用户名或 system）；审计用，不建 FK——配置修改不随账号删除而失效"),
        sa.PrimaryKeyConstraint("config_key"),
        comment="运行时业务配置（P4-1）：业务开关热更新的真相源；基础设施配置仍留 .env，见 settings.py CONFIG_BACKEND",
        **TABLE_KW,
    )


def downgrade() -> None:
    op.drop_table("system_config")
