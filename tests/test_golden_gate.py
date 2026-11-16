"""golden set 回归门禁测试（v4.1.0-p4.1 / Phase ④1）。

判据：基线文件自身必须过门禁（阈值一致性）；低于阈值的指标被拦截（退出码 1）；
缺失指标按失败处理。脚本主链路走 subprocess（真实退出码）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_golden_gate.py"
RESULTS = ROOT / "data" / "eval" / "results"
BASELINE = ROOT / "data" / "eval" / "gate_baseline.json"
GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"


def _run(result: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(GATE), "--result", str(result)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def test_baseline_file_passes_own_gate():
    """判据自洽：门禁基线取自 fixed-final，它自身必须通过。"""
    code, out = _run(RESULTS / "20261014-235751-fixed-final.json")
    assert code == 0, out
    assert "✅ 门禁通过" in out


def test_stale_result_blocked():
    """红灯路径：旧 baseline 结果（recall@5 0.8944/hit@5 0.9667）被拦。"""
    code, out = _run(RESULTS / "20261012-001334-baseline.json")
    assert code == 1
    assert "❌ 门禁失败" in out and "recall@5" in out


def test_missing_metric_counts_as_fail():
    """结果缺指标（如手工裁剪的 JSON）→ 门禁失败而非静默通过。"""
    import tempfile as tf

    tmp_path = Path(tf.mkdtemp())
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps({
        "meta": {"tag": "fake"},
        "metrics": {"recall@5": 0.95},  # 缺 hit@5/mrr@10/abstain
    }, ensure_ascii=False))
    code, out = _run(fake)
    assert code == 1 and "缺失" in out


def test_baseline_thresholds_present():
    """基线文件结构完整（阈值键齐全 + golden.jsonl 在位）。"""
    b = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(b["thresholds"]) >= {"recall@5", "hit@5", "mrr@10", "abstain_accuracy"}
    assert GOLDEN.exists()
