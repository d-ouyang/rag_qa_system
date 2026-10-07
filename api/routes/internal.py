"""
内部接口 —— `/api/v1/internal/*`（P2-11c）。

--------------------------------------------------------------------------
这个前缀下只有一个能力：**替网关做登录判定**
--------------------------------------------------------------------------
网关（NestJS/TypeScript）拿 `/api/auth/login` 的请求转到这里，后端用
`core/password_policy.verify()` 判完，返回「成 / 不成 + 成的话是谁」，
网关据此签发 JWT。

为什么要有这一跳（否掉的是设计规格 §8 D11 的字面表述）：判定规则会变成两份。
`verify()` 有 141 条断言守着，其中「防枚举文案逐字相同」「四条失败路径耗时等长」
「到期判据 `>` 不是 `>=`」三条在 TypeScript 里重写一遍，就是三份没有测试的副本。
代价是登录多一次内网 HTTP（实测 < 5ms）。

--------------------------------------------------------------------------
⚠️ 安全：这三个接口的信任边界
--------------------------------------------------------------------------
**它们能验证密码、能改 `failed_login_count`、能写 `last_login_at`、能落审计。**
拿到它们 ≈ 拿到半个登录系统。所以边界必须比「反代后面那台机器」更硬：

    1. **共享密钥**：`X-Internal-Token` 必须与 `INTERNAL_SHARED_SECRET` **常量时间**比对。
       不用共享密钥的话，「能连到 8000」就够了 —— 而「能连到 8000」这件事
       在容器网络里同网段任意服务都成立，将来多起一个容器就是漏洞。
    2. **不参与 OpenAPI**：`include_in_schema=False` —— 免得 `/docs` 变成一份
       「如何绕过网关登录」的说明书。
    3. **生产缺密钥 = 启动失败**（不是「不校验」也不是「随便给个默认值」）。
       见 `config/settings.py` 的 `INTERNAL_SHARED_SECRET`。

⚠️ **这三条不解决「8000 被映射出去」**。那件事的答案是拓扑：
`docker-compose.yml` 里 backend **不映射端口**（`make stack-up` 后 `docker port`
查不到 8000）。密钥只是第二道防线，不是第一道。
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from config.settings import settings
from core import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["内部接口"])


# --------------------------------------------------------------------------- #
# 共享密钥校验
# --------------------------------------------------------------------------- #
def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """
    校验 `X-Internal-Token`。

    **必须用 `hmac.compare_digest` 而不是 `==`**：字符串相等比较会在第一个
    不一样的字节上立刻返回，攻击者能靠响应时间一个字节一个字节把密钥试出来。
    这就是时序攻击，`compare_digest` 是它的标准解法。

    失败时**不回显任何信息**（不说「密钥长度不对」也不说「配置了没」）——
    这是一个内部接口，但它也可能被外部探测到。
    """
    expected = settings.INTERNAL_SHARED_SECRET
    provided = x_internal_token or ""
    if not expected:
        # 生产缺密钥应该启动就失败（settings 里已做）；走到这里说明配置被绕过
        # （比如测试里覆盖了 settings）。此时**拒绝**而不是放行 —— fail-closed。
        logger.error("INTERNAL_SHARED_SECRET 为空，内部接口一律拒绝（配置异常）")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="内部接口未启用",
        )
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        logger.warning("内部接口鉴权失败（密钥不匹配）")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部接口鉴权失败",
        )


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class InternalLoginBody(BaseModel):
    """
    登录请求体。

    `password` 刻意**没有长度上限**：加上限只会让「超长密码」变成一种能被
    区分出来的错误（他就知道自己的密码有多长），而校验真实长度必然要把它
    完整收下来。字段本身的限制是**内存**，不是业务规则。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class InternalLogoutBody(BaseModel):
    user_id: int | None = None
    username: str = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@router.post(
    "/auth/login",
    include_in_schema=False,
    dependencies=[Depends(require_internal_token)],
)
def internal_login(body: InternalLoginBody, request: Request) -> dict[str, Any]:
    """
    替网关做登录判定。

    ⚠️ **返回体里 `ok=false` 时也是 HTTP 200** —— 这是刻意的。

    「凭据不通过」不是「接口调用失败」：网关要拿到 `code` 才能决定
    「回 401 通用文案」还是「把他导到改密页」。而 `expired` 那条是
    `ok=false` 却**密码是对的**，两种处置完全相反。

    所以语义分层是：**HTTP 200 = 我正常处理了你的请求；
    业务上成没成，看 body 里的 `ok`。** 网关据此把 `ok=false` 转成 401。
    """
    client_ip = _client_ip(request)
    outcome = auth_service.login(body.username, body.password, client_ip=client_ip)
    return outcome.to_dict()


@router.post(
    "/auth/logout",
    include_in_schema=False,
    dependencies=[Depends(require_internal_token)],
)
def internal_logout(body: InternalLogoutBody, request: Request) -> dict[str, Any]:
    """
    记一次登出。

    JWT 无状态，**服务端没有任何东西被销毁** —— 这个接口存在的意义只有两个：
    ① 落一条审计（13d 交付时明确说「登录类审计等 11c」，这是其中一条）；
    ② 给将来加 token 黑名单时留好位置。
    语义在服务层注释里写明了：`server_side_revoked` 恒为 `false`，
    别把它读成「登出即失效」。
    """
    auth_service.logout(body.user_id, body.username, client_ip=_client_ip(request))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str | None:
    """
    取本次登录的来源 IP，供审计落库。

    与 `core.identity._client_ip()` 同源（同为 XFF 第一段、同样的前提「8000
    不对外可达」）。刻意不共用一个函数：那边解析的是**用户身份**（失败要 401），
    这边解析的是**审计字段**（失败就留空），两者的失败后果不同 ——
    共用一个函数会诱导后来者以为「解析不出来也有兜底」。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None
