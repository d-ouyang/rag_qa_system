"""
站点心情收集接口 —— `/api/v1/mood/*`（念安站「Are you happy today」的后端）。

--------------------------------------------------------------------------
定位：独立于 RAG 业务的公开小接口
--------------------------------------------------------------------------
nianan.site 底部的 MoodNote 表单提交 `{email, mood}`，落到独立的
`site_mood_submission` 表（core/schema.py / 迁移 0010）。它与 RAG 的
用户 / 会话 / 文档零关联：不外键、不进任何 RAG 查询路径、不出现在
问答返回里。放同一台服务器纯粹是因为「服务器就在那、MySQL 就在那」，
不是为了复用业务。

--------------------------------------------------------------------------
「公开」的边界由三层守住
--------------------------------------------------------------------------
    1. 网关侧 `@Public()` 白名单**精确**放行两个路径（gateway 的
       PUBLIC_MOOD_PATHS）—— 不是前缀通配，未来本模块新增接口默认仍要 token；
    2. submit 的闸门：per-IP 固定窗口限流（外层）+ 邮箱冷却（内层前置盾）
       + 库内查重（判定落在 MySQL，同邮箱永久只算一条，p4.2）；
    3. admin 的独立静态 token（MOOD_ADMIN_TOKEN）常量时间比对，
       与 RAG 的 JWT / INTERNAL_SHARED_SECRET 互不复用。

--------------------------------------------------------------------------
为什么业务拒绝是 HTTP 200 + `ok=false`（与 internal.py 同一哲学）
--------------------------------------------------------------------------
「你交得太频繁 / 这个邮箱刚交过」不是「接口调用失败」，是业务结果 ——
前端拿到 `reason` 才能给出对应的提示文案。语义分层：
**HTTP 200 = 我正常处理了你的请求；业务上成没成，看 body 里的 ok。**
真正的接口错误（参数不合法 400 / token 不对 401）才用 4xx。

--------------------------------------------------------------------------
Redis 挂了怎么办：fail-open
--------------------------------------------------------------------------
限流与去重闸门依赖 Redis。Redis 不可用时**放行**并记 WARNING，而不是拒绝
提交 —— 理由与 MEMORY_DEGRADE_ON_ERROR 同源：这是个心情表单，宁可收下
（极端情况无非多几行数据），也不让「缓存层抖一下」变成「用户看到发送失败」。
公开表单的滥用风险主要由拓扑兜底（8000 不对外、只有网关能进来）。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from config.settings import settings
from core.db import now_db, session_scope
from core.redis_store import get_redis_client
from core.schema import site_mood_submission_table as MOOD

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mood", tags=["站点心情收集"])

# 心情派生标签的阈值，与前端 MoodNote.tsx 的 moodWord() 一致（±0.2）。
_MOOD_SAD_THRESHOLD = -0.2
_MOOD_HAPPY_THRESHOLD = 0.2

# Referer / UA 的存储上限（与表列宽一致），超长截断而不是拒绝 ——
# 这两个字段是辅助信息，不值得为它让一次提交失败。
_MAX_PAGE_URL = 512
_MAX_USER_AGENT = 512


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class MoodSubmitBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    mood: float = Field(ge=-1.0, le=1.0, description="-1.00 低落 ~ 1.00 开心")


# --------------------------------------------------------------------------- #
# 邮箱格式校验（与前端 src/lib/email.ts 的 isValidEmail 同一套规则）
# --------------------------------------------------------------------------
# 前端已经校验过一遍，为什么后端还要再来一遍：前端校验是体验（提前提示），
# 后端校验是边界（绕过页面直接打接口的人拿不到免检）。两份规则刻意保持
# 同构，改任何一边都要同步另一边 —— 不一致的后果只是「前端说行、后端说不行」，
# 不会造成数据损坏，所以接受这份重复。
_EMAIL_LOCAL_RE = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_LABEL_RE = re.compile(r"^[a-zA-Z0-9-]+$")


def _is_valid_email(raw: str) -> bool:
    email = raw.strip()
    if len(email) < 3 or len(email) > 254:
        return False
    at = email.find("@")
    if at <= 0 or at != email.rfind("@"):
        return False
    local, domain = email[:at], email[at + 1:]
    if len(local) > 64:
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if not _EMAIL_LOCAL_RE.fullmatch(local):
        return False
    if len(domain) > 253 or ".." in domain:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not 0 < len(label) <= 63:
            return False
        if not _EMAIL_LABEL_RE.fullmatch(label):
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
    tld = labels[-1]
    return len(tld) >= 2 and tld.isalpha()


# --------------------------------------------------------------------------- #
# 来源 IP（与 internal.py 的 _client_ip 同源同构：XFF 第一跳）
# --------------------------------------------------------------------------
# 刻意不共用一个函数：那边解析的是审计字段，这边解析的是**限流键**——
# 被伪造的后果不同（审计错一行 vs 全站共享一个限流桶），但解析规则一致：
# 都依赖「8000 不对外可达，XFF 由可信代理链写入」这个前提。
# ⚠️ 部署前提：边缘 nginx 必须设置 `proxy_set_header X-Forwarded-For
# $remote_addr`（覆盖写而非追加），否则客户端可自带 XFF 头伪造第一跳。
def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host if request.client else "unknown"


def _mood_word(mood: float) -> str:
    if mood <= _MOOD_SAD_THRESHOLD:
        return "Sad"
    if mood >= _MOOD_HAPPY_THRESHOLD:
        return "Happy"
    return "Neutral"


# --------------------------------------------------------------------------- #
# Redis 双闸（外层限流 + 内层去重；fail-open）
# --------------------------------------------------------------------------- #
def _rate_limit_key(ip: str) -> str:
    prefix = (settings.REDIS_KEY_PREFIX or "rag").rstrip(":")
    return f"{prefix}:mood:ratelimit:{ip}"


def _email_dedup_key(email: str) -> str:
    prefix = (settings.REDIS_KEY_PREFIX or "rag").rstrip(":")
    # 存哈希不存明文：Redis 里不留 PII 副本，明文真身只在 MySQL。
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"{prefix}:mood:emaildup:{digest}"


def check_rate_limit(ip: str) -> bool:
    """
    per-IP 固定窗口：`INCR` 计数，首次出现时补 60s 过期，超过上限拒绝。

    固定窗口而不是滑动窗口：个人站的量级下「整分钟清零」与「严格 60s」
    的体感差异为零，而固定窗口两条命令（INCR + EXPIRE）就够了。
    INCR 与 EXPIRE 非原子的代价（count==1 时进程恰好挂掉 → key 永不过期）
    由 key 里带分钟号兜底不现实，这里接受它：最坏情况是那个 IP 的计数器
    不再清零 —— 部署时给这类 key 设统一 TTL 的运维约定即可，不值得上 Lua。
    """
    client = get_redis_client()
    count = int(client.incr(_rate_limit_key(ip)))
    if count == 1:
        client.expire(_rate_limit_key(ip), 60)
    return count <= settings.MOOD_RATE_LIMIT_PER_MINUTE


def check_email_cooldown(email: str) -> bool:
    """
    邮箱冷却去重：`SET NX EX` 抢占冷却位。返回 False = 窗口内已提交过。

    SET NX 的原子性在这里刚好是需要的语义：并发两笔同邮箱提交，
    只有一笔能抢占成功，另一笔按重复拒绝 —— 不需要额外的锁。

    ⚠️ p4.2 起它降级为「前置盾」：真正的去重判定在库内
    （email_exists_in_db）。它存在的意义只剩两块——
    10 分钟窗口内替 MySQL 挡掉重复点查，以及在「行被删掉、
    有人拿同一邮箱重交」的场景里提供一层与库无关的快速拒绝。
    """
    client = get_redis_client()
    occupied = client.set(
        _email_dedup_key(email),
        "1",
        nx=True,
        ex=int(settings.MOOD_EMAIL_COOLDOWN_SECONDS),
    )
    return bool(occupied)


def email_exists_in_db(email: str) -> bool:
    """
    库内去重（p4.2）：这张表里已有同邮箱的提交 → 永久算重复。

    为什么冷却窗不够：Redis 冷却只有 10 分钟，过期后同一邮箱能再交，
    表里于是长出同邮箱的多行（线上实测：同一邮箱隔天交出第二条）。
    「一个邮箱一条心情」就是这个模块的语义，所以判定必须落在库上；
    email 列有 idx_site_mood_email 索引，点查代价可忽略。
    Redis 异常时这里 fail-open（放行），与整条链路的可用性优先一致。
    """
    with session_scope() as session:
        row = session.execute(
            select(MOOD.c.id).where(MOOD.c.email == email).limit(1)
        ).first()
    return row is not None


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@router.post(
    "/submit",
    summary="提交心情与邮箱（公开）",
    description=(
        "nianan.site 底部表单的提交口。\n"
        "- 成功：`{ok: true, id}`；\n"
        "- 业务拒绝（HTTP 200）：`{ok: false, reason: rate_limited | duplicate}`；\n"
        "- 参数不合法：400。\n"
        "双闸之外还有库内查重：先 per-IP 限流（外层挡洪水），再邮箱冷却（内层挡重复），"
        "最后查库——表里已有同邮箱的提交，无论过多久都算重复（p4.2）。"
    ),
)
def submit_mood(body: MoodSubmitBody, request: Request) -> dict[str, Any]:
    email = body.email.strip().lower()
    if not _is_valid_email(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱格式不正确")

    mood = round(body.mood, 2)
    ip = _client_ip(request)
    referer = (request.headers.get("referer") or "")[:_MAX_PAGE_URL]
    user_agent = (request.headers.get("user-agent") or "")[:_MAX_USER_AGENT]

    # ---- 闸 1：per-IP 限流（外层）。Redis 异常 → 放行（fail-open，见模块注释）----
    try:
        if not check_rate_limit(ip):
            logger.warning("mood 提交触发限流 | ip=%s", ip)
            return {"ok": False, "reason": "rate_limited"}
    except Exception as e:  # noqa: BLE001 - 可用性优先
        logger.warning("mood 限流检查失败，放行 | ip=%s err=%s", ip, e)

    # ---- 闸 2：邮箱冷却（Redis 前置盾）----
    try:
        if not check_email_cooldown(email):
            logger.info("mood 提交命中邮箱冷却 | email_hash=%.8s…", hashlib.sha256(email.encode()).hexdigest())
            return {"ok": False, "reason": "duplicate"}
    except Exception as e:  # noqa: BLE001
        logger.warning("mood 邮箱冷却检查失败，放行 | err=%s", e)

    # ---- 闸 3：库内已有同邮箱 → 永久重复（p4.2；冷却过期也拦）----
    try:
        if email_exists_in_db(email):
            logger.info("mood 提交命中库内去重 | email_hash=%.8s…", hashlib.sha256(email.encode()).hexdigest())
            return {"ok": False, "reason": "duplicate"}
    except Exception as e:  # noqa: BLE001
        logger.warning("mood 库内去重检查失败，放行 | err=%s", e)

    # ---- 落库 ----
    with session_scope() as session:
        result = session.execute(
            MOOD.insert().values(
                email=email,
                mood=mood,
                mood_word=_mood_word(mood),
                page_url=referer,
                ip=ip,
                user_agent=user_agent,
                is_read=False,
                created_at=now_db(),
            )
        )
        new_id = result.inserted_primary_key[0]

    logger.info("mood 提交入库 | id=%s mood=%s word=%s", new_id, mood, _mood_word(mood))
    return {"ok": True, "id": int(new_id)}


@router.get(
    "/admin",
    include_in_schema=False,
    summary="查看提交记录（token 保护，不进 /docs）",
)
def mood_admin(token: str = "", limit: int = 100) -> dict[str, Any]:
    """
    查看最近提交。与 internal.py 同一条铁律：**不进 OpenAPI 文档** ——
    虽然有 token 守着，但把「查看个人邮箱数据」的入口写进公开说明书，
    等于给探测者指路。

    返回体刻意**不带 ip 列**：存它是为了限流与排查，不是给人看的，
    admin 页面没有展示它的正当理由。
    """
    expected = settings.MOOD_ADMIN_TOKEN
    provided = token or ""
    if not expected:
        # 与内部接口同一哲学：缺配置 = 拒绝一切（fail-closed），不是「不校验」。
        logger.error("MOOD_ADMIN_TOKEN 为空，mood/admin 一律拒绝（配置异常）")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="查看功能未启用",
        )
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        logger.warning("mood/admin 鉴权失败（token 不匹配）")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="查看口令不正确",
        )

    limit = max(1, min(int(limit), 500))
    with session_scope() as session:
        rows = session.execute(
            select(
                MOOD.c.id,
                MOOD.c.email,
                MOOD.c.mood,
                MOOD.c.mood_word,
                MOOD.c.page_url,
                MOOD.c.user_agent,
                MOOD.c.is_read,
                MOOD.c.created_at,
            )
            .order_by(desc(MOOD.c.created_at), desc(MOOD.c.id))
            .limit(limit)
        ).mappings().all()
        total = session.execute(
            select(func.count()).select_from(MOOD)
        ).scalar()

    items = []
    for row in rows:
        created = row["created_at"]
        items.append({
            "id": int(row["id"]),
            "email": row["email"],
            "mood": float(row["mood"]),
            "mood_word": row["mood_word"],
            "page_url": row["page_url"],
            "user_agent": row["user_agent"],
            "is_read": bool(row["is_read"]),
            "created_at": created.isoformat(timespec="seconds") if created else None,
        })
    return {"total": int(total or 0), "returned": len(items), "items": items}
