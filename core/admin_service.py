"""
管理端的业务编排 —— 员工 / 组织 / 密码的**动作层**（P2-13b / 13c）。

--------------------------------------------------------------------------
为什么要有这一层（路由能不能直接调 user_repo）
--------------------------------------------------------------------------
能跑，但会把三件事写进路由：

    1. 「唯一键冲突」翻成人话（HR 看不懂 `Duplicate entry 'wu.jing' for key
       'uk_user_username'`，而且这句里还带着数据库内部结构）；
    2. 每个写操作都要连带做的事（建号要发临时密码 + 写改密历史 + 裁剪历史）；
    3. 业务规则（不能停用自己、不能把最后一个管理员降权、离职不发证）。

散在路由里写，后果是「换一个入口就漏一条」——而这类漏法**不报错**：
界面上看起来一切正常，只是那条规矩没了。

更重要的一条：**13d 的审计要在这里插**。所有写操作都经过本模块的具名函数，
到时候补一行 `audit_log.write(...)` 就够了；如果写散在 20 个路由里，
审计就一定会漏几条，而漏的恰好是最不显眼的那几条。

--------------------------------------------------------------------------
为什么抛 AdminError 而不是 HTTPException
--------------------------------------------------------------------------
本模块不 import fastapi：它要能被脚本（seed / 将来的审计回放）直接调用，
也要能被单测不启服务地测。HTTP 语义（状态码）由路由层翻译。
翻译只有一处，所以「唯一键冲突 → 409」这种对应关系不会漂移。

--------------------------------------------------------------------------
「不能对自己动手」这三条规则
--------------------------------------------------------------------------
    · 不能停用 / 离职自己        —— 否则管理员一把就把自己关在门外
    · 不能改自己的角色           —— 同上，降权后连恢复都做不了
    · 不能重置自己的密码         —— 那是主应用「我的账号」自助改密的事；
                                   管理端存在「设置别人密码」这个动作本身
                                   就会诱导实现出「管理员能改别人密码」

外加一条 **最后一个 admin 不能被降权**（也不能被停用）：
没有管理员的系统，下次要做的第一件事就是改数据库。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from config.settings import settings
from core import password_policy as policy
from core import user_repo as repo
from core.db import now_db
from core.identity import Actor

logger = logging.getLogger(__name__)

# 唯一键 → 人话。键名在 str(IntegrityError) 里长这样：`user.uk_user_username`
_KEY_LABEL: dict[str, str] = {
    "uk_user_username": "登录名",
    "uk_user_employee_no": "工号",
    "uk_user_email": "邮箱",
    "uk_dept_code": "部门编码",
    "uk_position_code": "职位编码",
}

_DUP_RE = re.compile(r"Duplicate entry '(?P<value>[^']*)' for key '(?P<key>[^']*)'", re.I)


class AdminError(Exception):
    """管理端的业务错误。`status` 是建议的 HTTP 状态码，由路由层使用。"""

    def __init__(self, message: str, *, status: int = 400, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or "ADMIN_ERROR"


def _raise_readable_integrity_error(e: IntegrityError, fallback: str) -> None:
    """把 MySQL 的唯一键冲突翻成人话再抛。

    为什么不能用「先 SELECT 查一遍」代替：两个管理员同时点提交时，
    两个 SELECT 都通过，然后一个 INSERT 撞键 —— 那条必须拦得住。
    所以预检（给即时反馈）与兜底（给并发正确性）两边都要。
    """
    matched = _DUP_RE.search(str(e).replace("\\'", "'"))
    if matched:
        key = matched.group("key").split(".")[-1]
        label = _KEY_LABEL.get(key, "该字段")
        value = matched.group("value")
        raise AdminError(f"{label} {value} 已被使用，请换一个", status=409,
                         code="DUPLICATE_KEY") from e
    raise AdminError(fallback, status=409, code="DUPLICATE_KEY") from e


def _require(condition: bool, message: str, *, status: int = 400) -> None:
    if not condition:
        raise AdminError(message, status=status)


def _load_user(user_id: int) -> repo.UserRecord:
    record = repo.get(int(user_id))
    if record is None:
        raise AdminError("员工不存在或已被移除", status=404, code="USER_NOT_FOUND")
    return record


def _admin_ids() -> set[int]:
    return {u.id for u in repo.list_users(role=repo.ROLE_ADMIN, include_resigned=True)}


# --------------------------------------------------------------------------- #
# 建号 / 改资料
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NewUser:
    """建号的结果。`temporary_password` **只在这一次响应里出现**（见 §临时密码）。"""

    record: repo.UserRecord
    temporary_password: str


def create_user(
    actor: Actor,
    *,
    username: str,
    employee_no: str,
    display_name: str = "",
    email: str | None = None,
    phone: str | None = None,
    gender: str | None = None,
    department_id: int | None = None,
    position_id: int | None = None,
    role: str = repo.ROLE_USER,
) -> NewUser:
    """
    建一个员工，**并当场签发一个一次性临时密码**。

    为什么不提供「直接设置密码」这个参数：管理员**看不到**也**不该**
    知道员工的密码（设计规格 §0.1）。正确的流程是「发一个临时的 →
    员工首次登录被强制改掉」，所以这里只有一个出口，而且明文只出现一次。
    """
    _require(actor.can_staff, "没有权限新建员工", status=403)
    username = (username or "").strip()
    employee_no = (employee_no or "").strip()
    _require(bool(username), "登录名不能为空")
    _require(bool(employee_no), "工号不能为空")
    if role == repo.ROLE_ADMIN:
        # 提权动作与建号分开是有意的：建号时默认给最小权限，
        # 想给 admin 就再走一次 set_role —— 那样审计里会有两条清楚的记录。
        _require(actor.role == repo.ROLE_ADMIN, "只有系统管理员能直接建管理员账号", status=403)

    # 预检：给用户即时反馈（并发下仍靠下面的 IntegrityError 兜底）
    if repo.get_by_username(username):
        raise AdminError(f"登录名 {username} 已被使用，请换一个", status=409, code="DUPLICATE_KEY")
    if repo.get_by_employee_no(employee_no):
        raise AdminError(f"工号 {employee_no} 已被使用，请换一个", status=409, code="DUPLICATE_KEY")

    temporary = policy.generate_temporary_password()
    # 生成的临时密码必须过得了强度校验 —— 这是「随机口令」与「可用口令」的分界。
    # 这条理论上永不触发（generate_temporary_password 保证过），留着是因为
    # 一旦那条函数被改动，这里会立刻红，而不是等到员工登录时才发现登不进去。
    violations = policy.validate_strength(temporary, username=username, employee_no=employee_no)
    if violations:  # pragma: no cover - 防御性
        raise AdminError(f"生成的临时密码未通过强度校验：{'、'.join(violations)}")

    password_hash = policy.hash_password(temporary)
    stamp = now_db()
    try:
        user_id = repo.create_user(
            username=username,
            employee_no=employee_no,
            password_hash=password_hash,
            display_name=display_name or username,
            email=email,
            phone=phone,
            gender=gender,
            department_id=department_id,
            position_id=position_id,
            role=role,
            status=repo.STATUS_ACTIVE,
            must_change_password=True,
            password_changed_at=stamp,
            created_by=actor.id,
        )
    except IntegrityError as e:
        _raise_readable_integrity_error(e, "登录名、工号或邮箱与其他员工冲突")

    repo.add_password_history(user_id, password_hash, changed_by_user_id=actor.id, now=stamp)
    repo.prune_password_history(user_id, settings.PASSWORD_HISTORY_KEEP)
    record = _load_user(user_id)
    logger.info("新建员工 | actor=%s | id=%s | username=%s | role=%s",
                actor.username, user_id, username, role)
    return NewUser(record=record, temporary_password=temporary)


def update_profile(
    actor: Actor,
    user_id: int,
    **fields: Any,
) -> repo.UserRecord:
    """
    改员工资料。**不动** role / status / password —— 那三样各有自己的入口。

    允许改：display_name / email / phone / gender / department_id / position_id。
    """
    _require(actor.can_staff, "没有权限修改员工资料", status=403)
    allowed = {"display_name", "email", "phone", "gender", "department_id", "position_id"}
    unknown = set(fields) - allowed
    _require(not unknown, f"不允许改这些字段：{sorted(unknown)}")
    _load_user(user_id)  # 先确认这个人存在，否则「改了个不存在的人」会静默成功
    try:
        repo.update_profile(user_id, updated_by=actor.id, **fields)
    except IntegrityError as e:
        _raise_readable_integrity_error(e, "邮箱与其他员工冲突")
    logger.info("修改员工资料 | actor=%s | id=%s | fields=%s",
                actor.username, user_id, sorted(fields))
    return _load_user(user_id)


def set_status(actor: Actor, user_id: int, status: str) -> repo.UserRecord:
    """
    停用 / 复职 / 离职。**离职不删行** —— 他名下还有会话、文档与历史引用。

    三条额外规则：
      1. 不能对自己动手（否则一把就把自己关在门外）；
      2. 不能停用最后一个管理员（没有管理员的系统，下一步就是改数据库）；
      3. 仓库层会 `token_version + 1`，所以他手上还在有效期内的 token 立刻失效。
    """
    _require(actor.can_staff, "没有权限变更在职状态", status=403)
    _require(status in repo.STATUSES, f"状态只能是 {sorted(repo.STATUSES)}")
    record = _load_user(user_id)
    if actor.is_self(user_id):
        raise AdminError("不能停用或离职自己 —— 那会把自己关在管理端门外", status=403)
    if record.role == repo.ROLE_ADMIN and status != repo.STATUS_ACTIVE:
        admins = _admin_ids()
        if admins == {record.id}:
            raise AdminError("他是最后一个系统管理员，不能停用或离职", status=409)
    repo.set_status(user_id, status, updated_by=actor.id)
    logger.info("员工状态变更 | actor=%s | id=%s | %s → %s",
                actor.username, user_id, record.status, status)
    return _load_user(user_id)


def set_role(actor: Actor, user_id: int, role: str) -> repo.UserRecord:
    """
    改系统角色。与 `set_status` 一样会 `token_version + 1`（提权/降权都要立刻生效）。

    两条额外规则：不能改自己的角色；不能把最后一个管理员降下来。
    """
    _require(actor.role == repo.ROLE_ADMIN, "只有系统管理员能改角色", status=403)
    _require(role in repo.ROLES, f"角色只能是 {sorted(repo.ROLES)}")
    record = _load_user(user_id)
    if actor.is_self(user_id) and role != record.role:
        raise AdminError("不能改自己的角色 —— 降权后连恢复都做不了", status=403)
    if record.role == repo.ROLE_ADMIN and role != repo.ROLE_ADMIN:
        if _admin_ids() == {record.id}:
            raise AdminError("他是最后一个系统管理员，不能降权", status=409)
    repo.set_role(user_id, role, updated_by=actor.id)
    logger.info("员工角色变更 | actor=%s | id=%s | %s → %s",
                actor.username, user_id, record.role, role)
    return _load_user(user_id)


# --------------------------------------------------------------------------- #
# 密码（P2-13c）
# --------------------------------------------------------------------------- #
def reset_password(actor: Actor, user_id: int) -> tuple[repo.UserRecord, str]:
    """
    重置为**一次性**临时密码 + 强制首次登录改密。返回 (记录, 明文)。

    明文**只在这里返回一次**，不写日志、不落库明文、不再有第二次查看的入口
    （丢了就只能再重置一次 —— 这是单向哈希的必然代价，不是设计缺陷）。

    为什么不提供「设置一个我指定的密码」：管理员**不该知道**员工的密码。
    一旦存在这个接口，它就会被用来「统一改成同一个口令方便调试」。
    """
    _require(actor.can_reset_password, "人事账号不能重置密码（只能改资料）", status=403)
    record = _load_user(user_id)
    if actor.is_self(user_id):
        raise AdminError("重置自己的密码请去主应用的「我的账号」", status=403)
    if record.status != repo.STATUS_ACTIVE:
        raise AdminError(f"已停用或已离职的员工不签发新密码（当前状态：{record.status}）")

    temporary = policy.generate_temporary_password()
    violations = policy.validate_strength(
        temporary, username=record.username, employee_no=record.employee_no
    )
    if violations:  # pragma: no cover - 防御性
        raise AdminError(f"生成的临时密码未通过强度校验：{'、'.join(violations)}")

    password_hash = policy.hash_password(temporary)
    stamp = now_db()
    repo.update_password(user_id, password_hash, must_change_password=True, now=stamp)
    repo.add_password_history(user_id, password_hash, changed_by_user_id=actor.id, now=stamp)
    repo.prune_password_history(user_id, settings.PASSWORD_HISTORY_KEEP)
    logger.info("重置密码 | actor=%s | id=%s | must_change=1", actor.username, user_id)
    return _load_user(user_id), temporary


def set_must_change(actor: Actor, user_id: int, must_change: bool) -> repo.UserRecord:
    """
    手动开关「下次登录必须改密」。

    为什么要有这个开关：重置密码已经会置 1，但还有两种场景需要单独控制 ——
    ① 怀疑这个账号的密码已经外泄，强制他改掉（比停用温和）；
    ② 员工反馈改密流程卡住，管理员手动清零放行。
    """
    _require(actor.can_reset_password, "人事账号不能改这个开关（只能改资料）", status=403)
    record = _load_user(user_id)
    # ⚠️ 必须走 `set_must_change_password` 而不能走 `update_password`：
    # 后者会把 password_changed_at 写成 now，等于给旧密码续了 90 天。
    repo.set_must_change_password(user_id, bool(must_change), updated_by=actor.id)
    logger.info("强制改密开关 | actor=%s | id=%s | must_change=%s",
                actor.username, user_id, bool(must_change))
    return _load_user(user_id)


# --------------------------------------------------------------------------- #
# 部门
# --------------------------------------------------------------------------- #
def _descendant_ids(dept_id: int) -> set[int]:
    """自己 + 全部子孙的 id（防环用）。"""
    rows = repo.list_departments()
    children: dict[int | None, list[int]] = {}
    for r in rows:
        children.setdefault(r.parent_id, []).append(r.id)
    out: set[int] = set()
    stack = [dept_id]
    while stack:
        current = stack.pop()
        if current in out:
            continue
        out.add(current)
        stack.extend(children.get(current, []))
    return out


def create_department(
    actor: Actor,
    *,
    code: str,
    name: str,
    parent_id: int | None = None,
    leader_user_id: int | None = None,
    sort_order: int = 0,
) -> repo.DepartmentRecord:
    _require(actor.can_manage_org, "没有权限维护部门", status=403)
    try:
        dept_id = repo.create_department(
            code=code, name=name, parent_id=parent_id,
            leader_user_id=leader_user_id, sort_order=sort_order,
        )
    except IntegrityError as e:
        _raise_readable_integrity_error(e, "部门编码已被使用")
    record = repo.get_department(dept_id)
    if record is None:  # pragma: no cover - 建完立刻读，读不到是异常
        raise AdminError("部门创建后读取失败", status=500)
    logger.info("新建部门 | actor=%s | id=%s | code=%s", actor.username, dept_id, code)
    return record


def update_department(actor: Actor, dept_id: int, **fields: Any) -> repo.DepartmentRecord:
    """
    改部门的名称 / 上级 / 负责人 / 排序。

    上级**必须防环**：把 A 的上级设成 A 的子孙，`build_department_tree` 不会
    死循环，但环上的节点「谁都不是根」→ **整棵环从树上消失**。
    那是静默丢数据，比崩更糟（仓储层的文件头写明了这一点，所以校验在这里）。
    """
    _require(actor.can_manage_org, "没有权限维护部门", status=403)
    allowed = {"name", "parent_id", "leader_user_id", "sort_order"}
    unknown = set(fields) - allowed
    _require(not unknown, f"不允许改这些字段：{sorted(unknown)}")
    _require(repo.get_department(dept_id) is not None, "部门不存在", status=404)

    if "parent_id" in fields:
        parent_id = fields["parent_id"]
        if parent_id is not None:
            _require(
                parent_id != dept_id,
                "上级部门不能是自己",
            )
            _require(
                parent_id not in _descendant_ids(dept_id) - {dept_id},
                "上级部门不能是自己的下级（会形成环，整棵子树会从树上消失）",
            )
        repo.set_department_parent(dept_id, parent_id)
    if "leader_user_id" in fields:
        repo.set_department_leader(dept_id, fields["leader_user_id"])
    if "name" in fields or "sort_order" in fields:
        _apply_department_basics(dept_id, fields)

    record = repo.get_department(dept_id)
    if record is None:  # pragma: no cover
        raise AdminError("部门读取失败", status=500)
    logger.info("修改部门 | actor=%s | id=%s | fields=%s", actor.username, dept_id, sorted(fields))
    return record


def _apply_department_basics(dept_id: int, fields: dict[str, Any]) -> None:
    from sqlalchemy import update as sa_update

    from core.db import session_scope
    from core.schema import department_table

    values = {k: v for k, v in fields.items() if k in ("name", "sort_order")}
    if not values:
        return
    with session_scope() as session:
        session.execute(
            sa_update(department_table).where(department_table.c.id == dept_id).values(**values)
        )


def delete_department(actor: Actor, dept_id: int) -> None:
    """
    删部门。**只在既没有子部门、也没有成员时允许** ——
    部门是员工的归属锚点，删掉会让一批人的 `department_id` 悬空。
    """
    _require(actor.can_manage_org, "没有权限维护部门", status=403)
    _require(repo.get_department(dept_id) is not None, "部门不存在", status=404)
    children = [d for d in repo.list_departments() if d.parent_id == dept_id]
    _require(not children, f"还有 {len(children)} 个下级部门，请先移走或删除它们")
    members = repo.count_users_in_department(dept_id, only_active=False)
    _require(members == 0, f"部门下还有 {members} 名员工，请先调整他们的部门")

    from sqlalchemy import delete as sa_delete

    from core.db import session_scope
    from core.schema import department_table

    with session_scope() as session:
        session.execute(sa_delete(department_table).where(department_table.c.id == dept_id))
    logger.info("删除部门 | actor=%s | id=%s", actor.username, dept_id)


# --------------------------------------------------------------------------- #
# 职位
# --------------------------------------------------------------------------- #
def create_position(
    actor: Actor,
    *,
    code: str,
    name: str,
    level: str | None = None,
    sequence: str = repo.SEQUENCE_TECH,
) -> repo.PositionRecord:
    _require(actor.can_manage_org, "没有权限维护职位", status=403)
    try:
        pos_id = repo.create_position(code=code, name=name, level=level, sequence=sequence)
    except IntegrityError as e:
        _raise_readable_integrity_error(e, "职位编码已被使用")
    record = repo.get_position_by_code(code)
    if record is None:  # pragma: no cover
        raise AdminError("职位创建后读取失败", status=500)
    logger.info("新建职位 | actor=%s | id=%s | code=%s", actor.username, pos_id, code)
    return record


def update_position(actor: Actor, pos_id: int, **fields: Any) -> repo.PositionRecord:
    _require(actor.can_manage_org, "没有权限维护职位", status=403)
    allowed = {"code", "name", "level", "sequence"}
    unknown = set(fields) - allowed
    _require(not unknown, f"不允许改这些字段：{sorted(unknown)}")

    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from core.db import session_scope
    from core.schema import position_table

    with session_scope() as session:
        exists = session.execute(
            sa_select(position_table.c.id).where(position_table.c.id == pos_id)
        ).first()
        if exists is None:
            raise AdminError("职位不存在", status=404)
        try:
            session.execute(
                sa_update(position_table).where(position_table.c.id == pos_id).values(**fields)
            )
        except IntegrityError as e:
            _raise_readable_integrity_error(e, "职位编码已被使用")
    for candidate in repo.list_positions():
        if candidate.id == pos_id:
            logger.info("修改职位 | actor=%s | id=%s | fields=%s",
                        actor.username, pos_id, sorted(fields))
            return candidate
    raise AdminError("职位读取失败", status=500)  # pragma: no cover


def delete_position(actor: Actor, pos_id: int) -> None:
    """
    删职位。有人挂着这个职位时拒绝 —— 与部门同理（引用会悬空）。

    ⚠️ 这里的「有人挂」按**全部在职状态**数（含离职）：离职员工的记录也要
    能正确显示他当年的职位，所以引用是真的存在。
    """
    _require(actor.can_manage_org, "没有权限维护职位", status=403)

    from sqlalchemy import delete as sa_delete
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    from core.db import session_scope
    from core.schema import position_table, user_table

    with session_scope() as session:
        exists = session.execute(
            sa_select(position_table.c.id).where(position_table.c.id == pos_id)
        ).first()
        if exists is None:
            raise AdminError("职位不存在", status=404)
        used = session.execute(
            sa_select(sa_func.count()).select_from(user_table)
            .where(user_table.c.position_id == pos_id)
        ).scalar() or 0
        if used:
            raise AdminError(f"还有 {used} 名员工挂着这个职位，请先调整他们的职位")
        session.execute(sa_delete(position_table).where(position_table.c.id == pos_id))
    logger.info("删除职位 | actor=%s | id=%s", actor.username, pos_id)


# --------------------------------------------------------------------------- #
# 密码到期看板（P2-13c）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PasswordBoard:
    """
    到期看板。

    四个数字**互不包含**：一个人只会出现在其中一个桶里，
    否则「7 天内到期」和「已过期」会重复计数，看板加总对不上人数 ——
    那种数字没人敢拿去汇报。
    """

    must_change: list[dict[str, Any]]
    expiring: list[dict[str, Any]]
    expired: list[dict[str, Any]]
    stale_login: list[dict[str, Any]]
    locked: list[dict[str, Any]]
    generated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "must_change": self.must_change,
            "expiring_soon": self.expiring,
            "expired": self.expired,
            "stale_login": self.stale_login,
            "locked": self.locked,
            "counts": {
                "must_change": len(self.must_change),
                "expiring_soon": len(self.expiring),
                "expired": len(self.expired),
                "stale_login": len(self.stale_login),
                "locked": len(self.locked),
            },
            "generated_at": self.generated_at.isoformat(sep=" ", timespec="seconds"),
            "policy": {
                "expire_days": settings.PASSWORD_EXPIRE_DAYS,
                "warn_days": settings.PASSWORD_EXPIRE_WARN_DAYS,
            },
        }


def _brief(record: repo.UserRecord, now: datetime) -> dict[str, Any]:
    changed_at = record.password_changed_at
    return {
        "id": record.id,
        "username": record.username,
        "display_name": record.display_name,
        "department_id": record.department_id,
        "status": record.status,
        "must_change": bool(record.must_change_password),
        "expire_in_days": policy.expire_in_days(changed_at, now=now),
        "last_login_at": record.last_login_at.isoformat(sep=" ", timespec="seconds")
        if record.last_login_at else None,
    }


def password_board(actor: Actor, *, now: datetime | None = None,
                   stale_days: int = 90) -> PasswordBoard:
    """
    密码到期看板 —— 只统计**在职**的人。

    不统计离职/停用：他们本来就登不进来，把他们的「密码过期」放进看板，
    只会让管理员每看一次都要先想一遍「这些人为什么还在」。
    """
    _require(actor.can_staff, "没有权限查看密码看板", status=403)
    stamp = now or now_db()
    must_change: list[dict[str, Any]] = []
    expiring: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    locked: list[dict[str, Any]] = []

    for record in repo.list_users(status=repo.STATUS_ACTIVE, include_resigned=False):
        if record.is_locked_at(stamp):
            locked.append(_brief(record, stamp))
            continue
        if record.must_change_password:
            must_change.append(_brief(record, stamp))
            continue
        if policy.is_expired(record.password_changed_at, now=stamp):
            expired.append(_brief(record, stamp))
            continue
        if policy.should_warn_expire(record.password_changed_at, now=stamp):
            expiring.append(_brief(record, stamp))
            continue
        # 长期未登录：密码没到期，但人不见了 —— 这是「账号该回收」的信号，
        # 与密码到期是两件事，所以单独一个桶。
        if record.last_login_at is None or (stamp - record.last_login_at).days >= stale_days:
            stale.append(_brief(record, stamp))

    return PasswordBoard(
        must_change=must_change,
        expiring=expiring,
        expired=expired,
        stale_login=stale,
        locked=locked,
        generated_at=stamp,
    )
