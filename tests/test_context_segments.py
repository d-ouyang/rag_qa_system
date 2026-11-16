"""context budget 分段注册表测试（v4.1.0-p4.2 / Phase ④2）。

判据：各段预算可枚举（SEGMENTS 注册表）、可配（旗标热调）、摘要注入有硬兜底。
"""
from __future__ import annotations

import pytest

from core.context_budget import SEGMENTS, describe_budgets, get_segment_limit


def test_segments_registry_covers_all_injection_points():
    """prompt 三大注入段全部登记：检索资料 / 会话摘要 / 记忆轮数。"""
    assert set(SEGMENTS) == {"retrieval", "summary", "memory_turns"}
    for seg in describe_budgets():
        assert seg["flag"] and seg["desc"]


def test_get_segment_limit_unknown_raises():
    """未登记的段直接 KeyError——那是代码 bug，不能静默放行。"""
    with pytest.raises(KeyError):
        get_segment_limit("nope")


def test_get_segment_limit_reads_flag(monkeypatch):
    """旗标热调生效（mock flag_value 返回配置值）。"""
    import core.context_budget as cb

    monkeypatch.setattr(cb, "get_segment_limit", lambda name: {"summary": 800}.get(name, 0))
    assert cb.get_segment_limit("summary") == 800


def test_summary_hard_truncation(monkeypatch):
    """判据主用例：摘要超预算被硬截断（LLM 侧 300 字软提示的兜底）。"""
    from core.context_budget import budget_blocks

    long_summary = "很长的摘要" * 500  # 2500 字
    res = budget_blocks([long_summary], 1200)
    assert len(res.text) <= 1200
    assert res.text.endswith("…（超长截断）")


def test_summary_zero_disables_budget():
    from core.context_budget import budget_blocks

    res = budget_blocks(["摘要" * 1000], 0)
    assert res.text == "摘要" * 1000  # 0 = 不限
