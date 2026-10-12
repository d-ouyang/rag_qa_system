"""
Token 额度的**判定规则唯一处**（P2-15 · 15b/15c）。

与 `core/kb_acl.py` / `core/password_policy.py` 同构：**纯函数、不起库**，
`token_quota_monthly` 与已用量由调用方传进来。

--------------------------------------------------------------------------
为什么必须是唯一处（这是本仓库反复付过学费的地方）
--------------------------------------------------------------------------
11c 因为「登录判定在网关和后端各写一份」（`bcryptjs` vs `passlib`）返工；
14a 立项时把「判定规则要有单一出处」写进了 `kb_acl.py` 的文件头；
14f 又在 `set_role`（精确匹配）与 `set_kb_role`（宽松）之间踩了一次规格不一致。

所以这里刻意把**三件事**分开，且各只有一处：
    · `core/quota_policy.py` —— 判定（纯函数，本文）
    · `core/quota_repo.py`    —— 取数与聚合（SQL）
    · `core/admin_service.py` —— 编排 + 审计（写操作收口处）
路由层**不判任何额度**，与 `api/routes/documents.py` 的 `Depends` 形态一致。

--------------------------------------------------------------------------
本项的口径（先定死，否则后面全是来回改）
--------------------------------------------------------------------------
**按人 × 自然月**，三个数**分开报**：

    input       计费输入 token
    output      计费输出 token
    cache_read  服务商侧 prompt 缓存命中

⚠️ `cache_read` **不计入 total**。它的单价与普通输入不同（通常更便宜），
混进总额会算错钱 —— 而「算错钱」比「算得粗」严重得多：
它会让「谁超了」这个判断本身失真。

⚠️ 统计**只能从明细表 `chat_message` 出发**（join `session` 取 `user_id`），
不能只查 `session` 的汇总列 —— 会话被删就丢了，
而明细是历史真相（`session` 的汇总列是给「会话页显示」用的，不是给对账用的）。

--------------------------------------------------------------------------
🔴 本项**不做**什么（用户 2026-10-07 拍板）—— ⚠️ 已于 2026-10-11 被 P3-1 推翻
--------------------------------------------------------------------------
> 「超出 token 预警，不要做真的阻断或者禁用，只做提醒和看板。」

**2026-10-11 推翻（PLAN v3.0.0 §P3-1，用户拍板）**：系统已于 2026-10-09
对外上线（公网可访问），「只提醒」的前提是「用超了也没什么后果」——
而在对外服务上，用超 = 真实的钱与真实的滥用面（脚本刷量无人能挡）。
所以现在有 `can_ask()`，且有 `block` 档。

推翻的代价必须记账：原来那条「刻意缺席」的理由（「有了函数就会长出禁用按钮」）
在**内部**系统里成立，在**对外**系统里不成立 —— 同样是「长出按钮」，
在内部是误伤，在对外是止血。判据因此也变了：
    · 内部口径要回答「降级到哪个模型 / 谁承担超额」；
    · 对外口径只回答一件事：**本月已用 >= 限量 → 拒**，
      不降级、不预估单轮成本（宽限单语义天然覆盖「最后一问用超」）。

⏸ **仍然不做**的（本次没有一并放开，别顺手加）：
    · 单轮 token 预估（复杂度不值，见 `can_ask` 注释）；
    · 降级到便宜模型（那是另一张模型映射表 + 一轮新决策）；
    · 并发精确计量（同用户并发提问可能双双过预检、实际略超 —— 内部系统，
      写进 P3-1 迭代文档风险节即可）。
"""
from __future__ import annotations

from typing import Any, Literal

# --------------------------------------------------------------------------- #
# 状态档位
# --------------------------------------------------------------------------- #
#: 正常。含「额度为 0（不限）」这一情形 —— 那时永远走这一档。
STATUS_OK = "ok"
#: 接近上限（默认 ≥80%）。**只提示，不阻断。**
STATUS_WARN = "warn"
#: 已超额（默认 ≥100%）。**仍然只提示。**
STATUS_OVER = "over"

QuotaStatus = Literal["ok", "warn", "over"]

#: 状态的中文标签。管理端看板与主应用横幅都用它 ——
#: 两处各写一遍中文又是「两份清单」（§文件头那条教训）。
STATUS_LABELS: dict[str, str] = {
    STATUS_OK: "正常",
    STATUS_WARN: "接近上限",
    STATUS_OVER: "已超额",
}

#: 看板配色用的语义色。**刻意不给出「危险」这一档的名字** ——
#: 已超额在本项的定义里不是「出事了」，只是「用超了，需要有人看一眼」。
STATUS_COLORS: dict[str, str] = {
    STATUS_OK: "ok",       # 绿
    STATUS_WARN: "warn",   # 黄
    STATUS_OVER: "over",   # 红
}

# --------------------------------------------------------------------------- #
# 口径
# --------------------------------------------------------------------------- #
#: 计费总额 = input + output。
#:
#: ⚠️ **不含 `cache_read`**，理由见文件头。
#: ⚠️ 也没有「价格 → 金额」的换算：不同模型单价不同，
#:   换算表会随服务商调价而失效，而**额度本身就是 token 数**，
#:   不需要折成钱才能判断「谁超了」。真要算钱时应当另开一张
#:   `TOKEN_MODEL_PRICING` 表（计划书 §15c 提到过），
#:   而**不是**在这个函数里硬编码系数。
BILLABLE_FIELDS = ("input", "output")

# --------------------------------------------------------------------------- #
# 超额行为档位（P2-15c 立 · P3-1 扩）
# --------------------------------------------------------------------------- #
#: 仅提醒：横幅 + 看板标红，**不拦任何提问**（2026-10-07 的原拍板）。
OVER_ACTION_NOTIFY = "notify"
#: 阻断：本月已用 ≥ 限量时，提问接口直接 429（2026-10-11 拍板，PLAN v3.0.0 P3-1）。
OVER_ACTION_BLOCK = "block"

#: ⚠️ 2026-10-07 时这里只有 `notify` 一档，且注释明说「另两档连空钩子都不留」。
#:    2026-10-11 系统对外上线后补上 `block`（理由见文件头「推翻」那段）。
#:    仍然**没有**「降级」那一档：它要先回答「降级到哪个模型」，
#:    那是一张模型映射表 + 一轮新决策，不是在这里多写一个字符串。
OVER_ACTIONS = (OVER_ACTION_NOTIFY, OVER_ACTION_BLOCK)

#: 阻断时错误体里的稳定错误码（前端据此区分「额度用完」与「服务出错」）。
QUOTA_EXCEEDED_CODE = "quota_exceeded"

#: 429 错误体里 `message` 的文案。**不含数字**（数字在 `used` / `quota` 字段里）。
#:
#: ⚠️ 文案里那句「请联系管理员调整」不是客套：拦截上线后，
#:    员工本人**没有任何自助出口**（额度只能管理员改），
#:    不写清这一点，他只会反复重试 —— 而重试永远返回同一个 429。
BLOCK_MESSAGE = "本月 token 额度已用完，请联系管理员调整。"


def validate_over_action(value: str) -> str:
    """
    校验超额行为档位配置。**非法值抛 ValueError**（启动时调用 → 进程拒启）。

    为什么 fail-closed 而不是「不认识的值就当 notify」：
    把 "block" 静默当 "notify" 的后果是「管理员以为配了阻断，
    实际什么都没发生」—— 在对外服务上这就是成本失控；
    把 "notify" 静默当 "block" 则会突然切断正用着的人。
    两种静默都不可接受，所以宁可启动失败。
    """
    if value not in OVER_ACTIONS:
        raise ValueError(
            f"TOKEN_QUOTA_OVER_ACTION 只能是 {list(OVER_ACTIONS)}，"
            f"当前是 {value!r}。"
            f"notify = 只提醒不阻断（2026-10-07 拍板）；"
            f"block = 超额后提问接口返回 429（2026-10-11 拍板，PLAN v3.0.0 P3-1）。"
            "「降级」档位尚未实现 —— 判据要重新设计（PLAN §15c）。"
        )
    return value


def block_message(used: Any = 0, quota: Any = 0) -> str:
    """
    被拦截时给用户看的那句话。**纯文案函数，不含数字**（数字在错误体的
    `used` / `quota` 字段里，前端自己拼「已用 X / 限量 Y」）。

    ⚠️ 为什么数字不进这句话：`BLOCK_MESSAGE` 是常量，进了数字就变成模板，
    而模板迟早会被某个调用方拼错（「已用 / 限量」顺序颠倒这种错，
    看一眼很像对的）。数字归数字、文案归文案，各自只有一处。
    """
    return BLOCK_MESSAGE


def can_ask(used: Any, quota: Any) -> tuple[bool, str]:
    """
    **提问预检判据**（P3-1）。纯函数 —— 与 `status_of` 同一形状（不起库不读配置）。

        额度 ≤ 0（不限）        → (True, "")
        已用 < 限量             → (True, "")
        已用 ≥ 限量             → (False, BLOCK_MESSAGE)

    ⚠️ **不做单轮 token 预估**。理由（这是被刻意否掉的方案）：
    预估要先知道「这一轮会花多少」，而它取决于检索命中几条、
    历史带几轮、模型回多长 —— 全是运行时才知道的。硬估一个常数
    只会让「还差 200 token 就到线」的人被拦，而那个人其实问得出。
    「宽限单」语义天然覆盖了「最后一问用超」：提问时没超就放行，
    这一轮照常答完、照常计费，超了体现在**下一问**被拦。

    ⚠️ 返回的是 `(bool, str)` 而不是 bool：
    调用方要一句能直接下发给用户的话，让它自己拼 = 文案第二处漂移点。
    放行时第二个元素是空串（不是 None）—— 调用方 `if msg:` 即可，
    不用处理三态。
    """
    q = _to_int(quota)
    if q <= 0:
        return True, ""
    u = _to_int(used)
    if u < q:
        return True, ""
    return False, BLOCK_MESSAGE



def billable(input_tokens: Any, output_tokens: Any, cache_read_tokens: Any = 0) -> int:
    """计费 token 合计（input + output，**不含 cache_read**）。

    `cache_read_tokens` 收下只是为了签名完整与将来可能的分列展示 ——
    刻意**不用**它参与求和。若将来某天决定「cache_read 也计入」，
    那是一个**口径变更**，必须改这里并让所有依赖它的判据一起变，
    而不是让调用方自己决定加不加。
    """
    return _to_int(input_tokens) + _to_int(output_tokens)


def usage_percent(used: Any, quota: Any) -> float | None:
    """
    使用率（0~N，**不是** 0~1）。

    返回 `None` = **无额度**（额度为 0 / 负数 / 不是数字）。

    ⚠️ 刻意返回 `None` 而不是 `0.0` 或 `float('inf')`：
    「无额度」与「额度是 0」是两件不同的事 ——
    前者是「不管他」，后者是「他一个 token 都不能用」。
    混成一个 0 会让「不限额度的人」显示成「用量 0%」，
    而看板上「0%」与「不限」的行应该长得不一样（看板据此不画进度条）。
    """
    q = _to_int(quota)
    if q <= 0:
        return None
    return _to_int(used) * 100.0 / q


def status_of(
    used: Any,
    quota: Any,
    *,
    warn_percent: int = 80,
    over_percent: int = 100,
) -> str:
    """
    这个人此刻处于哪一档。**纯函数 —— 阈值由调用方传入**。

    ⚠️ 阈值不读 `settings`：本模块不起库也不读配置，
    这样它的测试不需要任何 fixture（与 `kb_acl.can()` 同一形状）。
    读配置的那一层是 `core/admin_service.py`。

    ⚠️ **判定顺序是 over → warn → ok**（从高到低），
    而不是反过来：反过来写的话，`over_percent < warn_percent`
    这种配置就会静默退化成「永远只会是 ok」，而且不报错。
    从高到低则无论阈值怎么配，最高档永远先命中。
    """
    pct = usage_percent(used, quota)
    if pct is None:
        return STATUS_OK
    if pct >= over_percent:
        return STATUS_OVER
    if pct >= warn_percent:
        return STATUS_WARN
    return STATUS_OK


def to_quota_int(value: Any) -> int:
    """
    把任意输入宽松转成额度整数（**永不抛**，非法值当 0 = 不限）。

    ⚠️ 暴露成公开函数而不是让调用方用私有的 `_to_int` ——
      写路径（`admin_service.set_token_quota`）需要同一套转换，
      而跨模块调 `_to_int` 是把「私有的」约定直接破坏掉。

    ❌ 否掉的方案：写路径自己 `int(value)`。
      那会让「字符串 `'500'` 能写进去、`'abc'` 抛 ValueError 变500」
      —— 同一件事两种待遇，而前端那侧正好会传字符串。
    """
    return _to_int(value)


def effective_quota(quota_override: Any, default_quota: Any) -> int:
    """
    生效额度 = 个人覆盖值，缺省时用全局默认。

    ⚠️ 「缺省」的判据是 **≤ 0**（而不是 `None`）——
    库里存的是 `BIGINT NOT NULL DEFAULT 0`，
    而 `.env` 里的 `TOKEN_QUOTA_DEFAULT_MONTHLY` 默认也是 0。
    两边都用 0 表示「不限」，于是这一条判据就够了；
    如果一边用 `NULL` 一边用 0，就得写 `if x is None or x <= 0` ——
    而那种双判据的函数总有一边会漏（14f 的 `set_role` vs `set_kb_role`）。
    """
    over = _to_int(quota_override)
    return over if over > 0 else _to_int(default_quota)


def describe(status: str) -> str:
    """给用户看的档位名。**不含具体百分比**（那属于用量看板，不属于一句话提示）。"""
    return STATUS_LABELS.get(status, STATUS_LABELS[STATUS_OK])


def banner_text(
    used: Any,
    quota: Any,
    *,
    status: str | None = None,
    remaining: int | None = None,
    over_action: str = OVER_ACTION_NOTIFY,
) -> str:
    """
    主应用顶部横幅的文案。

    ⚠️ **只描述事实，不劝阻、不威胁**。
    也不要出现「请节约使用」这类说教 —— 它对行为没有影响，
    只会让人对这条横幅脱敏，于是真超了也没人看。

    P3-1：`over_action` 只影响 **over 档**的最后一句：
        · `notify`（默认）→ 「本系统只做提醒，不会限制你继续使用。」（原话不动）
        · `block`          → 「超出后无法继续提问，请联系管理员调整。」
    为什么只改 over 那一句：warn 档的人**确实**还能继续问，
    告诉他「超出后无法提问」是把「将来的事」说成「现在的事」，
    那句话会立刻被他自己证伪（他还能问）—— 被证伪一次，
    真到 over 那句他也不信了。

    ⚠️ 默认参数刻意是 `notify`：**不传这个参数的调用方拿到的文案
    与 P3-1 之前逐字一致**，只有明确传了 block 的那一处会变。
    """
    q = _to_int(quota)
    if q <= 0:
        return ""
    u = _to_int(used)
    st = status or status_of(u, q)
    if st == STATUS_OK:
        return ""
    pct = usage_percent(u, q) or 0.0
    if remaining is None:
        remaining = max(0, q - u)
    verb = "已用完本月额度" if st == STATUS_OVER else "接近本月额度上限"
    tail = (
        "超出后无法继续提问，请联系管理员调整。"
        if (st == STATUS_OVER and over_action == OVER_ACTION_BLOCK)
        else "本系统只做提醒，不会限制你继续使用。"
    )
    return (
        f"{verb}：已用 {u:,} / {q:,} tokens（{pct:.0f}%），"
        f"剩余 {remaining:,}。{tail}"
    )


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _to_int(value: Any) -> int:
    """
    宽松转 int：**永不抛**。

    为什么宽松：这里的入参来自 `SUM()`（可能返回 `Decimal`）、
    JSON 提取（可能返回 `None`）与 `.env`（可能是字符串）。
    判定规则里抛异常的话，**一个脏数据会让整个看板 500**，
    而看板挂了没人会去查是哪个人的哪个字段不对。

    ⚠️ 与 `kb_acl` 同一取舍，但**方向相反**且各有理由：
    `kb_acl.is_canonical_kb_role()` 刻意严格（写路径的输入来自下拉框，
    宽松会静默改写别人的权限）；这里宽松（读路径的输入来自聚合查询，
    严格会让一条脏数据把整页炸掉）。
    """
    if value is None:
        return 0
    if isinstance(value, bool):  # bool 是 int 的子类，但当数字用是 bug
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


__all__ = [
    "BILLABLE_FIELDS",
    "BLOCK_MESSAGE",
    "OVER_ACTIONS",
    "OVER_ACTION_BLOCK",
    "OVER_ACTION_NOTIFY",
    "QUOTA_EXCEEDED_CODE",
    "QuotaStatus",
    "STATUS_COLORS",
    "STATUS_LABELS",
    "STATUS_OK",
    "STATUS_OVER",
    "STATUS_WARN",
    "banner_text",
    "billable",
    "block_message",
    "can_ask",
    "describe",
    "effective_quota",
    "to_quota_int",
    "validate_over_action",
    "status_of",
    "usage_percent",
]
