"""
身份解析 —— 「这次请求是谁」的**唯一**入口。

--------------------------------------------------------------------------
这份文件为什么在 P2-13a 就出现（它本来属于 12b）
--------------------------------------------------------------------------
管理端的第一件事不是画页面，而是回答「谁在点这个页面」。
`/api/v1/admin/*` 上任何一个动作都要知道操作者是谁 —— 否则「管理员不能给自己
降级」「hr 不能重置密码」这两条规矩一条都落不下去，而且缺的就是那种
**不报错的权限漏洞**：谁都能调，界面上也看不出来。

它对应设计规格 §5.1 里 **12b 的②（后端侧）**。网关侧的无条件剥离（12b ①）
本轮一行未动 —— 那是 ② 阶段的事。现在做的是**控制面**（管理端）的身份，
先把 `Actor` 这个概念定型，12b 把它套到数据面（会话 / 文档 / 检索）时不必返工。

--------------------------------------------------------------------------
信任边界：后端凭什么敢信任请求头（以及代价）
--------------------------------------------------------------------------
网关不给后端发签名，后端也不验 JWT —— **认证只有一个入口**（网关）。
「这个头必须是网关注入的」由**网络拓扑**保证：8000 不对外可达
（compose 里 backend 不映射端口）。

代价直说：一旦有人把 8000 映射出去，`curl -H 'X-User-Id: 7' localhost:8000/...`
就是完整的身份伪造。所以 `gateway` 模式下缺头一律 **401（fail-closed）**，
而不是「缺头就当匿名」——后者会把这个错误掩盖成一次普通的功能异常。

--------------------------------------------------------------------------
两种模式的差别只在「没有头的时候怎么办」
--------------------------------------------------------------------------
    gateway  生产。缺头 / 库里查不到 / 非在职 → 401。
    dev      本机。缺头 → 回落到 settings.IDENTITY_DEV_USERNAME；
             只有**这个**登录名在库里查不到时，才视作 `.env` 里的
             break-glass 超管（role=admin）。别的查不到 → 401。

dev 模式存在的理由：① 阶段登录真相源还在 `.env`（11c 才迁到 MySQL），
网关签发的 JWT 里 sub 是登录名，而那个登录名在 user 表里未必有一行。
没有 dev 回落的话，`make api` 起来后管理端会直接 401，而它的成因（user 表里
恰恰缺了一行同名账号）离症状（登录成功、一进去就被踢回登录页）很远 —— 排查会跑偏。

它的代价是本机有一个 admin 后门，所以它**只能**是默认值，不允许出现在部署环境。

--------------------------------------------------------------------------
为什么按 id 查不到时才按 username 查
--------------------------------------------------------------------------
现在网关注入的 `X-User-Id` 是 JWT 的 sub —— 一个用户名字符串
（网关到现在都不认识 uid）。11c 之后它会变成整数 user.id。
两种形态都支持，是为了让这次改造不必等 11c，将来也不用回来改：
数字走 `get_by_id`，非数字走 `get_by_username`，两条路同一个出口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from fastapi import Depends, Header, HTTPException, Request, status

from config.settings import settings
from core import password_policy as policy
from core import user_repo as repo
from core.user_repo import ROLE_ADMIN, ROLE_HR, STATUS_ACTIVE, UserRecord

logger = logging.getLogger(__name__)

MODE_DEV = "dev"
MODE_GATEWAY = "gateway"

#: 身份从哪来。写进 `/me` 的响应里，是为了让「我到底是谁」在界面上可解释 ——
#: 权限问题上，一个看不见来源的身份比没有身份更糟。
SOURCE_HEADER = "header"
SOURCE_DEV_FALLBACK = "dev_fallback"
SOURCE_BREAKGLASS = "breakglass"

#: 能管人事的角色（看/改员工资料、停用离职），但不能碰密码 —— 见 D10。
STAFF_ROLES = frozenset({ROLE_ADMIN, ROLE_HR})


@dataclass(frozen=True)
class Actor:
    """
    一次请求的操作者。

    与 `UserRecord` 的区别：它是**这一次**的视图，带权限判定与来源，
    而 `UserRecord` 是库里那一行的原样快照。

    `id=None` 只有一种取值场景 —— break-glass 超管（`.env` 里的人不在库里）。
    它因此**落不进** `created_by` / `updated_by`（那两列填 NULL），
    这是刻意接受的信息损失：等 P2-13d 把它写进审计时，能留下的只有用户名。

    `client_ip` 跟着身份一起走（P2-13d 起）：它是「这次请求」的属性而不是
    「这个人」的属性，放进 Actor 是为了让审计不必在每个服务函数里多收一个参数。
    """

    id: int | None
    username: str
    display_name: str
    role: str
    status: str
    source: str
    record: UserRecord | None = None
    #: 本次请求的来源 IP。取不到时为 None（**不编造** 「0.0.0.0」这类占位）。
    client_ip: str | None = None

    # ---------- 权限（只有三条，不多做）----------
    @property
    def can_staff(self) -> bool:
        """能否进管理端做人事操作。普通员工连员工列表都不该看到。"""
        return self.role in STAFF_ROLES

    @property
    def can_reset_password(self) -> bool:
        """能否重置他人密码 —— **admin 专属**（D10：hr 只能改资料）。"""
        return self.role == ROLE_ADMIN

    @property
    def can_manage_org(self) -> bool:
        """能否维护部门/职位。这属于组织信息，hr 也能做。"""
        return self.role in STAFF_ROLES

    def is_self(self, user_id: int) -> bool:
        return self.id is not None and int(user_id) == int(self.id)

    def to_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        stamp = now or datetime.now()
        changed_at = self.record.password_changed_at if self.record else None
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "status": self.status,
            "identity_source": self.source,
            "permissions": {
                "staff": self.can_staff,
                "reset_password": self.can_reset_password,
                "manage_org": self.can_manage_org,
            },
            "password": {
                "must_change": bool(self.record.must_change_password) if self.record else False,
                "expire_in_days": policy.expire_in_days(changed_at, now=stamp)
                if self.record else None,
                "expired": policy.is_expired(changed_at, now=stamp) if self.record else False,
                "warn": policy.should_warn_expire(changed_at, now=stamp) if self.record else False,
            },
        }


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _decode(value: str | None) -> str:
    """
    取请求头并做一次 URL 解码。

    网关对 `X-Username` 做过 `encodeURIComponent`（中文名含非 ASCII），
    `X-User-Id` 目前没编码。两边都 unquote 一次是安全的：
    不含 `%` 的串 unquote 是恒等操作。
    """
    return unquote(value or "").strip()


def _make_actor(record: UserRecord | None, username: str, source: str) -> Actor:
    if record is None:
        # break-glass 超管：只存在于 `.env`，库里那一行可能永远不会有。
        return Actor(
            id=None,
            username=username,
            display_name=f"{username}（内置超管）",
            role=ROLE_ADMIN,
            status=STATUS_ACTIVE,
            source=source,
        )
    return Actor(
        id=record.id,
        username=record.username,
        display_name=record.display_name or record.username,
        role=record.role,
        status=record.status,
        source=source,
        record=record,
    )


def resolve_actor(
    *,
    user_id_header: str | None = None,
    username_header: str | None = None,
    client_ip: str | None = None,
) -> Actor:
    """
    把身份头解析成一个 Actor。**本模块唯一的解析出口**，路由层不许自己读头。

    失败一律抛 HTTPException（401/403），不返回 None ——
    「解析不出身份」如果是返回值，调用方会写出 `if actor:` 这种
    「没有身份就跳过权限判断」的分支，而那正好是相反于 fail-closed 的方向。
    """
    mode = settings.IDENTITY_MODE
    raw_uid = _decode(user_id_header)
    raw_name = _decode(username_header)

    claimed = raw_uid or raw_name
    source = SOURCE_HEADER

    if not claimed:
        if mode == MODE_GATEWAY:
            # 生产下缺头 = 请求绕过了网关（或网关忘了注头），两者都必须看得见
            logger.warning("请求缺少身份头（gateway 模式下按 401 拒绝）")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少身份信息：本服务只接受来自鉴权网关的请求",
            )
        claimed = settings.IDENTITY_DEV_USERNAME
        source = SOURCE_DEV_FALLBACK
        logger.debug("无身份头 → dev 模式回落到 %s", claimed)

    record: UserRecord | None = None
    if claimed.isdigit():
        record = repo.get(int(claimed))
    if record is None:
        # 数字 id 查不到时仍要按用户名再试一次：
        # 「7」既可能是 uid，也可能是某人的登录名（纯数字用户名是合法的）
        record = repo.get_by_username(claimed)

    if record is None:
        # dev 模式的后门**只认一个名字**。写成「谁查不到都当超管」的话，
        # 任何一个拼错的 X-User-Id 都会得到一个 admin —— 那是把「一次手误」
        # 升级成「一次越权」，而且是静默的。
        if mode != MODE_DEV or claimed != settings.IDENTITY_DEV_USERNAME:
            logger.warning("身份头指向的用户在库里不存在 | claimed=%s mode=%s", claimed, mode)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="账号不存在或已被移除，请联系管理员",
            )
        source = SOURCE_BREAKGLASS
        logger.warning(
            "库里查不到 %s → dev 模式按 break-glass 超管放行（生产必须为 gateway 模式）",
            claimed,
        )

    actor = _make_actor(record, claimed, source)
    # ⚠️ 事后补一个字段而不是让 `_make_actor` 多收一个参数：Actor 是 frozen
    # dataclass，用 dataclasses.replace 重建成一个，比给构造函数加参数更不容易
    # 在调用点被漏掉（漏掉时 IP 为 None，审计里留空，而不会静默记成 127.0.0.1）。
    if client_ip:
        actor = replace(actor, client_ip=client_ip)

    # 已停用 / 已离职的人即使拿着还在有效期内的 token，也不许继续操作。
    # 现在网关还不知道 status（11c 才查库），所以这道校验必须由后端兜。
    if actor.record is not None and actor.status != STATUS_ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已停用或已离职，无法使用管理端",
        )
    return actor


# --------------------------------------------------------------------------- #
# FastAPI 依赖
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str | None:
    """
    取本次请求的来源 IP，供审计落库。

    优先取 `X-Forwarded-For` 的**第一段**：后端在网关后面，`request.client.host`
    拿到的是网关的地址（本地全是 127.0.0.1），记它等于没记。XFF 的第一段
    是网关注取的原始客户端地址。

    为什么敢信这个头（它由客户端自由填写）：与本文件顶部的信任边界同源 ——
    8000 不对外可达，能把请求送到后端的只有网关，所以这个头只可能由网关设置。
    **如果哪天把 8000 映射出去了，这里立刻变成可伪造的字段**，
    那时审计里的 IP 就只是「一个自称来自某地的人」。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


def current_actor(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_username: str | None = Header(default=None, alias="X-Username"),
) -> Actor:
    """取当前操作者。任何需要「知道是谁」的接口都依赖它。"""
    return resolve_actor(
        user_id_header=x_user_id,
        username_header=x_username,
        client_ip=_client_ip(request),
    )


def require_staff(actor: Actor = Depends(current_actor)) -> Actor:
    """
    管理端门槛：只有 admin / hr 能进。

    **后端必须自己再校验一次**，不能只靠网关的路径规则 ——
    设计规格 §5.2 第 13 行写明了理由：网关的路径级授权是粗筛，
    而「裸跑 8000 调试」「忘了配路径规则」这两种情况都会让它整个不生效。
    """
    if not actor.can_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员或人事权限才能使用管理端",
        )
    return actor


def require_admin(actor: Actor = Depends(current_actor)) -> Actor:
    """密码与角色这类高危动作的门槛（D10：hr 到此为止）。"""
    if actor.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该操作仅系统管理员可用（人事账号只能维护资料）",
        )
    return actor


__all__ = [
    "Actor",
    "MODE_DEV",
    "MODE_GATEWAY",
    "SOURCE_BREAKGLASS",
    "SOURCE_DEV_FALLBACK",
    "SOURCE_HEADER",
    "STAFF_ROLES",
    "current_actor",
    "require_admin",
    "require_staff",
    "resolve_actor",
]
