"""上下文注入预算测试（v4.1.0-p1.2 / Phase ①2 回灌规范）。"""
from types import SimpleNamespace

from core.context_budget import budget_blocks
from core.rag_chain import format_docs


def _doc(content: str, source: str = "员工手册.md", chunk_id: str = "c-1") -> SimpleNamespace:
    return SimpleNamespace(page_content=content, metadata={"source": source, "chunk_id": chunk_id})


def test_budget_keeps_all_within_limit():
    blocks = ["AAA", "BBB", "CCC"]
    res = budget_blocks(blocks, 100)
    assert res.text == "AAA\n\nBBB\n\nCCC" and res.kept == 3 and res.dropped == 0


def test_budget_drops_tail_blocks_order_preserved():
    res = budget_blocks(["A" * 30, "B" * 30, "C" * 30], 70)  # 只装得下前两块
    assert res.text.startswith("A" * 30)
    assert "C" * 30 not in res.text
    assert res.kept == 2 and res.dropped == 1


def test_budget_single_oversized_block_is_truncated():
    res = budget_blocks(["X" * 100, "Y" * 10], 50)
    assert res.text.startswith("X") and res.text.endswith("…（超长截断）")
    assert "Y" not in res.text  # 其余整块丢弃
    assert res.dropped == 1 and res.truncated


def test_budget_zero_or_negative_disables():
    blocks = ["AAA", "BBB"]
    for limit in (0, -5):
        res = budget_blocks(blocks, limit)
        assert res.text == "AAA\n\nBBB" and res.kept == 2


def test_format_docs_applies_flag_budget(monkeypatch):
    """接线判据：format_docs 走热开关——预算调小后尾部资料被丢。"""
    import core.rag_chain as rc

    monkeypatch.setattr(rc, "_inject_budget", lambda: 60)
    docs = [_doc("A" * 25, chunk_id="c1"), _doc("B" * 25, chunk_id="c2")]
    text = format_docs(docs)
    assert "A" * 25 in text and "B" * 25 not in text  # 块1 完整保留，块2 超预算整块丢弃


def test_format_docs_unlimited_by_default(monkeypatch):
    import core.rag_chain as rc

    monkeypatch.setattr(rc, "_inject_budget", lambda: 0)
    docs = [_doc("内容甲", chunk_id="c1"), _doc("内容乙", chunk_id="c2")]
    text = format_docs(docs)
    assert "内容甲" in text and "内容乙" in text  # 0 = 不限
