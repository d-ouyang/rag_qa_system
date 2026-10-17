"""module27 —— P4-5b：问答流程状态机 + qa_trace 分析镜像 + 溯源查询接口。

覆盖四块：
    1. `core/qa_flow.py` 纯函数：resolve_decision 三分支穷举 + flow_path 四终端；
    2. 结构守卫：schema.qa_trace_table 与迁移 0012 的列集合逐一对齐（AST 静态比对，
       运行期的「列注释逐字一致」由 module8 的 compare_metadata 兜）；
    3. `core/qa_trace_repo.py`：镜像写入 + 七种筛选 + describe 派生字段
       （跑在当前配置的 MySQL 上；沙箱 = rag_qa_sandbox，跑完清理）；
    4. HTTP：GET /api/v1/admin/qa-traces 权限矩阵（TestClient + current_actor 桩，
       require_staff / require_kb 走**真实**判定逻辑）。

清理锚点：所有写入行 session_id 一律 `p45b_` 前缀；收尾按前缀删 + 「全表总数
回到进组前的值」双向验证（坑 49：清理验证条件 ≠ 清理条件）。
"""

from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.logging_config import setup_logging

setup_logging()

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
# 1. qa_flow 纯函数
# --------------------------------------------------------------------------- #
print("== 1. qa_flow：决策点穷举 ==")
from core.qa_flow import (  # noqa: E402
    QaFlowState,
    STATE_LABELS,
    TERMINAL_STATES,
    flow_path,
    resolve_decision,
)

check("终端状态恰好四个",
      set(TERMINAL_STATES) == {"cache_hit", "chitchat", "zero_recall", "rag_qa"},
      f"实际 {sorted(TERMINAL_STATES)}")
check("resolve_decision：闲聊（无 prepared）",
      resolve_decision(prepared_present=False, docs_count=0) == "chitchat")
check("resolve_decision：零召回（0 条）",
      resolve_decision(prepared_present=True, docs_count=0) == "zero_recall")
check("resolve_decision：正常作答（≥1 条）",
      resolve_decision(prepared_present=True, docs_count=3) == "rag_qa")

_path_rag = flow_path("rag_qa")
_path_zero = flow_path("zero_recall")
_path_chit = flow_path("chitchat")
_path_cache = flow_path("cache_hit")
check("路径：每条都以 received 开头",
      all(p[0] == "received" for p in (_path_rag, _path_zero, _path_chit, _path_cache)))
check("路径：每条都以自己的终端状态结尾",
      all(p[-1] == d for p, d in ((_path_rag, "rag_qa"), (_path_zero, "zero_recall"),
                                  (_path_chit, "chitchat"), (_path_cache, "cache_hit"))))
check("路径：缓存命中最短（3 节点）", len(_path_cache) == 3, f"实际 {len(_path_cache)}")
check("路径：闲聊不经过检索/重排/生成",
      not {"retrieve", "rerank", "threshold", "generate"} & set(_path_chit))
check("路径：零召回止步于阈值过滤（不生成）",
      "threshold" in _path_zero and "generate" not in _path_zero)
check("路径：rag_qa 走完全程（含生成）",
      {"retrieve", "rerank", "threshold", "generate"} <= set(_path_rag))
try:
    flow_path("not_a_state")
    check("未知终端状态抛 ValueError", False, "未抛")
except ValueError:
    check("未知终端状态抛 ValueError", True)
check("状态标签覆盖全部状态（无第三份清单漂移）",
      set(STATE_LABELS) == {s.value for s in QaFlowState},
      f"缺 { {s.value for s in QaFlowState} - set(STATE_LABELS) }")

# --------------------------------------------------------------------------- #
# 2. schema ↔ 迁移 0012 列集合对齐（AST 静态）
# --------------------------------------------------------------------------- #
print("== 2. 结构守卫：schema 与迁移链（0012 建表 + 0013 加列）列对齐 ==")


def _columns_of(path: str) -> tuple[set[str], str]:
    """从源码里抽 qa_trace 的列名：定位第一参为 "qa_trace" 的 sa.Table /
    create_table 调用，只收集**它的** sa.Column —— 整文件扫会把其他表的列算进来
    （第一版就犯了这个错：schema.py 有 12 张表，diff 出 82 个假列）。"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), path)
    cols: set[str] = set()
    revision: str = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "revision" and isinstance(node.value, ast.Constant):
                    revision = str(node.value.value)
        # 建表（0012 的 create_table）
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("Table", "create_table") and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "qa_trace"):
            for arg in node.args + [kw.value for kw in node.keywords]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                            and sub.func.attr == "Column" and sub.args \
                            and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str):
                        cols.add(sub.args[0].value)
        # 后续迁移的加列（0013 的 op.add_column("qa_trace", ...)）也要算进列集合
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_column" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "qa_trace"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                        and sub.func.attr == "Column" and sub.args \
                        and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str):
                    cols.add(sub.args[0].value)
    return cols, revision


_schema_cols, _ = _columns_of("core/schema.py")
import glob as _glob  # noqa: E402

_mig_cols: set[str] = set()
_rev = ""
for _mig in sorted(_glob.glob("alembic/versions/*.py")):
    c, r = _columns_of(_mig)
    _mig_cols |= c
    if r:
        _rev = r  # 线性链：排序后最后一个文件的 revision 即链头
check("迁移链头是 0013（qa_trace 加列）", _rev == "0013", f"实际 {_rev}")
check("schema 与迁移列集合一致（数量含 id）",
      _schema_cols == _mig_cols,
      f"仅 schema 有: {sorted(_schema_cols - _mig_cols)}；仅迁移有: {sorted(_mig_cols - _schema_cols)}")
check("候选清单是 JSON 列", "candidates" in _schema_cols)

# --------------------------------------------------------------------------- #
# 3. repo：镜像 + 筛选（需要 MySQL；沙箱 = rag_qa_sandbox）
# --------------------------------------------------------------------------- #
print("== 3. qa_trace_repo：镜像与筛选 ==")
import sqlalchemy as sa  # noqa: E402

from core.db import session_scope  # noqa: E402
from core.qa_trace_repo import count_traces, describe, list_traces, record  # noqa: E402
from core.schema import qa_trace_table  # noqa: E402

SID = "p45b_sid01"
_OTHER = "p45b_other"


def _trace(decision: str, **over: object) -> dict:
    base: dict = {
        "decision": decision,
        "intent": "policy_consult",
        "route": "rag_qa" if decision != "chitchat" else "chitchat",
        "intent_source": "rule",
        "question": "p45b测试问题？",
        "standalone_question": None,
        "rewritten_query": None,
        "vector_query": None,
        "candidates": [],
        "threshold": None,
        "top1_before_filter": None,
        "filtered_count": None,
        "docs_returned": 0,
        "sources_count": 0,
        "bm25_hits": None,
        "bm25_only": None,
        "rrf_top": None,
        "hyde_used": False,
        "elapsed_ms": 12.5,
        "rejected": decision == "zero_recall",
    }
    base.update(over)
    return base


_total_before = count_traces()
try:
    # 3a. 写入与缺 decision 拒绝
    rid = record(session_id=SID, owner_id=7, trace=_trace("rag_qa", top1_before_filter=0.4321, threshold=0.35))
    check("record 返回自增 id", isinstance(rid, int) and rid > 0)
    try:
        record(session_id=SID, owner_id=None, trace={"question": "没终端状态"})
        check("缺 decision 拒绝镜像", False, "未抛")
    except ValueError:
        check("缺 decision 拒绝镜像", True)
    record(session_id=SID, owner_id=7, trace=_trace("zero_recall", top1_before_filter=0.31, threshold=0.35))
    record(session_id=_OTHER, owner_id=8, trace=_trace("chitchat"))
    # P4-5c 回归锚：rrf_top 实际是 chunk_id 字符串（"822:0"）而非分数——
    # 首版误建 DOUBLE 让镜像每轮必失败（Data truncated），此处锁死字符串可落库
    record(session_id=_OTHER, owner_id=8,
           trace=_trace("cache_hit", intent="chitchat", route="chitchat", rrf_top="822:0"))

    # 3b. describe 派生字段（列表按 id 倒序，别假设首行是哪条 —— 按决策取）
    rows = list_traces(session_id=SID)
    check("session_id 精确筛", len(rows) == 2, f"实际 {len(rows)}")
    rag_row = next(r for r in rows if r["decision"] == "rag_qa")
    zero_row = next(r for r in rows if r["decision"] == "zero_recall")
    check("describe 带 flow_path（rag_qa 全程）", rag_row["flow_path"][-1] == "rag_qa"
          and "generate" in rag_row["flow_path"])
    check("describe 算 top1_gap（负差距照算）", rag_row["top1_gap"] == -0.0821,
          f"实际 {rag_row['top1_gap']}")
    check("describe 算 top1_gap（拒答轮 = 差多少分）", zero_row["top1_gap"] == 0.04,
          f"实际 {zero_row['top1_gap']}")
    check("created_at 已转 isoformat", isinstance(rag_row["created_at"], str) and "T" in rag_row["created_at"])

    # 3c. 决策 / 意图 / 关键词 / 差距
    check("decision 筛", len(list_traces(decision="zero_recall", session_id=SID)) == 1)
    check("intent 筛", len(list_traces(intent="policy_consult")) >= 2)
    check("rrf_top 存字符串 chunk_id（P4-5c 回归锚：首版 DOUBLE 必失败）",
          any(r["rrf_top"] == "822:0" for r in list_traces(session_id=_OTHER)),
          f"实际 {[r['rrf_top'] for r in list_traces(session_id=_OTHER)]}")
    # P4-6 回归锚：依据校验结果透传 + describe 派生 low_confidence
    record(session_id=SID, owner_id=7,
           trace=_trace("rag_qa", top1_before_filter=0.30, threshold=0.31,
                        grounded=False, unsupported=["病假1天内不扣工资"]))
    _g = next(r for r in list_traces(session_id=SID) if r.get("grounded") is False)
    check("grounded/unsupported 透传", _g["unsupported"] == ["病假1天内不扣工资"],
          f"实际 {_g.get('unsupported')!r}")
    check("describe 派生 low_confidence（top1=0.30 落犬牙交错带）",
          _g["low_confidence"] is True, f"实际 {_g.get('low_confidence')!r}")
    check("keyword 筛命中原始问题", any(r["session_id"] == SID for r in list_traces(keyword="p45b测试")))
    # ⚠️ 必须带 session_id 限定：独立跑在 dev 库上时有真实用户的拒答轮（gap 同样 ≥0.03），
    # 裸查会混进线上数据——这是 2026-10-17 在 dev 实跑暴露的环境依赖断言
    check("min_top1_gap：差距 ≥0.03 只留 zero_recall 那条（限定本会话）",
          [r["decision"] for r in list_traces(min_top1_gap=0.03, session_id=SID)] == ["zero_recall"],
          f"实际 {[r['decision'] for r in list_traces(min_top1_gap=0.03, session_id=SID)]}")
    check("min_top1_gap：NULL 行（闲聊/缓存）自然排除",
          all(r["top1_before_filter"] is not None for r in list_traces(min_top1_gap=0.0)))
    check("日期筛（今天）命中", count_traces(date_from=date.today(), date_to=date.today()) >= 4)
    check("日期筛（未来一天）为 0",
          count_traces(date_from=date(2099, 1, 1), date_to=date(2099, 1, 2)) == 0)
    try:
        list_traces(limit=0)
        check("limit 越界抛 ValueError", False, "未抛")
    except ValueError:
        check("limit 越界抛 ValueError", True)
finally:
    # 清理 + 双向验证：前缀删光，且全表总数回到进组前（坑 49）
    with session_scope() as session:
        session.execute(sa.delete(qa_trace_table).where(qa_trace_table.c.session_id.like("p45b\\_%")))
    check("清理：前缀行删光", count_traces() - _total_before == 0,
          f"before={_total_before} after={count_traces()}")

# --------------------------------------------------------------------------- #
# 4. HTTP：/admin/qa-traces 权限矩阵
# --------------------------------------------------------------------------- #
print("== 4. HTTP 权限矩阵（require_staff / require_kb 真实逻辑） ==")
import core.identity as identity  # noqa: E402


def _actor(kb_role: str, *, staff: bool = True):
    return SimpleNamespace(id=1, username="wu.jing", role="admin",
                           kb_role=kb_role, source="test", can_staff=staff)


def _client_with(actor) -> object:
    from api.main import app
    from fastapi.testclient import TestClient

    app.dependency_overrides[identity.current_actor] = lambda: actor
    return TestClient(app)


try:
    # 4a. superadmin：放行（先造一条数据再查，顺便验证返回形状）
    record(session_id=SID, owner_id=7, trace=_trace("rag_qa"))
    client = _client_with(_actor("superadmin"))
    resp = client.get("/api/v1/admin/qa-traces", params={"session_id": SID})
    check("superadmin 200", resp.status_code == 200, f"实际 {resp.status_code} {resp.text[:200]}")
    body = resp.json()
    check("返回形状：items/total/decisions/intents",
          all(k in body for k in ("items", "total", "decisions", "intents")))
    check("items 带 flow_path", bool(body["items"]) and body["items"][0]["flow_path"][-1] == "rag_qa")
    check("decisions 下拉四项", len(body["decisions"]) == 4)
    check("intents 下拉七项且带中文标签",
          len(body["intents"]) == 7 and all(i["label"] for i in body["intents"]))

    # 4b. ops：放行（kb_acl：溯源查看 = ops/superadmin）
    resp_ops = _client_with(_actor("ops")).get("/api/v1/admin/qa-traces")
    check("ops 200", resp_ops.status_code == 200, f"实际 {resp_ops.status_code}")

    # 4c. qa / none：403（能进管理端也看不了别人的会话轨迹）
    for role in ("qa", "none"):
        resp_deny = _client_with(_actor(role)).get("/api/v1/admin/qa-traces")
        check(f"{role} 403", resp_deny.status_code == 403, f"实际 {resp_deny.status_code}")

    # 4d. 进不了管理端的（can_staff=False）：require_staff 先拦
    resp_user = _client_with(_actor("superadmin", staff=False)).get("/api/v1/admin/qa-traces")
    check("非管理端账号 403（staff 门槛在前）", resp_user.status_code == 403,
          f"实际 {resp_user.status_code}")

    # 4e. 筛选参数透传
    resp_f = _client_with(_actor("superadmin")).get(
        "/api/v1/admin/qa-traces", params={"decision": "chitchat", "session_id": SID})
    check("decision 筛透传（rag_qa 会话查 chitchat = 空）",
          resp_f.status_code == 200 and resp_f.json()["total"] == 0)
    resp_g = _client_with(_actor("superadmin")).get(
        "/api/v1/admin/qa-traces", params={"min_top1_gap": 0.9})
    check("min_top1_gap 大值 = 空而不炸（参数界内）",
          resp_g.status_code == 200 and resp_g.json()["total"] == 0)

    # 4f. 候选原文（P4-7）：批量取 + 参数校验（正文查询走桩，不碰 Chroma）
    import api.routes.admin as admin_routes

    orig_fetch = admin_routes.fetch_chunk_contents
    admin_routes.fetch_chunk_contents = lambda ids: {
        i: {"chunk_id": i, "file_name": "a.md", "content": f"正文-{i}"} for i in ids
    }
    try:
        resp_c = _client_with(_actor("superadmin")).get(
            "/api/v1/admin/qa-traces/chunks", params={"ids": "812:1,812:2"})
        check("chunks 批量取原文 200", resp_c.status_code == 200
              and resp_c.json()["items"]["812:1"]["content"] == "正文-812:1",
              f"实际 {resp_c.status_code} {resp_c.text[:150]}")
        resp_denied = _client_with(_actor("qa")).get(
            "/api/v1/admin/qa-traces/chunks", params={"ids": "812:1"})
        check("qa 档看原文 403（与主接口同门禁）", resp_denied.status_code == 403,
              f"实际 {resp_denied.status_code}")
    finally:
        admin_routes.fetch_chunk_contents = orig_fetch

    # 参数校验用**真函数**（桩会把解析也替掉，400 路径测不到）：
    # 非法格式在 parse_chunk_id 处抛 ValueError → 400，碰不到 Chroma
    resp_empty = _client_with(_actor("superadmin")).get(
        "/api/v1/admin/qa-traces/chunks", params={"ids": ","})
    check("ids 全空 400", resp_empty.status_code == 400, f"实际 {resp_empty.status_code}")
    resp_illegal = _client_with(_actor("superadmin")).get(
        "/api/v1/admin/qa-traces/chunks", params={"ids": "not-a-chunk"})
    check("非法 chunk_id 400", resp_illegal.status_code == 400,
          f"实际 {resp_illegal.status_code}")
finally:
    with session_scope() as session:
        session.execute(sa.delete(qa_trace_table).where(qa_trace_table.c.session_id.like("p45b\\_%")))
    from api.main import app

    app.dependency_overrides.pop(identity.current_actor, None)


print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
sys.exit(1 if FAIL else 0)
