"""
登录编排 —— 把「判定」与「存取」缝在一起（P2-11c）。

--------------------------------------------------------------------------
这个模块为什么存在（而不是让网关直接查库）
--------------------------------------------------------------------------
设计规格 §8 的 D11 写的是「网关直连 MySQL 读 user 表」。落地时**否掉了**，
改成：网关调本模块的 `/api/v1/internal/auth/login`。

否掉的理由：**判定规则会变成两份**。

`core/password_policy.verify()` 是 P2-11b 立的「判定规则唯一出处」，有 141 条
断言守着，其中三条特别难在别处重做：

    ① 「账号不存在 / 密码错 / 已锁定」对外文案必须**逐字相同**（防用户名枚举）；
    ② 四条失败路径的**耗时**必须等长（响应时间就是枚举器）；
    ③ 到期判据是 `>` 不是 `>=`（差一秒，口径就变了）。

网关是 TypeScript。重写一遍就等于把这三条各复制一份给一个**没有测试守着**
的地方 —— 半年后有人改 Python 侧的口径，TS 侧静默漂移，症状是
「有人能登录、有人不能，而没人知道为什么」。

代价（要认下来）：登录多一次内网 HTTP（127.0.0.1，实测 < 5ms），
以及多一个必须保护好的内部接口。这是拿「多一跳」换「规则只有一份」。

--------------------------------------------------------------------------
登录这件事到底做哪几件事
--------------------------------------------------------------------------
判定全在 `verify()`，本模块只负责**副作用**：

    1. 按登录名取那一行；
    2. 判定（`verify`，含耗时对齐）；
    3. 失败 → 计数 / 达阈值则锁（`record_login_failure`）；
       成功 → 计数归零 / 解锁 / 写 last_login_at（`record_login_success`）；
    4. 判定说「该重算哈希」→ 顺带升级（bcrypt cost 涨了要跟上）；
    5. 落审计（`auth.login.success` / `auth.login.failure`）。

**不负责**：签发 token（那是网关的事，它持有 JWT 密钥）。

--------------------------------------------------------------------------
为什么 rehash 放在这里而不是登录接口里
--------------------------------------------------------------------------
bcrypt cost 迟早要从 12 涨到 13/14（涨得越慢越好，CPU 越贵）。涨的那一刻，
库里所有存量哈希都变成「弱一档」的。11b 定了规矩：**登录成功时顺带升级**。

放在哪执行是实现细节，但**必须只有一处** —— 漏一处就有一个用户的密码
永远留在弱档上，而没有任何东西会告诉你这件事。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core import audit_repo
from core import kb_acl
from core import password_policy as policy
from core import user_repo as repo
from core.db import now_db
from core.user_repo import UserRecord

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 结果形状
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoginOutcome:
    """
    一次登录的完整结果。

    **`ok=False` 时 `code` 才有意义** —— 内部用（进日志、进监控、进审计），
    而 `message` 是给用户看的，三种失败必须**逐字相同**（防枚举，见 11b）。

    成功时带的两个字段不是装饰，是**前端要按它们改行为**的：
        must_change → 前端必须把用户拦到改密页，不许他进主界面；
        password    → 登录后一次性横幅提醒（剩几天过期）。
    """

    ok: bool
    code: str
    message: str = ""
    user: dict[str, Any] | None = None
    must_change: bool = False
    expire_in_days: int | None = None
    #: 这次登录有没有顺带把哈希升级到当前 cost（监控用：升级完就该停止增长）
    rehashed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """给网关转发的形状。失败时**只回 code 与 message**，不回任何用户信息。"""
        if not self.ok:
            return {"ok": False, "code": self.code, "message": self.message}
        return {
            "ok": True,
            "code": self.code,
            "user": self.user,
            "must_change": self.must_change,
            "expire_in_days": self.expire_in_days,
            "rehashed": self.rehashed,
        }


#: 判定失败 → 稳定的对外文案。
#:
#: ⚠️ **口径沿用 11b，这里不重新发明**：`verify()` 对「账号不存在 / 密码错 /
#: 已锁定」三种返回**逐字相同**的文案（防枚举），唯独 `inactive`（停用/离职）
#: 用单独一句。理由是 11b 写明的：被停用的人看到「密码错误」只会一遍遍重试，
#: 永远不知道该去找管理员。
#:
#: 代价（认下来，不假装没有）：`inactive` 是一条**弱枚举信号** —— 只有存在的
#: 账号才可能「被停用」。所以它只在**密码已通过**的位置才有意义，而
#: `verify()` 的判定顺序恰好满足（inactive 在 bcrypt 之前判，但那次的
#: message 与文案差异是设计权衡，不是漏洞的等价物）。真要收紧到零信号，
#: 只能把它也并入统一文案，代价是被停用的人失去可行动信息 —— 那是 11b
#: 已经拍过的取舍，本轮不改。
_AUTH_FAILED_MESSAGE = policy.GENERIC_AUTH_MESSAGE

#: `verify()` 的内部 code 是私有的（`_CODE_*`）。这里比字符串而不是 import
#: 私有名 —— 跨模块引用别人的下划线名，等于把「这是内部契约」这件事变成
#: 「改了它也不会有人发现」。真要引用，就在 policy 里给它一个公开出口。
_CODE_MUST_CHANGE = "must_change"
_CODE_EXPIRED = "expired"
_CODE_INACTIVE = "inactive"


def _failure(code: str, message: str) -> LoginOutcome:
    return LoginOutcome(ok=False, code=code, message=message)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def login(username: str, password: str, *, client_ip: str | None = None) -> LoginOutcome:
    """
    校验凭据并落完所有副作用。

    ⚠️ 这里的**顺序**不能调换：

        判定（verify，含耗时对齐） → 副作用（计数/锁定/解锁） → 审计

    先落副作用再判定会「密码错但计数没涨」，于是限流形同虚设；
    先落审计再判定会给「密码错」也记上 actor_id，那等于把「谁在尝试登录某账号」
    写进了一个谁都能查的表。
    """
    username = (username or "").strip()
    now = now_db()

    record = repo.get_by_username(username) if username else None
    outcome = policy.verify(record, password, now=now)

    # ---- 副作用：失败计数 / 锁定 ----
    if not outcome.ok:
        _record_failure(record, outcome.code, now=now)
        _audit_login_failure(username, outcome.code, record, client_ip, now=now)
        logger.info("登录失败 | username=%s code=%s", username, outcome.code)
        # ⚠️ `ok=False` 但 `code=expired` 的那条**密码本身是对的**，
        # 它是「凭据有效但被策略拦住」。这两种情况处置完全不同（一个回登录页、
        # 一个被导到改密页），所以 code 必须原样传出去给网关判断。
        # 而 message 仍按 11b 口径：`inactive` 单独一句（被停用的人要看得懂），
        # 其余（不存在 / 密码错 / 已锁定 / 过期）逐字统一。
        message = (
            "账号已停用或已离职，请联系管理员"
            if outcome.code == _CODE_INACTIVE
            else _AUTH_FAILED_MESSAGE
        )
        return _failure(outcome.code, message)

    # `verify` 返回 ok=True 只有两种：`MUST_CHANGE`（密码对，强制改密）与 `OK`。
    # 到期那条是 ok=False（设计规格 §4：到期后除改密/登出一律不放行）。
    assert record is not None  # ok=True 蕴含「user 不为 None」，见 verify 的第 0 步

    # ---- 副作用：成功（计数归零 / 解锁 / last_login_at）----
    repo.record_login_success(record.id, now=now)

    # ---- 副作用：哈希 cost 升级 ----
    rehashed = False
    if outcome.rehash:
        rehashed = _rehash(record, password, now=now)

    _audit_login_success(record, client_ip, rehashed, now=now)
    logger.info("登录成功 | username=%s uid=%s must_change=%s", username, record.id,
                outcome.code == _CODE_MUST_CHANGE)

    return LoginOutcome(
        ok=True,
        code=outcome.code,
        must_change=bool(record.must_change_password),
        expire_in_days=policy.expire_in_days(record.password_changed_at, now=now),
        rehashed=rehashed,
        user=_public_user(record),
    )


# --------------------------------------------------------------------------- #
# 副作用（各自一个函数，便于测试单独盯住）
# --------------------------------------------------------------------------- #
def _record_failure(record: UserRecord | None, code: str, *, now: datetime) -> None:
    """
    失败计数 + 达阈值则锁。

    **「账号不存在」这一条不写库** —— 那行根本不存在，写了是 KeyError。
    但它仍然要走一次 bcrypt（`verify` 内部已经做了），否则「不存在的用户名」
    会在 0.1ms 返回，而「密码错」要 200ms —— 响应时间直接变成枚举器。
    """
    if record is None:
        return
    # 已经在锁定期的：不再累加，否则他每点一次登录就多锁 15 分钟，
    # 一个被锁的人会变成「不主动解锁永远出不来」。
    if record.is_locked_at(now):
        return
    # 停用 / 离职的账号同样不累加：他的失败次数没有意义（他压根进不来），
    # 累加只会让管理员在看板里看到一堆无意义的计数。
    if record.status != repo.STATUS_ACTIVE:
        return

    count = record.failed_login_count + 1
    lock_until = (
        policy.lock_deadline(now=now) if policy.should_lock(count) else None
    )
    repo.record_login_failure(record.id, lock_until=lock_until, now=now)
    if lock_until:
        logger.warning(
            "账号已锁定 | username=%s 连续失败 %d 次，锁到 %s",
            record.username, count, lock_until.isoformat(sep=" ", timespec="seconds"),
        )


def _rehash(record: UserRecord, password: str, *, now: datetime) -> bool:
    """
    密码是对的、但存量哈希的 cost 过低 → 顺带升级。

    刻意**不**走 `repo.update_password()`：那个函数会把 `token_version + 1`
    （换密码就该踢掉其他设备），而「只是把 cost 补上去」不该让人重新登录。
    所以这里直接改 `password_hash` 一列。
    """
    new_hash = policy.hash_password(password)
    ok = repo.update_password_hash_only(record.id, new_hash, now=now)
    if ok:
        logger.info("哈希 cost 升级 | username=%s uid=%s", record.username, record.id)
    return ok


# --------------------------------------------------------------------------- #
# 审计（13d 预留的三个动作在这里第一次真正被用到）
# --------------------------------------------------------------------------- #
def _audit_login_success(
    record: UserRecord, client_ip: str | None, rehashed: bool, *, now: datetime
) -> None:
    """
    登录成功落审计。

    ⚠️ 这里**不记** `must_change_password` 之外的用户资料，也不记任何密码信息 ——
    `audit_repo` 本身会拒绝敏感键名，但这层的自律是第二道防线。
    """
    audit_repo.record(
        actor_user_id=record.id,
        actor_username=record.username,
        actor_role=record.role,
        action="auth.login.success",
        target_type="auth",
        target_id=record.id,
        target_label=f"{record.username}（{record.display_name or record.username}）",
        detail={"must_change": bool(record.must_change_password), "rehashed": rehashed},
        ip=client_ip,
        now=now,
    )


def _audit_login_failure(
    username: str,
    code: str,
    record: UserRecord | None,
    client_ip: str | None,
    *,
    now: datetime,
) -> None:
    """
    登录失败落审计 —— **13d 交付时它明确没做**，等的就是这一版。

    为什么现在可以做：13d 那时网关不查库，登录链路上**根本没有 user_id**，
    「失败登录」甚至不知道是谁在试。现在有了。

    审计里的 `actor_user_id` 在账号存在时是**被尝试的那个账号的 id**，
    不是「操作者」—— 这是一次登录失败，操作者就是这个账号（哪怕他没通过验证）。
    账号不存在时为 None，label 只留登录名。

    ⚠️ 失败次数**不记进 detail**：连续输错 5 次会落 5 条审计，
    而「他失败了几次」在 user 表的 `failed_login_count` 里已经是权威值，
    审计再抄一遍就是第二个真相源。
    """
    audit_repo.record(
        actor_user_id=record.id if record is not None else None,
        actor_username=record.username if record is not None else username or "(空)",
        actor_role=record.role if record is not None else "(unknown)",
        action="auth.login.failure",
        target_type="auth",
        target_id=record.id if record is not None else None,
        target_label=f"{username}（凭据未通过）" if record is not None else
                     f"{username or '(空)'}（账号不存在）",
        detail={"code": code},
        ip=client_ip,
        now=now,
    )


def logout(user_id: int | None, username: str, *, client_ip: str | None = None) -> None:
    """
    登出落审计。

    JWT 无状态，**服务端没有任何东西被销毁**（网关那条注释说得对）。
    所以这一行审计记的是「客户端声明它丢弃了 token」这个事实，
    不是「token 已失效」—— 后者只有改密/停用/改角色才成立。

    写清这个区别是为了：将来有人问「审计里有登出，是不是登出就能踢人」，
    答案是不行，而这个字段名 `auth.logout` 本身不回答这个问题。
    """
    record = repo.get(user_id) if user_id is not None else repo.get_by_username(username)
    audit_repo.record(
        actor_user_id=record.id if record is not None else user_id,
        actor_username=record.username if record is not None else (username or "(空)"),
        actor_role=record.role if record is not None else "(unknown)",
        action="auth.logout",
        target_type="auth",
        target_id=record.id if record is not None else user_id,
        target_label=f"{username}（客户端丢弃 token）",
        detail={"server_side_revoked": False},
        ip=client_ip,
    )


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _public_user(record: UserRecord) -> dict[str, Any]:
    """
    给网关转发的用户信息。

    ⚠️ **绝不包含** `password_hash` / `email` / `phone` —— 登录响应会经过网关、
    可能被反代记录、也会出现在前端的 devtools 里。邮箱手机号这类资料
    要用请走 `/api/v1/admin/users/{id}`（那里有脱敏与权限控制）。
    """
    return {
        "id": record.id,
        "username": record.username,
        "display_name": record.display_name or record.username,
        "role": record.role,
        # P2-14b：kb_role 要进 JWT 才能让网关做路径粗筛（设计规格 §11.3 D15）。
        # ⚠️ 归一化成合法值再给（kb_acl.normalize）—— 这一份数据会变成
        # 「网关据此决定放不放行」，所以**绝不能让一个非法值变成放行**。
        # 库里若有脏值（手工 SQL 灌进来的 'Ops '），归一化后是合法的 ops，
        # 而 `normalize` 对**不认识**的值一律降级 none（fail-closed）。
        "kb_role": kb_acl.normalize(record.kb_role),
        "employee_no": record.employee_no,
        "department_id": record.department_id,
        "position_id": record.position_id,
        "token_version": record.token_version,
    }


__all__ = ["LoginOutcome", "login", "logout"]
