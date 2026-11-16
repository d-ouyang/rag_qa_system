"""usage 归因测试（v4.1.0-p2.3 / Phase ②3）。

判据：给定 qa_trace 一行（query_id=自增 id），即得「谁（owner_id）、哪个会话、
什么流程（decision/intent）、多少 token（usage 三列）」的全量归因。

实现细节：qa_trace 有 mysql 方言列（DOUBLE/DATETIME(fsp=6)），sqlite create_all
建不了——测试手工建**最小表**（只含 record 实际写到的列）；引擎接管走
monkeypatch core.db.get_session_factory。
"""
from __future__ import annotations

import pytest

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import core.db as dbmod
from core import qa_trace_repo

_MIN_DDL = """
CREATE TABLE qa_trace (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id VARCHAR(64) NOT NULL,
  owner_id INTEGER,
  decision VARCHAR(20) NOT NULL,
  intent VARCHAR(40) NOT NULL,
  route VARCHAR(40) NOT NULL,
  question TEXT NOT NULL,
  docs_returned INTEGER NOT NULL DEFAULT 0,
  rejected BOOLEAN NOT NULL DEFAULT 0,
  usage_input_tokens BIGINT,
  usage_output_tokens BIGINT,
  usage_cache_read_tokens BIGINT,
  created_at DATETIME
)
"""


@pytest.fixture()
def trace_engine(monkeypatch):
    eng = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with eng.begin() as c:
        c.execute(text(_MIN_DDL))
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: __import__("sqlalchemy").orm.sessionmaker(bind=eng))
    yield eng


def _minimal_trace(decision: str = "rag_qa") -> dict:
    return {"decision": decision, "intent": "orders", "route": "rag_qa",
            "question": "统计一下订单", "docs_returned": 3}


def test_record_without_usage_leaves_null(trace_engine):
    qa_trace_repo.record(session_id="s-1", owner_id=1, trace=_minimal_trace())
    with trace_engine.connect() as c:
        row = c.execute(text("SELECT owner_id, session_id, usage_input_tokens FROM qa_trace")).mappings().one()
    assert row["owner_id"] == 1 and row["usage_input_tokens"] is None  # 老数据形态


def test_record_with_usage_snapshots_attribution(trace_engine):
    """判据主用例：一行 = 谁/会话/流程/token 全量归因（usage 经 _TRACE_KEYS 透传）。"""
    trace = _minimal_trace()
    trace["usage_input_tokens"] = 600
    trace["usage_output_tokens"] = 400
    trace["usage_cache_read_tokens"] = 50
    qid = qa_trace_repo.record(session_id="s-2", owner_id=2, trace=trace)
    with trace_engine.connect() as c:
        row = c.execute(
            text("SELECT id, owner_id, session_id, decision, usage_input_tokens, "
                 "usage_output_tokens, usage_cache_read_tokens FROM qa_trace WHERE id=:q"),
            {"q": qid},
        ).mappings().one()
    # 归因四要素齐了：query_id(=row id)/user/会话/流程 + token 三列
    assert row["id"] == qid and row["owner_id"] == 2 and row["session_id"] == "s-2"
    assert row["decision"] == "rag_qa"
    assert row["usage_input_tokens"] == 600
    assert row["usage_output_tokens"] == 400
    assert row["usage_cache_read_tokens"] == 50


def test_usage_zero_still_recorded(trace_engine):
    """缓存命中轮 0 token 也要落（归因完整性：0 与 NULL 语义不同）。"""
    trace = _minimal_trace("cache_hit")
    trace["usage_input_tokens"] = 0
    trace["usage_output_tokens"] = 0
    trace["usage_cache_read_tokens"] = 0
    qa_trace_repo.record(session_id="s-3", owner_id=3, trace=trace)
    with trace_engine.connect() as c:
        row = c.execute(text("SELECT usage_input_tokens, decision FROM qa_trace")).mappings().one()
    assert row["usage_input_tokens"] == 0 and row["decision"] == "cache_hit"


def test_trace_repo_ignores_unknown_keys(trace_engine):
    """_TRACE_KEYS 显式登记之外的多余键被静默忽略（防上游顺手带列）。"""
    trace = _minimal_trace()
    trace["unknown_future_key"] = "x"
    trace["usage_input_tokens"] = 42
    qa_trace_repo.record(session_id="s-4", owner_id=4, trace=trace)
    with trace_engine.connect() as c:
        row = c.execute(text("SELECT usage_input_tokens FROM qa_trace")).mappings().one()
    assert row["usage_input_tokens"] == 42  # 登记过的键正常透传
