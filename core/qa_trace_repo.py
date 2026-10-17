"""qa_trace 读写仓 —— 问答流程轨迹的分析镜像（P4-5 二期）。

--------------------------------------------------------------------------
定位
--------------------------------------------------------------------------
每轮轨迹的**展示**真相源是 `chat_message.turn_meta.qa_trace`（P4-5 一期），
本表是同一份数据的查询优化镜像（为什么需要镜像见迁移 0012 头注释）。

    · `record()`       —— 唯一写入点（rag_chain 三处写记忆后各调一次）。
                         **仓库层不吞异常**：DB 挂了就抛，由调用方决定姿态
                         —— 问答链路包一层 fail-open（分析挂了不能挡问答），
                         测试则让它原样炸出来。
    · `list_traces()` / `count_traces()` —— 溯源页的筛选查询，共用一套
                         WHERE（与 audit_repo 同款约定：「筛选后总数」与
                         「筛选后列表」必须来自同一条件，否则页码对不上）。

`min_top1_gap` 是本表存在的意义所在：`threshold - top1_before_filter >= X`
在 JSON 列上写不出来，在数值列上是一行 SQL。语义是「差距**至少** X 分」
—— 找的是「差得远」的拒答；想找「差一点就过」的 near-miss，把 decision
筛成 zero_recall 后看 top1_gap 排序即可（describe() 里已经算好这个字段）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa

from core.db import now_db, session_scope
from core.groundedness import is_low_confidence
from core.qa_flow import flow_path
from core.schema import qa_trace_table

logger = logging.getLogger(__name__)

#: trace dict 的键 → 表列名。trace 由 `_build_qa_trace()` / `_cache_qa_trace()`
#: 产出（P4-5 一期），键集合在这里显式登记：trace 里多出来的键（将来加字段）
#: 静默忽略 —— 镜像表加列是显式迁移的事，不该被上游顺手带进来。
_TRACE_KEYS: tuple[str, ...] = (
    "decision",
    "intent",
    "route",
    "intent_source",
    "question",
    "standalone_question",
    "rewritten_query",
    "vector_query",
    "candidates",
    "threshold",
    "top1_before_filter",
    "filtered_count",
    "docs_returned",
    "sources_count",
    "bm25_hits",
    "bm25_only",
    "rrf_top",
    "hyde_used",
    "elapsed_ms",
    "rejected",
    "grounded",       # P4-6：依据校验结果（bool | None）
    "unsupported",    # P4-6：未支撑论断清单（list[str]）
)


def record(*, session_id: str, owner_id: int | None, trace: dict[str, Any]) -> int:
    """
    镜像一条轨迹。返回自增 id。

    :param trace: `_build_qa_trace()` / `_cache_qa_trace()` 的产物（原样传入，
                  不要求调用方先挑字段）。decision 缺失视为脏数据直接抛 ——
                  一条没有终端状态的轨迹进了分析表就是毒数据。
    """
    if not trace.get("decision"):
        raise ValueError("qa_trace 缺少 decision（终端状态），拒绝镜像")
    values: dict[str, Any] = {
        "session_id": session_id,
        "owner_id": owner_id,
        "created_at": now_db(),
    }
    for key in _TRACE_KEYS:
        if key in trace:
            values[key] = trace[key]
    stmt = qa_trace_table.insert().values(**values)
    with session_scope() as session:
        return int(session.execute(stmt).inserted_primary_key[0])


# --------------------------------------------------------------------------- #
# 查询（只读）
# --------------------------------------------------------------------------- #
def _filters(
    *,
    decision: str | None = None,
    intent: str | None = None,
    session_id: str | None = None,
    keyword: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_top1_gap: float | None = None,
) -> list[Any]:
    """按可空条件拼 WHERE —— 全是 AND，语义与溯源页筛选栏一一对应。"""
    where: list[Any] = []
    if decision:
        where.append(qa_trace_table.c.decision == decision)
    if intent:
        where.append(qa_trace_table.c.intent == intent)
    if session_id:
        # 精确匹配：session_id 是复制粘贴来的随机串，模糊匹配没有意义
        where.append(qa_trace_table.c.session_id == session_id)
    if keyword:
        like = f"%{keyword}%"
        where.append(
            sa.or_(
                qa_trace_table.c.question.like(like),
                qa_trace_table.c.standalone_question.like(like),
            )
        )
    if date_from is not None:
        where.append(qa_trace_table.c.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to is not None:
        # 含当日：上界取「次日零点」的开区间比较做不到，用 <= 23:59:59.999999
        where.append(
            qa_trace_table.c.created_at
            <= datetime.combine(date_to, datetime.max.time())
        )
    if min_top1_gap is not None:
        # 「差多少分」>= X。两列任一为 NULL（没跑检索/没重排）自然排除在结果外。
        where.append(
            sa.and_(
                qa_trace_table.c.threshold.is_not(None),
                qa_trace_table.c.top1_before_filter.is_not(None),
                (qa_trace_table.c.threshold - qa_trace_table.c.top1_before_filter) >= min_top1_gap,
            )
        )
    return where


def list_traces(
    *,
    limit: int = 50,
    offset: int = 0,
    **conditions: Any,
) -> list[dict[str, Any]]:
    """
    按条件取一页，**最新在前**（id 倒序 ≈ 时间倒序，见 id 列注释）。

    :param conditions: decision / intent / session_id / keyword /
                       date_from / date_to / min_top1_gap（全部可空）
    """
    if limit <= 0 or limit > 200:
        raise ValueError("limit 必须在 1~200 之间")
    if offset < 0:
        raise ValueError("offset 不能为负")
    stmt = (
        sa.select(qa_trace_table)
        .where(*_filters(**conditions))
        .order_by(qa_trace_table.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    with session_scope() as session:
        rows = session.execute(stmt).mappings().all()
    return [describe(dict(row)) for row in rows]


def count_traces(**conditions: Any) -> int:
    """总数。与 `list_traces` 共用同一套条件，页码才对得上。"""
    stmt = sa.select(sa.func.count()).select_from(qa_trace_table).where(*_filters(**conditions))
    with session_scope() as session:
        return int(session.execute(stmt).scalar() or 0)


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """
    行 → API 载荷。附带两样派生值：

    · ``flow_path`` —— 实走路径（core/qa_flow.flow_path 派生），溯源页高亮用。
      后端算好下发，前端不自己再推一遍分支。
    · ``top1_gap``  —— 阈值与过滤前最高分的差（拒答轮 = 「差多少分」的证据）；
      任一为 NULL 时也是 NULL（与「没跑检索」的口径一致）。
    """
    out = dict(row)
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat()
    # ⚠️ mysql.DOUBLE 在 SQLAlchemy 里默认 asdecimal=True，读回来是 Decimal。
    # 派生计算必须先转 float：Decimal 参与的差值仍是 Decimal，JSON 序列化后
    # 前端拿到的是 "0.0400" 这种字符串形态的数，且 == 比较对不上 Python float。
    threshold = float(out["threshold"]) if out.get("threshold") is not None else None
    top1 = float(out["top1_before_filter"]) if out.get("top1_before_filter") is not None else None
    out["top1_gap"] = (
        round(threshold - top1, 4)
        if threshold is not None and top1 is not None
        else None
    )
    decision = out.get("decision")
    try:
        out["flow_path"] = flow_path(decision) if decision else []
    except ValueError:
        # 老数据/脏数据的终端状态不在状态机里：给空路径而不是让整页 500
        logger.warning("qa_trace 行的 decision 不在状态机里 | decision=%s | id=%s",
                       decision, out.get("id"))
        out["flow_path"] = []
    # P4-6：低置信徽标从 top1 派生（不落库——top1 已在列上，避免冗余两处不一致）
    out["low_confidence"] = is_low_confidence(out.get("top1_before_filter"))
    return out
