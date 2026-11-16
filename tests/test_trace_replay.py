"""事件流回放测试（v4.1.0-p3.1 / Phase ③1）。

判据：给定 query_id 回放「路由→改写→检索→重排→生成→校验」全链路——
投影只重组不编造（未发生的阶段标 skipped）；usage 归因三列在生成步；
audit 合规投影取 owner 最近事件。
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
  intent_source VARCHAR(20) DEFAULT '',
  question TEXT NOT NULL,
  standalone_question TEXT,
  rewritten_query TEXT,
  vector_query TEXT,
  candidates JSON,
  threshold DOUBLE,
  top1_before_filter DOUBLE,
  filtered_count INTEGER,
  docs_returned INTEGER NOT NULL DEFAULT 0,
  sources_count INTEGER NOT NULL DEFAULT 0,
  bm25_hits INTEGER,
  bm25_only BOOLEAN,
  rrf_top DOUBLE,
  hyde_used BOOLEAN,
  usage_input_tokens BIGINT,
  usage_output_tokens BIGINT,
  usage_cache_read_tokens BIGINT,
  grounded BOOLEAN,
  unsupported TEXT,
  rejected BOOLEAN NOT NULL DEFAULT 0,
  elapsed_ms INTEGER,
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


def _rag_trace() -> dict:
    return {
        "decision": "rag_qa", "intent": "orders", "route": "rag_qa",
        "intent_source": "rule", "question": "统计订单",
        "standalone_question": "统计全部订单总量",
        "candidates": [{"source": "f1.md", "score": 0.9}, {"source": "f2.md", "score": 0.7}],
        "threshold": 0.3, "top1_before_filter": 0.9, "filtered_count": 0,
        "docs_returned": 2, "elapsed_ms": 1200,
        "usage_input_tokens": 600, "usage_output_tokens": 400,
        "usage_cache_read_tokens": 50, "grounded": True, "unsupported": [],
    }


def test_get_trace_by_query_id(trace_engine):
    qid = qa_trace_repo.record(session_id="s-1", owner_id=1, trace=_rag_trace())
    row = qa_trace_repo.get_trace(qid)
    assert row and row["question"] == "统计订单" and row["usage_input_tokens"] == 600


def test_get_trace_missing_returns_none(trace_engine):
    assert qa_trace_repo.get_trace(999999) is None


def test_timeline_full_rag_path(trace_engine):
    """判据主用例：rag_qa 轮的时间线覆盖全六阶段。"""
    qid = qa_trace_repo.record(session_id="s-1", owner_id=1, trace=_rag_trace())
    row = qa_trace_repo.get_trace(qid)
    stages = [s["stage"] for s in qa_trace_repo.replay_timeline(row)]
    assert stages == ["route", "rewrite", "retrieve", "rerank", "generate", "verify"]
    by = {s["stage"]: s for s in qa_trace_repo.replay_timeline(row)}
    assert by["rerank"]["top1_before_filter"] == 0.9
    assert by["generate"]["usage_input_tokens"] == 600  # p2.3 归因列进生成步
    assert by["verify"]["grounded"] is True


def test_timeline_cache_hit_marks_skipped(trace_engine):
    """投影不编造：缓存命中轮的检索/重排/校验标 skipped 并给原因。"""
    trace = {"decision": "cache_hit", "intent": "orders", "route": "rag_qa",
             "question": "统计订单", "docs_returned": 1,
             "usage_input_tokens": 0, "usage_output_tokens": 0, "usage_cache_read_tokens": 0}
    qid = qa_trace_repo.record(session_id="s-1", owner_id=1, trace=trace)
    row = qa_trace_repo.get_trace(qid)
    stages = {s["stage"]: s for s in qa_trace_repo.replay_timeline(row)}
    assert stages["retrieve"]["status"] == "skipped"
    assert stages["rerank"]["status"] == "skipped"
    assert stages["rewrite"]["status"] == "skipped"  # 未改写（不编造）
    assert stages["generate"]["usage_input_tokens"] == 0  # 0 token 也投影


def test_timeline_zero_recall(trace_engine):
    trace = {"decision": "zero_recall", "intent": "orders", "route": "rag_qa",
             "question": "查不到的东西", "docs_returned": 0, "candidates": []}
    qid = qa_trace_repo.record(session_id="s-1", owner_id=1, trace=trace)
    row = qa_trace_repo.get_trace(qid)
    stages = {s["stage"]: s for s in qa_trace_repo.replay_timeline(row)}
    assert stages["retrieve"]["status"] == "skipped"
    assert "零召回" in stages["retrieve"]["why"]


def test_audit_projection_query_shape(trace_engine):
    """audit 合规投影的查询形态：owner 最近事件（在 audit_log 最小表上验证 SQL 语义）。"""
    eng = trace_engine
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "actor_user_id INTEGER, action VARCHAR(48), target_type VARCHAR(24), "
            "target_id VARCHAR(64), created_at DATETIME)"
        ))
        c.execute(text(
            "INSERT INTO audit_log (actor_user_id, action, target_type) VALUES "
            "(2, 'user.login', 'user'), (2, 'user.token_quota.change', 'user'), "
            "(3, 'user.login', 'user')"
        ))
    from core.schema import audit_log_table

    stmt = (
        sa.select(audit_log_table.c.id, audit_log_table.c.action)
        .where(audit_log_table.c.actor_user_id == 2)
        .order_by(audit_log_table.c.id.desc())
        .limit(5)
    )
    rows = eng.connect().execute(stmt).mappings().all()
    assert [r["action"] for r in rows] == ["user.token_quota.change", "user.login"]  # owner=2 的两条，最新在前
