"""检索工具契约测试（v4.1.0-p1.0 / Phase ①1 第一步）。

断言维度：
  · 集中批注表的语义正确性（高危操作显式标记、只读组件幂等）；
  · registry 的悲观缺省（未批注=最危险档）与 unannotated() 抓漏；
  · override 注入 Fake（评测/测试的可注入性判据）。
"""
import pytest

from core.tool_spec import (
    RETRIEVAL_TOOL_SPECS,
    RagToolRegistry,
    ToolSpec,
)

EXPECTED_COMPONENTS = {
    "retriever", "vector_store", "bm25_index",
    "embedding", "mineru_client", "parsing", "chunk_rebuild",
}


def test_central_specs_cover_all_components():
    assert set(RETRIEVAL_TOOL_SPECS) == EXPECTED_COMPONENTS


def test_chunk_rebuild_is_high_risk_non_idempotent():
    """改切分=整库重建——契约面必须显式标高危/非幂等/独立权限域。"""
    spec = RETRIEVAL_TOOL_SPECS["chunk_rebuild"]
    assert spec.risk_level == "high"
    assert spec.side_effect is True
    assert spec.idempotent is False
    assert spec.permission == "admin:rebuild"


def test_readonly_components_are_idempotent():
    for name in ("retriever", "vector_store", "bm25_index", "parsing"):
        spec = RETRIEVAL_TOOL_SPECS[name]
        assert spec.side_effect is False and spec.idempotent is True, name


def test_registry_pessimistic_default_and_unannotated_probe():
    reg = RagToolRegistry()
    reg.register("retriever", lambda: [])          # 有集中批注
    reg.register("mystery_component", lambda: 1)   # 无批注 → 悲观档
    assert reg.spec("retriever").risk_level == "low"
    assert reg.spec("mystery_component") == ToolSpec()  # 悲观缺省
    assert reg.unannotated() == ["mystery_component"]


def test_override_injects_fake_and_returns_original():
    reg = RagToolRegistry()
    reg.register("embedding", lambda q: ["real-vec"])
    fake_calls: list[str] = []

    def fake_embed(q: str) -> list[float]:
        fake_calls.append(q)
        return [0.0]

    original = reg.override("embedding", fake_embed)
    assert reg.get("embedding")("hi") == [0.0] and fake_calls == ["hi"]
    reg.override("embedding", original)  # 恢复
    assert reg.get("embedding")("hi") == ["real-vec"]


def test_override_unknown_component_raises():
    with pytest.raises(KeyError):
        RagToolRegistry().override("nope", lambda: 1)
