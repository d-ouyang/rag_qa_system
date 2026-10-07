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

第一道防线是**网络拓扑**：8000 不对外可达（compose 里 backend 不映射端口）。
但 12b 施工前的实测证明**这一道在本机裸跑形态下不成立**：

    curl -H 'X-User-Id: 440' localhost:8000/api/v1/admin/users   → 200 + 完整员工名单

原因有二：① 本机 uvicorn 绑的是 127.0.0.1，同机任何进程都能连（不只是「外部」）；
② `.env` 默认 `IDENTITY_MODE=dev`，而无身份头会回落到 break-glass 超管。
所以「拓扑」是一道**依赖部署形态**的防线 —— 哪天有人把端口映射出去、
或者在容器里多起一个同网段的服务，它就断了，而代码里没有任何东西会察觉。

因此 12b 加了**第二道**：网关转发时注入 `X-Internal-Auth`（值=与网关共享的
`INTERNAL_SHARED_SECRET`），`gateway` 模式下**验过它才认身份头**，缺或错一律 401。
于是「伪造 X-User-Id」不再等于「伪造身份」—— 攻击者还得知道那个密钥。

⚠️ 这个方案不是白拿的，代价要说清：
  · **密钥要下发到两处**（`.env` 与 `gateway/.env`），漏配的表现是
    「登录正常但所有数据接口 401」—— 症状与病因隔着一个进程。
    所以网关侧生产缺密钥直接启动失败，后端侧缺则 503（见 settings注释）。
  · **密钥轮换要同步改两处**，漏改的后果同上（不是静默降级，是全站 401，
    这点反而是好的 —— 失败得很响）。
  · 它**不能**防「已经拿到密钥的人」。密钥进了环境变量就等于进了容器的
    env，`docker inspect` 看得到。所以它防的是「拓扑破了」与「顺手 curl 一下」，
    不是「攻陷了网关进程」。后者要靠 12d/P2-14 的对象级校验与审计。
  · 与11c 的 `/api/v1/internal/*` **共用同一个密钥**：它们本来就是同一个信任
    关系（「网关与后端之间」），分成两个只会让运维多记一个值、少改一处而全站挂。

两种模式的差别只在「没有头/没有证明的时候怎么办」
--------------------------------------------------------------------------
    gateway  生产。缺证明 / 缺头 / 库里查不到 / 非在职 → 401。
    dev      本机。缺头 → 回落到 settings.IDENTITY_DEV_USERNAME；
             只有**这个**登录名在库里查不到时，才视作 `.env` 里的
             break-glass 超管（role=admin）。别的查不到 → 401。
             ⚠️ dev 模式**不验网关证明**（否则 `make api` 裸跑时
             所有 curl 调试都要先造一个证明头，那不叫本地调试）。

dev 模式存在的理由：① `make api` / `make test` 起来后要能直接调接口；
它同时是个**已知的本机后门**，所以它只能是非生产默认值，
而且它认的那个名字必须是 `.env` break-glass 超管（见上面「只认一个名字」）。

--------------------------------------------------------------------------
为什么按 id 查不到时才按 username 查
--------------------------------------------------------------------------
现在网关注入的 `X-User-Id` 是 `user.id` 的十进制串（11c 起）。
仍保留按用户名回落，是为了让 11c 之前签发的旧 token（最长 12h 窗口，
`sub` 是登录名）还能用 —— 两种形态同一个出口。
"""
from __future__ import annotations

import logging
import hmac
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

#: 身份头的**唯一**清单 —— 与 `gateway/src/auth/identity-headers.ts` 的
#: `INBOUND_IDENTITY_HEADERS` 一一对应。
#:
#: ⚠️ 两份清单分别在两个语言里，**没有任何东西会校验它们是否还对得上** ——
#: 那正是 13d踩过的坑（后端写 `{"from","to"}`、前端读 `detail.status`，
#: 两边各自自洽，页面上整列 `—`，后端全绿、接口 200、不报错）。
#: `tests/test_module14_trust_boundary.py` 会**读本文件的源码**与 TS 那份比对，
#: 少一个就红。所以往两边加头时，**两边都要改**，而且会被测试抓住。
IDENTITY_HEADERS = (
    "x-user-id",
    "x-username",
    "x-user-role",
    # ⚠️ `x-role` 不注入但照样要剥：设计规格 §5.1.2 与复现脚本
    # `probe-header-strip.cjs` 里写的角色头是 `X-Role`，而 11c 真正注入的
    # 是 `X-User-Role`。两个名字在文档与代码之间对不上已持续三个版本
    # （§5.2 第 2 行写的也是 `x-role`）—— 只剥其中一个，另一个就是后门，
    # 而「后端将来会不会读它」是会变的（12d 就要读 role）。
    # 两个都剥的代价是零，收益是不管将来读哪个都读不到客户端填的值。
    "x-role",
    "x-dept-id",
    "x-token-version",
    "x-identity-source",
    "x-internal-auth",
)

#: 网关转发时必须携带的「网关证明」（P2-12b）。
#: 与11c 的 `/api/v1/internal/*` 共用 `INTERNAL_SHARED_SECRET`——
#: 它们本来就是同一个信任关系，分成两个密钥只会让运维多记一个值。
GATEWAY_PROOF_HEADER = "x-internal-auth"


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


def verify_gateway_proof(proof_header: str | None) -> None:
    """
    校验「这个请求确实来自网关」（P2-12b）。**不通过一律 401，不降级。**

    为什么这道不可省（12b 施工前的实测，不是推理）：

        curl -H 'X-User-Id: 440' localhost:8000/api/v1/admin/users
        → 200，返回完整员工名单

    拓扑那唯一一道防线在**本机裸跑形态下不成立**：uvicorn 绑的是 127.0.0.1，
    同机任何进程都能连上；而 `.env` 默认 `IDENTITY_MODE=dev`，无身份头时
    还会回落到 break-glass 超管。所以「8000 不对外」这个前提一旦不成立
    （有人映射端口、容器里多起一个同网段服务），伪造就成立**且不报错**。

    `hmac.compare_digest` 而不是 `==`：普通比较会在第一个不同的字节处返回，
    攻击者据此逐字节猜密钥（时序侧信道）。这条在 `api/routes/internal.py`
    已是同样的做法，两处刻意保持一致。

    **不比较「有没有这个头」，只比较「对不对」** —— 后者已经隐含前者。
    另外注意本函数**不抛 503**：没配 `INTERNAL_SHARED_SECRET` 是**部署错误**，
    而部署错误要在启动/健康检查那一层暴露（`/api/v1/system/health` 会报），
    在每个请求上抛 503 只会把它变成一片噪声、并且掩盖真正的「证明错了」。
    所以缺配置时一律按「证明不通过」处理 —— fail-closed 方向一致。
    """
    expected = settings.INTERNAL_SHARED_SECRET
    provided = proof_header or ""
    if not expected or not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        logger.warning(
            "网关证明缺失或不匹配（gateway 模式下按 401 拒绝）| "
            "configured=%s provided_len=%d",
            bool(expected), len(provided),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请求未经过鉴权网关",
        )


def resolve_actor(
    *,
    user_id_header: str | None = None,
    username_header: str | None = None,
    client_ip: str | None = None,
    token_version_header: str | None = None,
    gateway_proof_header: str | None = None,
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

    # ------------------------------------------------------------------ #
    # 网关证明（P2-12b）：必须在**读身份头之前**验
    # ------------------------------------------------------------------ #
    # 顺序很要紧：先验「这话是不是网关说的」，再问「网关说的是谁」。
    # 反过来写成「先解析身份，发现不对再验」的话，
    # 缺头这个分支就会绕过证明检查 —— 而那恰恰是最常见的一种情况。
    if mode == MODE_GATEWAY:
        verify_gateway_proof(gateway_proof_header)

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
    # 网关（11c 起）登录时查过库，但**那是一次性的**：他登录之后被停用，
    # 手里的 token 还没过期 —— 那一刻的「在职」判定早已与现实脱节。
    if actor.record is not None and actor.status != STATUS_ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已停用或已离职，无法使用管理端",
        )

    # ------------------------------------------------------------------ #
    # token_version 比对（P2-11c）
    # ------------------------------------------------------------------ #
    # 上面那道 status 校验**只挡住了停用/离职**，没挡住「改密之后旧 token 仍有效」——
    # 改密时人还是 active，而新旧两个 token 都指向同一个 active 的人。
    # 所以要靠 token_version：改密 / 停用 / 改角色都会让它 +1（见 user_repo），
    # 于是「这个 token 是改密之前签的」变成一个可判定的事实。
    #
    # ⚠️ 头缺了**不拦**（`None` 直接放过），这是刻意的：
    #   · 开发时（dev 模式、裸跑 curl）没有这个头，拦住的话本地一切 401；
    #   · 11c 之前签发的 token 里没有 `ver`（最长 12h 窗口），拦住的话
    #     升级瞬间所有人被踢下线 —— 而那次踢下线毫无安全收益（那些 token
    #     签发时确实是有效的）。
    # 真要收紧（生产强制要求 ver 存在），那是「破坏性变更」级别的决定，
    # 该在 12b 收口时做，不该偷偷夹在 11c 里。
    if actor.record is not None and token_version_header is not None:
        claimed = token_version_header.strip()
        if claimed.isdigit():
            if int(claimed) != int(actor.record.token_version):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="登录状态已失效（密码或权限已变更），请重新登录",
                )
            logger.warning(
                "token_version 不匹配（token 已失效）| uid=%s 持有=%s 库里=%s",
                actor.record.username, claimed, actor.record.token_version,
            )
        # 不是纯数字 = 网关发来的东西不对，**不拦但记一条**：
        # 它要么是伪造（但 8000 不对外可达），要么是网关的 bug。
        # 两种都不该让请求失败，但都要留下痕迹。
        else:
            logger.warning(
                "X-Token-Version 不是数字，已忽略 | value=%r uid=%s",
                token_version_header[:32], actor.record.username,
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
    x_token_version: str | None = Header(default=None, alias="X-Token-Version"),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
) -> Actor:
    """取当前操作者。任何需要「知道是谁」的接口都依赖它。"""
    return resolve_actor(
        user_id_header=x_user_id,
        username_header=x_username,
        client_ip=_client_ip(request),
        token_version_header=x_token_version,
        gateway_proof_header=x_internal_auth,
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
    "GATEWAY_PROOF_HEADER",
    "IDENTITY_HEADERS",
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
    "verify_gateway_proof",
]
