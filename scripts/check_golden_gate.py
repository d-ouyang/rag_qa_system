#!/usr/bin/env python3
"""golden set 回归门禁（v4.1.0-p4.1 / Phase ④1）——指标不达基线即失败。

判据直击「改切分 = 整库重建」的高危场景：切分参数/Prompt/模型改动后，
跑 make eval-retrieval TAG=<新tag> 产出指标 JSON，再跑本脚本对照
data/eval/gate_baseline.json 的阈值——任一指标跌破阈值 → 退出码 1。

用法：
  make golden-gate RESULT=data/eval/results/<stamp>-<tag>.json
  .venv/bin/python scripts/check_golden_gate.py --result <path> [--strict]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "data" / "eval" / "gate_baseline.json"
RESULTS = ROOT / "data" / "eval" / "results"

#: `eval_retrieval.py` 的结果文件命名，如 `20261104-195312-fixed-final.json`。
#: 用它把「检索评测」与同目录下的 timing-stats / answer_guard 等产物区分开。
_RESULT_NAME = re.compile(r"^\d{8}-\d{6}-.+\.json$")

#: 阈值键 → 结果 JSON 的 metrics 取值路径（abstain 嵌套）
_READERS = {
    "recall@5": lambda m: m.get("recall@5"),
    "recall@10": lambda m: m.get("recall@10"),
    "hit@5": lambda m: m.get("hit@5"),
    "mrr@10": lambda m: m.get("mrr@10"),
    "abstain_accuracy": lambda m: (m.get("abstain") or {}).get("accuracy"),
}


def _latest_result() -> Path:
    """自动取最新一份**检索评测**结果（`YYYYMMDD-HHMMSS-tag.json` 命名，按时间戳排序）。

    为什么不能只排除 `gate*`（p4.2g 修正）：`data/eval/results/` 里同时躺着
    `timing-stats-*.json`（每日 04:10 的耗时快照，没有 metrics）和
    `answer_guard-*.json`（答案守卫，也不是检索指标）。旧实现按「最新文件」
    取，于是「每天跑过一次耗时统计之后，make golden-gate 必然假红」——
    报的是「recall@5 缺失」，看起来像检索出了大问题，实际只是抓错了文件。
    门禁的红必须指向真问题，否则它会被当成噪音绕过。

    只认 `eval_retrieval.py` 产出的时间戳命名，是最小且确定的判据。
    """
    files = sorted(
        f for f in RESULTS.glob("*.json") if _RESULT_NAME.match(f.name)
    )
    if not files:
        raise SystemExit(
            "data/eval/results/ 下没有检索评测结果（YYYYMMDD-HHMMSS-tag.json）"
            "——先跑 make eval-retrieval"
        )
    return files[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="golden set 回归门禁")
    ap.add_argument("--result", default="", help="评测结果 JSON 路径（缺省=最新一份）")
    ap.add_argument("--strict", action="store_true", help="严格模式：允许值必须 ≥ 基线原文（无容差）")
    args = ap.parse_args()

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    thresholds = dict(baseline["thresholds"])
    result_path = Path(args.result) if args.result else _latest_result()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metrics = result.get("metrics") or {}
    tag = (result.get("meta") or {}).get("tag", result_path.stem)

    print(f"门禁基线：{baseline['baseline_tag']}（{baseline['baseline_date']}）")
    print(f"待检结果：{result_path.name}（tag={tag}）")
    print("-" * 64)

    failed: list[str] = []
    for key, reader in _READERS.items():
        if key not in thresholds:
            continue
        limit = float(thresholds[key])
        actual = reader(metrics)
        if actual is None:
            failed.append(f"{key}：结果里缺该指标")
            print(f"  {key:<18} 缺失                            阈值≥{limit}")
            continue
        actual = float(actual)
        ok = actual >= limit
        if not ok:
            failed.append(f"{key}：{actual:.4f} < 阈值 {limit}")
        print(f"  {key:<18} {actual:.4f} {'✅' if ok else '❌ 低于'}阈值 {limit}")

    print("-" * 64)
    if failed:
        print(f"❌ 门禁失败（{len(failed)} 项不达标）：")
        for f in failed:
            print(f"  · {f}")
        print("本次改动禁止合并/部署。若是有意为之的口径变更，先更新 gate_baseline.json 并说明原因。")
        return 1
    print("✅ 门禁通过：全部指标不低于基线阈值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
