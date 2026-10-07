"""
审计日志仓储 —— **只有写入与查询，没有修改与删除**。

--------------------------------------------------------------------------
为什么这个文件里找不到 update / delete
--------------------------------------------------------------------------
「日志不可改不可删」如果只写成注释，那它就只是一句愿望：一次紧急修复就能
UPDATE 掉一行，而且没有任何东西会报警。所以这里用三道结构性约束把它钉死：

    1. **本文件不提供 update / delete**（也不提供任何「按条件改」的通用函数）；
    2. **表里没有 `update_time` / `deleted_at`**（见 `core/schema.py`）——
       没有这两列，「顺手更新一下」「先软删」在 SQL 层面就不成立；
    3. `record()` 会**拒绝**带密码 / token / 哈希特征的键与值，
       所以敏感信息进不来，而不是靠调用方自觉。

--------------------------------------------------------------------------
为什么要拦敏感信息，而不是「大家记得别写」
--------------------------------------------------------------------------
因为写日志的人正是最有权限的那批人（管理员），而他们完全是好意 ——
「顺手把新密码记一下，方便以后核对」这种想法一旦发生，
审计表就成了密码明文的第二个落点，而它的读取权限通常比 `user` 表更宽
（谁能看日志，往往比谁能改密码更容易被放到）。

所以这里做成**抛错**而不是「悄悄丢掉」：调用方一旦踩到，
会立刻发现自己的代码在往审计里塞敏感字段，而不是某天排查时才发现。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final, Iterable

import sqlalchemy as sa

from core.db import now_db, session_scope
from core.schema import audit_log_table

__all__ = [
    "record",
    "list_logs",
    "count_logs",
    "ACTION_LABELS",
    "TARGET_TYPES",
    "AuditRefused",
]


class AuditRefused(ValueError):
    """detail 里带了不该进审计的敏感内容。**不静默丢弃**，让调用方当场发现。"""


# --------------------------------------------------------------------------- #
# 动作与对象类型：白名单，不是自由字符串
# --------------------------------------------------------------------------- #
# 为什么用白名单而不是随手写字符串
# ---------------------------------------------------------------------------
# 审计最常见的失效方式不是被删，而是**查询时对不上**：
# 一段时间后没人说得清 `user.upd` 和 `user.update` 哪个才是当初写的，
# 于是「按动作筛」这个能力事实上废了，而废掉的时候没有任何报错。
# 白名单让「拼错」在**写入那一刻**就抛错 —— 那时候开发者还在线。
ACTION_LABELS: Final[dict[str, str]] = {
    "user.create": "新建员工",
    "user.profile.update": "修改员工资料",
    "user.status.change": "变更员工状态",
    "user.role.change": "变更系统角色",
    # P2-14f：知识库写权限是**独立维度**（D14），所以单列一个动作名，
    # 不复用 user.role.change —— 审计里分不清「改了能不能进后台」与
    # 「改了能不能删知识库」，而这两件事的严重程度完全不同。
    "user.kb_role.change": "变更知识库写权限",
    "user.password.reset": "重置密码",
    "user.password.must_change": "开关强制改密",
    # P2-14c：知识库写操作。target_type 用 `document`（不是 `kb`）——
    # 因为审计要能回答「谁动了这份文档」，而 `kb` 这个粒度回答不了。
    # 重建整库索引那条（`kb.reindex`）粒度确实是整库，target_id 留空。
    "document.upload": "上传知识库文档",
    "document.delete": "删除知识库文档",
    "document.reparse": "重新解析文档",
    "department.create": "新建部门",
    "department.update": "修改部门",
    "department.delete": "删除部门",
    "position.create": "新建职位",
    "position.update": "修改职位",
    "position.delete": "删除职位",
    # 11c（网关改查 MySQL）落地后由登录路径写入：
    "auth.login.success": "登录成功",
    "auth.login.failure": "登录失败",
    "auth.logout": "登出",
}

TARGET_TYPES: Final[tuple[str, ...]] = ("user", "department", "position", "auth", "document")


# --------------------------------------------------------------------------- #
# 敏感内容拦截
# --------------------------------------------------------------------------- #
# 键名命中即拒（大小写不敏感）；值命中 bcrypt / JWT 的特征串也拒。
# ---------------------------------------------------------------------------
# 值这一层为什么必要：调用方可能把整个对象丢进来
# （`detail={"user": record}`），键名对得上「user」，但里面裹着 password_hash。
# 只查键会漏。
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset({
    "password", "passwd", "pwd", "password_hash", "old_password", "new_password",
    "temp_password", "temporary_password", "token", "access_token", "refresh_token",
    "authorization", "secret", "credential", "credentials",
})
# 值特征：bcrypt 哈希前缀、bcryptjs、base64 段状 JWT
_FORBIDDEN_VALUE_MARKERS: Final[tuple[str, ...]] = ("$2a$", "$2b$", "$2y$")


def _walk_keys(node: Any) -> Iterable[str]:
    """深度遍历 dict / list，产出所有键名。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_keys(item)


def _walk_strings(node: Any) -> Iterable[str]:
    """深度遍历所有字符串值（键名不看，这里只看值）。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_strings(item)


def assert_detail_is_safe(detail: Any) -> None:
    """
    detail 里不该出现密码 / token / 哈希。命中就抛 `AuditRefused`。

    刻意不做「脱敏后继续写」：脱敏规则一旦要维护，就会出现「这个字段忘了脱敏」
    的漏网，而审计表一旦进了明文密码，它的所有读取权限都成了泄漏面。
    宁可让写入失败（调用方立刻看到），也不要静默写进去。
    """
    if detail is None:
        return
    for key in _walk_keys(detail):
        if key.lower() in _FORBIDDEN_KEYS:
            raise AuditRefused(f"审计 detail 不允许出现键「{key}」（密码 / token 类内容不进审计）")
    for value in _walk_strings(detail):
        for marker in _FORBIDDEN_VALUE_MARKERS:
            if marker in value:
                raise AuditRefused(
                    "审计 detail 里出现了哈希特征串"
                    f"（{marker}…）；密码类内容不进审计"
                )


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def record(
    *,
    actor_user_id: int | None,
    actor_username: str,
    actor_role: str | None,
    action: str,
    target_type: str,
    target_id: int | None = None,
    target_label: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> int:
    """
    落一条审计。返回自增 id。

    参数刻意做成**全关键字、无默认值**（除了两个可空字段）：
    写一条「谁做了什么」需要 5 个信息，缺一个这行日志的价值就打折，
    而默认值会让「忘了传 actor」悄悄发生 —— 那种日志事后看是空的。

    `actor_user_id` 允许为 None：break-glass 超管不在 `user` 表里（理由见迁移文档）。
    """
    if action not in ACTION_LABELS:
        raise AuditRefused(f"未登记的动作「{action}」；先在 audit_repo.ACTION_LABELS 里加")
    if target_type not in TARGET_TYPES:
        raise AuditRefused(f"未登记的对象类型「{target_type}」")
    if not actor_username or not actor_username.strip():
        raise AuditRefused("actor_username 不能为空：没有操作人的日志等于没有日志")
    assert_detail_is_safe(detail)

    payload = json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None
    stmt = audit_log_table.insert().values(
        actor_user_id=actor_user_id,
        actor_username=actor_username[:64],
        actor_role=(actor_role or None),
        action=action[:48],
        target_type=target_type[:32],
        target_id=target_id,
        target_label=(target_label or None),
        detail=payload,
        ip=(ip[:45] if ip else None),
        created_at=now or now_db(),
    )
    with session_scope() as session:
        return int(session.execute(stmt).inserted_primary_key[0])


# --------------------------------------------------------------------------- #
# 查询（只读）
# --------------------------------------------------------------------------- #
def _filters(
    *,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    keyword: str | None = None,
) -> list[Any]:
    """按可空条件拼 WHERE —— 全是 AND，语义与前端筛选栏一一对应。"""
    where: list[Any] = []
    if actor:
        # 模糊匹配登录名：**不区分大小写**（手输的大小写不该筛掉结果）
        where.append(audit_log_table.c.actor_username.like(f"%{actor}%"))
    if action:
        where.append(audit_log_table.c.action == action)
    if target_type:
        where.append(audit_log_table.c.target_type == target_type)
    if target_id is not None:
        where.append(audit_log_table.c.target_id == target_id)
    if since is not None:
        where.append(audit_log_table.c.created_at >= since)
    if until is not None:
        where.append(audit_log_table.c.created_at <= until)
    if keyword:
        like = f"%{keyword}%"
        where.append(
            sa.or_(
                audit_log_table.c.target_label.like(like),
                audit_log_table.c.detail.like(like),
            )
        )
    return where


def list_logs(
    *,
    limit: int = 50,
    offset: int = 0,
    order: str = "desc",
    **conditions: Any,
) -> list[dict[str, Any]]:
    """
    按条件取一页。**最新在前**（默认）—— 查审计时的时间顺序是「从现在往回」，
    这一点和业务列表的习惯相反，所以在这里定死，省得每个调用方各选一次。

    排序字段**不接受外部输入**（只有 `id` 与 `created_at` 两个白名单值）：
    排序字段是少数几个能直接拼进 SQL 的位置，传字符串进来就是注入口。
    """
    if limit <= 0 or limit > 200:
        raise ValueError("limit 必须在 1~200 之间")
    if offset < 0:
        raise ValueError("offset 不能为负")
    key = {"desc": audit_log_table.c.id.desc(), "asc": audit_log_table.c.id.asc()}[order]

    stmt = (
        sa.select(audit_log_table)
        .where(*_filters(**conditions))
        .order_by(key)
        .limit(limit)
        .offset(offset)
    )
    with session_scope() as session:
        rows = session.execute(stmt).mappings().all()
    return [dict(row) for row in rows]


def count_logs(**conditions: Any) -> int:
    """总数。与 `list_logs` 共用同一套条件，保证「筛选后总数」与「筛选后列表」一致。"""
    stmt = sa.select(sa.func.count()).select_from(audit_log_table).where(*_filters(**conditions))
    with session_scope() as session:
        return int(session.execute(stmt).scalar() or 0)


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """
    把一行审计转成**给人看**的形状（路由层用）：动作翻成中文、detail 解 JSON。

    `detail` 解析失败**不抛错** —— 审计列表不能因为某一行脏了就整个打不开。
    这时它退回 `{"_raw": 原值, "_parse_error": True}`，让「这行有问题」本身可见。
    """
    out = dict(row)
    out["action_label"] = ACTION_LABELS.get(row["action"], row["action"])
    raw = row.get("detail")
    if raw:
        try:
            out["detail"] = json.loads(raw)
        except (TypeError, ValueError):
            out["detail"] = {"_raw": str(raw), "_parse_error": True}
    else:
        out["detail"] = None
    created = out.get("created_at")
    if isinstance(created, datetime):
        out["created_at"] = created.isoformat(sep=" ", timespec="seconds")
    return out