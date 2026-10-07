"""P2-14a：知识库写权限维度 `user.kb_role`（谁能改全公司共用的那一个知识库）

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08

--------------------------------------------------------------------------
这一列为什么存在
--------------------------------------------------------------------------
2026-10-08 之前的知识库**没有任何写权限**：任何登录用户都能上传、删除、
重解析。实测（不是推理，是 14.0 的基线探针）：

    普通员工 chen.jie → POST /api/v1/documents/upload   → 202 + doc_id=587
    普通员工 chen.jie → DELETE /api/v1/documents/587→ {"record_removed": true}

四条写路由（upload / upload-batch / reparse / delete）**一条身份依赖都没有**。

而那时的方案（第 4 版的 D13）是「建 `project` + `user_project` 表按库授权」。
实测推翻了它：**库里从来没有 `project` 表**（只有 alembic_version /
audit_log / chat_message / department / document / folder / position /
session / user / user_password_history 共 10 张）。更重要的是用户已经决定
「**所有人共用一个企业知识库，不隔离**」—— 不存在「多个库」，那么
「谁能管哪个库」这个问题就不成立，`user_project.role` 无处安放。

所以这一列是**替代方案**（PLAN §11.3 **D14**）：一条正交的权限维度，
回答「这个人能不能改知识库、改到什么程度」。

--------------------------------------------------------------------------
为什么**不**加进 user.role（这是本迁移最要紧的一个决定）
--------------------------------------------------------------------------
`user.role` 三档（`admin` / `hr` / `user`）管的是「**能不能进管理端**」，
判据在 `core/identity.py` 的 `STAFF_ROLES` / `is_admin()`。往里加第四个值会：

  1. 让管理端权限**一起漂** —— 任何一处 `role == "admin"` 的分支都要重新审。
     这是 P2-11b 踩过的「一处改处处受影响」，那次是 141 条断言才兜住的；
  2. **表达不了真实存在的组合** —— 一个人完全可能是 `role=user`
     （进不了管理端）但 `kb_role=ops`（该由他维护知识库）。塞进一个字段
     就只能靠加特例分支，越加越乱；
  3. 让「改role」的语义变得含糊 —— `set_role()` 会 `token_version+1`，
     而「改知识库权限」该不该把人踢下线是另一个问题（D15 已定：**要**踢，
     但那是这个函数自己的决定，不该由字段归属决定）。

**两个维度正交**：`role` × `kb_role` 共有 3×5 = 15 种组合，全部合法。

--------------------------------------------------------------------------
为什么列是 VARCHAR 而不是 ENUM
--------------------------------------------------------------------------
与 `user.role` / `user.status` / `document.status` 同一取舍（见 `core/schema.py`
文件头与迁移 0001）：MySQL 的 ENUM 加值要锁表，而这张表在企业里是「天天加新人」
的表 —— 为了加一个权限档位去锁一张业务表不划算。取值集合靠应用层校验，
唯一出处是 `core/kb_acl.py`，而**校验点放在仓储入口**（`set_kb_role`），
理由与 `user_repo._check_role` 一致：失败要早、要响。

    set_kb_role(7, "boss")  → 立刻 ValueError
    （而不是）写进库里 → 半年后权限判定判不出这个值 → 「他为什么什么都传不了」

--------------------------------------------------------------------------
为什么默认是 none 而不是 superadmin
--------------------------------------------------------------------------
fail-closed。新加的员工在管理员明确授权之前**什么都做不了**。

反过来的默认值（默认超管）会把「忘了授权」变成「所有人都能删知识库」，
而这类系统的管理员改权限的频率远低于加人的频率 —— 也就是说
**默认值写错的那条路径，被走到的次数会多得多**。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# 取值集合的**字面副本**。唯一权威在 `core/kb_acl.py`，这里刻意不 import它 ——
# 迁移文件必须能在「代码还没更新」的环境里单独跑（回滚、跨版本部署都会遇到），
# 靠 import 耦合会让迁移反过来依赖应用代码的当前状态。
KB_ROLES = ("none", "ops", "qa", "dev", "superadmin")


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "kb_role",
            sa.String(16),
            nullable=False,
            server_default="none",
            # ⚠️ 这段注释必须与 `core/schema.py` 里user.kb_role 列的注释
            # **逐字相同**，否则 `compare_metadata()` 会报 `modify_comment`
            # —— 那是 P2-11a 立下的「结构与视图必须一致」守卫，diff 必须为 0。
            # 两处各写一份的原因见 schema.py 文件头（结构快照 vs 查询侧视图
            # 是两份文件，本项目刻意不合并）。
            comment=(
                "知识库写权限(P2-14a)：none(只读,默认)/ops(可传可删)/qa·dev·superadmin(额外可重灌索引)；"
                "与 role 正交 —— role 管能不能进管理端，kb_role 管能不能改知识库"
            ),
        ),
    )
    # 索引：14f 的管理端列表要「按 kb_role 筛」，而这张表在企业里会到几百行。
    # 不建索引的话筛选退化成全表扫 —— 单次不致命，但它是「进管理端第一屏」的查询。
    op.create_index("idx_user_kb_role", "user", ["kb_role"])


def downgrade() -> None:
    op.drop_index("idx_user_kb_role", table_name="user")
    op.drop_column("user", "kb_role")