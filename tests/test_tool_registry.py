"""registry 引导与收编测试（v4.1.0-p1.1 / Phase ①1b）。"""
import pytest

from core.tool_registry import get_registry, reset_registry
from core.tool_spec import ToolSpec


@pytest.fixture(autouse=True)
def _fresh():
    reset_registry()
    yield
    reset_registry()


def test_bootstrap_covers_all_seven_components():
    reg = get_registry()
    assert set(reg.all_names()) == {
        "retriever", "vector_store", "bm25_index", "embedding",
        "mineru_client", "parsing", "chunk_rebuild",
    }
    for name in reg.all_names():
        assert reg.has(name) and callable(reg.get(name))
        assert isinstance(reg.spec(name), ToolSpec)
    assert reg.unannotated() == []  # 全部走集中批注，无悲观兜底


def test_chunk_rebuild_spec_is_high_risk():
    spec = get_registry().spec("chunk_rebuild")
    assert spec.risk_level == "high" and spec.idempotent is False


def test_override_retriever_fake():
    """收编判据：override("retriever") 后 registry 取到 Fake——评测/测试可注入。"""
    reg = get_registry()

    class FakeRetriever:
        def invoke(self, q: str) -> list:
            return [{"page_content": "FAKE", "metadata": {"chunk_id": "c-x"}}]

    original = reg.override("retriever", FakeRetriever())
    try:
        assert reg.get("retriever").invoke("x")[0]["page_content"] == "FAKE"
    finally:
        reg.override("retriever", original)
    assert reg.get("retriever") is original
