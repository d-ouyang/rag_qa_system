# pyright: basic
"""
模块24测试文件：运行时配置服务 ConfigService（v4.0.0 P4-1，PLAN-v4.0.0.md）。

--------------------------------------------------------------------------
它与相邻模块的分工
--------------------------------------------------------------------------
    module23  检索前 Query 增强的**开关分支行为**（默认档：settings 值驱动）
    module24  ConfigService 本身：类型体系 / 优先级 / TTL / 故障姿态 /
              **DB 档压过 settings 默认档**（热更新的核心承诺）+ rag_chain 联动

覆盖点
--------------------------------------------------------------------------
1.  **类型体系**：_type_of 的 bool/int 判别顺序（isinstance(True, int) 为真，顺序反了
    布尔开关会被记成整数）；set_flag 拒绝 None/list/dict 与跨类型改写。
2.  **取值优先级**：DB 档 > .env/代码默认档。set_flag 后即便 default 传反值也拿 DB 的。
3.  **类型守卫**：库里值类型与调用方期望不符（如 bool 键被写成 "yes"）→ 告警 + 回落默认，
    绝不 truthy 静默放行。
4.  **TTL 回源**：ttl=0 每次回源（外部直改立即可见）；ttl 窗口内缓存优先，
    reload() 强制回源。失败也刷新时间戳（DB 挂起不放大故障）。
5.  **故障姿态**：读 fail-open（回落默认，不炸）；写 fail-closed（落库失败必须抛出）。
6.  **rag_chain 联动**：settings.QUERY_REWRITE=False 但 config 里翻 true，
    _prepare_parallel 必须走改写——证明「改配置不用重启」在生产链路真的成立。

运行：
    .venv/bin/python tests/test_module24_config_service.py
（本模块不依赖 MySQL / Chroma / Redis：store 全部用内存替身或故障替身注入）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import patch

from config.settings import settings
from core.config_service import (
    ConfigError,
    ConfigService,
    MemoryConfigStore,
    _Entry,
    _type_of,
    config_service,
)
from core.rag_chain import RAGChain

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


# --------------------------------------------------------------------------- #
# Fake 基础设施（与 module23 同构的最小替身）
# --------------------------------------------------------------------------- #
class _FakeRetriever:
    def __init__(self):
        self.reranker = None
        self.calls: list[tuple[str, object]] = []

    def retrieve(self, query: str, *, vector_query: str | None = None, **_kwargs):
        self.calls.append((query, vector_query))
        return []


class _FakeLLMClient:
    provider = "siliconflow"
    model_name = "fake-model"

    def get_llm(self):
        raise AssertionError("本测试不应触达真实 LLM（_rewrite_question 已被桩替换）")


class _FakeClassifier:
    def classify(self, question: str):  # noqa: ANN001, ARG002
        return SimpleNamespace(route="rag_qa", intent=SimpleNamespace(value="knowledge_query"))


class _BrokenStore:
    """故障替身：读抛异常（模拟 MySQL 挂了），写也抛（模拟落库失败）。"""

    def load_all(self):
        raise RuntimeError("mysql down")

    def upsert(self, key, entry):  # noqa: ANN001
        raise RuntimeError("mysql down")

    def delete(self, key):  # noqa: ANN001
        pass


# --------------------------------------------------------------------------- #
# 1. 类型体系
# --------------------------------------------------------------------------- #
def test_type_system():
    print("== 1. 类型体系（_type_of / 跨类型改写拒绝）==")
    check("bool 先于 int 判别", _type_of(True) == "bool", f"got={_type_of(True)}")
    check("int 判别", _type_of(1) == "int")
    check("float 判别", _type_of(0.5) == "float")
    check("str 判别", _type_of("x") == "str")
    for bad in (None, [1], {"a": 1}):
        try:
            _type_of(bad)
            check(f"拒绝 {type(bad).__name__} 类型", False, "未抛 ConfigError")
        except ConfigError:
            check(f"拒绝 {type(bad).__name__} 类型", True)

    svc = ConfigService(MemoryConfigStore())
    svc.set_flag("k", 5)
    try:
        svc.set_flag("k", "x")
        check("跨类型改写（int→str）被拒", False, "未抛 ConfigError")
    except ConfigError:
        check("跨类型改写（int→str）被拒", True)
    svc.unset_flag("k")
    svc.set_flag("k", "x")  # unset 之后可以换类型
    check("unset 后允许换类型", svc.get_flag("k", "") == "x")


# --------------------------------------------------------------------------- #
# 2. 取值优先级：DB 档 > 默认档
# --------------------------------------------------------------------------- #
def test_precedence():
    print("== 2. 取值优先级（DB 档 > .env/代码默认档）==")
    svc = ConfigService(MemoryConfigStore())
    check("无键 → 回落默认档", svc.get_flag("query_rewrite", False) is False)
    svc.set_flag("query_rewrite", True)
    check("有键 → DB 档压过默认档（default 传 False 也拿 true）",
          svc.get_flag("query_rewrite", False) is True)
    svc.unset_flag("query_rewrite")
    check("unset → 回到默认档", svc.get_flag("query_rewrite", False) is False)

    # .env 默认档透传：settings 里的值是 default 那一档（module23 走的路径）
    old = settings.QUERY_REWRITE
    settings.QUERY_REWRITE = True
    try:
        check("settings 档经 default 透传（离线无键时生效）",
              svc.get_flag("query_rewrite", settings.QUERY_REWRITE) is True)
    finally:
        settings.QUERY_REWRITE = old


# --------------------------------------------------------------------------- #
# 3. 类型守卫
# --------------------------------------------------------------------------- #
def test_type_guard():
    print("== 3. 类型守卫（库值类型不符 → 告警 + 回落默认）==")
    store = MemoryConfigStore()
    # ttl=0：每次读取都回源——本组要直改 store 背后改值，不能让进程缓存挡在中间
    svc = ConfigService(store, ttl_seconds=0)
    store._data["sw"] = _Entry("yes", "str", "", None, "tester")
    check("bool 期望不吃字符串 'yes'（防 truthy 静默放行）",
          svc.get_flag("sw", False) is False)
    store._data["sw"] = _Entry(1, "int", "", None, "tester")
    check("bool 期望不吃 int 1（isinstance(True,int) 陷阱）",
          svc.get_flag("sw", False) is False)
    check("int 期望正常吃 int", svc.get_flag("sw", 0) == 1)
    store._data["sw"] = _Entry(3, "int", "", None, "tester")
    check("float 期望接受 int 并转 float",
          svc.get_flag("sw", 0.0) == 3.0 and isinstance(svc.get_flag("sw", 0.0), float))
    store._data["sw"] = _Entry("s", "str", "", None, "tester")
    check("str 期望正常吃 str", svc.get_flag("sw", "") == "s")
    check("default=None 时原样返回（调用方自担类型）",
          svc.get_flag("sw", None) == "s")


# --------------------------------------------------------------------------- #
# 4. TTL 回源
# --------------------------------------------------------------------------- #
def test_ttl_reload():
    print("== 4. TTL 回源（ttl=0 立即可见 / 窗口内缓存优先 / reload 强制）==")
    store = MemoryConfigStore()
    svc = ConfigService(store, ttl_seconds=0)
    svc.set_flag("k", 1)
    store._data["k"] = store._data["k"]._replace(value=2)  # 模拟别的实例直改库
    check("ttl=0：外部改动下一次读取即可见", svc.get_flag("k", 0) == 2)

    svc2 = ConfigService(MemoryConfigStore(), ttl_seconds=3600)
    svc2.set_flag("k", 1)
    svc2._store._data["k"] = svc2._store._data["k"]._replace(value=2)
    check("TTL 窗口内：缓存优先（外部改动暂不可见）", svc2.get_flag("k", 0) == 1)
    svc2.reload()
    check("reload() 强制回源后可见", svc2.get_flag("k", 0) == 2)


# --------------------------------------------------------------------------- #
# 5. 故障姿态：读 fail-open / 写 fail-closed
# --------------------------------------------------------------------------- #
def test_failure_modes():
    print("== 5. 故障姿态（读 fail-open / 写 fail-closed）==")
    svc = ConfigService(_BrokenStore(), ttl_seconds=60)
    check("DB 挂了：get_flag 回落默认值不抛", svc.get_flag("query_rewrite", False) is False)
    try:
        svc.set_flag("query_rewrite", True)
        check("DB 挂了：set_flag 必须抛（管理端要看得见没保存成功）", False, "未抛")
    except Exception:
        check("DB 挂了：set_flag 必须抛（管理端要看得见没保存成功）", True)


# --------------------------------------------------------------------------- #
# 6. rag_chain 联动：config 档压过 settings 档
# --------------------------------------------------------------------------- #
def test_rag_chain_integration():
    print("== 6. rag_chain 联动（config 翻开关 → _prepare_parallel 走改写，无需重启）==")
    retr = _FakeRetriever()
    chain = RAGChain(retriever=retr, llm_client=_FakeLLMClient(), memory=object())
    chain._rewrite_question = lambda q, h: ("改写后的问题", {"input_tokens": 0,
                                                            "output_tokens": 0,
                                                            "cache_read_tokens": 0})
    old_rw = settings.QUERY_REWRITE
    settings.QUERY_REWRITE = False  # .env/代码默认档：关
    try:
        with patch("core.rag_chain.get_intent_classifier", return_value=_FakeClassifier()):
            config_service.unset_flag("query_rewrite")  # 确保起点干净
            _intent, prepared = chain._prepare_parallel("原问题", [])
            check("双关都关：不重写（默认档行为不变）",
                  prepared["standalone_question"] == "原问题")

            config_service.set_flag("query_rewrite", True, actor="module24")  # 热翻开关
            retr.calls.clear()
            _intent, prepared = chain._prepare_parallel("原问题", [])
            check("config 翻 true：settings 仍 False 也走改写（DB 档胜出）",
                  prepared["standalone_question"] == "改写后的问题",
                  f"got={prepared['standalone_question']!r}")
            check("改写后重新检索", ("改写后的问题", None) in retr.calls, f"calls={retr.calls!r}")
    finally:
        config_service.unset_flag("query_rewrite")
        settings.QUERY_REWRITE = old_rw


# --------------------------------------------------------------------------- #
# 7. 表定义与迁移链（结构一致性，module8 在线版的前置哨）
# --------------------------------------------------------------------------- #
def test_schema_and_migration_chain():
    print("== 7. system_config 表定义与迁移链 ==")
    from core import schema

    cols = {c.name for c in schema.system_config_table.columns}
    expected = {"config_key", "config_value", "value_type", "description", "updated_at", "updated_by"}
    check("schema.system_config_table 列齐全", expected <= cols, f"got={sorted(cols)}")
    pk = [c.name for c in schema.system_config_table.primary_key.columns]
    check("主键是 config_key", pk == ["config_key"], f"got={pk}")

    # 迁移链：revision/down_revision 必须一条链且 0011 是头（用 ast 解析，不 import 迁移模块）
    import ast

    versions_dir = Path(__file__).parent.parent / "alembic" / "versions"
    revs: dict[str, str | None] = {}
    for f in versions_dir.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rev = down = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "revision":
                        rev = ast.literal_eval(node.value)
                    if isinstance(t, ast.Name) and t.id == "down_revision":
                        down = ast.literal_eval(node.value)
        if rev:
            revs[rev] = down
    children = {d for d in revs.values() if d}
    heads = [r for r in revs if r not in children]
    check("迁移链只有一个 head", len(heads) == 1, f"heads={heads}")
    check("head 是 0011（system_config）", heads == ["0011"], f"heads={heads}")
    check("0011 的 down_revision 是 0010", revs.get("0011") == "0010",
          f"got={revs.get('0011')}")


if __name__ == "__main__":
    test_type_system()
    test_precedence()
    test_type_guard()
    test_ttl_reload()
    test_failure_modes()
    test_rag_chain_integration()
    test_schema_and_migration_chain()
    print(f"\n== 模块24 运行时配置服务：PASS={PASS} FAIL={FAIL} ==")
    sys.exit(1 if FAIL else 0)
