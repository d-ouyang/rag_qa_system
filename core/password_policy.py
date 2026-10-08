"""
密码策略 —— **判定规则的唯一出处**（P2-11b）。

--------------------------------------------------------------------------
这个模块为什么「几乎」不 import 数据库模块
--------------------------------------------------------------------------
「新密码是否命中历史」「这个密码到期没有」「连错几次要锁」——这些都是
判定。把它们放在仓储层，就只能连着 MySQL 一起测，单测会慢、会因为容器没起
而假绿或假红。

所以这里的分层是：

    core/password_policy.py   纯判定，不做任何查询（只依赖 settings、bcrypt）
    core/user_repo.py         纯存取（存哈希、读历史、裁历史）

需要历史时，调用方（未来的登录服务 / P2-11c）把哈希列表取出来传进来：

    recent = user_repo.list_recent_password_hashes(uid, settings.PASSWORD_HISTORY_KEEP)
    if policy.hits_history(new_password, recent):
        return "不能复用最近用过的密码"

这样「能不能判重」这件事，不起数据库也能测。

唯一的例外是 `STATUS_ACTIVE` —— 从 user_repo 导入，为的是「在职状态」这个
枚举只有一处出处。它不构成循环：user_repo 不 import 本模块，且它的 Engine
是懒创建，导入它不会去连库。

--------------------------------------------------------------------------
三个容易写错、这里刻意做对的地方
--------------------------------------------------------------------------
1. **失败文案要一模一样**。`not_found` / `wrong_password` / `locked` 三种
   内部 code 不同（要进日志与监控），但**对外 message 必须是同一句**。否则
   「这个账号存在，只是密码错了」就是免费的用户名枚举器。
2. **耗时也要对齐**。bcrypt 是故意慢的，慢到爆破不划算；但也慢到**能当
   侧信道**：如果「账号不存在」直接返回（0.1ms）、「密码错」要跑 bcrypt
   （300ms），攻击者用响应时间就能数出哪些账号存在。所以每条失败路径都会
   跑一次**真实**的 bcrypt 比对（`_burn`），只是比的对象不同。
3. **到期判据是 `>` 不是 `>=`**。`now - changed_at > 天数` —— 「正好到期
   那一刻」仍算有效，差一秒才算过期。边界写进测试了，谁也别随手改口径。

--------------------------------------------------------------------------
bcrypt 的 72 字节上限
--------------------------------------------------------------------------
bcrypt 只用密码的**前 72 字节**，更长的部分被静默忽略 —— 这意味着
「abc...(72字节)A」和「abc...(72字节)B」在库里是同一个哈希。
`validate_strength` 因此把**UTF-8 编码后的字节数**列为一条独立校验，
而不是只数字符长度：中文密码 30 个字 = 90 字节，已经超了。
"""
from __future__ import annotations

import math
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import bcrypt

from config.settings import settings
# 只为了拿到 status 的取值集合（「在职状态」这个枚举的唯一出处）。
# 引入user_repo 不会造成循环：user_repo 不 import 本模块（它只提供存取原语），
# 而 user_repo 顶层也不建连接（core.db 的 Engine 是懒创建）。
from core.user_repo import STATUS_ACTIVE

# --------------------------------------------------------------------------- #
# 对外文案
# --------------------------------------------------------------------------- #
#: 「账号不存在」「密码错」「已锁定」三种情况共用的**唯一**对外文案。
GENERIC_AUTH_MESSAGE = "账号或密码错误"

#: bcrypt 最多只取密码前 72 字节（超出会被静默忽略 → 两个不同密码同一哈希）
BCRYPT_MAX_BYTES = 72

_CODE_OK = "ok"
_CODE_NOT_FOUND = "not_found"
_CODE_WRONG_PASSWORD = "wrong_password"
_CODE_LOCKED = "locked"
_CODE_INACTIVE = "inactive"
_CODE_MUST_CHANGE = "must_change"
_CODE_EXPIRED = "expired"

# P2-18：公开别名 —— 自助改密路径要判断「过期用户放行改密」，
# 跨模块调 `_CODE_*` 私有名是破坏约定（与 to_quota_int 公开的同一理由）。
CODE_OK = _CODE_OK
CODE_MUST_CHANGE = _CODE_MUST_CHANGE
CODE_EXPIRED = _CODE_EXPIRED

# 字符类别（复杂度判定用）。不设「必须大写+数字+符号」那种组合规则 ——
# 那类规则逼出的是 `Abc123!@#` 这种模式化口令，见设计规格 §4。
_CLASSES = (string.ascii_lowercase, string.ascii_uppercase, string.digits,
            "!@#$%^&*()-_=+[]{};:,.?/")

# 临时密码用的字符集：**去掉易混字符**（0/O、1/l/I）。人工抄写一次抄错就登不进去，
# 而这类错误会被当成「密码错误」，排查方向直接跑偏。
#
# ⚠️ 这三个「候选池」必须各自过滤，**不能只在最后对成品做一次替换** ——
# 第一版只在「强制的那一位数字」上做了过滤，结果小写位照样能抽到 `l`、
# 大写位能抽到 `I`/`O`（每四个临时密码里就有约一个含易混字符）。
# 正确做法是：**所有**抽取都从已过滤的池子里取。
_TEMP_LOWER = [c for c in string.ascii_lowercase if c not in "l"]
_TEMP_UPPER = [c for c in string.ascii_uppercase if c not in "IO"]
_TEMP_DIGIT = [c for c in string.digits if c not in "01"]
_TEMP_SYMBOLS = "!@#$%&*"
_TEMP_ALPHABET = (
    "".join(_TEMP_LOWER) + "".join(_TEMP_UPPER) + "".join(_TEMP_DIGIT)
)
_TEMP_CONFUABLES = set("0O1lI")


class _PasswordHolder(Protocol):
    """`verify()` 只需要用户记录上的这几个字段。用 Protocol 而不是 import
    `UserRecord`，是为了让这个模块在结构上就不依赖数据库层。"""

    password_hash: str
    status: str
    must_change_password: bool
    password_changed_at: datetime | None
    failed_login_count: int
    locked_until: datetime | None

    def is_locked_at(self, now: datetime | None = None) -> bool: ...


# --------------------------------------------------------------------------- #
# 计时对齐
# --------------------------------------------------------------------------- #
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """
    一个**真实** cost 的假哈希，只用于把「账号不存在」这条路径的耗时补齐。

    为什么不在模块导入时就生成：那会让每次启动多花 300ms，而大多数请求
    根本不会走到这条路径。惰性生成 + 进程内缓存即可。
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = bcrypt.hashpw(
            b"timing-equalizer-not-a-real-password",
            bcrypt.gensalt(rounds=settings.BCRYPT_COST),
        ).decode("utf-8")
    return _DUMMY_HASH


def _burn(password: str, against: str | None) -> None:
    """跑一次真实 bcrypt 比对把耗时补上。比错了也没关系，本来就是白跑。"""
    target = against or _dummy_hash()
    try:
        bcrypt.checkpw(password.encode("utf-8"), target.encode("utf-8"))
    except (ValueError, TypeError):
        # 哈希串本身不合法（比如库里被写坏了）——这里不该炸，只当没对上
        pass


# --------------------------------------------------------------------------- #
# 强度校验
# --------------------------------------------------------------------------- #
def validate_strength(
    password: str,
    *,
    username: str = "",
    employee_no: str = "",
    min_length: int | None = None,
) -> list[str]:
    """
    校验密码强度，**返回违规项列表**（空列表 = 通过）。

    刻意返回列表而不是抛异常/返回 bool：管理端要逐条展示「哪里不达标」，
    只给一个 `False` 的话前端只能自己复刻规则，两处一漂移就出错提示不一致。

    规则（与设计规格 §4 一致）：

    1. 长度 ≥ `PASSWORD_MIN_LENGTH`（默认 10）
    2. **不能是纯数字**
    3. 至少覆盖 2 类字符（小写 / 大写 / 数字 / 符号）
    4. 不能与登录名或工号相同（忽略大小写）
    5. UTF-8 编码后**不超过 72 字节**（bcrypt 的硬上限）
    """
    low = settings.PASSWORD_MIN_LENGTH if min_length is None else max(1, int(min_length))
    violations: list[str] = []

    if not password:
        return ["密码不能为空"]

    if len(password) < low:
        violations.append(f"长度不足 {low} 位（当前 {len(password)} 位）")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        violations.append(f"过长：UTF-8 编码后超过 {BCRYPT_MAX_BYTES} 字节（bcrypt 只取前 72 字节）")
    if password.isdigit():
        violations.append("不能是纯数字")

    covered = sum(1 for group in _CLASSES if any(ch in group for ch in password))
    if covered < 2:
        violations.append("至少包含小写字母、大写字母、数字、符号中的两类")

    lowered = password.lower()
    for label, value in (("登录名", username), ("工号", employee_no)):
        if value and lowered == value.strip().lower():
            violations.append(f"不能与{label}相同")

    return violations


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, cost: int | None = None) -> str:
    """算 bcrypt 哈希。`cost` 默认取 `settings.BCRYPT_COST`（12）。"""
    rounds = settings.BCRYPT_COST if cost is None else int(cost)
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(f"密码过长：UTF-8 编码后超过 {BCRYPT_MAX_BYTES} 字节")
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)
    ).decode("utf-8")


def hash_cost(stored_hash: str) -> int | None:
    """
    读出存量哈希的 cost；哈希串不合法时返回 None（不抛）。

    ⚠️ **`bcrypt` 没有 `bcrypt.cost()` 这个 API**（4.x/5.x 都没有）——
    cost 只存在于哈希串的第 3 段，必须自己解析：
        `$2b$12$<22 salt><31 hash>`  →  split('$') = ['', '2b', '12', '...']
    写库时不小心把非 bcrypt 的串存进来（早期手工插数据、迁移回退）就会走到
    「解析不出来」这条分支，此时返回 None 而不是抛异常。
    """
    try:
        parts = stored_hash.split("$")
    except (AttributeError, TypeError):
        return None
    # 结构必须是 ['', <version>, <cost>, <body>]，且 version/cost 非空
    if len(parts) < 4 or not parts[1] or not parts[2].isdigit():
        return None
    return int(parts[2])


def needs_rehash(stored_hash: str, *, target_cost: int | None = None) -> bool:
    """
    存量哈希的 cost 是否低于当前目标 → 该在**登录成功时顺带重算升级**。

    为什么不强制这批人改密：一次成本升级不应该把所有人赶去改密码。
    判据只有一条「存量 cost < 目标 cost」，不需要第二个常量（曾想过加
    `BCRYPT_LEGACY_COST`，最后发现是死配置——它能表达的这条规则已经被本
    函数覆盖了，于是删掉，避免「配了但不生效」）。

    哈希串不合法 / 读不出 cost 时返回 False：不要在登录路径上因为一个坏数据
    就把用户踢去改密，那会掩盖真正的问题（库里存的不是 bcrypt 串）。
    """
    target = settings.BCRYPT_COST if target_cost is None else int(target_cost)
    existing = hash_cost(stored_hash)
    return existing is not None and existing < target


# --------------------------------------------------------------------------- #
# 到期（pull 模型）
# --------------------------------------------------------------------------- #
def is_expired(
    password_changed_at: datetime | None,
    *,
    now: datetime | None = None,
    expire_days: int | None = None,
) -> bool:
    """
    是否已过期。判据：`now - password_changed_at > 天数`（**严格大于**）。

    `password_changed_at is None` → **按已过期处理**（fail-closed）。
    理由：无法证明它没过期，就不能放行；而且这种情况下一并强制改密就能纠正。
    这与 §5.1「头必须由网关注入，否则 401」是同一个思路。
    """
    days = settings.PASSWORD_EXPIRE_DAYS if expire_days is None else int(expire_days)
    if days <= 0:
        return False  # 显式关闭到期策略
    if password_changed_at is None:
        return True
    return (now or datetime.now()) - password_changed_at > timedelta(days=days)


def expire_in_days(
    password_changed_at: datetime | None,
    *,
    now: datetime | None = None,
    expire_days: int | None = None,
) -> int | None:
    """
    剩余天数。**负数 = 已过期**，正数 = 还剩几天。

    向上取整：`还剩 0.5 天` 对用户来说是「今天到期」，说「还剩 0 天」比说
    「还剩 1 天」更贴近直觉（前端横幅写「今天到期」）。

    `password_changed_at is None` → None（未知，调用方按过期处理）。
    """
    days = settings.PASSWORD_EXPIRE_DAYS if expire_days is None else int(expire_days)
    if password_changed_at is None:
        return None
    deadline = password_changed_at + timedelta(days=days)
    remain = (deadline - (now or datetime.now())).total_seconds()
    return math.ceil(remain / 86400)


def should_warn_expire(
    password_changed_at: datetime | None,
    *,
    now: datetime | None = None,
    warn_days: int | None = None,
    expire_days: int | None = None,
) -> bool:
    """是否该弹「密码即将到期」横幅：未过期，且剩余 ≤ `PASSWORD_EXPIRE_WARN_DAYS`。"""
    warn = settings.PASSWORD_EXPIRE_WARN_DAYS if warn_days is None else int(warn_days)
    remain = expire_in_days(password_changed_at, now=now, expire_days=expire_days)
    return remain is not None and 0 <= remain <= warn


# --------------------------------------------------------------------------- #
# 历史不可复用
# --------------------------------------------------------------------------- #
def hits_history(candidate: str, recent_hashes: list[str], *, limit: int | None = None) -> bool:
    """
    候选密码是否命中最近的历史哈希。

    逐条 `bcrypt.checkpw`：**本机实测 cost 12 单次约 165ms**（M 系列；换机器
    数字会变，此处只说明量级），保留 5 条约 0.85s。这个耗时只发生在「改密」这一次
    动作上，不在登录路径上 —— 登录只比 1 条。
    （否掉的更快方案是给密码算 HMAC 指纹做 O(1) 判重，理由见迁移 0004 文件头：
    多一份要保管的密钥，换来的只是改密时少 0.7 秒。）

    `limit` 用于「只查最近 N 条」，避免调用方传进来一大串。
    """
    keep = settings.PASSWORD_HISTORY_KEEP if limit is None else max(0, int(limit))
    if keep == 0:
        return False
    for stored in recent_hashes[:keep]:
        if not stored:
            continue
        try:
            if bcrypt.checkpw(candidate.encode("utf-8"), stored.encode("utf-8")):
                return True
        except (ValueError, TypeError):
            continue  # 坏数据跳过，不让它把整个改密流程打断
    return False


# --------------------------------------------------------------------------- #
# 失败锁定
# --------------------------------------------------------------------------- #
def should_lock(failed_count: int, *, max_failures: int | None = None) -> bool:
    """连续失败次数是否已达锁定阈值。"""
    limit = settings.PASSWORD_MAX_FAILURES if max_failures is None else max(1, int(max_failures))
    return int(failed_count) >= limit


def lock_deadline(*, now: datetime | None = None, lock_minutes: int | None = None) -> datetime:
    """锁定到什么时候。调用方把这个值写进 `user.locked_until`。"""
    minutes = settings.PASSWORD_LOCK_MINUTES if lock_minutes is None else max(1, int(lock_minutes))
    return (now or datetime.now()) + timedelta(minutes=minutes)


# --------------------------------------------------------------------------- #
# 临时密码
# --------------------------------------------------------------------------- #
def generate_temporary_password(*, length: int = 12) -> str:
    """
    生成一次性临时密码（管理员重置用）。

    三个要求：
    - 强度够 —— 强制四类字符各一，剩下的随机填充并洗牌，所以生成结果**必然**
      通过 `validate_strength`（长度 ≥ 10 时）；
    - 用 `secrets` 而不是 `random` —— 后者不是密码学安全的；
    - 去掉易混字符（0/O、1/l/I）—— 人工抄一次抄错就登不进去，而这种错误会被
      当成「密码错误」，排查方向直接跑偏。

    **只在创建/重置的响应里显示一次**（不落日志、不再返回）。这条由调用方
    （P2-13b 管理端）负责，本模块只负责生成。
    """
    n = max(10, int(length))
    # 四类各一，全部从**已过滤**的池子里取（见上方 _TEMP_* 的警告）
    core = [
        secrets.choice(_TEMP_LOWER),
        secrets.choice(_TEMP_UPPER),
        secrets.choice(_TEMP_DIGIT),
        secrets.choice(_TEMP_SYMBOLS),
    ]
    pool = _TEMP_ALPHABET + _TEMP_SYMBOLS
    rest = [secrets.choice(pool) for _ in range(n - len(core))]
    chars = core + rest
    # 洗牌：否则「小写在前、大写在后、符号在最后」，一眼就是机器生成的
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    out = "".join(chars)
    # 兜底自检：这一段是「断言写进生产代码」，代价是一次 O(n) 的集合运算，
    # 收益是**任何将来的改动都不可能把易混字符漏回来**（第一版就是这么漏的，
    # 而且它逃过了 15 遍里的 13 遍）。与其指望每次改动都记得过滤，
    # 不如让越界直接变成不可能。
    if set(out) & _TEMP_CONFUABLES:  # pragma: no cover - 理论上不可达
        raise AssertionError(f"临时密码出现了易混字符（内部错误）：{out!r}")
    return out


# --------------------------------------------------------------------------- #
# 登录校验：统一出口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerifyOutcome:
    """
    一次登录校验的结果。

    `code` 给日志与监控看（能区分「密码错」和「已锁定」）；
    `message` 给用户看 —— **「账号不存在 / 密码错 / 已锁定」三者的 message
    必须是同一句**，否则就是个用户名枚举器。
    """

    ok: bool
    code: str
    message: str = ""
    rehash: bool = False
    detail: dict = field(default_factory=dict)


def verify(user: _PasswordHolder | None, candidate: str, *, now: datetime | None = None) -> VerifyOutcome:
    """
    登录密码校验 —— **本模块唯一的对外出口**。

    判定顺序（每一步都会 `_burn` 一次，把四条路径的耗时拉到同一量级）：

        0. 账号不存在        → 文案同「密码错」
        1. 非在职（停用/离职）→ 单独文案（这条需要用户看得懂，否则会去客服而不是改密）
        2. 处于锁定期         → 文案同「密码错」
        3. bcrypt 比对        → 文案同「密码错」
        4. 强制改密 → 登录成功但带 must_change 标记（不是失败）
        5. 密码过期       → 失败，提示改密（到期后只放行「改密」与「登出」）

    `rehash=True` 表示「密码是对的，但存量哈希的 cost 过低，请调用方顺带重算」。
    本模块**不写库**（它是纯判定），落库由调用方执行 `user_repo.update_password`。
    """
    stamp = now or datetime.now()

    # 0. 账号不存在 —— 仍然跑一次 bcrypt，把耗时对齐到「密码错」那条路径
    if user is None:
        _burn(candidate, None)
        return VerifyOutcome(False, _CODE_NOT_FOUND, GENERIC_AUTH_MESSAGE)

    # 1. 非在职
    if user.status != STATUS_ACTIVE:
        _burn(candidate, user.password_hash)
        return VerifyOutcome(False, _CODE_INACTIVE, "账号已停用或已离职，请联系管理员")

    # 2. 锁定期内。放在密码比对之前：锁定期的意义就是不再消耗真实验证
    if user.is_locked_at(stamp):
        _burn(candidate, user.password_hash)
        return VerifyOutcome(False, _CODE_LOCKED, GENERIC_AUTH_MESSAGE)

    # 3. 真正的比对
    try:
        matched = bcrypt.checkpw(candidate.encode("utf-8"), user.password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        matched = False
    if not matched:
        return VerifyOutcome(False, _CODE_WRONG_PASSWORD, GENERIC_AUTH_MESSAGE)

    # 密码正确 —— 往下都是「成功之后」的判断
    rehash = needs_rehash(user.password_hash)

    # 4. 强制改密（管理员刚重置的临时密码）
    if user.must_change_password:
        return VerifyOutcome(
            True, _CODE_MUST_CHANGE, "首次登录或密码已被重置，请先修改密码", rehash
        )

    # 5. 过期 —— 密码本身是对的，但**不放行**（设计规格 §4：到期后除「改密」
    #    与「登出」外一律 403）。所以 ok=False：调用方拿到的是「凭据有效但
    #    被策略拦住」，而不是「密码错了」——这两者的处置完全不同。
    if is_expired(user.password_changed_at, now=stamp):
        return VerifyOutcome(
            False, _CODE_EXPIRED, "密码已过期，请修改密码后继续使用", rehash,
            detail={"expired": True, "expire_in_days": expire_in_days(
                user.password_changed_at, now=stamp)},
        )

    return VerifyOutcome(True, _CODE_OK, "", rehash)