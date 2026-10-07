"""
user / department / position 的仓储 —— 组织与账号数据的**唯一**读写入口（P2-11a）。

--------------------------------------------------------------------------
这一层为什么现在才存在
--------------------------------------------------------------------------
P0-2 的账号校验完全发生在网关（`.env` 里的 bcrypt 串），后端连「来的是谁」
都不知道，`user` 表建了三个月一行都没有。现在要把账号迁到 MySQL（PLAN §11
D2 由 P2-11c 显式反转），表得先准备好 —— 但**只准备表和存储原语**：

    本模块负责：把数据正确地读写进 MySQL
    P2-11b 负责：密码到期、历史不可复用、失败锁定这些**判定规则**
    P2-11c 负责：网关查库、token 签发与吊销
    P2-12b 负责：请求进来时「你是谁」的解析
    P2-13b 负责：管理端接口与权限校验

把「判定规则」放在仓储层会立刻坏掉：同一个「是否锁定」的判断会在
`api/routes/admin/*`、网关查询、种子脚本里各写一遍，三处漂移。
所以这里只提供**原语**（`update_password` / `record_login_failure` /
`bump_token_version`），规则由 P2-11b 注入。

--------------------------------------------------------------------------
为什么 role / status 的取值集合写在 Python 里而不是数据库
--------------------------------------------------------------------------
列是 VARCHAR（0001 对 document.status 的同一取舍：ENUM 加值要锁表），
取值集合靠应用层校验。校验点放在**仓储入口**，理由是失败要早、要响：

    set_status(7, "activ")   → 立刻 ValueError
    （而不是）写进库里 → 半年后登录逻辑判不出这个值 → 「这人怎么登不进去」

所以本模块的每个写入口都先过 `_check_role` / `_check_status`。
代价是新增取值要同时改列注释与这两处 —— 但这正是「取值集合有单一出处」
该有的样子。

--------------------------------------------------------------------------
为什么 update_time 一律显式写
--------------------------------------------------------------------------
迁移 0003 的文件头解释过：不用 `ON UPDATE CURRENT_TIMESTAMP`，
因为那在 SQLAlchemy Core 里没法用列属性表达，会让 DDL 快照与结构视图
对不上。代价就是**应用层负责维护**，本模块每个 UPDATE 都带它，
漏写就会留下「时间戳停在旧值」的历史记录 —— 那比时间戳不准更麻烦，
因为它看起来是对的。

--------------------------------------------------------------------------
为什么离职是 status=resigned 而不是删行
--------------------------------------------------------------------------
user 一旦被引用就是别人的锚点：`session.user_id`、`document` 的上传者、
`department.leader_user_id`、`chat_message` 的历史。删行会让这些引用全部变成
悬空指针，而「离职员工的会话记录还要能查」是企业系统的常规审计要求。
所以本模块**不提供 delete_user**，只提供 `set_status(..., "resigned")`。
真正要清理的是 P2-10 定的数据保留策略，届时也只清归档行、不删业务行。

--------------------------------------------------------------------------
手机号为什么默认脱敏
--------------------------------------------------------------------------
`user.phone` 是个人信息。`UserRecord.to_dict()` 默认输出 `138****8000`
这种形式，必须显式传 `raw_phone=True` 才给原值 —— 让「忘记脱敏」这件事
在代码评审时是个需要解释的选择，而不是一次疏忽。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, or_, select, update

from core.db import now_db, session_scope
from core.kb_acl import KB_ROLE_NONE, check_kb_role
from core.schema import (
    department_table,
    position_table,
    user_password_history_table,
    user_table,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 取值集合（唯一出处：改这里 + 改列注释，两处）
# --------------------------------------------------------------------------- #
ROLE_ADMIN = "admin"
ROLE_HR = "hr"
ROLE_USER = "user"
ROLES = frozenset({ROLE_ADMIN, ROLE_HR, ROLE_USER})

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_RESIGNED = "resigned"
STATUSES = frozenset({STATUS_ACTIVE, STATUS_DISABLED, STATUS_RESIGNED})

SEQUENCE_TECH = "tech"
SEQUENCE_MANAGEMENT = "management"
SEQUENCE_FUNCTION = "function"
SEQUENCES = frozenset({SEQUENCE_TECH, SEQUENCE_MANAGEMENT, SEQUENCE_FUNCTION})

# 区分「这个字段不改」与「把它设为 NULL」。用 None 当哨兵不行 ——
# 「不改邮箱」与「把邮箱清空」是两件不同的事。
_UNSET: Any = object()


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"role 只能是 {sorted(ROLES)}，收到 {role!r}")
    return role


def _check_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"status 只能是 {sorted(STATUSES)}，收到 {status!r}")
    return status


def _check_sequence(sequence: str) -> str:
    if sequence not in SEQUENCES:
        raise ValueError(f"sequence 只能是 {sorted(SEQUENCES)}，收到 {sequence!r}")
    return sequence


def mask_phone(phone: str | None) -> str | None:
    """手机号脱敏：``13812345678`` → ``138****5678``。短于 7 位的一律打码。"""
    if not phone:
        return phone
    if len(phone) < 7:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


# --------------------------------------------------------------------------- #
# 记录对象
# --------------------------------------------------------------------------- #
@dataclass
class UserRecord:
    """一条员工记录。字段与 user 表一一对应，不做加工。"""

    id: int
    username: str
    employee_no: str
    display_name: str
    email: str | None
    phone: str | None
    gender: str | None
    department_id: int | None
    position_id: int | None
    role: str
    kb_role: str
    status: str
    password_hash: str
    password_changed_at: datetime | None
    must_change_password: bool
    token_version: int
    failed_login_count: int
    locked_until: datetime | None
    last_login_at: datetime | None
    created_by: int | None
    updated_by: int | None
    create_time: datetime
    update_time: datetime
    deleted_at: datetime | None

    def to_dict(self, *, raw_phone: bool = False) -> dict[str, Any]:
        """
        转成 JSON 友好的 dict（**不含** password_hash）。

        手机号默认脱敏，见模块文件头。`password_hash` 压根不进 dict ——
        它没有需要出现在接口上的正当理由，谁要它谁去查库。
        """
        return {
            "id": self.id,
            "username": self.username,
            "employee_no": self.employee_no,
            "display_name": self.display_name,
            "email": self.email,
            "phone": self.phone if raw_phone else mask_phone(self.phone),
            "gender": self.gender,
            "department_id": self.department_id,
            "position_id": self.position_id,
            "role": self.role,
            "kb_role": self.kb_role,
            "status": self.status,
            "must_change_password": bool(self.must_change_password),
            "token_version": int(self.token_version),
            "failed_login_count": int(self.failed_login_count),
            "locked_until": self.locked_until.isoformat(sep=" ", timespec="microseconds")
            if self.locked_until else None,
            "last_login_at": self.last_login_at.isoformat(sep=" ", timespec="microseconds")
            if self.last_login_at else None,
            "create_time": self.create_time.isoformat(sep=" ", timespec="seconds"),
            "update_time": self.update_time.isoformat(sep=" ", timespec="microseconds"),
        }

    @property
    def is_active(self) -> bool:
        """只有 active 能登录。离职（resigned）与停用（disabled）都不行。"""
        return self.status == STATUS_ACTIVE

    def is_locked_at(self, now: datetime | None = None) -> bool:
        """
        是否处于登录锁定期内（纯读字段，不含阈值判定）。

        「连错几次锁多久」是 P2-11b 的口径；这里只回答「截至某一刻，
        锁还没到期吗」。
        """
        if not self.locked_until:
            return False
        return self.locked_until > (now or now_db())


@dataclass
class DepartmentRecord:
    id: int
    code: str
    name: str
    parent_id: int | None
    leader_user_id: int | None
    sort_order: int
    create_time: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "parent_id": self.parent_id,
            "leader_user_id": self.leader_user_id,
            "sort_order": int(self.sort_order),
            "create_time": self.create_time.isoformat(sep=" ", timespec="seconds"),
        }


@dataclass
class PositionRecord:
    id: int
    code: str
    name: str
    level: str | None
    sequence: str
    create_time: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "sequence": self.sequence,
            "create_time": self.create_time.isoformat(sep=" ", timespec="seconds"),
        }


def _row_to_user(row: Any) -> UserRecord:
    """
    Row → dataclass，用显式字段名而不是 `UserRecord(**row._mapping)`。

    后者会在表加列后因为多出一个未知关键字参数而在**运行时**抛 TypeError，
    且失败点会落在「读用户」而不是「写用户」（对齐 document_repo 的同一取舍）。
    """
    return UserRecord(
        id=int(row.id),
        username=row.username,
        employee_no=row.employee_no or "",
        display_name=row.display_name,
        email=row.email,
        phone=row.phone,
        gender=row.gender,
        department_id=int(row.department_id) if row.department_id is not None else None,
        position_id=int(row.position_id) if row.position_id is not None else None,
        role=row.role,
        # ⚠️ 唯一用 getattr 容错的字段，理由与其他字段不同：
        # 其余列要么在 0001 建表时就有、要么在 0003 一次补齐，
        # 只有 kb_role 是**本轮（0006）刚加的**，存在「迁移还没跑」的窗口
        # （比如有人直接 checkout 新代码而忘了 make db-upgrade）。
        # 那时若抛 AttributeError，`resolve_actor()` 会 500，
        # 症状是「刚拉完代码整个系统进不去」—— 而真实原因只是一列没建。
        # 降级成 none 的话，最坏结果是**所有人暂时没有写权限**（fail-closed），
        # 而不是所有人进不去。后者会让人往完全错误的方向排查。
        kb_role=getattr(row, "kb_role", None) or KB_ROLE_NONE,
        status=row.status,
        password_hash=row.password_hash,
        password_changed_at=row.password_changed_at,
        must_change_password=bool(row.must_change_password),
        token_version=int(row.token_version),
        failed_login_count=int(row.failed_login_count),
        locked_until=row.locked_until,
        last_login_at=row.last_login_at,
        created_by=int(row.created_by) if row.created_by is not None else None,
        updated_by=int(row.updated_by) if row.updated_by is not None else None,
        create_time=row.create_time,
        update_time=row.update_time,
        deleted_at=row.deleted_at,
    )


def _row_to_department(row: Any) -> DepartmentRecord:
    return DepartmentRecord(
        id=int(row.id),
        code=row.code,
        name=row.name,
        parent_id=int(row.parent_id) if row.parent_id is not None else None,
        leader_user_id=int(row.leader_user_id) if row.leader_user_id is not None else None,
        sort_order=int(row.sort_order),
        create_time=row.create_time,
    )


def _row_to_position(row: Any) -> PositionRecord:
    return PositionRecord(
        id=int(row.id),
        code=row.code,
        name=row.name,
        level=row.level,
        sequence=row.sequence,
        create_time=row.create_time,
    )


# --------------------------------------------------------------------------- #
# 写入：账号
# --------------------------------------------------------------------------- #
def create_user(
    *,
    username: str,
    employee_no: str,
    password_hash: str,
    display_name: str = "",
    email: str | None = None,
    phone: str | None = None,
    gender: str | None = None,
    department_id: int | None = None,
    position_id: int | None = None,
    role: str = ROLE_USER,
    kb_role: str = KB_ROLE_NONE,
    status: str = STATUS_ACTIVE,
    must_change_password: bool = False,
    password_changed_at: datetime | None = None,
    created_by: int | None = None,
) -> int:
    """
    建一个员工，返回 user.id。

    `password_hash` 由调用方传入 —— **本模块不生成哈希**。bcrypt 的 cost、
    随机盐、强度校验全在 P2-11b，这里收什么就存什么。

    `must_change_password=True` 是「管理员建号 / 重置为临时密码」的标准用法：
    员工首次登录会被强制改密（判定在 11c，标记落在这里）。
    """
    _check_role(role)
    _check_status(status)
    # 校验并取规范值（去空格 + 小写），拿到的一定是合法 kb_role
    kb_role = check_kb_role(kb_role)
    if not username or not username.strip():
        raise ValueError("username 不能为空")
    if not employee_no or not employee_no.strip():
        raise ValueError("employee_no 不能为空（应用层不允许写成空串，"
                         "否则第二个没有工号的员工会撞唯一键）")
    if not password_hash:
        raise ValueError("password_hash 不能为空")

    # ⚠️ password_changed_at 缺省**补成 now**，而不是接受 NULL（P2-11c 踩出来的）。
    #
    # 11b 定了「`password_changed_at` 为 NULL 时按已过期处理」（fail-closed）——
    # 那是对「迁移前的老行」与「11a 遗留的空行」的正确处置。
    # 但 create_user 传进来一个哈希、**不传时间**时，语义上显然是「这个人刚建、
    # 密码刚定」，落成 NULL 等于建出一个**一登录就报「密码已过期」**的账号。
    #
    # 现状：两条真实路径（admin_service 建号、seed_users 种子）都显式传了时间，
    # 所以没出过事 —— 是本模块的测试写漏了才发现。留着的风险是：将来第三个
    # 调用方（数据迁移？批量导入？）忘了传，就是一个静默的「所有人都登不进去」。
    # 兜底放在这一层，是因为它是唯一能让「漏传」不可能出错的地方。
    #
    # 真要表达「这个密码从一开始就没有有效日期」（比如导入一个只知哈希、
    # 不知何时设的外部账号），仍然可以显式传 password_changed_at=None 吗？
    # **不行** —— 分不清「显式要 NULL」与「忘了传」。所以这里把两种都补成 now，
    # 要造过期账号请用 update_password() 传一个过去的时间，那才是明确的表达。
    password_changed_at = password_changed_at or now_db()

    now = now_db()
    values = {
        "username": username.strip(),
        "employee_no": employee_no.strip(),
        "display_name": display_name or username,
        "email": (email or None),
        "phone": (phone or None),
        "gender": (gender or None),
        "department_id": department_id,
        "position_id": position_id,
        "role": role,
        "kb_role": kb_role,
        "status": status,
        "password_hash": password_hash,
        "password_changed_at": password_changed_at,
        "must_change_password": bool(must_change_password),
        "token_version": 0,
        "failed_login_count": 0,
        "created_by": created_by,
        "updated_by": created_by,
        "update_time": now,
    }
    with session_scope() as session:
        result = session.execute(user_table.insert().values(**values))
        user_id = int(result.inserted_primary_key[0])
    logger.info(
        "员工已创建 | id=%s | username=%s | employee_no=%s | role=%s | kb_role=%s | status=%s",
        user_id, values["username"], values["employee_no"], role, kb_role, status,
    )
    return user_id


# --------------------------------------------------------------------------- #
# 读取：账号
# --------------------------------------------------------------------------- #
def get(user_id: int) -> UserRecord | None:
    stmt = select(user_table).where(user_table.c.id == user_id)
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_user(row) if row else None


def get_by_username(username: str) -> UserRecord | None:
    stmt = select(user_table).where(user_table.c.username == username)
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_user(row) if row else None


def get_by_employee_no(employee_no: str) -> UserRecord | None:
    stmt = select(user_table).where(user_table.c.employee_no == employee_no)
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_user(row) if row else None


def list_users(
    *,
    department_id: int | None = None,
    role: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    include_resigned: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[UserRecord]:
    """
    列表。默认**不含离职**（`include_resigned=False`）—— 选人框里不该出现已离职的人。

    `keyword` 同时匹配姓名 / 登录名 / 工号 / 邮箱，因为管理员找一个人的
    路径不止一条（有时记工号，有时记邮箱）。
    """
    stmt = select(user_table)
    if department_id is not None:
        stmt = stmt.where(user_table.c.department_id == department_id)
    if role is not None:
        stmt = stmt.where(user_table.c.role == _check_role(role))
    if status is not None:
        stmt = stmt.where(user_table.c.status == _check_status(status))
    elif not include_resigned:
        stmt = stmt.where(user_table.c.status != STATUS_RESIGNED)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                user_table.c.display_name.like(like),
                user_table.c.username.like(like),
                user_table.c.employee_no.like(like),
                user_table.c.email.like(like),
            )
        )
    stmt = (
        stmt.order_by(user_table.c.department_id, user_table.c.id)
        .limit(max(1, int(limit)))
        .offset(max(0, int(offset)))
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [_row_to_user(r) for r in rows]


def count_users_in_department(department_id: int, *, only_active: bool = True) -> int:
    """部门下还有几个人。删部门前的前置检查（也要给 P2-14 的 D13 当默认授权依据）。"""
    stmt = select(user_table.c.id).where(user_table.c.department_id == department_id)
    if only_active:
        stmt = stmt.where(user_table.c.status == STATUS_ACTIVE)
    with session_scope() as session:
        return len(session.execute(stmt).all())


# --------------------------------------------------------------------------- #
# 更新：账号
# --------------------------------------------------------------------------- #
def update_profile(
    user_id: int,
    *,
    display_name: Any = _UNSET,
    email: Any = _UNSET,
    phone: Any = _UNSET,
    gender: Any = _UNSET,
    department_id: Any = _UNSET,
    position_id: Any = _UNSET,
    updated_by: int | None = None,
) -> bool:
    """
    改员工资料。**不动** role / status / password —— 那三样各有自己的入口，
    因为它们都要连带 token_version。

    不传某个参数 = 不改它；传 ``None`` = 把它清空。这个区别靠 `_UNSET` 哨兵实现，
    传 `None` 清空邮箱是真的会清掉。
    """
    values: dict[str, Any] = {}
    for field, value in (
        ("display_name", display_name),
        ("email", email),
        ("phone", phone),
        ("gender", gender),
        ("department_id", department_id),
        ("position_id", position_id),
    ):
        if value is not _UNSET:
            values[field] = value
    if not values:
        return False
    values["update_time"] = now_db()
    values["updated_by"] = updated_by

    stmt = update(user_table).where(user_table.c.id == user_id).values(**values)
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def set_status(user_id: int, status: str, *, updated_by: int | None = None) -> bool:
    """
    改在职状态，**并让已签发的 token 立刻失效**（`token_version + 1`）。

    为什么连坐 token_version：JWT 是无状态的、签发后无法收回（PLAN §11 D4 定了
    不做 token 白名单）。若只改 status 不动 ver，员工被停用/离职后，
    他手上的 token 在有效期内还能继续用 —— 这正是「停用了人还能用半天」的老问题。

    恢复 active 也同样 +1：不能让一个「离职前签发的、还宽限期的 token」
    在恢复后复活。
    """
    _check_status(status)
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            status=status,
            token_version=user_table.c.token_version + 1,
            updated_by=updated_by,
            update_time=now_db(),
        )
    )
    with session_scope() as session:
        affected = session.execute(stmt).rowcount > 0
    if affected:
        logger.info("员工状态变更 | id=%s | status=%s | token_version 已 +1", user_id, status)
    return affected


def set_role(user_id: int, role: str, *, updated_by: int | None = None) -> bool:
    """改系统角色，同样 +1 token_version（提权/降权都要立刻生效）。"""
    _check_role(role)
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            role=role,
            token_version=user_table.c.token_version + 1,
            updated_by=updated_by,
            update_time=now_db(),
        )
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def set_kb_role(user_id: int, kb_role: str, *, updated_by: int | None = None) -> bool:
    """
    改知识库写权限，**同样 +1 token_version**（提权/降权都要立刻生效）。

    ⚠️ 为什么 `kb_role` 变更也要 `token_version+1`（P2-14b 之后这条才生效，
    在此之前 `kb_role` 不进 JWT，所以老 token 拿不到旧值）：

    `kb_role` 会**进 JWT**（D15），网关用它做粗筛。不+1 的话，
    管理员刚把一个`none` 提到 `superadmin`，那个人手里的旧 token
    在最长 12h 内仍然带着 `kb_role=none` —— 网关会继续拦他，
    于是「授权了却还要重新登录才生效」。而**降权**方向更危险：
    把一个 `superadmin` 降到 `none`，旧 token 还能删知识库。

    所以：**凡是「权限」字段的变更，都必须立刻使旧 token 失效**。
    这条与 `set_role` / `set_status` / `update_password` 是同一个理由。
    """
    kb_role = check_kb_role(kb_role)
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            kb_role=kb_role,
            token_version=user_table.c.token_version + 1,
            updated_by=updated_by,
            update_time=now_db(),
        )
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def bump_token_version(user_id: int) -> int:
    """单独 +1 并返回新的值。密码变更等场景用（策略在 P2-11b）。"""
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(token_version=user_table.c.token_version + 1, update_time=now_db())
    )
    with session_scope() as session:
        session.execute(stmt)
        row = session.execute(
            select(user_table.c.token_version).where(user_table.c.id == user_id)
        ).first()
    return int(row[0]) if row else 0


def update_password(
    user_id: int,
    password_hash: str,
    *,
    must_change_password: bool = False,
    now: datetime | None = None,
) -> bool:
    """
    落一次密码变更：**只负责存**。

    它顺手做了三件「不做就会出事」的事：

    1. `password_changed_at = now` —— 90 天到期算的就是这一列；
    2. `token_version + 1` —— 改密后其他设备上的旧 token 全部失效；
    3. `failed_login_count = 0`、`locked_until = NULL` —— 改密成功等于解锁。

    哈希由调用方给（cost / 强度 / 历史不可复用的校验在 P2-11b）。
    """
    if not password_hash:
        raise ValueError("password_hash 不能为空")
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            password_hash=password_hash,
            password_changed_at=now or now_db(),
            must_change_password=bool(must_change_password),
            failed_login_count=0,
            locked_until=None,
            token_version=user_table.c.token_version + 1,
            update_time=now_db(),
        )
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def set_must_change_password(
    user_id: int,
    must_change: bool,
    *,
    updated_by: int | None = None,
) -> bool:
    """
    只改 `must_change_password` 一列 —— 这个开关**绝不能**走 `update_password`。

    为什么单独开一个入口：`update_password` 会把 `password_changed_at` 写成
    now（那是它该做的事：换密码 = 重新起算 90 天）。而「强制他下次登录改密」
    **没有换密码** —— 走那条路等于顺手给他的旧密码续了 90 天有效期，
    而管理员的意图恰恰相反（往往是「他的密码可能泄露了，让他改掉」）。
    实测过：点一下开关，`password_changed_at` 就从三个月前跳到刚才。

    它**不动** `token_version`：不换密码就不该把人踢下线。
    """
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            must_change_password=bool(must_change),
            updated_by=updated_by,
            update_time=now_db(),
        )
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def update_password_hash_only(
    user_id: int,
    password_hash: str,
    *,
    now: datetime | None = None,
) -> bool:
    """
    **只**替换 `password_hash` 一列 —— 登录时的 cost 升级专用（P2-11c）。

    为什么不能走 `update_password()`：那个函数是「换密码」语义，会连带
    `token_version + 1`（改密后其他设备全部失效）、`password_changed_at = now`
    （给密码续了 90 天）、清强制改密标志。把「只是把 cost 补上去」做成
    一次换密码，等于**用户每次登录都被踢下线、每次登录都续一次有效期** ——
    90 天到期这个策略就永远不会触发了。

    所以这里刻意只动一列。`password_changed_at` 不动是有意的：改的是
    哈希的**表示形式**（cost 参数），不是密码本身。
    """
    if not password_hash:
        raise ValueError("password_hash 不能为空")
    stamp = now or now_db()
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(password_hash=password_hash, update_time=stamp)
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def record_login_success(user_id: int, *, now: datetime | None = None) -> bool:
    """登录成功：失败计数归零、清锁、写最后登录时间。"""
    stamp = now or now_db()
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            failed_login_count=0,
            locked_until=None,
            last_login_at=stamp,
            update_time=stamp,
        )
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def record_login_failure(
    user_id: int,
    *,
    lock_until: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """
    记录一次登录失败，**返回累计的连续失败次数**。

    连加几次就锁、锁多久，是 P2-11b 的口径；调用方把算好的 `lock_until`
    传进来。这里只负责计数（用 `failed_login_count + 1` 的原子自增，
    不用「先查再写」，理由与 document_repo 的原子抢任务一样）。
    """
    stamp = now or now_db()
    stmt = (
        update(user_table)
        .where(user_table.c.id == user_id)
        .values(
            failed_login_count=user_table.c.failed_login_count + 1,
            locked_until=lock_until,
            update_time=stamp,
        )
    )
    with session_scope() as session:
        session.execute(stmt)
        row = session.execute(
            select(user_table.c.failed_login_count).where(user_table.c.id == user_id)
        ).first()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# 改密历史（P2-11b）
# --------------------------------------------------------------------------- #
# 这里只负责**存取**。「新密码是否命中历史」这个判定在 core/password_policy.py ——
# 刻意不放仓储层：判定要能被单测，而单测连一个 MySQL 都不该起。
def add_password_history(
    user_id: int,
    password_hash: str,
    *,
    changed_by_user_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """
    追加一条改密历史，返回自增 id。

    `changed_by_user_id` 传None 表示「本人自助改密」；管理员重置时传管理员 id。
    刻意不用 0 占位（与 0001 里 `folder.user_id` 不用伪 id 同一个理由）。
    """
    if not password_hash:
        raise ValueError("password_hash 不能为空")
    values = {
        "user_id": user_id,
        "password_hash": password_hash,
        "changed_at": now or now_db(),
        "changed_by_user_id": changed_by_user_id,
    }
    with session_scope() as session:
        result = session.execute(user_password_history_table.insert().values(**values))
        return int(result.inserted_primary_key[0])


def list_recent_password_hashes(user_id: int, limit: int) -> list[str]:
    """
    取最近 `limit` 条历史哈希，**按时间倒序**（最近的在最前）。

    只返回哈希字符串本身：判定只需要它，不需要时间与操作人。
    """
    stmt = (
        select(user_password_history_table.c.password_hash)
        .where(user_password_history_table.c.user_id == user_id)
        .order_by(
            user_password_history_table.c.changed_at.desc(),
            user_password_history_table.c.id.desc(),
        )
        .limit(max(1, int(limit)))
    )
    with session_scope() as session:
        return [r[0] for r in session.execute(stmt).all()]


def count_password_history(user_id: int) -> int:
    stmt = (
        select(user_password_history_table.c.id)
        .where(user_password_history_table.c.user_id == user_id)
    )
    with session_scope() as session:
        return len(session.execute(stmt).all())


def prune_password_history(user_id: int, keep: int) -> int:
    """
    只保留最近 `keep` 条，裁掉更旧的，返回裁掉几条。

    为什么按「时间倒序留 keep 条」而不是按 id：改密时间由应用显式传入，
    同一毫秒连改两次时用 `id` 兜底排序（见 `list_recent_password_hashes`），
    否则「哪条是最新的」在同秒并发下会不稳定。

    为什么裁掉而不是永不删：全量历史既不能提高安全性（离线可验证的组合
    与「最近用过哪几个」无关），又会无限增长。见迁移 0004 的文件头。
    """
    keep_n = max(1, int(keep))
    survivors = list_recent_password_hashes_ids(user_id, keep_n)
    if not survivors:
        return 0
    stmt = (
        delete(user_password_history_table)
        .where(user_password_history_table.c.user_id == user_id)
        .where(user_password_history_table.c.id.notin_(survivors))
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount or 0


def list_recent_password_hashes_ids(user_id: int, limit: int) -> list[int]:
    """取最近 N 条历史的**行 id**（裁剪用），排序口径与取哈希时一致。"""
    stmt = (
        select(user_password_history_table.c.id)
        .where(user_password_history_table.c.user_id == user_id)
        .order_by(
            user_password_history_table.c.changed_at.desc(),
            user_password_history_table.c.id.desc(),
        )
        .limit(max(1, int(limit)))
    )
    with session_scope() as session:
        return [int(r[0]) for r in session.execute(stmt).all()]


# --------------------------------------------------------------------------- #
# 写入 / 读取：部门
# --------------------------------------------------------------------------- #
def create_department(
    *,
    code: str,
    name: str,
    parent_id: int | None = None,
    leader_user_id: int | None = None,
    sort_order: int = 0,
) -> int:
    if not code or not code.strip():
        raise ValueError("部门 code 不能为空")
    if not name or not name.strip():
        raise ValueError("部门 name 不能为空")
    values = {
        "code": code.strip(),
        "name": name.strip(),
        "parent_id": parent_id,
        "leader_user_id": leader_user_id,
        "sort_order": int(sort_order),
    }
    with session_scope() as session:
        result = session.execute(department_table.insert().values(**values))
        dept_id = int(result.inserted_primary_key[0])
    logger.info("部门已创建 | id=%s | code=%s | name=%s", dept_id, values["code"], values["name"])
    return dept_id


def get_department(dept_id: int) -> DepartmentRecord | None:
    stmt = select(department_table).where(department_table.c.id == dept_id)
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_department(row) if row else None


def get_department_by_code(code: str) -> DepartmentRecord | None:
    stmt = select(department_table).where(department_table.c.code == code)
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_department(row) if row else None


def list_departments() -> list[DepartmentRecord]:
    """按 (sort_order, id) 升序 —— 管理端树控件直接用这个顺序即可。"""
    stmt = select(department_table).order_by(department_table.c.sort_order, department_table.c.id)
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [_row_to_department(r) for r in rows]


def set_department_parent(dept_id: int, parent_id: int | None) -> bool:
    """
    改部门的上级（树结构调整）。

    **不防环**：把 A 的上级设成 B 的下级会形成环，`build_department_tree` 遇到环
    不会死循环（它按 parent 是否在 nodes 里判断，环上的节点谁都不是根，会**整棵
    环消失**，不是无限递归）—— 但那是「静默丢数据」，比崩更糟。

    所以调用方（管理端 P2-13b）必须自己校验「不能选自己的子孙做上级」。
    仓储层只负责落库，不替业务做树形决策。
    """
    stmt = (
        update(department_table)
        .where(department_table.c.id == dept_id)
        .values(parent_id=parent_id)
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def set_department_leader(dept_id: int, leader_user_id: int | None) -> bool:
    """
    设部门负责人。

    为什么负责人重要：按 P2-14 的 D13，部门共享知识库的默认 writer 就是他
    （见 PLAN §11.3 D13）。改这里等于改「谁能往这个部门的库里传文档」。
    """
    stmt = (
        update(department_table)
        .where(department_table.c.id == dept_id)
        .values(leader_user_id=leader_user_id)
    )
    with session_scope() as session:
        return session.execute(stmt).rowcount > 0


def build_department_tree(rows: list[DepartmentRecord]) -> list[dict[str, Any]]:
    """
    把扁平部门列表组装成树（管理端 P2-13b 直接消费）。

    刻意**不在 SQL 里做递归**：部门表深度撑死三层，用 Python 组装比 CTE 更直观，
    也省掉 MySQL 版本差异。

    孤儿节点（parent_id 指向不存在的部门）会被提到根层级而不是静默丢弃 ——
    数据坏了要看得见。
    """
    nodes = {r.id: {**r.to_dict(), "children": []} for r in rows}
    roots: list[dict[str, Any]] = []
    for r in rows:
        node = nodes[r.id]
        parent = nodes.get(r.parent_id) if r.parent_id is not None else None
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)
    return roots


# --------------------------------------------------------------------------- #
# 写入 / 读取：职位
# --------------------------------------------------------------------------- #
def create_position(
    *,
    code: str,
    name: str,
    level: str | None = None,
    sequence: str = SEQUENCE_TECH,
) -> int:
    if not code or not code.strip():
        raise ValueError("职位 code 不能为空")
    if not name or not name.strip():
        raise ValueError("职位 name 不能为空")
    values = {
        "code": code.strip(),
        "name": name.strip(),
        "level": (level or None),
        "sequence": _check_sequence(sequence),
    }
    with session_scope() as session:
        result = session.execute(position_table.insert().values(**values))
        pos_id = int(result.inserted_primary_key[0])
    logger.info("职位已创建 | id=%s | code=%s | name=%s", pos_id, values["code"], values["name"])
    return pos_id


def get_position(pos_id: int) -> PositionRecord | None:
    """按 id 取职位。与 `get_department` 对称。

    P2-13d 补的：删除职位的审计要记「删的是哪个职位」，
    而行删掉之后就查不到了，所以必须在删之前拿快照。
    """
    stmt = select(position_table).where(position_table.c.id == int(pos_id))
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_position(row) if row else None


def get_position_by_code(code: str) -> PositionRecord | None:
    stmt = select(position_table).where(position_table.c.code == code)
    with session_scope() as session:
        row = session.execute(stmt).mappings().first()
    return _row_to_position(row) if row else None


def list_positions(*, sequence: str | None = None) -> list[PositionRecord]:
    stmt = select(position_table)
    if sequence is not None:
        stmt = stmt.where(position_table.c.sequence == _check_sequence(sequence))
    stmt = stmt.order_by(position_table.c.sequence, position_table.c.level, position_table.c.id)
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [_row_to_position(r) for r in rows]