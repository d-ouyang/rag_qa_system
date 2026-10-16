"""
业务表的 Python 侧定义（SQLAlchemy Core）—— 应用读写时用的类型化视图。

P2-11a 起是**八张**表：user / department / position / user_password_history /
audit_log / folder / session / chat_message / document。
前四张是组织、账号与审计（本期新增），后四张是 P0-1/P0-3 的存量。
P3-6 起追加第十张 `quota_usage_monthly`（月度用量快照，归档审计）。
site-mood（0010）起追加 `site_mood_submission`（念安站心情收集，独立于 RAG）。
v4.0.0 P4-1 起追加 `system_config`（运行时业务配置，ConfigService 热更新真相源）。
v4.0.0 P4-5b 起追加 `qa_trace`（问答流程轨迹的分析镜像，跨会话筛选）。

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

# --------------------------------------------------------------------------- #
# P2-11a：组织与账号三张表
# --------------------------------------------------------------------------- #
user_table = sa.Table(
    "user",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("username", sa.String(64), nullable=False, comment="登录名，唯一"),
    sa.Column("employee_no", sa.String(32), nullable=False,
              comment="工号，唯一且不随人变；与 username 分开是因为登录名可改、工号不可改"),
    sa.Column("display_name", sa.String(128), nullable=False, server_default="", comment="姓名"),
    sa.Column("email", sa.String(128), nullable=True,
              comment="公司邮箱；MySQL 唯一索引允许多个 NULL，故允许不填"),
    sa.Column("phone", sa.String(20), nullable=True,
              comment="手机号；属个人信息，展示时必须脱敏"),
    sa.Column("gender", sa.String(8), nullable=True,
              comment="可选，仅展示，不参与任何权限或筛选逻辑"),
    sa.Column("department_id", sa.BigInteger(), nullable=True, comment="所属部门 department.id"),
    sa.Column("position_id", sa.BigInteger(), nullable=True,
              comment="职位 position.id；职级挂在职位上，不放这里"),
    sa.Column("role", sa.String(16), nullable=False, server_default="user",
              comment="系统角色：admin/hr/user；与职位职级正交，职级高低不自动换权限"),
    sa.Column("kb_role", sa.String(16), nullable=False, server_default="none",
              comment="知识库写权限(P2-14a)：none(只读,默认)/ops(可传可删)/qa·dev·superadmin(额外可重灌索引)；"
                      "与 role 正交 —— role 管能不能进管理端，kb_role 管能不能改知识库"),
    # ⚠️ 注释必须与 alembic/versions/0007_user_token_quota.py 里的**逐字相同**
    # （那里也写了同样一句提醒）—— `compare_metadata()` 的 diff 必须为 0，
    # 而 `modify_comment` 正是它会报的一类差异。
    sa.Column("token_quota_monthly", sa.BigInteger(), nullable=False, server_default="0",
              comment="月度token 额度(P2-15)：0=不限（用全局默认 TOKEN_QUOTA_DEFAULT_MONTHLY）；"
                      "计费口径=输入+输出，不含cache_read（服务商侧 prompt 缓存，单价不同，混入会算错钱）"),
    sa.Index("idx_user_token_quota", "token_quota_monthly"),
    sa.Column("status", sa.String(16), nullable=False, server_default="active",
              comment="在职状态：active/disabled/resigned；只有 active 能登录，离职走 resigned 而非删行"),
    sa.Column("password_hash", sa.String(255), nullable=False, server_default="",
              comment="bcrypt 哈希；绝不放明文"),
    sa.Column("password_changed_at", mysql.DATETIME(fsp=6), nullable=True,
              comment="最后一次改密时间，算到期用（策略见 P2-11b）"),
    sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("0"),
              comment="管理员重置为临时密码后置 1，本人改完清 0"),
    sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0"),
              comment="改密/停用/改角色时 +1；JWT 里的 ver 与之比对，不匹配即失效（免去 token 白名单，见 PLAN §11 D4）"),
    sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
              comment="连续登录失败次数；成功登录归零"),
    sa.Column("locked_until", mysql.DATETIME(fsp=6), nullable=True,
              comment="锁定到期时间；连续失败达阈值时写入"),
    sa.Column("last_login_at", mysql.DATETIME(fsp=6), nullable=True, comment="最后一次成功登录时间"),
    sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人 user.id；人事操作留痕"),
    sa.Column("updated_by", sa.BigInteger(), nullable=True, comment="最后修改人 user.id"),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("update_time", mysql.DATETIME(fsp=6), nullable=False,
              server_default=sa.text("CURRENT_TIMESTAMP(6)"),
              comment="最后修改时间；由应用显式写入，不依赖 ON UPDATE（迁移里不便声明）"),
    sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True,
              comment="软删除标记；日常不删行，离职用 status=resigned"),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("username", name="uk_user_username"),
    sa.UniqueConstraint("employee_no", name="uk_user_employee_no"),
    sa.UniqueConstraint("email", name="uk_user_email"),
    sa.Index("idx_user_dept_status", "department_id", "status"),
    sa.Index("idx_user_role", "role"),
    sa.Index("idx_user_kb_role", "kb_role"),
    comment="用户表（P2-11a 启用为账号真相源；原 is_active 布尔列已下线，权威字段是 status）",
    **TABLE_KW,
)

department_table = sa.Table(
    "department",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("code", sa.String(32), nullable=False, comment="部门编码，唯一"),
    sa.Column("name", sa.String(128), nullable=False, comment="部门名称"),
    sa.Column("parent_id", sa.BigInteger(), nullable=True,
              comment="上级部门 id；自关联表达「中心→部门→组」，不建闭包表（规模到不了那个量级）"),
    sa.Column("leader_user_id", sa.BigInteger(), nullable=True,
              comment="部门负责人 user.id；知识库是全公司共用的，写权限改由 user.kb_role 授予（见 core/kb_acl.py），"
                      "本列不再隐含「负责人默认能写」——2026-10-08 原 D13 已作废"),
    sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="同级排序"),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("code", name="uk_dept_code"),
    sa.Index("idx_dept_parent", "parent_id"),
    sa.Index("idx_dept_leader", "leader_user_id"),
    comment="部门（P2-11a）",
    **TABLE_KW,
)

position_table = sa.Table(
    "position",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("code", sa.String(32), nullable=False, comment="职位编码，唯一"),
    sa.Column("name", sa.String(128), nullable=False, comment="职位名称"),
    sa.Column("level", sa.String(16), nullable=True,
              comment="职级（如 P6）；放这里而不是放 user，因为同岗位的人职级一般一致，改一次动一处"),
    sa.Column("sequence", sa.String(16), nullable=False, server_default="tech",
              comment="职位序列：技术/管理/职能"),
    sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("code", name="uk_position_code"),
    sa.Index("idx_position_level", "level"),
    comment="职位（含职级，P2-11a）",
    **TABLE_KW,
)

user_password_history_table = sa.Table(
    "user_password_history",
    metadata,
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("user_id", sa.BigInteger(), nullable=False, comment="员工 user.id"),
    sa.Column("password_hash", sa.String(255), nullable=False,
              comment="当时的 bcrypt 哈希；用于「历史不可复用」的逐条比对"),
    sa.Column("changed_at", mysql.DATETIME(fsp=6), nullable=False, comment="这次改密发生的时间"),
    sa.Column("changed_by_user_id", sa.BigInteger(), nullable=True,
              comment="操作人 user.id；管理员重置时是管理员 id，本人改密为空"),
    sa.PrimaryKeyConstraint("id"),
    # 支持「取某个员工最近 N 条」—— 没有这个索引，每次改密都要全表扫
    sa.Index("idx_pwh_user_changed", "user_id", "changed_at"),
    comment="改密历史（P2-11b；只追加，超出保留条数的旧记录会被裁掉）",
    **TABLE_KW,
)

# 审计日志：**刻意没有** update_time / deleted_at 两列。
# 没有这两列，「顺手改一下」「先软删」在 SQL 层面就不成立 ——
# 「不可改不可删」要落在结构上，不能只靠谁都知道的约定。
audit_log_table = sa.Table(
    "audit_log",
    metadata,
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
    sa.Index("idx_audit_created", "created_at"),
    sa.Index("idx_audit_actor", "actor_user_id", "created_at"),
    sa.Index("idx_audit_target", "target_type", "target_id"),
    sa.Index("idx_audit_action", "action"),
    comment="审计日志（P2-13d；只追加，无 update_time / deleted_at —— 改不了也删不掉）",
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
    # P0-3 / 迁移 0002 追加。物理位置在 upload_time 之后（op.add_column 不指定 after 就是追加到末尾），
    # 这里按同样的顺序写，免得读的人对着两份定义数位置。
    sa.Column("parse_started_at", mysql.DATETIME(fsp=6), nullable=True,
              comment="本次解析开始时间；仅 status=parsing 时非空，用于识别被 kill 留下的僵尸任务"),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("storage_path", name="uk_doc_storage_path"),
    sa.Index("idx_doc_project_status", "project_id", "status"),
    sa.Index("idx_doc_status", "status"),
    sa.Index("idx_doc_upload_time", "upload_time"),
    comment="文档元数据（P0-3 启用）",
    **TABLE_KW,
)

# --------------------------------------------------------------------------- #
# P3-6：月度用量快照（归档审计；判定路径不读它）
# --------------------------------------------------------------------------- #
# ⚠️ 列注释与 alembic/versions/0009 逐字相同（compare_metadata 守着，diff 必须为 0）。
quota_usage_monthly_table = sa.Table(
    "quota_usage_monthly",
    metadata,
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
    sa.Index("idx_quota_monthly_period", "period"),
    comment=(
        "月度用量快照（P3-6）：归档审计用；"
        "判定路径不读它——预检与看板仍走 quota_repo 实时聚合（chat_message 明细在，实时算永远准）"
    ),
    **TABLE_KW,
)

# --------------------------------------------------------------------------- #
# site-mood：念安站「Are you happy today」心情收集（独立于 RAG 业务的公开小表）
# --------------------------------------------------------------------------- #
# ⚠️ 列注释与 alembic/versions/0010 逐字相同（compare_metadata 守着，diff 必须为 0）。
# 与 RAG 业务表的关系：**没有关系** —— 不外键到 user，不进任何 RAG 查询路径；
# 公开提交口（POST /api/v1/mood/submit）由 Redis 双闸（per-IP 限流 + 邮箱冷却）守护，
# 查看口（GET /api/v1/mood/admin）由独立静态 token 守护，判定见 api/routes/mood.py。
site_mood_submission_table = sa.Table(
    "site_mood_submission",
    metadata,
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

# --------------------------------------------------------------------------- #
# v4.0.0 P4-1：运行时业务配置（ConfigService 的热更新真相源）
# --------------------------------------------------------------------------- #
# ⚠️ 列注释与 alembic/versions/0011 逐字相同（compare_metadata 守着，diff 必须为 0）。
# 与 .env 的分工：**基础设施**（DB/Redis/Chroma 连接、密钥、端口、CORS）留 .env、
# 重启才生效；**业务开关 / 参数**进本表，管理端（P4-2）改了即时生效。
# 读写门面是 core/config_service.py（进程内缓存 + TTL 回源），请求链路不直接碰本表。
system_config_table = sa.Table(
    "system_config",
    metadata,
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

# ⚠️ 列注释与 alembic/versions/0012 逐字相同（compare_metadata 守着，diff 必须为 0）。
# P4-5 二期：问答流程回溯的**分析镜像**。每轮的完整轨迹已随 turn_meta.qa_trace
# 落在 chat_message（面板数据源），但 JSON 列没法按 decision/intent/日期建索引 ——
# 「近 30 天有多少轮 zero_recall、拒答差距都差多少分」这类跨会话问题查不动。
# 本表是同一份数据的查询优化形态：写入点唯一（rag_chain 三处写记忆后各镜像一次，
# fail-open），列 = 过滤/聚合维度 + 详情渲染所需的标量，candidates 存 JSON。
# 不建 message 外键：会话快照（Redis/MySQL session store）不回传 message id，
# 自包含才能让「溯源页」不依赖会话存储的实现细节。
qa_trace_table = sa.Table(
    "qa_trace",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True,
              comment="自增主键；按时间近似有序，溯源页默认按 id 倒序 = 按时间倒序"),
    sa.Column("session_id", sa.String(64), nullable=False,
              comment="会话 id；与 chat_message.session_id 同源。不建 FK——会话可能被清理，轨迹留存"),
    sa.Column("owner_id", sa.Integer(), nullable=True,
              comment="提问者 user.id；可空——内部调用可能无归属。不建 FK 同 session_id"),
    sa.Column("decision", sa.String(20), nullable=False,
              comment="终端状态四选一：cache_hit/chitchat/zero_recall/rag_qa（core/qa_flow.py 唯一定义处）"),
    sa.Column("intent", sa.String(40), nullable=False,
              comment="意图（IntentType 值）；缓存命中轮取被缓存那次回答的意图"),
    sa.Column("route", sa.String(40), nullable=False,
              comment="路由：rag_qa | chitchat"),
    sa.Column("intent_source", sa.String(20), nullable=False, server_default="",
              comment="意图判定来源：rule/llm/cache；空串=未知（老数据）"),
    sa.Column("question", sa.Text(), nullable=False,
              comment="用户原始问题（缓存命中轮 = 本轮实际输入，不一定是当初被缓存的那句）"),
    sa.Column("standalone_question", sa.Text(), nullable=True,
              comment="改写后的独立问句；闲聊轮与缓存命中轮为 NULL"),
    sa.Column("rewritten_query", sa.Text(), nullable=True,
              comment="Query 改写产物；未开启改写或无变化时为 NULL"),
    sa.Column("vector_query", sa.Text(), nullable=True,
              comment="HyDE 假设答案（向量召回用它）；未启用 HyDE 时为 NULL"),
    sa.Column("candidates", sa.JSON(), nullable=True,
              comment="重排前候选清单（含来源/分数）；闲聊与缓存命中轮为 NULL。JSON 列不建索引，仅详情渲染"),
    sa.Column("threshold", mysql.DOUBLE(), nullable=True,
              comment="重排分数阈值；未重排/未跑检索为 NULL。与 top1_before_filter 配合算「差多少分」"),
    sa.Column("top1_before_filter", mysql.DOUBLE(), nullable=True,
              comment="阈值过滤前的最高分；未跑检索为 NULL。min_top1_gap 过滤 = threshold - 本列 >= X"),
    sa.Column("filtered_count", sa.Integer(), nullable=True,
              comment="被阈值过滤掉的候选数；未跑检索为 NULL"),
    sa.Column("docs_returned", sa.Integer(), nullable=False, server_default="0",
              comment="过滤后返回的文档数；闲聊=0"),
    sa.Column("sources_count", sa.Integer(), nullable=False, server_default="0",
              comment="答案引用的来源数（sources 列表长度）"),
    sa.Column("bm25_hits", sa.Integer(), nullable=True,
              comment="BM25 关键词召回命中数；混合检索未开启为 NULL（「没开」≠「开了没命中」）"),
    sa.Column("bm25_only", sa.Integer(), nullable=True,
              comment="仅 BM25 命中、向量未命中的候选数；混合未开启为 NULL"),
    sa.Column("rrf_top", sa.String(64), nullable=True,
              comment="RRF 融合后排首位的 chunk_id（如 \"822:0\"）；混合未开启为 NULL。⚠️ 是 ID 不是分数——首版误建 DOUBLE 导致镜像必失败（P4-5c 修订）"),
    sa.Column("hyde_used", sa.Boolean(), nullable=False, server_default=sa.text("0"),
              comment="本轮向量召回是否用了 HyDE 假设答案"),
    sa.Column("elapsed_ms", mysql.DOUBLE(), nullable=False, server_default="0",
              comment="整轮耗时（毫秒）；缓存命中轮通常接近 0"),
    sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.text("0"),
              comment="是否拒答（decision=zero_recall 的派生布尔，冗余存储换过滤便利）"),
    sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False,
              comment="落表时间（now_db() 口径）；日期过滤与「按天聚合」都查它"),
    sa.Index("ix_qa_trace_decision", "decision"),
    sa.Index("ix_qa_trace_intent", "intent"),
    sa.Index("ix_qa_trace_session", "session_id"),
    sa.Index("ix_qa_trace_created", "created_at"),
    comment="问答流程轨迹（P4-5 二期）：turn_meta.qa_trace 的分析镜像，跨会话按状态/意图/日期筛选；写入点唯一在 rag_chain，fail-open",
    **TABLE_KW,
)

__all__ = [
    "metadata",
    "user_table",
    "user_password_history_table",
    "department_table",
    "position_table",
    "folder_table",
    "session_table",
    "chat_message_table",
    "document_table",
    "quota_usage_monthly_table",
    "site_mood_submission_table",
    "system_config_table",
    "qa_trace_table",
]
