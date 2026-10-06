"""P2-11a：组织与账号表（user 扩表 + department + position）

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06

--------------------------------------------------------------------------
这一版把「用户」从一张空表变成真的账号体系底座

--------------------------------------------------------------------------
0001 建 `user` 表时，它的定位写得很直白：「本期只建表备用。网关账号仍走
`.env`」。三年后的现实是：那张表**一行都没有**，全仓库没有任何 Python
代码读过它 —— 因为 P0-2 的账号校验完全发生在网关（`.env` 的 bcrypt 串），
后端连「来的是谁」都不知道。

于是现在要补的不是「一张用户表」，而是三件互相咬合的东西：

    department（部门树）   人事的基本单位，也是 P2-12 知识库按部门共享的归属
    position（职位+职级）  「职级不自动换权限」这条裁决的落点
    user（扩表）           账号 + 组织归属 + 密码策略状态 + 吊销计数

只加 user 不加另两张，会立刻逼出两个坏问题：部门字段只能塞字符串（没法
做「按部门筛人」），职级只能塞在 user 上（同岗位的人改一次要动 N 行）。
所以这三张表是**一次到位**，不是分三批。

--------------------------------------------------------------------------
为什么下线 `is_active`，而不是留着它

--------------------------------------------------------------------------
0001 的 user 表有一个 `is_active`（TINYINT 布尔）。它表达不了企业场景里
最常见的一种状态：**离职**。布尔只有「在/不在」，而真实人事系统至少有

    在职(active)  停用(disabled，比如账号被冻结、密码泄露待处理)  离职(resigned)

两种「不在」的性质完全不同：停用的员工要能恢复，离职的员工不该再出现在
任何选择列表里，但**都必须保留历史行**（他的会话、问答记录、文档归属还
指着这个 user.id）。

留着 `is_active` 的代价是两个列表达同一件事且迟早不一致：一个说
`is_active=0`，另一个说 `status='resigned'`，应用层该信谁？
**两份真相源必然漂移**，而漂移在权限场景下的表现是「离职的人还能登录」。

所以 0003 直接 `DROP COLUMN is_active`，权威字段是 `status`。
下线它没有任何数据代价：这张表**零行**，且全仓库无代码引用该列
（`grep is_active` 只命中 0001 与 core/schema.py 两处定义本身）。

代价记下来：若将来要回退到 0002，只能靠 `status` 回填 `is_active`
（本迁移的 downgrade 已经做了这层回填，见下），而 `resigned` 会映射成
`is_active=1`（布尔表达不了三态）—— 这是回退方向的信息损失，属于**主动接受**。

--------------------------------------------------------------------------
为什么 status 用 VARCHAR 而不是 ENUM / 为什么 token_version 取代 token 白名单

--------------------------------------------------------------------------
· **VARCHAR**：沿用 0001 对 document.status 的同一取舍 —— ENUM 加值要
  ALTER TABLE（表级 DDL，锁表），而取值集合还在长（P2-14 之后可能要加
  `locked`）。取值集合写在列注释里，靠 `core/user_repo.py` 校验。
· **token_version 取代 Redis token 白名单**：无状态 JWT 一旦签发就无法收回，
  PLAN §11 D4 已定「不做白名单」，那么「停用/改密后旧 token 仍在有效期内」
  怎么解决？靠把 `token_version` 写进 JWT、由后端比对（设计规格 §8.1 建议 3）。
  本迁移只是把这个计数器的**存储位置**建出来，真正的比对逻辑在 P2-11c。

--------------------------------------------------------------------------
为什么不加 FOREIGN KEY（沿用 0001 的决定）

--------------------------------------------------------------------------
0001 已明确：不建 FK。本迁移继续遵守，因为

    session.user_id 现在是 NULL，而 user 表零行 ——
    一旦加 FK，`session` 与 `user` 之间立刻出现「孤儿行」，
    而孤儿行只能靠「先补 user 行、再改 session.user_id」这种手工数据修复解开，
    比没有 FK 更难处理。

完整性继续靠应用层：删部门前先查有没有在职员工（P2-13b 的职责），
而不是让数据库替你拒绝。

--------------------------------------------------------------------------
`employee_no` 的默认值，以及为什么必须紧跟一步回填
--------------------------------------------------------------------------
`employee_no` 是 NOT NULL 的工号列。0001 建的是空表，所以理论上直接
`ADD COLUMN ... NOT NULL` 不会有任何问题；但这份迁移要能在**非空表上执行**
（P2-11d 之后更是必然如此 —— 表里已经有种子员工）。给一个空串默认值，
MySQL 在 ADD COLUMN 时给存量行填隐式默认值，于是**不会**因1364 报错。

但空串紧接着就撞上唯一键：`uk_user_employee_no` 不允许两行同为 ''。
所以 add_column 之后**必须**先把空串换成 `LEGACY-<id>`，再 create_index。

⚠️ **这条限制是 P2-11d 才暴露出来的**：11a 交付时 user 表是空的，验收脚本只灌1 行，
所以「重放需要空表」这个前提一直没被触发。直到种子数据把 10 个员工放进去，
迁移往返验收第一次真的撞上它（报 1062 Duplicate entry ''）。
**教训**：迁移的「只在空表上验证过」等价于「没验证过」——
验收夹具必须覆盖真实数据量，而不是刚好绕过边界。

回填值取 `LEGACY-<id>`：确定、唯一（带主键）、且一看就知道是迁移补的占位工号，
需要人工换成真实工号。**空串做不到** —— 它既不唯一（所以建不了索引），也不可辨识。

反过来，**正常路径永远写不到空串**：应用层 `create_user()` 把 employee_no 列为必填。
万一真有人绕过应用层写进来，唯一键会拦住第二条 —— 这是期望行为，让脏数据失败得响亮。

--------------------------------------------------------------------------
为什么 update_time 不用 ON UPDATE CURRENT_TIMESTAMP

--------------------------------------------------------------------------
MySQL 的 `ON UPDATE CURRENT_TIMESTAMP` 只对 `TIMESTAMP` / `DATETIME` 列
自动生效，但**无法用 SQLAlchemy Core 的列属性表达**，只能整段写成
`server_default=text("DEFAULT ... ON UPDATE ...")`。那会让
`core/schema.py`（结构视图）与迁移（DDL 快照）里出现一段无法逐字对照的
DDL 片段，`compare_metadata` 也读不懂它。

代价：本列的自动性要由应用负责 —— `core/user_repo.py` 里所有写操作都显式
带上 `update_time`。这比「靠数据库兜底」更可控：至少不会出现「没人改但时间
变了」导致的时间线误读。

--------------------------------------------------------------------------
为什么本迁移不建 project / user_project

--------------------------------------------------------------------------
它们分别是 P2-12a（知识库隔离单元）与 P2-14a（写权限授予）的落点。
本轮建它们会导致「project 有了但归属模型没定」的半成品状态，
而 `document.project_id` 从字符串 `default` 迁到 `project.id` 强关联
本身还需要一次独立的数据迁移（现存 35 个文档）。分开做更小步可验。

--------------------------------------------------------------------------
downgrade 会丢什么

--------------------------------------------------------------------------
    DROP TABLE department / position —— 整个部门树与职位体系**不可再生**
    DROP 17 列 user —— 所有人事扩展数据**不可再生**
    ADD COLUMN is_active —— 已按 status 回填（离职者会落成 1，见上文）

所以 downgrade 只用于「这次迁移没落地、需要回到 P2-11a 之前」的场景，
**不要**用它来回退生产的人事数据。验收脚本里的往返会先清掉自己造的
带前缀数据再执行，避免把不可逆的丢失伪装成「往返成功」。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}
_TS = sa.text("CURRENT_TIMESTAMP")
_TS6 = sa.text("CURRENT_TIMESTAMP(6)")
_ZERO = sa.text("0")

# user 表的新列。定义与 core/schema.py 逐字一致（含 comment）——
# 不一致会被 module11 的 compare_metadata 断言报出来。
# 注意 display_name / password_hash **不在这里**：0001 已有这两列，
# 这里只改 display_name 的列注释（见 upgrade 里的 MODIFY COLUMN）。
_USER_COLUMNS = [
    sa.Column(
        "employee_no", sa.String(32), nullable=False, server_default="",
        comment="工号，唯一且不随人变；与 username 分开是因为登录名可改、工号不可改",
    ),
    sa.Column(
        "email", sa.String(128), nullable=True,
        comment="公司邮箱；MySQL 唯一索引允许多个 NULL，故允许不填",
    ),
    sa.Column("phone", sa.String(20), nullable=True, comment="手机号；属个人信息，展示时必须脱敏"),
    sa.Column(
        "gender", sa.String(8), nullable=True,
        comment="可选，仅展示，不参与任何权限或筛选逻辑",
    ),
    sa.Column("department_id", sa.BigInteger(), nullable=True, comment="所属部门 department.id"),
    sa.Column(
        "position_id", sa.BigInteger(), nullable=True,
        comment="职位 position.id；职级挂在职位上，不放这里",
    ),
    sa.Column(
        "role", sa.String(16), nullable=False, server_default="user",
        comment="系统角色：admin/hr/user；与职位职级正交，职级高低不自动换权限",
    ),
    sa.Column(
        "status", sa.String(16), nullable=False, server_default="active",
        comment="在职状态：active/disabled/resigned；只有 active 能登录，离职走 resigned 而非删行",
    ),
    sa.Column(
        "password_changed_at", mysql.DATETIME(fsp=6), nullable=True,
        comment="最后一次改密时间，算到期用（策略见 P2-11b）",
    ),
    sa.Column(
        "must_change_password", sa.Boolean(), nullable=False, server_default=_ZERO,
        comment="管理员重置为临时密码后置 1，本人改完清 0",
    ),
    sa.Column(
        "token_version", sa.Integer(), nullable=False, server_default=_ZERO,
        comment="改密/停用/改角色时 +1；JWT 里的 ver 与之比对，不匹配即失效（免去 token 白名单，见 PLAN §11 D4）",
    ),
    sa.Column(
        "failed_login_count", sa.Integer(), nullable=False, server_default=_ZERO,
        comment="连续登录失败次数；成功登录归零",
    ),
    sa.Column(
        "locked_until", mysql.DATETIME(fsp=6), nullable=True,
        comment="锁定到期时间；连续失败达阈值时写入",
    ),
    sa.Column("last_login_at", mysql.DATETIME(fsp=6), nullable=True, comment="最后一次成功登录时间"),
    sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人 user.id；人事操作留痕"),
    sa.Column("updated_by", sa.BigInteger(), nullable=True, comment="最后修改人 user.id"),
    sa.Column(
        "update_time", mysql.DATETIME(fsp=6), nullable=False, server_default=_TS6,
        comment="最后修改时间；由应用显式写入，不依赖 ON UPDATE（迁移里不便声明）",
    ),
    sa.Column(
        "deleted_at", mysql.DATETIME(fsp=6), nullable=True,
        comment="软删除标记；日常不删行，离职用 status=resigned",
    ),
]

_USER_INDEXES = [
    ("idx_user_dept_status", ["department_id", "status"], False),
    ("idx_user_role", ["role"], False),
    ("uk_user_employee_no", ["employee_no"], True),
    ("uk_user_email", ["email"], True),
]


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 部门：自关联树表达「中心 → 部门 → 组」
    # ------------------------------------------------------------------ #
    op.create_table(
        "department",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(32), nullable=False, comment="部门编码，唯一"),
        sa.Column("name", sa.String(128), nullable=False, comment="部门名称"),
        sa.Column(
            "parent_id", sa.BigInteger(), nullable=True,
            comment="上级部门 id；自关联表达「中心→部门→组」，不建闭包表（规模到不了那个量级）",
        ),
        sa.Column(
            "leader_user_id", sa.BigInteger(), nullable=True,
            comment="部门负责人 user.id；按 P2-14 的 D13，默认是该部门共享知识库的 writer",
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=_ZERO, comment="同级排序"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=_TS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uk_dept_code"),
        sa.Index("idx_dept_parent", "parent_id"),
        sa.Index("idx_dept_leader", "leader_user_id"),
        comment="部门（P2-11a）",
        **TABLE_KW,
    )

    # ------------------------------------------------------------------ #
    # 职位：职级挂在这里，改一次动一处
    # ------------------------------------------------------------------ #
    op.create_table(
        "position",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(32), nullable=False, comment="职位编码，唯一"),
        sa.Column("name", sa.String(128), nullable=False, comment="职位名称"),
        sa.Column(
            "level", sa.String(16), nullable=True,
            comment="职级（如 P6）；放这里而不是放 user，因为同岗位的人职级一般一致，改一次动一处",
        ),
        sa.Column(
            "sequence", sa.String(16), nullable=False, server_default="tech",
            comment="职位序列：技术/管理/职能",
        ),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=_TS),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uk_position_code"),
        sa.Index("idx_position_level", "level"),
        comment="职位（含职级，P2-11a）",
        **TABLE_KW,
    )

    # ------------------------------------------------------------------ #
    # user 扩表
    # ------------------------------------------------------------------ #
    for col in _USER_COLUMNS:
        op.add_column("user", col)

    # 关键：先回填 employee_no，再建唯一键。
    #
    # ADD COLUMN 只能给存量行填默认值，于是所有旧行都拿到空串；而空串只能有一条
    # （唯一键）。**不回填的话，这张迁移在「user 表已有 ≥2 行」的库上根本跑不起来**
    # —— 报1062 Duplicate entry ''。
    #
    # 这个坑是P2-11d 之后才暴露的：11a 交付时 user 表是空的，验收脚本只灌1 行，
    # 所以「重放需要空表」这个前提一直没被触发。直到种子数据把 10 个员工放进去，
    # 迁移往返验收第一次真的撞上了它。
    #
    # 回填值取 `LEGACY-<id>` 而不是继续留空：它确定、唯一（因为带主键），
    # 且一看就知道是「迁移补的占位工号」，需要人工换成真实工号。
    # 空串做不到这一点 —— 它既不唯一（所以建不了索引），也不可辨识。
    op.execute(
        "UPDATE `user` SET `employee_no` = CONCAT('LEGACY-', id) WHERE `employee_no` = ''"
    )

    # display_name 是 0001 就有的列，不 ADD 只改注释（0001 写的是「展示名」，
    # 现在统一成「姓名」，与 core/schema.py 对齐）
    op.execute(
        "ALTER TABLE `user` MODIFY COLUMN `display_name` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '姓名'"
    )
    # 表注释同样要同步：user 表从「本期未启用」变成「账号真相源」。
    # 漏这条 compare_metadata 会报 add_table_comment（改了 schema.py 忘同步 DDL 的反向版本）。
    op.execute(
        "ALTER TABLE `user` COMMENT = '用户表（P2-11a 启用为账号真相源；"
        "原 is_active 布尔列已下线，权威字段是 status）'"
    )

    for name, cols, unique in _USER_INDEXES:
        if unique:
            op.create_index(name, "user", cols, unique=True)
        else:
            op.create_index(name, "user", cols)

    # 权威字段是 status，布尔列下线（理由见文件头）
    op.drop_column("user", "is_active")


def downgrade() -> None:
    # 布尔列先回来，并按 status 回填 —— 否则回退后离职者会被标成「在职」。
    op.add_column(
        "user", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )
    op.execute("UPDATE `user` SET `is_active` = (`status` = 'active')")

    for name, _cols, _unique in _USER_INDEXES:
        op.drop_index(name, table_name="user")

    for col in reversed(_USER_COLUMNS):
        op.drop_column("user", col.name)

    # 表注释回到 0001 的口径，否则回退后 compare_metadata 会报 add_table_comment
    op.execute("ALTER TABLE `user` COMMENT = '用户表（本期未启用，见 PLAN-v2.0.0 §11 D2）'")

    op.drop_table("position")
    op.drop_table("department")