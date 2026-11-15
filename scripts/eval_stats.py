"""问答耗时/质量统计：从 chat_message.turn_meta 聚合 timings、decision、grounded、dual/fallback。

用法：
    .venv/bin/python scripts/eval_stats.py --days 7
    .venv/bin/python scripts/eval_stats.py --days 1 --out data/eval/results/timing-stats.md

数据来源：
    chat_message.turn_meta（assistant 行）。
    `timings` 是 2026-11-04 新增字段；老轮次没有，会退化用 `elapsed_ms` 计入总计，
    分阶段（检索/改写、生成、依据校验）则只统计有该字段的轮次。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from core.db import session_scope  # noqa: E402


# 会话名前缀 → 数据来源标签。
#
# 为什么需要这个：本机库里绝大多数行是评测脚本写的（verifykbx / evalguard），
# 少量是手工联调（diag-）。只看「统计轮次」会把「没有真人提问」和
# 「评测脚本没跑」混成同一个 0，导致连续多天报 0 却查不出是哪一类断了。
# 所以把来源拆开报：评测归零 = 没跑评测；真实归零 = 没人用。
SOURCE_RULES: tuple[tuple[str, str], ...] = (
    ("evalguard-", "评测(evalguard)"),
    ("verifykbx-", "评测(verifykbx)"),
    ("vfy-", "评测(vfy)"),
    ("diag-", "诊断(diag)"),
)


def source_of(session_id: str | None) -> str:
    """按会话名前缀判定数据来源，识别不出的都算真实/手工流量。"""
    if not session_id:
        return "无会话"
    for prefix, label in SOURCE_RULES:
        if session_id.startswith(prefix):
            return label
    return "真实/手工"


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f} ms" if v < 1000 else f"{v / 1000:.1f} s"


def main() -> int:
    parser = argparse.ArgumentParser(description="问答耗时/质量统计（最近 N 天）")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, args.days))
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT session_id, turn_meta, create_time FROM chat_message "
                "WHERE role = 'assistant' AND turn_meta IS NOT NULL "
                "AND create_time >= :since"
            ),
            {"since": since},
        ).all()
        # 全表最后写入时间：用于在「0 轮」时直接说明距离上次落库过了多久，
        # 免得每次都要人工去 MySQL 里翻一遍才知道是真没人问还是采集坏了。
        last_write = session.execute(
            text("SELECT MAX(create_time) FROM chat_message WHERE role = 'assistant'")
        ).scalar()

    prepare: list[float] = []
    generate: list[float] = []
    grounded_ms: list[float] = []
    total: list[float] = []
    decisions: Counter[str] = Counter()
    grounded_counts: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    dual = fallback = slow = 0

    for row in rows:
        sources[source_of(row.session_id)] += 1
        meta = row.turn_meta
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                continue
        if not isinstance(meta, dict):
            continue

        timings = meta.get("timings")
        if isinstance(timings, dict):
            for key, bucket in (
                ("prepare_ms", prepare),
                ("generate_ms", generate),
                ("grounded_ms", grounded_ms),
                ("total_ms", total),
            ):
                v = timings.get(key)
                if isinstance(v, (int, float)):
                    bucket.append(float(v))
            g = timings.get("generate_ms")
            if isinstance(g, (int, float)) and g >= 60000:
                slow += 1
        if not (isinstance(timings, dict) and timings.get("total_ms")):
            elapsed = meta.get("elapsed_ms")
            if isinstance(elapsed, (int, float)):
                total.append(float(elapsed))

        trace = meta.get("qa_trace")
        if isinstance(trace, dict):
            decisions[str(trace.get("decision") or "unknown")] += 1
            if trace.get("dual_used"):
                dual += 1
            if trace.get("fallback_used"):
                fallback += 1

        grounded = meta.get("grounded")
        if isinstance(grounded, dict):
            grounded_counts[str(grounded.get("grounded"))] += 1
        else:
            grounded_counts["none"] += 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or (ROOT / "data" / "eval" / "results" / f"timing-stats-{stamp}.md")
    out = out if out.is_absolute() else (ROOT / out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 距上次落库多久：0 轮时这是唯一能区分「没人问」和「采集坏了」的线索。
    if last_write is None:
        last_write_line = "- 库内最后写入：无任何记录"
        gap_days = None
    else:
        gap = datetime.now() - last_write
        gap_days = gap.total_seconds() / 86400
        last_write_line = (
            f"- 库内最后写入：{last_write:%Y-%m-%d %H:%M}"
            f"（{gap_days:.1f} 天前）"
        )

    lines = [
        f"# 问答耗时/质量统计（最近 {args.days} 天）",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 统计轮次：{len(rows)}",
        last_write_line,
        "",
    ]

    # 窗口内没有样本时，把「为什么是 0」写清楚。
    # 否则这行 0 会被误读成服务故障，实际上多数情况是根本没人提问
    # ——管理端的看板/审计/登录都不会写 chat_message，不进这个统计。
    if not rows:
        lines += [
            "> ⚠️ 近 "
            f"{args.days} 天无新增问答记录"
            + (f"，最后一条落库在 {gap_days:.1f} 天前" if gap_days is not None else "")
            + "。",
            ">",
            "> 本报表只统计「问答落库」：登录、管理端巡检（看板/qa-trace/用户/审计）"
            "都不会写入 chat_message。",
            "> 因此 0 轮通常意味着**没人提问**，而不是服务或采集故障。"
            "排查时先确认后端是否有 `POST /api/v1/qa/ask` 请求。",
            "",
        ]

    lines += [
        "| 阶段 | p50 | p95 | 最大 | 样本 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, vals in (
        ("检索/改写", prepare),
        ("生成", generate),
        ("依据校验", grounded_ms),
        ("总计", total),
    ):
        lines.append(
            f"| {name} | {fmt(pct(vals, 0.5))} | {fmt(pct(vals, 0.95))} | "
            f"{fmt(max(vals) if vals else None)} | {len(vals)} |"
        )
    lines += [
        "",
        "| 指标 | 数量 |",
        "| --- | --- |",
        f"| grounded=false | {grounded_counts.get('False', 0)} |",
        f"| grounded=true | {grounded_counts.get('True', 0)} |",
        f"| 未校验 | {grounded_counts.get('None', 0) + grounded_counts.get('none', 0)} |",
        f"| dual_used | {dual} |",
        f"| fallback_used | {fallback} |",
        f"| 生成 ≥ 60s（超时重试） | {slow} |",
        "",
        "| 终端状态 | 数量 |",
        "| --- | --- |",
    ]
    for k, v in decisions.most_common():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "| 数据来源 | 轮次 |",
        "| --- | --- |",
    ]
    if sources:
        for k, v in sources.most_common():
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("| — | 0 |")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "days": args.days,
        "rows": len(rows),
        "prepare": {"p50": pct(prepare, 0.5), "p95": pct(prepare, 0.95), "max": max(prepare) if prepare else None, "n": len(prepare)},
        "generate": {"p50": pct(generate, 0.5), "p95": pct(generate, 0.95), "max": max(generate) if generate else None, "n": len(generate)},
        "grounded": {"p50": pct(grounded_ms, 0.5), "p95": pct(grounded_ms, 0.95), "max": max(grounded_ms) if grounded_ms else None, "n": len(grounded_ms)},
        "total": {"p50": pct(total, 0.5), "p95": pct(total, 0.95), "max": max(total) if total else None, "n": len(total)},
        "decisions": dict(decisions),
        "grounded_counts": dict(grounded_counts),
        "source_breakdown": dict(sources),
        "last_write": last_write.isoformat(timespec="seconds") if last_write else None,
        "dual_used": dual,
        "fallback_used": fallback,
        "slow_generate_60s": slow,
    }
    out.with_suffix(".json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n".join(lines))
    print(f"\n统计已写入：{out}")
    print(f"JSON 摘要：{out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
