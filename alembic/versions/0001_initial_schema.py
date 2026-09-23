"""P0-1 初始表结构：user / folder / session / chat_message / document

Revision ID: 0001
Revises:
Create Date: 2026-09-23

--------------------------------------------------------------------------
【重要】为什么这份 DDL 是手写的、且刻意不 import core/schema.py
--------------------------------------------------------------------------
Alembic 的迁移脚本是**冻结的历史快照**，不是「当前模型」的别名。

如果迁移里写 `from core.schema import metadata; metadata.create_all(bind)`，
那么三个月后你给 session 表加一列时，**所有历史迁移会一起改行为** ——
一个全新的空库跑 `upgrade head` 会直接建成最新结构，中间那些
「加列 / 回填 / 删旧列」的迁移全部失去意义，而且在已有数据的库上重跑会撞车。
这就是迁移必须自包含的原因：宁可字母面上重复一遍列定义。

代价是「DDL 与 core/schema.py 可能漂移」。这个代价由
`tests/test_module8_mysql_session_store.py` 里的一项**结构一致性断言**兜住：
反射线上库得到的列集合 vs core/schema.py 的列集合，两者必须相等。

--------------------------------------------------------------------------
其它设计取舍（都写在最容易被问「为什么」的地方）
--------------------------------------------------------------------------
· 不用 FOREIGN KEY：本项目的删除链路是应用层显式三件事（磁盘 / Chroma / MySQL 记录），
  FK 会引入隐式级联与额外的锁顺序约束；且 user / folder 本期未启用，
  FK 只会变成后续变更的摩擦。完整性靠 Store 层的单事务写入保证。
· status 用 VARCHAR 不用 ENUM：ENUM 加值要 ALTER TABLE（且是表级 DDL，会锁表），
  而解析状态机在本项目里还会长（P0-3 之后可能加 canceled 等）。
  取值集合写在列注释里，靠应用层校验。
· id 用有符号 BIGINT 不用 UNSIGNED：自增不会走到 2^63，
  而 UNSIGNED 在跨库/ORM 比较时会引入一堆「负数溢出」的边界噪音，收益为零。
· 不冗余建索引：`uk_msg_session_seq(session_id, seq)` 已能同时服务
  `WHERE session_id=? ORDER BY seq`，再加一个 (session_id) 索引纯属写放大。
· `last_active_at` 用 DATETIME(6)（微秒精度）：会话 TTL 判定是
  `now - last_active > ttl`，测试里常用 ttl_seconds=0/1。若只有秒精度，
  0.4 秒的间隔会被截断成 0，导致「本该过期」的会话判成未过期，
  语义直接错。见 p0.1 迭代文档「已知边界」一节的时区说明。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 全库统一字符集：utf8mb4 才能存 emoji（utf8mb3 存 4 字节字符会直接报错）
TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}

# 建表时写死的时间戳默认值。写成模块级常量，避免五张表各写一遍字符串写错。
_TS_DEFAULT = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # user：本期只建表备用。网关账号仍走 .env（PLAN §11 决策 D2），
    #       建出来是为了下次切换用户体系时不必再动 DDL。
    # ------------------------------------------------------------------ #
    op.create_table(
        "user",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(64), nullable=False, comment="登录名，唯一"),
        sa.Column("display_name", sa.String(128), nullable=False, server_default="", comment="展示名"),
        sa.Column("password_hash", sa.String(255), nullable=False, server_default="",
                  comment="bcrypt 哈希；绝不放明文"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=_TS_DEFAULT),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uk_user_username"),
        comment="用户表（本期未启用，见 PLAN-v2.0.0 §11 D2）",
        **TABLE_KW,
    )

    # ------------------------------------------------------------------ #
    # folder：会话分组。本期只建表，不做 UI。
    # ------------------------------------------------------------------ #
    op.create_table(
        "folder",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # 可空：语义是「尚未绑定用户」。不用 NOT NULL DEFAULT 0 —— 0 是伪用户 id，
        # 会让「查某个用户的文件夹」这种查询在没绑定时悄悄返回别人的数据。
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=_TS_DEFAULT),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_folder_user", "user_id"),
        comment="会话文件夹（本期未做 UI）",
        **TABLE_KW,
    )

    # ------------------------------------------------------------------ #
    # session：会话主表。id 用前端生成的字符串，不用自增 ——
    #          会话 id 会出现在 URL / 前端 localStorage 里，自增 id 会泄露业务量。
    # ------------------------------------------------------------------ #
    op.create_table(
        "session",
        sa.Column("id", sa.String(64), nullable=False, comment="会话 id（前端生成）"),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=False, server_default="default",
                  comment="知识库/项目隔离维度"),
        sa.Column("folder_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("usage_input_token", sa.BigInteger(), nullable=False, server_default=sa.text("0"),
                  comment="累计输入 token（汇总列，明细在 chat_message，两者可对账）"),
        sa.Column("usage_output_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # ⚠️ 计划书只列了 input/output 两列，这里补到 4 列。原因：
        #    `MemoryManager._USAGE_ZERO` 实际维护 4 个键（input/output/cache_read/requests），
        #    少两列会让用量**无法原样往返** —— module6 契约测试直接断言 `usage["requests"] == 1`，
        #    漏了就直接红。补齐同类汇总列，比塞一个 JSON blob 更贴合「汇总列要能 SQL 查」的初衷。
        sa.Column("usage_cache_read_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("usage_requests", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # ⚠️ 刻意**不加** ON UPDATE CURRENT_TIMESTAMP：读会话列表（touch=False）
        #    绝不能刷新活跃时间，否则「扫一眼列表」就等于给所有会话无限续命。
        #    刷新与否必须由 Store 显式决定。
        sa.Column("last_active_at", mysql.DATETIME(fsp=6), nullable=False,
                  comment="最后活跃时间（微秒精度，供 TTL 判定）"),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("0"),
                  comment="软过期/归档标记；物理删除留到 P2-10 定保留策略"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=_TS_DEFAULT),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_session_last_active", "last_active_at"),
        sa.Index("idx_session_user_active", "user_id", "is_archived", "last_active_at"),
        sa.Index("idx_session_folder", "folder_id"),
        comment="会话主表（真相源）",
        **TABLE_KW,
    )

    # ------------------------------------------------------------------ #
    # chat_message：消息明细。MEDIUMTEXT 而非 TEXT ——
    #                TEXT 上限 64KB，用户粘一篇长文档就直接截断/报错。
    # ------------------------------------------------------------------ #
    op.create_table(
        "chat_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        # seq：会话内单调序号。为什么不用自增 id 排序 ——
        #      编辑重发会 UPDATE 中间某行再追加新行，id 序与逻辑序可能分叉；
        #      且 seq 参与唯一键，能在库层面挡住「同一轮被写两次」。
        sa.Column("seq", sa.Integer(), nullable=False, comment="会话内序号，从 0 起"),
        sa.Column("role", sa.String(16), nullable=False, comment="user | assistant | system"),
        sa.Column("content", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("usage_input_token", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("usage_output_token", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("ref_ids", sa.JSON(), nullable=True,
                  comment="引用切片 id 列表（只存 chunk_id，不存正文快照，见 PLAN §11 D3）"),
        # turn_meta 是本轮展示元数据的**权威副本**（sources/intent/usage/elapsed_ms/ts…）。
        # 为什么不能只靠 ref_ids/usage 那几列反推 exchange_meta ——
        #   ① 那些列是「有损投影」，反推不出 intent / elapsed_ms / standalone_question 等键；
        #      而 `GET /qa/history` 要把 meta 原样回填，丢了就是前端引用条数变 0 的回归；
        #   ② 反推不出「本轮到底有没有 meta」：一轮 meta 为空 dict 与完全没有 meta
        #      在投影列上长得一模一样，load 出来长度就不对了。
        # 有了它，「SQL NULL = 本轮无 meta」变成显式事实，exchange_meta 可精确还原。
        sa.Column("turn_meta", sa.JSON(none_as_null=True), nullable=True,
                  comment="本轮展示元数据原文（含 sources/intent/usage/耗时/时间戳）；NULL = 本轮无 meta"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=_TS_DEFAULT),
        sa.PrimaryKeyConstraint("id"),
        # 唯一键兼作 (session_id) 前缀索引，无需再建单列索引
        sa.UniqueConstraint("session_id", "seq", name="uk_msg_session_seq"),
        comment="会话消息明细",
        **TABLE_KW,
    )

    # ------------------------------------------------------------------ #
    # document：文档元数据。P0-3 启用（上传即返回 + Worker 异步解析）。
    #           注意这里**不存文件二进制**，只存磁盘相对路径。
    # ------------------------------------------------------------------ #
    op.create_table(
        "document",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="即 doc_id"),
        sa.Column("project_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("file_name", sa.String(255), nullable=False, comment="原始文件名，仅展示"),
        sa.Column("storage_path", sa.String(512), nullable=False,
                  comment="磁盘相对路径（uuid 文件名，防重名覆盖）"),
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # 取值：pending / parsing / success / fail（应用层校验，见文件头说明）
        sa.Column("status", sa.String(16), nullable=False, server_default="pending",
                  comment="pending|parsing|success|fail"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fail_reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
                  comment="解析尝试次数；配合 P0-3 的原子 UPDATE 抢任务"),
        sa.Column("upload_time", sa.DateTime(), nullable=False, server_default=_TS_DEFAULT),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path", name="uk_doc_storage_path"),
        sa.Index("idx_doc_project_status", "project_id", "status"),
        sa.Index("idx_doc_status", "status"),
        sa.Index("idx_doc_upload_time", "upload_time"),
        comment="文档元数据（P0-3 启用）",
        **TABLE_KW,
    )


def downgrade() -> None:
    # 逆序删。本项目五张表之间没有 FK，所以顺序其实不敏感，
    # 但仍按依赖方向写 —— 将来真加了 FK 时不至于忘掉这件事。
    op.drop_table("document")
    op.drop_table("chat_message")
    op.drop_table("session")
    op.drop_table("folder")
    op.drop_table("user")
