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
from core import audit_repo
from core import kb_acl
from core import password_policy as policy
from core import quota_policy
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
# 审计（P2-13d）—— 13b/13c 刻意留出来的口子
# --------------------------------------------------------------------------- #
def _user_label(record: repo.UserRecord | None) -> str | None:
    """审计里的对象标签：`chen.jie（陈杰）`。冗余存一份，对象改名后日志仍读得懂。"""
    if record is None:
        return None
    name = record.display_name or ""
    return f"{record.username}（{name}）" if name and name != record.username else record.username


def _audit(
    actor: Actor,
    action: str,
    target_type: str,
    *,
    target_id: int | None = None,
    target_label: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """
    落一条审计。**审计失败不阻断业务**（下面说明为什么这么选），但一定打 error 日志。

    取舍：fail-closed（审计写不进去就拒绝这次操作）在安全上更严，可这里是内网管理端，
    而审计表与业务表在**同一个 MySQL**——它写不进的情形（磁盘满、表锁死）几乎同时
    意味着业务本身也写不进去。那时把操作一起拒掉，只是让「磁盘满了」从一个
    明确报错变成「管理端不能用」，而**已经写成功的那部分业务数据并不会回滚**
    （业务与审计不在一个事务里，见下），于是结果是「一半做了、一半没做，且都没记录」。

    真正能根治的是「业务写与审计写同事务」，那要求 repo 层接受外部 session
    （现在每个 repo 函数自己 `session_scope()`），属于结构性改造，本轮不做。
    所以这里的取舍是：**保可用 + 醒目留痕 + 测试断言正常路径 100% 落审计**。
    """
    try:
        audit_repo.record(
            actor_user_id=actor.id,
            actor_username=actor.username,
            actor_role=actor.role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_label=target_label,
            detail=detail,
            ip=actor.client_ip,
        )
    except Exception:  # noqa: BLE001 - 审计不能反过来打断业务，见上面说明
        logger.error(
            "审计写入失败（业务已完成，但这条动作没有留痕）| action=%s | actor=%s | target=%s:%s",
            action, actor.username, target_type, target_id,
            exc_info=True,
        )


def _audit_field_changes(before: Any, after: Any, fields: Any) -> dict[str, Any]:
    """
    只记**真正变了**的字段（`{字段: [旧, 新]}`）。

    为什么不是把提交上来的字段全记一遍：那会让「打开弹窗又取消」这类没改动的操作
    也产出一条「改了 6 个字段」的日志，几天后没人信这条日志。
    """
    changes: dict[str, Any] = {}
    for key in sorted(fields):
        old = getattr(before, key, None)
        new = getattr(after, key, None)
        if old != new:
            changes[key] = [old, new]
    return changes


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
    # ⚠️ detail 里**没有**临时密码，也不写哈希：审计表的读取权限通常比 user 表更宽，
    # 它一旦存了密码，就成了第二个明文落点。`audit_repo.record()` 也会主动拒绝
    # 这类内容（结构性防线，不是靠这一行的自觉）。
    _audit(
        actor, "user.create", "user",
        target_id=user_id, target_label=_user_label(record),
        detail={
            "username": username,
            "employee_no": employee_no,
            "role": role,
            "department_id": department_id,
            "position_id": position_id,
            "issued_temporary_password": True,
        },
    )
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
    before = _load_user(user_id)  # 先确认这个人存在，否则「改了个不存在的人」会静默成功
    try:
        repo.update_profile(user_id, updated_by=actor.id, **fields)
    except IntegrityError as e:
        _raise_readable_integrity_error(e, "邮箱与其他员工冲突")
    after = _load_user(user_id)
    logger.info("修改员工资料 | actor=%s | id=%s | fields=%s",
                actor.username, user_id, sorted(fields))
    changes = _audit_field_changes(before, after, fields)
    if changes:
        _audit(
            actor, "user.profile.update", "user",
            target_id=user_id, target_label=_user_label(after),
            # 只记真正变了的字段：没改的字段记进去会让日志噪声淹没重点
            detail={"changes": changes},
        )
    else:
        # 一个字段都没变（打开弹窗又点了保存）**不落审计**。
        # 理由：一条「改了 6 个字段」但 details 为空的记录，会让人以为真改过；
        # 而几天后没人再信这条日志时，审计就整体失效了 —— 这是它唯一的敌人。
        logger.debug("资料未实际变化，不落审计 | actor=%s | id=%s", actor.username, user_id)
    return after


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
    after = _load_user(user_id)
    _audit(
        actor, "user.status.change", "user",
        target_id=user_id, target_label=_user_label(record),
        # 状态变更是「他下次能不能登录」，所以顺手记下连带后果：旧 token 是否已失效
        detail={"from": record.status, "to": status,
                "token_version": after.token_version},
    )
    return after


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
    after = _load_user(user_id)
    _audit(
        actor, "user.role.change", "user",
        target_id=user_id, target_label=_user_label(record),
        detail={"from": record.role, "to": role,
                "token_version": after.token_version},
    )
    return after


# --------------------------------------------------------------------------- #
# 知识库写权限（P2-14f）
# --------------------------------------------------------------------------- #
def set_kb_role(actor: Actor, user_id: int, kb_role: str) -> repo.UserRecord:
    """
    下发 / 收回知识库写权限。**与 `set_role` 并列的独立维度**（D14）。

    --------------------------------------------------------------------------
    为什么权限在库里、判定在 `core/kb_acl.py`，而这里只做编排
    --------------------------------------------------------------------------
    与 `set_role` 同构：`role` 的合法值在 `repo.ROLES`、判权限在 `identity`，
    `kb_role` 的合法值在 `kb_acl.KB_ROLES`、判权限在 `kb_acl.can`。
    **服务层不重写一遍判据** —— 那是 13d 踩过的「两份清单各自漂」。

    --------------------------------------------------------------------------
    为什么 `require_admin` 而不是 `require_staff`
    --------------------------------------------------------------------------
    hr 能改员工资料，但**不能改权限** —— 与 D10「hr 不能重置密码」同一取舍。
    理由不是不信任 hr，而是**权限变更的后果不对称**：
    改资料填错了员工自己能看出来；把谁能删全公司知识库这件事写错了，
    当事人根本不会知道（14a 已把「读开放写收紧」做成了默认）。
    所以凡是「权限」两个字，一律admin 专属。

    --------------------------------------------------------------------------
    为什么不能改自己的 kb_role
    --------------------------------------------------------------------------
    与 `set_role` 同一条理由，但这里**后果更隐蔽**：
    把自己从 `superadmin` 降到 `none` 之后，你手上的 token 立刻失效
    （`token_version+1`），重新登录会发现「我连知识库都改不了了」，
    而恢复的方法是**再找一个人来改** —— 一个人把自己锁在外面时，
    那个「另一个人」未必存在。

    ⚠️ 但**升自己的权不做禁止**：把自己从 `none` 提到 `ops` 是无害的
    （他本来就能看，只是能改了），而禁止它会带来一个荒唐的后果 ——
    「唯一一个想维护知识库的人恰好是管理员，于是他必须找同事来授权给他」。
    所以规则是**只禁降级，不禁升级**，与 `set_role` 的判断方式一致
    （那里判的是 `role != record.role` 后再单独处理，这里判 `kb_role` 变小）。
    """
    _require(actor.role == repo.ROLE_ADMIN, "只有系统管理员能改知识库写权限", status=403)
    # ⚠️ **精确匹配**，不走 `kb_acl.check_kb_role`（它会 strip + lower）。
    #
    # 这与 `set_role` 的 `role in repo.ROLES` 是同一条规格：**写路径只认规范值。**
    # `check_kb_role` 的宽松是为**读**路径准备的（14a：手工 SQL 灌进库的
    # `'Ops '` 不规整就会让判据失配），而这里的输入来自管理端的**下拉框**，
    # 14f 已规定前端不接受自由输入 —— 宽松在这条路上没有任何正当来源。
    #
    # 宽松反而会咬人：管理员发来 `'SUPERADMIN'`、库里静默存成 `superadmin`，
    # 而审计的 detail 里 from/to 记的是**规范化之后**的值，于是
    # 「我明明选了 A，它存成了 B」这件事在任何一界都看不出来。
    # 宁可 400 让人重选一次，也不要静默改写别人的权限。
    #
    # 为什么不自己写 `kb_role in KB_ROLES`：那正是本函数要避免的
    # 「两份清单各自漂」。判据仍然来自 kb_acl，只是换一个更严的入口 ——
    # `is_canonical_kb_role` 就是 kb_acl 里的「严格版合法性」（14f 加的）。
    _require(kb_acl.is_canonical_kb_role(kb_role),
             f"知识库写权限档位只能是 {sorted(kb_acl.KB_ROLES)}")
    target = kb_acl.normalize(kb_role)
    record = _load_user(user_id)

    # 只禁降级，不禁升级（理由见文档字符串）
    if actor.is_self(user_id):
        was_allowed = kb_acl.capabilities(record.kb_role)
        now_allowed = kb_acl.capabilities(target)
        lost = [a for a in was_allowed if was_allowed[a] and not now_allowed[a]]
        if lost:
            names = {"upload": "上传", "delete": "删除", "reindex": "重建索引"}
            raise AdminError(
                f"不能收回自己的知识库权限（会失去：{'、'.join(names[a] for a in lost)}）"
                f" —— 降权会让你的登录立刻失效，而恢复要找别人改",
                status=403,
            )

    if target == record.kb_role:
        # 幂等：值没变就不动库。理由与 `set_role` 一致 ——
        # 不变的「变更」若也 +1 token_version，会把对方白踢下线一次。
        # 仍然返回 after（而不是 record），让调用方的响应形状与其他接口一致。
        return record

    repo.set_kb_role(user_id, target, updated_by=actor.id)
    logger.info("知识库写权限变更 | actor=%s | id=%s | %s → %s",
                actor.username, user_id, record.kb_role, target)
    after = _load_user(user_id)
    _audit(
        actor, "user.kb_role.change", "user",
        target_id=user_id, target_label=_user_label(record),
        # ⚠️ `from`/`to` 记的是**档位名**。审计与界面都能答出
        # 「谁在什么时候把谁从哪一档改到哪一档」，而 `kb_role` 不进
        # 「谁能看谁的什么」那类筛选（D13 已作废，无按档位检索的需求）。
        detail={"from": record.kb_role, "to": target,
                "from_label": kb_acl.KB_ROLE_LABELS.get(record.kb_role, record.kb_role),
                "to_label": kb_acl.KB_ROLE_LABELS.get(target, target),
                "token_version": after.token_version},
    )
    return after


# --------------------------------------------------------------------------- #
# Token 额度（P2-15b）
# --------------------------------------------------------------------------- #
def set_token_quota(actor: Actor, user_id: int, quota_monthly: Any) -> repo.UserRecord:
    """
    改某人的月度 token 额度。**只提醒不阻断**（用户 2026-10-07 拍板）。

    --------------------------------------------------------------------------
    为什么 `require_admin` 而不是 `require_staff`
    --------------------------------------------------------------------------
    与 `set_role` / `set_kb_role` 同一条理由（D10）：hr 能改员工资料，
    但**不能改额度**。理由不是不信任 hr，而是**后果不对称** ——
    资料填错了员工自己能看出来；额度填错了，
    员工看到的只是一条横幅（「你已用完本月额度」），
    而他不会想到那条横幅背后的数字是别人刚填错的。

    ⚠️ 但因为本项**不阻断**，这条限制的实际影响只是
    「别让 hr 顺手把某个部门的额度清零」，而不是一道锁。

    --------------------------------------------------------------------------
    🔴 为什么**允许改自己的**额度 —— 与 `set_kb_role` 相反
    --------------------------------------------------------------------------
    `set_kb_role` 禁止降自己的权，理由是「降权会让 token 立刻失效，
    而恢复要找别人改，一个人把自己锁在外面时那个『另一个人』未必存在」。

    而额度**不 bump token_version**（理由见 `user_repo.set_token_quota`），
    所以「把额度改小」不会让任何人掉线、不会锁死任何功能 ——
    最坏的结果只是他自己接下来会看到一条横幅。
    用一个不成立的危害去禁止一个无害的操作，只会逼出荒唐的后果
    （「唯一一个想给自己设额度的人恰好是管理员，于是他必须找同事」）。

    ⚠️ 所以这里**只加一条提示**：改自己的额度时在审计 detail 里标出来
    （`self=True`），让事后回看的人知道这一条是「自己给自己设的」。

    --------------------------------------------------------------------------
    为什么**值没变就不动库、也不落审计**
    --------------------------------------------------------------------------
    与 `set_kb_role` / `set_role` 同一取舍：不变的「变更」若也产出一条
    「改了额度」的审计，几天后没人信这条审计。
    管理端下拉框默认选中当前值 —— 用户点一下确认就产生一条假审计。
    """
    _require(actor.role == repo.ROLE_ADMIN, "只有系统管理员能改token 额度", status=403)

    # ⚠️ **不接受负数**。判据放在服务层而不是仓储层：
    #   仓储层抛 ValueError 会变成 500（没人读那条堆栈），
    #   而这里是明确的 400 + 可读原因。
    #   `to_quota_int` 宽松（字符串/浮点都能吃），但**负数在这里是硬错误** ——
    #   `quota_policy` 里 `effective_quota` 把 ≤0 都当「不限」，
    #   而 -1 在用户眼里是「倒欠我token」，两者不能混。
    # ⚠️ 这里**不拦布尔**：`TokenQuotaBody` 的 `mode="before"` 校验器已经拦了，
    #   而 Pydantic 会先把 `true` 转成 `1` —— 到了这里它已经是普通整数，
    #   写一个 `isinstance(raw, bool)` 分支只会是**永远不执行的死代码**，
    #   而死代码比没有代码更坏：它看起来在防什么。
    quota = quota_policy.to_quota_int(quota_monthly)
    if quota < 0:
        raise AdminError("额度不能为负数（0 表示不限）", status=400)

    record = _load_user(user_id)
    if quota == record.token_quota_monthly:
        # 幂等：值没变就不动库、不落审计（理由见文档字符串）
        return record

    repo.set_token_quota(user_id, quota, updated_by=actor.id)
    logger.info("Token 额度变更 | actor=%s | id=%s | %s → %s | self=%s",
                actor.username, user_id, record.token_quota_monthly, quota,
                actor.is_self(user_id))
    after = _load_user(user_id)
    _audit(
        actor, "user.token_quota.change", "user",
        target_id=user_id, target_label=_user_label(record),
        detail={
            "from": int(record.token_quota_monthly),
            "to": quota,
            # ⚠️ 记**生效额度**而不只是个人值：审计要能回答
            # 「当时他到底被限制在多少」，而 0 的含义是「用全局默认」，
            # 只记 0 的话，三个月后没人知道全局默认当时是多少。
            "effective_from": quota_policy.effective_quota(
                record.token_quota_monthly, settings.TOKEN_QUOTA_DEFAULT_MONTHLY),
            "effective_to": quota_policy.effective_quota(
                quota, settings.TOKEN_QUOTA_DEFAULT_MONTHLY),
            "default_quota": int(settings.TOKEN_QUOTA_DEFAULT_MONTHLY),
            "self": actor.is_self(user_id),
            # 刻意**不记** token_version：额度不 bump 它（见user_repo），
            # 记一个永不变的值进审计只会让人误以为「它会变」。
        },
    )
    return after


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
    after = _load_user(user_id)
    # 这一行是本模块最要紧的审计：**只记「重置过」，绝不记密码本身**。
    # 它必须能回答「谁重置了陈杰的密码」，而不能回答「陈杰的新密码是什么」。
    _audit(
        actor, "user.password.reset", "user",
        target_id=user_id, target_label=_user_label(record),
        detail={"must_change": True, "token_version": after.token_version,
                "temporary_password_issued": True},
    )
    return after, temporary


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
    after = _load_user(user_id)
    _audit(
        actor, "user.password.must_change", "user",
        target_id=user_id, target_label=_user_label(record),
        # 特意记 must_change 变了却 password_changed_at 没变 ——
        # 「开关不续期」是一条需要能被事后核对的性质。
        detail={"from": bool(record.must_change_password), "to": bool(must_change),
                "password_changed_at_untouched": True},
    )
    return after


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
    _audit(actor, "department.create", "department",
           target_id=dept_id, target_label=f"{code}（{record.name}）",
           detail={"code": code, "parent_id": parent_id,
                   "leader_user_id": leader_user_id})
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
    before = repo.get_department(dept_id)

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
    assert before is not None
    _audit(actor, "department.update", "department",
           target_id=dept_id,
           target_label=f"{record.code}（{record.name}）",
           detail={"changes": _audit_field_changes(before, record, fields)})
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
    snapshot = repo.get_department(dept_id)
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
    assert snapshot is not None
    # 标签取删除**之前**的快照：行没了之后再查就只能拿到 id，日志会变成一串数字
    _audit(actor, "department.delete", "department",
           target_id=dept_id,
           target_label=f"{snapshot.code}（{snapshot.name}）",
           detail={"code": snapshot.code})


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
    _audit(actor, "position.create", "position",
           target_id=pos_id, target_label=f"{code}（{record.name}）",
           detail={"code": code, "sequence": sequence, "level": level})
    return record


def update_position(actor: Actor, pos_id: int, **fields: Any) -> repo.PositionRecord:
    _require(actor.can_manage_org, "没有权限维护职位", status=403)
    allowed = {"code", "name", "level", "sequence"}
    unknown = set(fields) - allowed
    _require(not unknown, f"不允许改这些字段：{sorted(unknown)}")

    # ⚠️ 快照必须在 UPDATE **之前**取：审计要记的是「从什么变成什么」，
    # 改完再查就只剩新值，changes 永远是空的（一条看起来很详细、实际零信息的日志）。
    before = next((p for p in repo.list_positions() if p.id == pos_id), None)
    _require(before is not None, "职位不存在", status=404)

    from sqlalchemy import update as sa_update

    from core.db import session_scope
    from core.schema import position_table

    with session_scope() as session:
        try:
            session.execute(
                sa_update(position_table).where(position_table.c.id == pos_id).values(**fields)
            )
        except IntegrityError as e:
            _raise_readable_integrity_error(e, "职位编码已被使用")
    after = next((p for p in repo.list_positions() if p.id == pos_id), None)
    if after is None:  # pragma: no cover - 刚改完的行走不到这里
        raise AdminError("职位读取失败", status=500)
    logger.info("修改职位 | actor=%s | id=%s | fields=%s", actor.username, pos_id, sorted(fields))
    _audit(actor, "position.update", "position",
           target_id=pos_id, target_label=f"{after.code}（{after.name}）",
           detail={"changes": _audit_field_changes(before, after, fields)})
    return after


def delete_position(actor: Actor, pos_id: int) -> None:
    """
    删职位。有人挂着这个职位时拒绝 —— 与部门同理（引用会悬空）。

    ⚠️ 这里的「有人挂」按**全部在职状态**数（含离职）：离职员工的记录也要
    能正确显示他当年的职位，所以引用是真的存在。
    """
    _require(actor.can_manage_org, "没有权限维护职位", status=403)

    # 同上：删除前的快照（行没了就查不到了）
    snapshot = repo.get_position(pos_id)
    _require(snapshot is not None, "职位不存在", status=404)

    from sqlalchemy import delete as sa_delete
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    from core.db import session_scope
    from core.schema import position_table, user_table

    with session_scope() as session:
        used = session.execute(
            sa_select(sa_func.count()).select_from(user_table)
            .where(user_table.c.position_id == pos_id)
        ).scalar() or 0
        if used:
            raise AdminError(f"还有 {used} 名员工挂着这个职位，请先调整他们的职位")
        session.execute(sa_delete(position_table).where(position_table.c.id == pos_id))
    logger.info("删除职位 | actor=%s | id=%s", actor.username, pos_id)
    assert snapshot is not None
    _audit(actor, "position.delete", "position",
           target_id=pos_id,
           target_label=f"{snapshot.code}（{snapshot.name}）",
           detail={"code": snapshot.code})


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
