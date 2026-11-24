"""零召回话术分化 + 依据校验问句回声过滤（v4.1.0-p4.5）。

两处修的都是「说错话」而不是「答错内容」：

1. 零召回统一回「请先向知识库录入相关资料」——把「我没有这个数据」说成
   「资料没录」。用户会去找管理员补文档，补了也没用。
2. 依据校验把**用户的问题原句**列成「无依据论断」——每问一次挂一条误报。
   误报多了，用户会学会无视整个校验标记，比漏报更有害。

两处都用确定性规则实现，所以都能单测；这也是选规则不选 prompt 的原因。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.groundedness import drop_question_echo  # noqa: E402
from core.rag_chain import _NO_CONTEXT_ANSWER, _no_context_answer  # noqa: E402


# --------------------------------------------------------------------------- #
# A. 零召回话术：三种来源分开说
# --------------------------------------------------------------------------- #
def test_personal_data_question():
    a = _no_context_answer("我这个月一共报销了多少钱？")
    assert "个人业务数据" in a
    # 必须给出下一步去哪查，否则等于只说「不知道」
    assert "OA" in a or "财务" in a


def test_personal_data_balance_question():
    a = _no_context_answer("帮我算一下我今年还剩几天年假")
    assert "个人业务数据" in a


def test_action_request():
    a = _no_context_answer("帮我订一张明天去北京的机票")
    assert "做不到" in a
    # 代办被拒时要给出替代动作，不能只是拒绝
    assert "流程" in a


def test_policy_question_stays_generic():
    """最危险的误分类：制度题被当成个人数据，用户就永远看不到答案。"""
    for q in ("年假有几天？", "员工年假怎么计算", "报销标准是多少"):
        a = _no_context_answer(q)
        assert "个人业务数据" not in a, q
        assert "做不到" not in a, q
        assert a == _NO_CONTEXT_ANSWER, q


def test_out_of_scope_falls_back_to_generic():
    a = _no_context_answer("今天上海天气怎么样？")
    assert a == _NO_CONTEXT_ANSWER


def test_empty_question_does_not_crash():
    assert _no_context_answer("") == _NO_CONTEXT_ANSWER


# --------------------------------------------------------------------------- #
# B. 依据校验：剔掉「问题回声」
# --------------------------------------------------------------------------- #
Q = "员工使用公司账号，有哪些明令禁止的行为？"


def test_drops_exact_question():
    assert drop_question_echo([Q, "禁止弱密码"], Q) == ["禁止弱密码"]


def test_drops_question_with_punctuation_difference():
    """判官常把问题末尾的标点换掉再抄一遍。"""
    assert drop_question_echo(["员工使用公司账号,有哪些明令禁止的行为"], Q) == []


def test_drops_when_unsupported_is_part_of_question():
    assert drop_question_echo(["有哪些明令禁止的行为"], Q) == []


def test_keeps_real_unsupported_claim():
    claim = "连续输错 3 次密码锁定 24 小时"
    assert drop_question_echo([claim], Q) == [claim]


def test_short_strings_are_not_dropped():
    """太短的串包含关系太多，宁可留着也不能误杀。"""
    assert drop_question_echo(["年假"], "年假有几天？") == ["年假"]


def test_empty_question_keeps_everything():
    assert drop_question_echo(["任意论断"], "") == ["任意论断"]


def test_drops_blank_items():
    assert drop_question_echo(["  ", Q], Q) == []


# --------------------------------------------------------------------------- #
# B'. 集成：判官说 false 但只举了问题本身 → 应改判通过
# --------------------------------------------------------------------------- #
class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def invoke(self, _messages):  # noqa: ANN001
        return _FakeMessage(self._payload)


class _FakeClient:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def get_llm(self):
        return _FakeLLM(self._payload)


def _patch_judge(monkeypatch, payload: str) -> None:
    import core.rag_chain as rc

    monkeypatch.setattr(rc, "get_llm_client", lambda **kwargs: _FakeClient(payload))


def test_groundedness_turns_pass_when_only_echo(monkeypatch):
    from core.groundedness import check_groundedness

    _patch_judge(monkeypatch, '{"grounded": false, "unsupported": ["%s"]}' % Q)
    got = check_groundedness(question=Q, context="禁止弱密码。", answer="禁止弱密码。")
    assert got["grounded"] is True
    assert got["unsupported"] == []


def test_groundedness_fails_on_real_claim(monkeypatch):
    from core.groundedness import check_groundedness

    claim = "连续输错 3 次密码锁定 24 小时"
    _patch_judge(monkeypatch, '{"grounded": false, "unsupported": ["%s"]}' % claim)
    got = check_groundedness(question=Q, context="禁止弱密码。", answer=claim)
    assert got["grounded"] is False
    assert got["unsupported"] == [claim]
