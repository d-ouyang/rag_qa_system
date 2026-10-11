"""
月度用量快照（P3-6）—— 归档审计用，**判定路径不读这里**。

--------------------------------------------------------------------------
它解决什么问题
--------------------------------------------------------------------------
P3-1 的拦截上线后，「这个月用了多少」从看板数字变成了**执法依据**。
而唯一的证据源 `chat_message.usage_*` 寄居在聊天记录里 ——
将来清理 / 归档聊天记录（chat_message 是全库增长最快的表，几乎必然会发生），
用量历史就跟着没了，届时「他上个月到底超没超」死无对证。

本模块把「每账期每人用了多少」定期抄进 `quota_usage_monthly`，只增不删。

--------------------------------------------------------------------------
为什么数值来源是 usage_by_user() 本身，而不是自己再写一段聚合 SQL
--------------------------------------------------------------------------
`quota_repo` 里那段「两段聚合」CTE 是被 bug 教出来的（2026-10-07），
它的 docstring 里有完整的验尸报告。本模块如果为了「快照更简单」再写一段
看似等价的 SUM，等于把那颗雷重新埋一遍 —— 而且埋在「平时没人看」的
归档表里，炸得更晚、更难发现。

直接调 `usage_by_user()`：快照与实时聚合在写入瞬间**逐位相等**，
「连跑两日快照与 usage_by_user 对账一致」这条验收从「要证明」变成「恒成立」。
判定路径（预检 / 看板）也仍走同一份函数 —— 全项目只有一份用量口径。

--------------------------------------------------------------------------
只增不删，怎么保证
--------------------------------------------------------------------------
1. 本模块**没有**任何 DELETE（测试会扫源码钉住这一点）；
2. 写入路径只存在「当前账期」一种：`snapshot_period()` 不接受指定账期的参数，
   「把历史账期覆盖掉」在函数签名上就不成立。
   （要修正历史，走人工 SQL 并留下记录，不该是例行任务的权限。）
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from core.db import get_engine, now_db
from core.quota_repo import period_bounds, usage_by_user
from core.schema import quota_usage_monthly_table

logger = logging.getLogger(__name__)

#: 当前账期标签（'YYYY-MM'）。账期口径与 quota_repo.period_bounds 一致：
#: start_day=1 的自然月 —— 横幅、看板、拦截、快照四处同一把尺子。


def current_period(when: date | None = None) -> str:
    start, _ = period_bounds(when, start_day=1)
    return f"{start.year:04d}-{start.month:02d}"


def snapshot_period() -> dict[str, Any]:
    """
    聚合并覆盖写**今天所在账期**的快照。

    ⚠️ 刻意**不接受**「指定某个账期」的参数：写入路径只存在「当前账期」一种，
    「把历史账期覆盖掉」在函数签名上就不成立（要修正历史走人工 SQL 并留记录，
    不该是例行任务的权限）。要测 past 行为，就造当前账期的用量数据。

    :return: {"period", "rows", "input_tokens", "output_tokens", "requests"}
             供 Celery 任务写日志（任务结果不进 result backend，真相在表里）。
    """
    period = current_period()
    buckets = usage_by_user(start_day=1)

    written = 0
    totals = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    if buckets:
        stmt = mysql_insert(quota_usage_monthly_table).values([
            {
                "user_id": b.user_id,
                "period": period,
                "input_tokens": int(b.input_tokens),
                "output_tokens": int(b.output_tokens),
                "requests": int(b.requests),
                "updated_at": now_db(),
            }
            for b in buckets
        ])
        stmt = stmt.on_duplicate_key_update(
            input_tokens=stmt.inserted.input_tokens,
            output_tokens=stmt.inserted.output_tokens,
            requests=stmt.inserted.requests,
            updated_at=stmt.inserted.updated_at,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        written = len(buckets)
        for b in buckets:
            totals["input_tokens"] += int(b.input_tokens)
            totals["output_tokens"] += int(b.output_tokens)
            totals["requests"] += int(b.requests)

    logger.info(
        "月度用量快照完成 | 账期=%s 行数=%d 输入=%d 输出=%d 请求数=%d",
        period, written, totals["input_tokens"], totals["output_tokens"], totals["requests"],
    )
    return {"period": period, "rows": written, **totals}


def snapshot_rows(period: str | None = None) -> list[dict[str, Any]]:
    """读快照（对账 / 未来归档查询用）。不传 period = 全部账期。"""
    sql = ("SELECT user_id, period, input_tokens, output_tokens, requests, updated_at "
           "FROM quota_usage_monthly")
    params: dict[str, Any] = {}
    if period is not None:
        sql += " WHERE period = :period"
        params["period"] = period
    sql += " ORDER BY period, user_id"
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


def verify_against_usage() -> dict[str, Any]:
    """
    对账：快照表 vs `usage_by_user()` 实时聚合，逐人逐列比（当前账期）。

    ⚠️ 只对**当前账期** —— 历史账期的明细可能已被清理，
    「快照 != 实时」在那里是预期而非错误（那正是快照存在的意义）。

    :return: {"ok", "period", "compared", "mismatches": [...]}
    """
    period = current_period()
    live = {b.user_id: b for b in usage_by_user(start_day=1)}
    snap = {int(r["user_id"]): r for r in snapshot_rows(period)}

    mismatches: list[dict[str, Any]] = []
    for user_id in sorted(set(live) | set(snap)):
        b = live.get(user_id)
        row = snap.get(user_id)
        live_vals = (
            int(b.input_tokens) if b else 0,
            int(b.output_tokens) if b else 0,
            int(b.requests) if b else 0,
        )
        snap_vals = (
            int(row["input_tokens"]) if row else 0,
            int(row["output_tokens"]) if row else 0,
            int(row["requests"]) if row else 0,
        )
        # 一方缺失按 0 处理：明细全被清掉后实时聚合为空、快照还在 —— 那不算错
        if live_vals != snap_vals:
            mismatches.append({
                "user_id": user_id,
                "snapshot": dict(zip(("input", "output", "requests"), snap_vals)),
                "live": dict(zip(("input", "output", "requests"), live_vals)),
            })
    return {
        "ok": not mismatches,
        "period": period,
        "compared": len(set(live) | set(snap)),
        "mismatches": mismatches,
    }


__all__ = ["current_period", "snapshot_period", "snapshot_rows", "verify_against_usage"]


# --------------------------------------------------------------------------- #
# 手动执行入口：python -m core.quota_snapshot（make snapshot-usage 也走这里）
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json

    from config.logging_config import setup_logging

    setup_logging()
    result = snapshot_period()
    print(json.dumps(result, ensure_ascii=False))
    check = verify_against_usage()
    if check["ok"]:
        print(f"对账：✅ 一致（账期={check['period']}，比对 {check['compared']} 人）")
    else:
        print(f"对账：❌ {len(check['mismatches'])} 处不一致")
        for m in check["mismatches"]:
            print(f"   user_id={m['user_id']} snapshot={m['snapshot']} live={m['live']}")
        raise SystemExit(1)
