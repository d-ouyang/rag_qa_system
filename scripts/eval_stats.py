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
                "SELECT turn_meta, create_time FROM chat_message "
                "WHERE role = 'assistant' AND turn_meta IS NOT NULL "
                "AND create_time >= :since"
            ),
            {"since": since},
        ).all()

    prepare: list[float] = []
    generate: list[float] = []
    grounded_ms: list[float] = []
    total: list[float] = []
    decisions: Counter[str] = Counter()
    grounded_counts: Counter[str] = Counter()
    dual = fallback = slow = 0

    for row in rows:
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

    lines = [
        f"# 问答耗时/质量统计（最近 {args.days} 天）",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 统计轮次：{len(rows)}",
        "",
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
