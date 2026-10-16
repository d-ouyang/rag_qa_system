# pyright: basic
"""
模块25测试文件：管理端运行时配置接口（v4.0.0 P4-2，PLAN-v4.0.0.md）。

--------------------------------------------------------------------------
它与相邻模块的分工
--------------------------------------------------------------------------
    module24  ConfigService 机制本身（类型/优先级/TTL/故障姿态）
    module25  P4-2 的「清单 + 门禁 + 接线」：注册表契约、GET/PATCH 权限矩阵、
              审计落点、以及**各读取点真的换成了热读**（行为 + 源码双验）

覆盖点
--------------------------------------------------------------------------
1.  **注册表契约**：describe_flags() 字段齐全、key 字典序稳定、数值项有边界、
    敏感项结构性缺席（注册表里不许出现任何密钥类 key）。
2.  **校验**：类型不符 / 越界 / 未注册 key 一律拒绝。
3.  **HTTP 权限矩阵**（TestClient + dependency_overrides）：
    superadmin GET/PATCH 全通；非 superadmin 403；未注册 key 404；
    类型错 / 越界 422；PATCH 成功后 ConfigService 值立变 + 审计被调用。
4.  **读取点接线**（行为级）：retriever.top_k 热读且显式参数优先、
    显式 use_reranker=False 锁死不被热开启绕过、阈值 0=关闭过滤、
    MemoryManager.max_turns 热读。
5.  **源码级扫描**：老读取点（settings.QA_CACHE_ENABLED 等）必须已不存在——
    防止「新热读加了、旧快照没删」的双源漂移（P3-4 修订 2 的教训）。

运行：
    .venv/bin/python tests/test_module25_admin_config.py
（不依赖 MySQL/Redis：store 用进程内默认 memory；审计用 spy 捕获）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException

from config.settings import settings
from core import audit_repo
from core.config_flags import FLAG_REGISTRY, describe_flags, validate_flag_value
from core.config_service import ConfigError, config_service
from core.identity import require_superadmin
from core.memory_manager import MemoryManager
from core.retriever import RAGRetriever
from core.session_store import MemorySessionStore

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def _cleanup_flags() -> None:
    """把测试写进进程内配置的键全部清掉（回落默认档），避免组间串味。"""
    for key in list(FLAG_REGISTRY):
        config_service.unset_flag(key)


class _FakeVS:
    store_type = "fake"

    def similarity_search_with_score(self, query: str, k: int, filter_dict=None):  # noqa: ANN001
        return []


def _actor(kb_role: str = "superadmin"):
    return SimpleNamespace(id=1, username="wu.jing", role="admin", kb_role=kb_role, source="test")


# --------------------------------------------------------------------------- #
# 1. 注册表契约
# --------------------------------------------------------------------------- #
def test_registry_contract():
    print("== 1. 注册表契约 ==")
    flags = describe_flags()
    keys = [f["key"] for f in flags]
    check("key 字典序稳定", keys == sorted(keys), f"got={keys}")
    check("v1 注册 8 项", len(keys) == 8, f"got={keys}")
    for f in flags:
        ok = all(k in f for k in ("key", "label", "card", "value_type", "description", "value", "default", "min", "max"))
        if not ok:
            check(f"{f.get('key')} 字段齐全", False, f"missing={f}")
            break
        if f["value_type"] in ("int", "float") and (f["min"] is None or f["max"] is None):
            check(f"{f['key']} 数值项有边界", False, f"min={f['min']} max={f['max']}")
            break
    else:
        check("全部项字段齐全", True)
        check("数值项都有上下界", True)
    # 敏感项结构性缺席：注册表里不许有任何密钥类 key（密钥永不进热更新白名单）
    check("注册表无密钥类 key",
          not any(w in k for k in keys for w in ("secret", "password", "token", "api_key")),
          f"keys={keys}")
    _cleanup_flags()


# --------------------------------------------------------------------------- #
# 2. 校验
# --------------------------------------------------------------------------- #
def test_validate():
    print("== 2. 校验（类型 / 边界 / 未注册）==")
    for bad in ("yes", 1):
        try:
            validate_flag_value("query_rewrite", bad)
            check(f"bool 键拒绝 {bad!r}", False, "未抛")
        except ConfigError:
            check(f"bool 键拒绝 {bad!r}", True)
    try:
        validate_flag_value("search_top_k", 0)
        check("top_k=0 被拒（越下界）", False, "未抛")
    except ConfigError:
        check("top_k=0 被拒（越下界）", True)
    try:
        validate_flag_value("search_top_k", 99)
        check("top_k=99 被拒（越上界）", False, "未抛")
    except ConfigError:
        check("top_k=99 被拒（越上界）", True)
    try:
        validate_flag_value("not_a_flag", True)
        check("未注册 key 被拒", False, "未抛")
    except ConfigError:
        check("未注册 key 被拒", True)
    assert validate_flag_value("rerank_score_threshold", 0.15) == 0.15
    check("合法 float 原样通过", True)
    _cleanup_flags()


# --------------------------------------------------------------------------- #
# 3. HTTP 权限矩阵
# --------------------------------------------------------------------------- #
def _client_with(actor):
    from api.main import app

    app.dependency_overrides[require_superadmin] = lambda: actor
    return TestClient(app)


def test_http_matrix():
    print("== 3. HTTP 权限矩阵（superadmin / 非超管 / 校验失败）==")
    recorded: list[dict] = []
    with patch.object(audit_repo, "record", side_effect=lambda **kw: (recorded.append(kw), 1)[1]):
        client = _client_with(_actor("superadmin"))
        res = client.get("/api/v1/admin/config")
        check("superadmin GET 200", res.status_code == 200, f"got={res.status_code}")
        body = res.json()
        check("GET 返回 can_edit=True + flags", body.get("can_edit") is True and len(body.get("flags", [])) == 8)

        res = client.patch("/api/v1/admin/config/query_rewrite", json={"value": True})
        check("superadmin PATCH 200", res.status_code == 200, f"got={res.status_code} {res.text[:120]}")
        check("ConfigService 值立变（DB 档胜出）",
              config_service.get_flag("query_rewrite", False) is True)
        check("审计被调用且动作/对象正确",
              bool(recorded) and recorded[-1]["action"] == "config.update"
              and recorded[-1]["target_type"] == "system" and recorded[-1]["target_label"] == "query_rewrite"
              and recorded[-1]["actor_username"] == "wu.jing",
              f"last={recorded[-1] if recorded else None}")

        res = client.patch("/api/v1/admin/config/nope", json={"value": True})
        check("未注册 key PATCH 404", res.status_code == 404, f"got={res.status_code}")
        res = client.patch("/api/v1/admin/config/query_rewrite", json={"value": "yes"})
        check("类型错 422", res.status_code == 422, f"got={res.status_code}")
        res = client.patch("/api/v1/admin/config/search_top_k", json={"value": 0})
        check("越界 422", res.status_code == 422, f"got={res.status_code}")
        res = client.patch("/api/v1/admin/config/search_top_k", json={"value": None})
        check("None 值 422", res.status_code == 422, f"got={res.status_code}")

        # 非 superadmin：门禁依赖抛 403
        app = client.app
        from fastapi import status as st

        def _deny():
            raise HTTPException(status_code=st.HTTP_403_FORBIDDEN, detail="仅知识库超管可用")

        app.dependency_overrides[require_superadmin] = _deny
        res = client.get("/api/v1/admin/config")
        check("非超管 GET 403", res.status_code == 403, f"got={res.status_code}")
        res = client.patch("/api/v1/admin/config/query_rewrite", json={"value": False})
        check("非超管 PATCH 403", res.status_code == 403, f"got={res.status_code}")
        app.dependency_overrides.pop(require_superadmin, None)
    _cleanup_flags()


# --------------------------------------------------------------------------- #
# 4. 读取点接线（行为级）
# --------------------------------------------------------------------------- #
def test_wiring_behavior():
    print("== 4. 读取点接线（行为级）==")
    # retriever.top_k：缺省热读 / 显式参数优先
    r = RAGRetriever(vector_store=_FakeVS(), use_reranker=False)
    check("top_k 缺省 = settings 档", r.top_k == settings.SEARCH_TOP_K, f"got={r.top_k}")
    config_service.set_flag("search_top_k", 7)
    check("config 翻 7 → top_k=7", r.top_k == 7)
    config_service.unset_flag("search_top_k")

    r_explicit = RAGRetriever(vector_store=_FakeVS(), use_reranker=False, top_k=3)
    config_service.set_flag("search_top_k", 7)
    check("显式 top_k=3 压过 config 7", r_explicit.top_k == 3)
    config_service.unset_flag("search_top_k")

    # 显式 use_reranker=False = 实例锁死，配置热开也不许绕过
    config_service.set_flag("rerank_enabled", True)
    check("显式禁用 + config 开 → 仍不重排（实例锁死）", r._rerank_active() is False)
    # 阈值：0 = 关闭过滤（None）
    config_service.set_flag("rerank_score_threshold", 0.0)
    check("阈值 0 → None（关闭过滤）", r._effective_threshold() is None)
    config_service.set_flag("rerank_score_threshold", 0.2)
    check("阈值 0.2 热读", r._effective_threshold() == 0.2)
    config_service.unset_flag("rerank_score_threshold")
    config_service.unset_flag("rerank_enabled")

    # MemoryManager：显式参数优先 / 缺省热读
    mm = MemoryManager(max_turns=2, store=MemorySessionStore(ttl_seconds=60))
    mm2 = MemoryManager(store=MemorySessionStore(ttl_seconds=60))
    check("max_turns 显式参数优先", mm.max_turns == 2)
    config_service.set_flag("memory_max_turns", 9)
    check("config 翻 9 → 缺省实例热读 9", mm2.max_turns == 9)
    check("显式实例仍 2（不被热改波及）", mm.max_turns == 2)
    config_service.unset_flag("memory_max_turns")


# --------------------------------------------------------------------------- #
# 5. 源码级扫描：老读取点必须已移除（防双源漂移）
# --------------------------------------------------------------------------- #
def test_source_scan():
    print("== 5. 源码扫描（老快照读取点必须清干净）==")
    root = Path(__file__).parent.parent
    checks = [
        ("core/qa_cache.py", "settings.QA_CACHE_ENABLED"),
        ("api/routes/documents.py", "settings.DOC_DOWNLOAD_ENABLED"),
        ("core/retriever.py", "self.top_k: int = int("),
        ("core/memory_manager.py", "self.max_turns: int = int("),
        ("core/rag_chain.py", "settings.QUERY_REWRITE or settings.QUERY_HYDE"),
    ]
    for rel, needle in checks:
        text = (root / rel).read_text(encoding="utf-8")
        check(f"{rel} 不再含「{needle}」", needle not in text)


if __name__ == "__main__":
    test_registry_contract()
    test_validate()
    test_http_matrix()
    test_wiring_behavior()
    test_source_scan()
    print(f"\n== 模块25 管理端运行时配置：PASS={PASS} FAIL={FAIL} ==")
    sys.exit(1 if FAIL else 0)
