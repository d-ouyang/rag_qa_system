"""知识库口径冲突台账（v4.1.0-p4.5）。

台账是人工维护的事实源，这里只测「机器侧不添乱」：
命中要准、提示要能读懂、没命中要字节级不变、台账坏了不能挡回答。
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.kb_conflicts import (  # noqa: E402
    append_conflict_notice,
    format_conflict_notice,
    load_conflicts,
    match_conflicts,
)

A = "内部管理制度/08-补充制度/22_差旅管理制度与出差申请单.md"
B = "内部管理制度/04-审批流程/12_费用报销管理制度.md"

_TMP = ROOT / ".eval_tmp" / f"kbconf-{uuid.uuid4().hex}"


def _write_ledger(conflicts: list[dict]) -> Path:
    _TMP.mkdir(parents=True, exist_ok=True)
    path = _TMP / "kb_conflicts.json"
    path.write_text(json.dumps({"version": 1, "conflicts": conflicts}, ensure_ascii=False), encoding="utf-8")
    return path


def _cf(status: str = "open") -> list[dict]:
    return [{
        "id": "cf-t",
        "topic": "出差报销的提交时限",
        "status": status,
        "sides": [
            {"file": A, "ref": "GQ-AD-2026-006 第四条", "value": "返回后 15 个工作日"},
            {"file": B, "ref": "GQ-FI-2026-002 第四条", "value": "费用发生后 30 个工作日"},
        ],
        "owner": "财务 / 行政",
    }]


def setup_function() -> None:
    _TMP.mkdir(parents=True, exist_ok=True)


def teardown_function() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


def test_load_only_open():
    path = _write_ledger(_cf("resolved"))
    assert load_conflicts(path) == []


def test_load_open_items():
    path = _write_ledger(_cf("open"))
    assert len(load_conflicts(path)) == 1


def test_match_one_side_is_enough():
    """cf-001 的实况：模型只捞到差旅制度那一片。只命中一侧必须也提示。"""
    hits = match_conflicts([{"file_name": A}], _cf("open"))
    assert [h["id"] for h in hits] == ["cf-t"]


def test_match_by_basename():
    hits = match_conflicts([{"file_name": "22_差旅管理制度与出差申请单.md"}], _cf("open"))
    assert len(hits) == 1


def test_match_irrelevant_source():
    hits = match_conflicts([{"file_name": "内部管理制度/02-考勤假期/05_考勤管理制度.md"}], _cf("open"))
    assert hits == []


def test_match_dedupes_when_both_sides_hit():
    hits = match_conflicts([{"file_name": A}, {"file_name": B}], _cf("open"))
    assert len(hits) == 1


def test_notice_contains_both_values():
    text = format_conflict_notice(_cf("open"))
    assert "15 个工作日" in text and "30 个工作日" in text
    assert "知识库口径冲突" in text


def test_no_hit_leaves_answer_untouched():
    answer = "出差报销须在 15 个工作日内提交。"
    assert append_conflict_notice(answer, [{"file_name": "无关.md"}]) == answer


def test_hit_appends_notice():
    answer = "出差报销须在 15 个工作日内提交。"
    got = append_conflict_notice(answer, [{"file_name": A}])
    assert got.startswith(answer)
    assert "知识库口径冲突" in got


def test_broken_ledger_is_fail_open():
    _TMP.mkdir(parents=True, exist_ok=True)
    path = _TMP / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_conflicts(path) == []


def test_missing_ledger_is_fail_open():
    assert load_conflicts(_TMP / "nope.json") == []
