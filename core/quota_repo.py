"""
Token 用量的**取数与聚合**（P2-15 · 15a）。

只做 SQL，不做判定（判定在 `core/quota_policy.py`）、不做编排（在
`core/admin_service.py`）—— 与 `core/kb_acl.py` / `core/user_repo.py` 的分工一致。

--------------------------------------------------------------------------
🔴 口径（本模块存在的全部理由就是把它定死）
--------------------------------------------------------------------------
**按人 × 自然月**，来源是 `chat_message` 明细 join `session` 取 `user_id`。

⚠️ **为什么不能只查 `session` 的汇总列**（`usage_input_token` 等四列）：

    会话被删 → 汇总行一起没了 → 「他这个月用得少」

而 `chat_message` 是**历史真相**。计划书 §15a 原文也是这个意思
（「会话被删就丢了，而明细是历史真相」）。

⚠️ **代价，说清楚**：明细的 `usage_cache_read_token` **没有独立列** ——
`chat_message` 只有 `usage_input_token` / `usage_output_token` 两列，
cache_read 藏在 `turn_meta` 这个 JSON 里的 `usage.cache_read_tokens`。
所以：

    input / output  → 直接 SUM 两列（快，能走索引）
    cache_read      → 要么 SUM(session 的汇总列)，要么 JSON 提取

本模块**默认走 `session` 汇总列**取 cache_read（快，且不必碰 JSON），
并在返回值里**显式带上它的来源标记** `cache_read_source`。
看板与横幅都读这个标记 —— 将来若有人改成 JSON 提取，
那个标记会变，而**看板上「这个数从哪来」是必须能被看见的事实**：
两个口径的数字不一样，而它们都叫「cache_read」。

--------------------------------------------------------------------------
⚠️ 一个已知的口径边界：「查不到归属」有**两种**
--------------------------------------------------------------------------
`session.user_id` 允许为 NULL（12c 迁移里就是可空的），
但**实测发现第三种情况才是真正的坑**（2026-10-07）：

    session.user_id = 657，6894 个 input token，
    而 `user` 表里已经没有 657 这一行

于是只查 `user_id IS NULL` 的「未归属」会返回 **0**，
而对账恒等式**不成立**（全库 11926 ≠ 按人 5032 + 未归属 0）。

所以 `unattributed_total()` 的口径是「`user_id` 为空**或**指向一个不存在的用户」，
并且**分别计数**。两者的运维含义不同：
`orphan`（主人被删）正常情况下应为 0 —— `user` 表刻意不删行、只置 `resigned`，
所以它出现就说明有人**手工 SQL 删过**；`null`（压根没主人）是另一种情况。

这个函数存在的意义始终是**让漏算变得可见**：
看板可以对账，`tests/test_module16_quota.py` 会断言
`按人合计 + 未归属 == 总计`。一个不允许被发现的漏算，
比一个会报警的漏算危险得多 —— 后者会有人去查，前者不会。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text

from core.db import get_engine

# --------------------------------------------------------------------------- #
# 期间
# --------------------------------------------------------------------------- #


def period_bounds(when: date | None = None, *, start_day: int = 1) -> tuple[date, date]:
    """
    「自然月」的起止日期（含首含尾，返回闭区间）。

    ⚠️ `start_day` 是**闭区间起始日**（1 = 当月 1 号）。
    留成参数是为了将来「按结算周期」时不用改代码
    （`settings.TOKEN_QUOTA_PERIOD_START_DAY`）。

    ⚠️ 返回 `date` 而不是 `datetime`：本模块的 SQL 一律用
    `create_time >= 期初 00:00:00 AND create_time < 次期 00:00:00`
    的**左闭右开**写法。

    理由（实测过，不是设想的）：本库所有时间列都是秒级 `datetime`
    （`chat_message` / `session` / `user` …，`information_schema.COLUMNS`
    查的 `COLUMN_TYPE` 全是 `datetime`，没有 `datetime(6)`），
    所以「漏掉微秒」在**今天**并不是真实故障 ——
    真正的理由是**区间拼接**：

    左闭右开让「本月」与「下月」首尾相接、无缝无重叠，
    于是「按任意月份区间求和」永远等于「逐月求和之和」。
    而 `<= 本月最后一秒23:59:59` 这种写法，每个区间都要自己算一遍
    「最后一刻」，一旦某张表的精度是 `DATETIME(6)`，
    就得写 `23:59:59.999999` —— 少写一位就是**静默漏掉最后那一秒**，
    而且它不会报错，只会让某个月的汇总少一点，
    于是「按区间求和 ≠ 逐月求和之和」，而这种偏差没人会去查。

    ⚠️ 换言之：左闭右开不是洁癖，是为了让**将来**有人把某张表改成
    `DATETIME(6)` 时，这个模块不需要跟着改。
    """
    d = when or date.today()
    start = date(d.year, d.month, start_day)
    if start_day == 1:
        end_next = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    else:
        # start_day > 1 时「本月起始」可能落在下个月，取下下个月的同日
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        end_next = date(y, m, start_day)
    end = date.fromordinal(end_next.toordinal() - 1)  # 次期前一日 = 本期最后一日
    return start, end


# --------------------------------------------------------------------------- #
# 聚合结果
# --------------------------------------------------------------------------- #
@dataclass
class UsageBucket:
    """一个人的某期间用量。字段与看板列一一对应。"""

    user_id: int
    username: str
    display_name: str
    department_id: int | None = None
    department_name: str | None = None
    quota_override: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    #: cache_read 的来源标记：`session_summary` | `chat_message_json`。
    #: 存在的理由见文件头「两个口径都叫 cache_read」那段。
    cache_read_source: str = "session_summary"
    #: 该期间内的**会话数**（区分「用了 100 次」与「一次问了 100 轮」）
    session_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "department_id": self.department_id,
            "department_name": self.department_name,
            "quota_override": self.quota_override,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_read_source": self.cache_read_source,
            "requests": self.requests,
            "session_count": self.session_count,
        }


#: cache_read 的两种取数口径（见文件头）
CACHE_READ_FROM_SESSION = "session_summary"
CACHE_READ_FROM_JSON = "chat_message_json"


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
#: 期间筛选的 SQL 片段与参数。
#:
#: ⚠️ **左闭右开**（`< 次期`）而不是 `<= 本期最后一秒` —— 理由见
#: `period_bounds` 的 docstring（一句话：让「本月」与「下月」首尾相接，
#: 将来某张表改成 `DATETIME(6)` 时这个模块不用跟着改）。
_PERIOD_PREDICATE = "m.create_time >= :start AND m.create_time < :end_exclusive"

#: 计数用哪个时间列：`chat_message.create_time`（消息产生的时间），
#: **不是** `session.create_time`。一个上个月开的会话、这个月聊了 10 次，
#: 那 10 次就属于**这个月**。用 session 的时间会把它们全算到上个月。
_PERIOD_FROM = """
FROM chat_message m
JOIN session s ON s.id = m.session_id
"""


def usage_by_user(
    *,
    when: date | None = None,
    start_day: int = 1,
    user_id: int | None = None,
    department_id: int | None = None,
) -> list[UsageBucket]:
    """
    按人聚合某期间的用量。**没有用量的员工不出现**（返回列表里不会有 0 行）。

    ⚠️ 「没有用量就不出现」是刻意的：看板上只列**本月动过的人**。
    列出 200 个 0 用量的员工会让真正超了的那个人淹没在列表里 ——
    而看板的作用就是「找出该关注的人」。

    要看全量（含 0 用量）用 `usage_board()`，它左连接 `user`。
    """
    start, end = period_bounds(when, start_day=start_day)
    end_exclusive = date.fromordinal(end.toordinal() + 1)

    # 注意：`user_id` / `department_id` 的过滤作用在**外层**（CTE 之后），
    # 因为 CTE 里只有 `session` 与 `chat_message`，还没有 `user` 表可join。
    where: list[str] = []
    params: dict[str, Any] = {"start": str(start), "end_exclusive": str(end_exclusive)}
    if user_id is not None:
        where.append("sess.user_id = :user_id")
        params["user_id"] = user_id
    if department_id is not None:
        where.append("u.department_id = :department_id")
        params["department_id"] = department_id
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # --------------------------------------------------------------------------
    # 🔴 为什么必须是「两段聚合」（这一段是被 bug 教出来的，2026-10-07）
    # --------------------------------------------------------------------------
    # 第一版写成「子查询按 `s2.id` 分组再 join 进来」：
    #
    #     LEFT JOIN (SELECT s2.id sid, SUM(s2.usage_cache_read_token) ...
    #                FROM session s2 GROUP BY s2.id) su ON su.sid = s.id
    #     ... COALESCE(SUM(su.cache_read), 0)
    #
    # 看起来「已经先聚合了」，但 **`session.id` 是主键**，
    # 按它分组 = 每会话一行 = **跟没聚合一样**。
    # 外层 `SUM(su.cache_read)` 仍然在**消息粒度**上把该会话的 cache_read
    # 加了「消息条数」次：探针会话 777 tokens / 3 条消息 → 算成 2331（=777×3）。
    #
    # ⚠️ 而**当时那条断言是绿的**—— 因为库里唯一 cache_read 非零的会话
    # 恰好是那条 orphan（主人已被删），而 orphan 根本不在看板里，
    # 于是判据「在当前数据下无从验起」。改成造临时样本才立刻暴露。
    #
    # --------------------------------------------------------------------------
    # ❌ 否掉的方案：`SUM(DISTINCT s.usage_cache_read_token)`
    #    「不重复」看起来成立，但**两个会话恰好同值时会漏掉一个**
    #    （A=777、B=777 → DISTINCT 只剩 777）。而「两人用量相同」
    #    在 0 用量、小用量、默认额度这些场景里非常常见 ——
    #    用「看起来对」换「一定错」，不划算。
    #
    # ✅ 采用：CTE 先把**消息按会话收敛成一行**，外层再 `SUM`。
    #    收敛之后每会话只出现一次，既不重复、也不受同值影响。
    # --------------------------------------------------------------------------
    sql = f"""
        WITH sess AS (
            -- 第一段：把**期间内的消息**按会话收敛成一行。
            -- 收敛之后每个会话只有一行，下面接汇总列时才不会重复计数。
            SELECT s.id AS sid,
                   s.user_id AS user_id,
                   COALESCE(SUM(m.usage_input_token), 0)  AS input_tokens,
                   COALESCE(SUM(m.usage_output_token), 0) AS output_tokens,
                   -- 🔴 只数 assistant：user 消息不是一次请求
                   --   （口径与 `usage_by_session` 一致，理由见那里）
                   COALESCE(SUM(m.role = 'assistant'), 0)   AS requests,
                   -- 汇总列本身是「整会话累计」的，取一次即可（不能 SUM）
                   COALESCE(s.usage_cache_read_token, 0)  AS cache_read
            FROM chat_message m
            JOIN session s ON s.id = m.session_id
            WHERE {_PERIOD_PREDICATE}
            GROUP BY s.id, s.user_id
        )
        SELECT
            u.id                  AS user_id,
            u.username            AS username,
            u.display_name        AS display_name,
            u.department_id       AS department_id,
            d.name                AS department_name,
            u.token_quota_monthly AS quota_override,
            COALESCE(SUM(sess.input_tokens), 0)  AS input_tokens,
            COALESCE(SUM(sess.output_tokens), 0) AS output_tokens,
            COALESCE(SUM(sess.cache_read), 0)    AS cache_read_tokens,
            COALESCE(SUM(sess.requests), 0)      AS requests,
            COUNT(*)                             AS session_count
        FROM sess
        JOIN `user` u ON u.id = sess.user_id
        LEFT JOIN department d ON d.id = u.department_id
        {where_sql}
        GROUP BY u.id, u.username, u.display_name, u.department_id,
                 d.name, u.token_quota_monthly
        -- ⚠️ ORDER BY 必须写**聚合表达式本身**，不能写 SELECT 的别名
        --   `input_tokens + output_tokens`：那些别名是聚合结果，
        --   在 `only_full_group_by` 下不被认可（MySQL 1055）。
        ORDER BY (COALESCE(SUM(sess.input_tokens), 0)
                + COALESCE(SUM(sess.output_tokens), 0)) DESC, u.id
    """
    return [_bucket_from_row(r) for r in _query(sql, params)]


def usage_board(
    *,
    when: date | None = None,
    start_day: int = 1,
    include_zero: bool = True,
) -> list[UsageBucket]:
    """
    看板数据：**所有在职员工**（`include_zero=True` 时含本月 0 用量的人）。

    与 `usage_by_user` 的区别就是左连接 vs 内连接。两者并存是有意的 ——
    「本月谁用了」与「本月谁没用」是两个不同的问题（前者对账，后者找人）。
    """
    start, end = period_bounds(when, start_day=start_day)
    end_exclusive = date.fromordinal(end.toordinal() + 1)
    join_type = "LEFT JOIN" if include_zero else "JOIN"

    # ⚠️ 这里的 `sess` CTE 与 `usage_by_user` 里那个是**同一段逻辑**，
    #   重复是有意的代价：把两段抽成一个公共常量看着省事，
    #   但 CTE 是整段 SQL 内联的字符串，抽出来反而要处理 f-string 拼接，
    #   而两处的过滤条件不同（这里要 `user_id IS NOT NULL`，那边不过滤）。
    #   真要合并，应当合并的是「按会话收敛」这个**概念**，落到
    #   `tests/test_module16_quota.py` 里断言两个函数口径一致。
    sql = f"""
        WITH sess AS (
            -- 🔴 第一段：消息按**会话**收敛成一行（原因见 `usage_by_user`
            #   上方那段注释：按 `session.id` 分组等于没分组）。
            SELECT s.id AS sid,
                   s.user_id AS user_id,
                   COALESCE(SUM(m.usage_input_token), 0)  AS input_tokens,
                   COALESCE(SUM(m.usage_output_token), 0) AS output_tokens,
                   -- 🔴 只数 assistant（口径与 `usage_by_session` 一致）
                   COALESCE(SUM(m.role = 'assistant'), 0)   AS requests,
                   COALESCE(s.usage_cache_read_token, 0)  AS cache_read
            FROM chat_message m
            JOIN session s ON s.id = m.session_id
            WHERE {_PERIOD_PREDICATE} AND s.user_id IS NOT NULL
            GROUP BY s.id, s.user_id
        )
        SELECT
            u.id                  AS user_id,
            u.username            AS username,
            u.display_name        AS display_name,
            u.department_id       AS department_id,
            d.name                AS department_name,
            u.token_quota_monthly AS quota_override,
            COALESCE(agg.input_tokens, 0)  AS input_tokens,
            COALESCE(agg.output_tokens, 0) AS output_tokens,
            COALESCE(agg.cache_read, 0)    AS cache_read_tokens,
            COALESCE(agg.requests, 0)      AS requests,
            COALESCE(agg.session_count, 0) AS session_count
        FROM `user` u
        LEFT JOIN department d ON d.id = u.department_id
        {join_type} (
            SELECT sess.user_id AS uid,
                   COALESCE(SUM(sess.input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(sess.output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(sess.cache_read), 0)    AS cache_read,
                   COALESCE(SUM(sess.requests), 0)      AS requests,
                   COUNT(*)                             AS session_count
            FROM sess
            GROUP BY sess.user_id
        ) agg ON agg.uid = u.id
        WHERE u.status = 'active'
        ORDER BY (COALESCE(agg.input_tokens, 0) + COALESCE(agg.output_tokens, 0)) DESC, u.id
    """
    params = {"start": str(start), "end_exclusive": str(end_exclusive)}
    return [_bucket_from_row(r) for r in _query(sql, params)]


def usage_by_session(session_id: str) -> dict[str, int]:
    """
    单个会话的用量（给会话页显示用，与 `session` 表的汇总列对账用）。

    ⚠️ 这一条**故意从明细算**，而不是直接读 `session` 的汇总列 ——
    因为它是**对账**用的：两处数字不一样时，说明「汇总列漏了更新」或
    「明细被删了」，而那正是需要被发现的异常。
    如果它只是把汇总列换个地方读一遍，那它就永远等于自己，什么也验不出来。

    --------------------------------------------------------------------------
    🔴 `requests` 的口径 = **assistant 消息数**（= 一次模型调用 = 一次请求）
    --------------------------------------------------------------------------
    user 消息不是一次请求。而 `usage_by_user` / `usage_board` 早期版本用
    `COUNT(*)`（含 user 消息），实测同一个探针会话两边给出 3 与 2 ——
    「同名字段两个口径」是本仓库反复付过学费的形状（11c / 14a / 14f）。

    所以口径定为 assistant 消息数，三个函数全部照它。
    ⚠️ 注意它与 `session.usage_requests` 汇总列**未必相等** ——
    那个列是应用层写的，历史上不一定等于 assistant 消息数。
    这正是本函数存在的意义：两处不一样时能看出来。
    """
    sql = f"""
        SELECT
            COALESCE(SUM(m.usage_input_token), 0)  AS input_tokens,
            COALESCE(SUM(m.usage_output_token), 0) AS output_tokens,
            COUNT(*)                               AS requests
        FROM chat_message m
        WHERE m.session_id = :sid AND m.role = 'assistant'
    """
    rows = _query(sql, {"sid": session_id})
    if not rows:
        return {"input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "requests": 0}
    r = rows[0]
    # cache_read 只能从 session 汇总列拿（明细没有独立列，理由见文件头）
    # ⚠️ 必须带别名：`__getitem__` 走列名（`_query` 返回的是 dict 列表）。
    #   写成 `SELECT COALESCE(...)` 不取别名时，返回的 dict 用的是整段表达式当键，
    #   而本函数原来按下标 `r2[0][0]` 取 → KeyError: 0。
    #   也就是说这条路径在真实调用下**从来没跑通过**（测试当时没跑到它）。
    r2 = _query(
        "SELECT COALESCE(usage_cache_read_token, 0) AS cache_read "
        "FROM session WHERE id = :sid",
        {"sid": session_id},
    )
    return {
        "input_tokens": int(r["input_tokens"]),
        "output_tokens": int(r["output_tokens"]),
        "cache_read_tokens": int(r2[0]["cache_read"]) if r2 else 0,
        "requests": int(r["requests"]),
    }


def unattributed_total(
    *, when: date | None = None, start_day: int = 1
) -> dict[str, int]:
    """
    **查不到归属**的用量。

    ⚠️ 这个函数存在的唯一理由是**让漏算可见**（文件头那段话）。
    看板与测试都断言 `按人合计 + 本函数 == 全库总计`。
    少了它，一个静默的漏算可以潜伏很久而没人发现 ——
    而「用量统计漏了某个人」这类偏差，靠人眼是永远查不出来的。

    --------------------------------------------------------------------------
    🔴 「查不到归属」有**两种**，第一种是第二种的特例
    --------------------------------------------------------------------------
    2026-10-07 实测踩到：库里当时有 `session.user_id = 657`、
    6894 个 input token，而 `user` 表里**已经没有 657 这一行**
    （那是我前一天手工删掉的测试残留 `ding.ouyang`）。

    于是 `unattributed_total()` 最初只查 `user_id IS NULL` 时**返回 0**，
    而对账恒等式**不成立**：

        全库总计 11926  ≠  按人合计 5032  +  未归属 0

    差额 6894 就是它。第一版把这个当成「聚合写错了」去查 SQL，
    查了半天才发现聚合是对的、**是「未归属」的定义漏了一种**。

    所以这里的口径是「`user_id` 为空**或**指向一个不存在的用户」，
    并且**分别计数**（`orphan` 与 `null`）——
    两者的运维含义不同：前者是「有人离职/被删了，历史用量还挂着」
    （`user` 表刻意不删行、只置 `resigned`，所以正常情况下**不该**出现），
    后者是「这会话压根没有主人」。
    """
    start, end = period_bounds(when, start_day=start_day)
    end_exclusive = date.fromordinal(end.toordinal() + 1)
    sql = f"""
        SELECT
            COALESCE(SUM(m.usage_input_token), 0)  AS input_tokens,
            COALESCE(SUM(m.usage_output_token), 0) AS output_tokens,
            COUNT(*)                               AS messages,
            COALESCE(SUM(CASE WHEN s.user_id IS NULL THEN 1 ELSE 0 END), 0) AS null_owner_msgs,
            COALESCE(SUM(CASE WHEN s.user_id IS NULL THEN m.usage_input_token ELSE 0 END), 0) AS null_owner_input,
            COALESCE(SUM(CASE WHEN s.user_id IS NOT NULL THEN m.usage_input_token ELSE 0 END), 0) AS orphan_input
        FROM chat_message m
        JOIN session s ON s.id = m.session_id
        LEFT JOIN `user` u ON u.id = s.user_id
        WHERE {_PERIOD_PREDICATE} AND (s.user_id IS NULL OR u.id IS NULL)
    """
    rows = _query(sql, {"start": str(start), "end_exclusive": str(end_exclusive)})
    if not rows:
        return {"input_tokens": 0, "output_tokens": 0, "messages": 0,
                "null_owner_input": 0, "orphan_input": 0}
    r = rows[0]
    return {
        "input_tokens": int(r["input_tokens"]),
        "output_tokens": int(r["output_tokens"]),
        "messages": int(r["messages"]),
        # 「压根没有主人」的那部分
        "null_owner_input": int(r["null_owner_input"]),
        # 「主人已被删除」的那部分 —— 正常情况下应为 0
        "orphan_input": int(r["orphan_input"]),
    }


def grand_total(*, when: date | None = None, start_day: int = 1) -> dict[str, int]:
    """全库该期间的总用量（**不分人**，含 `user_id IS NULL` 的那些）。

    对账用：`grand_total()` 应等于 `sum(usage_by_user()) + unattributed_total()`。
    """
    start, end = period_bounds(when, start_day=start_day)
    end_exclusive = date.fromordinal(end.toordinal() + 1)
    sql = f"""
        SELECT
            COALESCE(SUM(m.usage_input_token), 0)  AS input_tokens,
            COALESCE(SUM(m.usage_output_token), 0) AS output_tokens,
            COUNT(*)                               AS messages
        FROM chat_message m
        WHERE {_PERIOD_PREDICATE}
    """
    rows = _query(sql, {"start": str(start), "end_exclusive": str(end_exclusive)})
    if not rows:
        return {"input_tokens": 0, "output_tokens": 0, "messages": 0}
    r = rows[0]
    return {
        "input_tokens": int(r["input_tokens"]),
        "output_tokens": int(r["output_tokens"]),
        "messages": int(r["messages"]),
    }


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _query(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


def _bucket_from_row(r: dict[str, Any]) -> UsageBucket:
    return UsageBucket(
        user_id=int(r["user_id"]),
        username=str(r["username"]),
        display_name=str(r["display_name"]),
        department_id=(int(r["department_id"]) if r.get("department_id") is not None else None),
        department_name=(str(r["department_name"]) if r.get("department_name") else None),
        quota_override=int(r.get("quota_override") or 0),
        input_tokens=int(r.get("input_tokens") or 0),
        output_tokens=int(r.get("output_tokens") or 0),
        cache_read_tokens=int(r.get("cache_read_tokens") or 0),
        requests=int(r.get("requests") or 0),
        session_count=int(r.get("session_count") or 0),
        cache_read_source=CACHE_READ_FROM_SESSION,
    )


__all__ = [
    "CACHE_READ_FROM_JSON",
    "CACHE_READ_FROM_SESSION",
    "UsageBucket",
    "grand_total",
    "period_bounds",
    "unattributed_total",
    "usage_board",
    "usage_by_session",
    "usage_by_user",
]
