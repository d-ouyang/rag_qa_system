"""
五张表的 Python 侧定义（SQLAlchemy Core）—— 应用读写时用的类型化视图。

--------------------------------------------------------------------------
与 alembic/versions/0001_*.py 的关系（别把这两份合并）
--------------------------------------------------------------------------
    alembic/versions/*.py   DDL 的**历史快照**。写下去就不再改，
                            因为老迁移必须能在新代码下原样重放。
    本文件                  当前的**结构视图**。随业务演进改。

刻意不共享同一份定义，代价是「可能漂移」。这个代价由
`tests/test_module8_mysql_session_store.py` 的
`compare_metadata()` 断言兜住 —— 它用 Alembic 自己的比较器
拿本文件的 metadata 去 diff 线上库，diff 非空即测试失败。
比手写一堆「列名是否相等」强得多：列类型、可空性、索引、唯一键、列注释
的漂移它都能发现。

> 列注释与表注释**也必须**在这里写一份，否则 compare_metadata 会报
> modify_comment。看着像噪音，其实有价值：它逼着两份定义逐字段对齐，
> 而不是只对齐到「列名一样」这种粗粒度。

--------------------------------------------------------------------------
为什么用 Core Table 而不是 ORM declarative
--------------------------------------------------------------------------
会话的存取是「整条快照 diff 成行级写入」，天然是集合操作，不是对象图操作：
    · 读：一条 SELECT 取回全部消息 → 组装成 list[dict]
    · 写：算出「要 INSERT 哪几行 / UPDATE 哪一行 / DELETE 哪些行」后批量执行
用 ORM 反而要先把行映射成对象、再让 Session 去猜「你想 INSERT 还是 UPDATE」，
还得跟 identity map 斗智斗勇。Core 直接表达意图，也更贴近实际发出的 SQL。

--------------------------------------------------------------------------
一个容易踩的点：表名/列名与 MySQL 保留字
--------------------------------------------------------------------------
`SESSION` 与 `USER` 在 MySQL 8 里都是**非保留关键字**
（`INFORMATION_SCHEMA.KEYWORDS` 里 RESERVED=0），可直接用作表名。
真正要避开的是 `USAGE`（RESERVED=1）—— 所以用量列全都带前缀写成
`usage_input_token` / `usage_output_token`，不留裸 `usage` 列。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# 所有表共用一份 MetaData：compare_metadata() 必须一次拿到全部表，
# 分成多份会让 diff 出现「表不存在」的假告警。
metadata = sa.MetaData()

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}

user_table = sa.Table(
    "user",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("username", sa.String(64), nullable=False, comment="登录名，唯一"),
    sa.Column("display_name", sa.String(128), nullable=False, server_default="", comment="展示名"),
    sa.Column("password_hash", sa.String(255), nullable=False, server_default="",
              comment="bcrypt 哈希；绝不放明文"),
    sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("username", name="uk_user_username"),
    comment="用户表（本期未启用，见 PLAN-v2.0.0 §11 D2）",
    **TABLE_KW,
)

folder_table = sa.Table(
    "folder",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("user_id", sa.BigInteger(), nullable=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    sa.Index("idx_folder_user", "user_id"),
    comment="会话文件夹（本期未做 UI）",
    **TABLE_KW,
)

session_table = sa.Table(
    "session",
    metadata,
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
    sa.Column("usage_cache_read_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("usage_requests", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    # fsp=6 是必须的，不是「顺手写细一点」：会话 TTL 判定是 now - last_active > ttl，
    # 测试里常用 ttl_seconds=0/1；秒精度会把 0.4s 截断成 0，导致过期会话判成未过期。
    sa.Column("last_active_at", mysql.DATETIME(fsp=6), nullable=False,
              comment="最后活跃时间（微秒精度，供 TTL 判定）"),
    sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("0"),
              comment="软过期/归档标记；物理删除留到 P2-10 定保留策略"),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    sa.Index("idx_session_last_active", "last_active_at"),
    sa.Index("idx_session_user_active", "user_id", "is_archived", "last_active_at"),
    sa.Index("idx_session_folder", "folder_id"),
    comment="会话主表（真相源）",
    **TABLE_KW,
)

chat_message_table = sa.Table(
    "chat_message",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("session_id", sa.String(64), nullable=False),
    sa.Column("seq", sa.Integer(), nullable=False, comment="会话内序号，从 0 起"),
    sa.Column("role", sa.String(16), nullable=False, comment="user | assistant | system"),
    sa.Column("content", mysql.MEDIUMTEXT(), nullable=False),
    sa.Column("usage_input_token", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("usage_output_token", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("is_cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column("ref_ids", sa.JSON(), nullable=True,
              comment="引用切片 id 列表（只存 chunk_id，不存正文快照，见 PLAN §11 D3）"),
    # none_as_null=True 是**必须**的，不是风格问题：
    # SQLAlchemy 的 JSON 类型默认 none_as_null=False —— 此时 Python 的 None 会被序列化成
    # **JSON 字面量 null**（一个非 NULL 的列值），于是 `WHERE turn_meta IS NULL` 永远查不到，
    # 「本轮无 meta」的判定直接失效。开了它才是真 SQL NULL。
    sa.Column("turn_meta", sa.JSON(none_as_null=True), nullable=True,
              comment="本轮展示元数据原文（含 sources/intent/usage/耗时/时间戳）；NULL = 本轮无 meta"),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    # 唯一键兼作 (session_id) 前缀索引，无需再建单列索引
    sa.UniqueConstraint("session_id", "seq", name="uk_msg_session_seq"),
    comment="会话消息明细",
    **TABLE_KW,
)

document_table = sa.Table(
    "document",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="即 doc_id"),
    sa.Column("project_id", sa.String(64), nullable=False, server_default="default"),
    sa.Column("file_name", sa.String(255), nullable=False, comment="原始文件名，仅展示"),
    sa.Column("storage_path", sa.String(512), nullable=False,
              comment="磁盘相对路径（uuid 文件名，防重名覆盖）"),
    sa.Column("file_size", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("status", sa.String(16), nullable=False, server_default="pending",
              comment="pending|parsing|success|fail"),
    sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("fail_reason", sa.String(512), nullable=False, server_default=""),
    sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
              comment="解析尝试次数；配合 P0-3 的原子 UPDATE 抢任务"),
    sa.Column("upload_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("storage_path", name="uk_doc_storage_path"),
    sa.Index("idx_doc_project_status", "project_id", "status"),
    sa.Index("idx_doc_status", "status"),
    sa.Index("idx_doc_upload_time", "upload_time"),
    comment="文档元数据（P0-3 启用）",
    **TABLE_KW,
)

__all__ = [
    "metadata",
    "user_table",
    "folder_table",
    "session_table",
    "chat_message_table",
    "document_table",
]
