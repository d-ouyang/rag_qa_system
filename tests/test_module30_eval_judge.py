"""答案级语义评测（LLM 裁判）的纯函数测试（v4.1.0-p4.4）。

只测不需要基础设施的那部分：解析、聚合、阈值判定、用例筛选。
真链路与真裁判由 `make eval-judge` 手动跑——这些逻辑的错误一旦进聚合，
表现是「分数看起来正常但结论错了」，比脚本崩掉更难发现。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _temp_dir():
    tmp = ROOT / ".eval_tmp" / uuid.uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

_SPEC = importlib.util.spec_from_file_location("eval_judge", ROOT / "scripts" / "eval_judge.py")
assert _SPEC and _SPEC.loader
eval_judge = importlib.util.module_from_spec(_SPEC)
sys.modules["eval_judge"] = eval_judge
_SPEC.loader.exec_module(eval_judge)


# --------------------------------------------------------------------------- #
# 裁判输出解析
# --------------------------------------------------------------------------- #
def test_parse_plain_json():
    got = eval_judge.parse_judge('{"faithful": true, "score": 4, "reason": "第 2 段有依据"}')
    assert got == {"faithful": True, "score": 4, "reason": "第 2 段有依据"}


def test_parse_fenced_with_chatter():
    """真实模型会包 ```json 围栏、前后加客套话。"""
    text = '好的，我的判断是：\n```json\n{"faithful": false, "score": 1, "reason": "片段未提扣薪"}\n```\n以上。'
    got = eval_judge.parse_judge(text)
    assert got["faithful"] is False
    assert got["score"] == 1


def test_parse_string_score_and_chinese_bool():
    got = eval_judge.parse_judge('{"faithful": "是", "score": "5", "reason": "ok"}')
    assert got["faithful"] is True
    assert got["score"] == 5


def test_parse_rejects_no_json():
    """解析失败必须抛——猜分数比缺一条更危险。"""
    with pytest.raises(ValueError):
        eval_judge.parse_judge("这条答案看起来很忠实。")


def test_parse_rejects_non_bool_faithful():
    with pytest.raises(ValueError):
        eval_judge.parse_judge('{"faithful": "大概吧", "score": 4}')


def test_parse_rejects_out_of_range():
    with pytest.raises(ValueError):
        eval_judge.parse_judge('{"faithful": true, "score": 9}')


# --------------------------------------------------------------------------- #
# 片段拼装
# --------------------------------------------------------------------------- #
def _src(index: int, snippet: str, name: str = "制度.pdf") -> dict:
    return {"index": index, "chunk_id": f"{index}:0", "file_name": name, "snippet": snippet}


def test_build_contexts_has_index_and_chunk_id():
    text = eval_judge.build_contexts([_src(1, "年假 5 天起")])
    assert "[1]" in text
    assert "1:0" in text
    assert "年假 5 天起" in text


def test_build_contexts_skips_blank_snippet():
    text = eval_judge.build_contexts([_src(1, "有效"), _src(2, "   ")])
    assert "有效" in text
    assert "[2]" not in text


def test_build_contexts_caps_count():
    text = eval_judge.build_contexts([_src(i, f"片段{i}") for i in range(1, 11)])
    assert "[6]" in text
    assert "[7]" not in text


# --------------------------------------------------------------------------- #
# 多次裁决聚合
# --------------------------------------------------------------------------- #
def _v(score: int, faithful: bool) -> dict:
    return {"score": score, "faithful": faithful, "reason": ""}


def test_aggregate_median_not_mean():
    """单次抖动不该把结论拉走：3,3,5 的中位数是 3，不是 3.67。"""
    got = eval_judge.aggregate([_v(3, True), _v(3, True), _v(5, True)])
    assert got["score"] == 3
    assert got["faithful"] is True


def test_aggregate_majority_faithful():
    got = eval_judge.aggregate([_v(4, False), _v(4, True), _v(4, True)])
    assert got["faithful"] is True


def test_aggregate_tie_is_conservative():
    """偶数平票判不忠实：门禁宁可误报，不放过一条编造。"""
    got = eval_judge.aggregate([_v(4, True), _v(4, False)])
    assert got["faithful"] is False


def test_aggregate_empty():
    got = eval_judge.aggregate([])
    assert got["score"] is None
    assert got["votes"] == 0


# --------------------------------------------------------------------------- #
# 汇总与阈值
# --------------------------------------------------------------------------- #
def _record(cid: str, score, faithful, error=None) -> dict:
    return {
        "id": cid,
        "error": error,
        "judge": {"score": score, "faithful": faithful, "votes": 3 if score is not None else 0, "reasons": []},
    }


def test_summarize_excludes_unjudged_from_denominator():
    """判分失败的题不能摊进平均分——那会把「没测」算成「考得不错」。"""
    metrics = eval_judge.summarize([
        _record("q1", 5, True),
        _record("q2", 3, True),
        _record("q3", None, None, error="裁判超时"),
    ])
    assert metrics["judged"] == 2
    assert metrics["judge_score_avg"] == 4.0
    assert metrics["faithful_rate"] == 1.0
    assert metrics["errors"] == ["q3"]


def test_summarize_flags_low_score_and_unfaithful():
    metrics = eval_judge.summarize([
        _record("q1", 5, True),
        _record("q2", 2, False),
    ])
    assert metrics["low_score_ids"] == ["q2"]
    assert metrics["unfaithful_ids"] == ["q2"]


def test_gate_pass():
    metrics = {"judged": 2, "errors": [], "judge_score_avg": 4.5, "faithful_rate": 1.0}
    assert eval_judge.evaluate_gate(metrics, min_score=4.0, min_faithful=0.9) == []


def test_gate_fails_on_low_score():
    metrics = {"judged": 2, "errors": [], "judge_score_avg": 3.1, "faithful_rate": 1.0}
    assert eval_judge.evaluate_gate(metrics, min_score=4.0, min_faithful=0.9)


def test_gate_fails_on_low_faithful():
    metrics = {"judged": 2, "errors": [], "judge_score_avg": 4.8, "faithful_rate": 0.8}
    assert eval_judge.evaluate_gate(metrics, min_score=4.0, min_faithful=0.9)


def test_gate_fails_when_any_case_unjudged():
    metrics = {"judged": 2, "errors": ["q3"], "judge_score_avg": 5.0, "faithful_rate": 1.0}
    assert eval_judge.evaluate_gate(metrics, min_score=4.0, min_faithful=0.9)


def test_gate_fails_when_nothing_judged():
    problems = eval_judge.evaluate_gate({"judged": 0, "errors": []}, min_score=4.0, min_faithful=0.9)
    assert problems


# --------------------------------------------------------------------------- #
# 用例筛选
# --------------------------------------------------------------------------- #
def _case_file(tmp: Path, rows: list[dict]) -> Path:
    """在项目内目录造临时用例文件。

    不用 pytest 的 `tmp_path`：系统临时目录在某些沙箱里会被注入钩子，
    建目录时报 FileExistsError，和被测逻辑无关却让用例红。
    """
    path = tmp / "golden.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


def test_load_cases_skips_out_of_scope():
    """外域题的正确行为是拒答，用忠实度量它没有意义。"""
    with _temp_dir() as tmp:
        path = _case_file(tmp, [
            {"id": "q1", "question": "年假几天", "should_answer": True},
            {"id": "q2", "question": "帮我订会议室", "should_answer": False},
        ])
        cases = eval_judge.load_cases(path, case_id=None, limit=None)
    assert [c["id"] for c in cases] == ["q1"]


def test_load_cases_limit():
    with _temp_dir() as tmp:
        path = _case_file(tmp, [{"id": f"q{i}", "question": "问", "should_answer": True} for i in range(5)])
        cases = eval_judge.load_cases(path, case_id=None, limit=3)
    assert len(cases) == 3
