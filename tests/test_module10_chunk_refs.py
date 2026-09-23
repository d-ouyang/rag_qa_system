# pyright: basic
"""
模块10测试文件：验证切片引用反查链路（chunk_id 契约）—— v2.0.0 P0-4a。

运行：
    .venv/bin/python tests/test_module10_chunk_refs.py      （或 make test）

依赖：
    **需要 MySQL**（document 表 + chat_message 表的真相源）。连不上时整模块 SKIP
    并返回 0，让 `make test` 在没起中间件的机器上仍然能全绿。
    设 `REQUIRE_MYSQL=1` 可把 SKIP 变成失败（CI / 发布前自检用）。
    **不需要 Redis / worker / 大模型**：向量库用独立临时 Chroma，
    检索与重排只在内存里构造 Document 验证，不走真链路。

覆盖点：
1. chunk_id 契约：build / parse 的往返与边界（含 isdigit 的陷阱）
2. 写入侧：parse_and_index 写出的 metadata 里 chunk_id 与 doc_id+chunk_index 自洽
3. 读取侧：get_chunk_by_position 命中 / 未命中 / 类型严格
4. 孤儿识别与清理：只清孤儿、不动有效切片、幂等
5. sources 带出 chunk_id：新切片直读、P0-3a 老切片兜底、遗留切片为 None
6. ref_ids 落库：chat_message.ref_ids 从「恒为 []」变成真值
7. HTTP 接口层：200 / 400（格式非法）/ 404（随文档删除）/ 404（切片失效）
8. 验收 4 的离线版：库里不再存在缺 doc_id 的切片

与相邻测试的分工（别混）：
    module3  向量库基础（增删查、分数换算）
    module8  MySQL 会话存储：迁移零漂移 / 并发 / 软 TTL
    module9  文档解析链路：状态机 + 队列 + 文档接口（upload/list/chunks/reparse/delete）
    module10 引用反查：chunk_id 契约 + 切片级接口（本文件）
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

# 想要导入自定义包或者模块，建议将项目根目录加入系统路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging

setup_logging()

ROOT = Path(__file__).parent.parent

PASS = 0
FAIL = 0
SKIPPED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """统一的断言输出：通过/失败计数并打印。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def skip(name: str, why: str) -> None:
    """显式记录跳过（而不是静默不跑）—— 静默跳过会让「全绿」失去意义。"""
    SKIPPED.append(f"{name}（{why}）")
    print(f"  [SKIP] {name} | {why}")


# --------------------------------------------------------------------------- #
# 前置：MySQL 可用性
# --------------------------------------------------------------------------- #
from core.db import check_connection, session_scope  # noqa: E402
from core.schema import chat_message_table, document_table, session_table  # noqa: E402
from sqlalchemy import delete as sa_delete  # noqa: E402
from sqlalchemy import select  # noqa: E402

conn = check_connection()
if not conn.get("ok"):
    print(f"\n[SKIP] MySQL 不可达，本模块无法验证：{conn.get('detail')}")
    print("       起库：make infra（或 docker compose up -d mysql）")
    print("       强制要求：REQUIRE_MYSQL=1")
    if os.environ.get("REQUIRE_MYSQL") == "1":
        sys.exit(1)
    sys.exit(0)

from config.settings import settings  # noqa: E402
from core import document_repo as repo  # noqa: E402
from core.parsing import parse_and_index, resolve_storage_path  # noqa: E402
from core.vector_store import (  # noqa: E402
    CHUNK_ID_SEP,
    VectorStoreManager,
    build_chunk_id,
    parse_chunk_id,
)
from langchain_core.documents import Document  # noqa: E402

print(f"\nMySQL 已连通：{conn.get('detail')}")

U = uuid.uuid4().hex[:8]
UPLOAD = ROOT / "upload"

# 本模块的所有向量操作都在这张临时库里，绝不动 vector_db/
_tmp = tempfile.TemporaryDirectory()
vs = VectorStoreManager(store_type="chroma", persist_dir=Path(_tmp.name), collection_name=f"m10_{U}")


def _cleanup_documents() -> None:
    """
    只删**本模块自己建的**文档行（file_name 带 `m10_<uuid>_` 前缀）。

    ⚠️ 不能图省事写 `sa_delete(document_table)` 全表清空：
    本机 MySQL 里同时放着真实知识库文档，跑一次回归测试就把它们抹掉，
    而它们的切片还留在 Chroma 里 —— 下一次问答会召回一堆
    `document_exists=false` 的「幽灵引用」，且没有任何报错提示是谁干的。
    与 `_cleanup_files()` / `_cleanup_sessions()` 一样按前缀限定，三处口径一致。
    """
    with session_scope() as s:
        s.execute(
            sa_delete(document_table).where(document_table.c.file_name.like(f"m10_{U}%"))
        )


def _cleanup_files() -> None:
    for p in UPLOAD.glob(f"m10_{U}*"):
        p.unlink(missing_ok=True)


_cleanups: list = []


def _cleanup_sessions() -> None:
    with session_scope() as s:
        s.execute(sa_delete(chat_message_table).where(chat_message_table.c.session_id.like(f"m10_{U}%")))
        # session 表的主键列名就是 id（不是 session_id），chat_message 那边才叫 session_id
        s.execute(sa_delete(session_table).where(session_table.c.id.like(f"m10_{U}%")))


_cleanups.append(_cleanup_sessions)
_cleanups.append(_cleanup_documents)
_cleanups.append(_cleanup_files)


# --------------------------------------------------------------------------- #
# 第 1 组：chunk_id 契约（build / parse 往返与边界）
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：chunk_id 契约 ==")

check("build_chunk_id 是 doc_id:chunk_index", build_chunk_id(12, 3) == "12:3", build_chunk_id(12, 3))
check("往返一致", parse_chunk_id(build_chunk_id(7, 42)) == (7, 42))

# 合法形态：两侧空格、前导零都放行（它们解析出的就是同一个切片，不是「猜」）
check("两侧空格被忽略", parse_chunk_id("  12:3  ") == (12, 3))
check("前导零归一（指向同一个切片）", parse_chunk_id("012:3") == (12, 3))
check("第 0 片是合法序号", parse_chunk_id("1:0") == (1, 0))

# 非法形态：一律 ValueError（不是静默容错、也不是 500）
_bad_inputs: list[tuple[str, str]] = [
    ("abc", "完全不是数字"),
    ("12", "缺 chunk_index"),
    ("12:3:4", "多了一个分隔符"),
    ("12:", "chunk_index 为空"),
    (":3", "doc_id 为空"),
    ("-1:0", "doc_id 为负"),
    ("1.0:2", "小数"),
    ("", "空串"),
]
for raw, why in _bad_inputs:
    try:
        got = parse_chunk_id(raw)
        check(f"非法输入应抛 ValueError（{why}）", False, f"收到 {raw!r} 却返回 {got}")
    except ValueError:
        check(f"非法输入应抛 ValueError（{why}）", True)

# 这两条是 `isdigit()` 的陷阱：它对全角数字与上标都返回 True，
# 但 int() 对上标会抛异常 —— 于是「校验通过、转换炸掉」，表现成 500 而不是 400。
for raw, why in [("１２:３", "全角数字"), ("1:²", "上标数字")]:
    try:
        got = parse_chunk_id(raw)
        check(f"isdigit 陷阱应被挡掉（{why}）", False, f"收到 {raw!r} 却返回 {got}")
    except ValueError:
        check(f"isdigit 陷阱应被挡掉（{why}）", True)


# --------------------------------------------------------------------------- #
# 第 2 组：写入侧 —— parse_and_index 写全了三个字段
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：写入侧的 chunk_id ==")

# 先清一遍：上一次失败的运行可能留下了同前缀的脏数据，会让断言出现假阴/假阳
for _fn in _cleanups:
    _fn()

SRC = UPLOAD / f"m10_{U}_引用.txt"
SRC.write_text(
    "差旅报销标准：市内交通实报实销，住宿按职级上限。" * 60,
    encoding="utf-8",
)
SRC_REL = f"upload/{SRC.name}"

doc_id = repo.create_pending(file_name=f"m10_{U}_引用.txt", storage_path=SRC_REL, file_size=SRC.stat().st_size)
n_chunks = parse_and_index(doc_id, SRC_REL, file_name=SRC.name, store=vs)
# 手动补一次状态推进：parse_and_index 只管「解析入库」，写状态是 run_parse_task 的事
# （见 core/parsing.py 的分层说明）。这里要让后续断言里的 document_status 是 success。
repo.mark_success(doc_id, n_chunks)
check("解析出多片（否则后面的序号断言没意义）", n_chunks >= 3, str(n_chunks))

raw = vs._store.get(where={"doc_id": doc_id}, include=["metadatas"])  # noqa: SLF001
metas = raw["metadatas"]
check("每个切片都带 chunk_id", all(m.get("chunk_id") for m in metas), str(metas[0].get("chunk_id")))
check(
    "chunk_id 与 doc_id+chunk_index 完全自洽",
    all(m.get("chunk_id") == build_chunk_id(m.get("doc_id"), m.get("chunk_index")) for m in metas),
    str([m.get("chunk_id") for m in metas]),
)
check(
    "chunk_id 覆盖全部序号且不重复（引用键必须是唯一的）",
    sorted(m["chunk_id"] for m in metas) == sorted(set(m["chunk_id"] for m in metas)),
    str(sorted(m["chunk_id"] for m in metas)),
)
check("chunk_id 的分隔符与常量一致", CHUNK_ID_SEP in (metas[0].get("chunk_id") or ""), str(metas[0].get("chunk_id")))

# 幂等：重复解析不应该让 chunk_id 漂移（漂移 = 历史引用集体失效）
_ids_before = sorted(m["chunk_id"] for m in metas)
parse_and_index(doc_id, SRC_REL, file_name=SRC.name, store=vs)
_ids_after = sorted(m["chunk_id"] for m in
                    vs._store.get(where={"doc_id": doc_id}, include=["metadatas"])["metadatas"])  # noqa: SLF001
check("**重复解析后 chunk_id 不变**（历史引用不会失效）", _ids_before == _ids_after,
      f"{_ids_before[:2]} -> {_ids_after[:2]}")


# --------------------------------------------------------------------------- #
# 第 3 组：读取侧 —— get_chunk_by_position
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：按坐标取切片 ==")

hit = vs.get_chunk_by_position(doc_id, 0)
check("命中第 0 片", hit is not None)
check("命中项的 chunk_id 是规范形态", hit is not None and hit["chunk_id"] == f"{doc_id}:0", str(hit and hit["chunk_id"]))
check("正文非空", hit is not None and len(hit["content"]) > 0)
check("char_count 与正文长度一致", hit is not None and hit["char_count"] == len(hit["content"]))
check("带出 doc_id 与 chunk_index", hit is not None and (hit["doc_id"], hit["chunk_index"]) == (doc_id, 0))
check("带出归属项目与文件名", hit is not None and hit["file_name"] == SRC.name, str(hit and hit["file_name"]))

check("越界序号返回 None（不是抛异常）", vs.get_chunk_by_position(doc_id, 9999) is None)
check("不存在的 doc_id 返回 None", vs.get_chunk_by_position(2**62, 0) is None)
check("字符串 doc_id 也可用（接口层拿到的是字符串时会走这条路）",
      (vs.get_chunk_by_position(str(doc_id), 0) or {}).get("chunk_index") == 0)

for bad in [True, "abc", 1.5]:
    try:
        vs.get_chunk_by_position(bad, 0)
        check(f"非法 doc_id 应抛 TypeError（{bad!r}）", False, "却静默返回了")
    except TypeError:
        check(f"非法 doc_id 应抛 TypeError（{bad!r}）", True)

# 类型严格：元数据里 chunk_index 落成字符串 "0" 时不能误命中第 0 片。
# 这是 Chroma where 精确匹配的同一个道理 —— 类型不一致就是两条不同的数据。
vs.add_documents([
    Document(
        page_content="刻意让 chunk_index 是字符串",
        metadata={"source": "/x/stridx.txt", "doc_id": 777, "chunk_index": "0"},
    )
])
check("chunk_index 是字符串时不被误命中（类型严格）", vs.get_chunk_by_position(777, 0) is None)
vs.delete_by_doc_id(777)


# --------------------------------------------------------------------------- #
# 第 4 组：孤儿识别与清理
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：孤儿切片的识别与清理 ==")

vs.add_documents([
    Document(
        page_content="P0-3 之前的遗留切片，连 doc_id 都没有",
        metadata={"source": "/x/legacy.txt", "file_name": "legacy.txt"},
    ),
    Document(
        page_content="文档记录已被删，切片却留下来了",
        metadata={"source": "/x/gone.txt", "doc_id": 888, "chunk_index": 0},
    ),
])
valid = {doc_id}
orphans = vs.list_orphan_chunks(valid)
check("识别出 2 条孤儿", len(orphans) == 2, str(len(orphans)))
check("孤儿里含「无 doc_id」的遗留切片",
      any(not isinstance(m.get("doc_id"), int) for _, m in orphans))
check("孤儿里含「doc_id 不在有效集合」的切片",
      any(m.get("doc_id") == 888 for _, m in orphans))

before = vs.count()
deleted = vs.purge_orphan_chunks(valid)
check("清理了 2 条", deleted == 2, str(deleted))
check("有效切片一条没少（**只清孤儿**）", vs.count_by_doc_id(doc_id) == n_chunks,
      f"{vs.count_by_doc_id(doc_id)} vs {n_chunks}")
check("总数按预期下降", vs.count() == before - 2, f"{before} -> {vs.count()}")
check("有效文档的第 0 片仍然取得到", vs.get_chunk_by_position(doc_id, 0) is not None)
check("**清理是幂等的**（再跑一次返回 0）", vs.purge_orphan_chunks(valid) == 0)


# --------------------------------------------------------------------------- #
# 第 5 组：sources 带出 chunk_id
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：_extract_sources 与 _resolve_chunk_id ==")

from core.rag_chain import RAGChain, _resolve_chunk_id  # noqa: E402

docs_new = [
    Document(page_content="新切片 A", metadata={
        "source": "/x/a.txt", "doc_id": 12, "chunk_index": 2, "chunk_id": "12:2",
        "rerank_score": 0.9, "vector_similarity": 0.8,
    }),
    Document(page_content="新切片 B", metadata={
        "source": "/x/b.txt", "doc_id": 13, "chunk_index": 0, "chunk_id": "13:0",
    }),
]
src_new = RAGChain._extract_sources(docs_new)
check("sources 带 chunk_id", [s["chunk_id"] for s in src_new] == ["12:2", "13:0"], str(src_new))
check("原有字段一个没丢（index/source/snippet/两个分数）",
      all({"index", "source", "snippet", "rerank_score", "vector_similarity"} <= set(s) for s in src_new))
check("snippet 仍是 200 字截断（不是全文）", all(len(s["snippet"]) <= 200 for s in src_new))

# P0-3a 期间入库的切片：只有 doc_id + chunk_index，没有 chunk_id
docs_old = [
    Document(page_content="P0-3a 切片", metadata={"source": "/x/c.txt", "doc_id": 14, "chunk_index": 5}),
]
src_old = RAGChain._extract_sources(docs_old)
check("**老切片由 doc_id+chunk_index 兜底拼出 chunk_id**（不必整库重建）",
      src_old[0]["chunk_id"] == "14:5", str(src_old[0].get("chunk_id")))

# P0-3 之前的遗留切片：连 doc_id 都没有
docs_legacy = [Document(page_content="遗留切片", metadata={"source": "/x/d.txt", "file_name": "d.txt"})]
src_legacy = RAGChain._extract_sources(docs_legacy)
check("遗留切片的 chunk_id 为 None（前端据此渲染成不可点击）",
      src_legacy[0]["chunk_id"] is None, str(src_legacy[0].get("chunk_id")))

# _resolve_chunk_id 的边界
check("元数据里已有 chunk_id 时直接采用", _resolve_chunk_id({"chunk_id": "9:9", "doc_id": 1, "chunk_index": 2}) == "9:9")
check("doc_id 是 bool 时拒绝（True 不该被当成文档 1）", _resolve_chunk_id({"doc_id": True, "chunk_index": 0}) is None)
check("chunk_index 是 bool 时拒绝", _resolve_chunk_id({"doc_id": 1, "chunk_index": True}) is None)
check("chunk_index 缺失时拒绝", _resolve_chunk_id({"doc_id": 1}) is None)
check("chunk_id 是空串时不采用（退回拼）", _resolve_chunk_id({"chunk_id": "", "doc_id": 3, "chunk_index": 4}) == "3:4")

# ---- 响应模型契约：最容易静默失效的一环 ----
# 直觉上「_extract_sources() 往 dict 里塞了什么，接口就返回什么」，这是**想当然**。
# FastAPI 的 response_model 会按 pydantic 字段过滤，多出来的键被**静默丢掉**
# （pydantic 默认 extra='ignore'）。P0-4a 首次联调就栽在这儿：
# 后端日志里 chunk_id 好端端在，HTTP 响应里一个都没有，且不报任何错、测试也全绿。
# 所以这里不逐个字段断言，而是断言「产出的键 ⊆ 模型声明的字段」——
# 以后再往 sources 里加字段却忘了同步 `SourceItem`，这条会立刻红。
from api.routes.qa import SourceItem  # noqa: E402

_produced_keys = set(src_new[0]) | set(src_old[0]) | set(src_legacy[0])
_missing_fields = _produced_keys - set(SourceItem.model_fields)
check("**_extract_sources 产出的每个键都在 SourceItem 里声明过**（防 response_model 静默丢弃）",
      not _missing_fields, f"未声明：{sorted(_missing_fields)}")
check("chunk_id 能穿过 SourceItem 序列化（问答接口真的带得出去）",
      SourceItem(**src_new[0]).model_dump().get("chunk_id") == "12:2")
check("chunk_id 为 None 时也带得出去（不能被当成缺失字段吞掉）",
      SourceItem(**src_legacy[0]).model_dump().get("chunk_id") is None)
# 会话历史用的是同一份 SourceItem（刷新页面后引用要还在），所以这里测一次就够


# --------------------------------------------------------------------------- #
# 第 6 组：ref_ids 落库（P0-1b 建好的列，P0-4a 才真正有值）
# --------------------------------------------------------------------------- #
print("\n== 第 6 组：chat_message.ref_ids 接通 ==")

from core.mysql_store import MySQLSessionStore  # noqa: E402
from core.session_store import SessionSnapshot  # noqa: E402

check("纯函数层面：_extract_ref_ids 读 chunk_id",
      MySQLSessionStore._extract_ref_ids(  # type: ignore[attr-defined]
          {"sources": [{"chunk_id": "12:3"}, {"chunk_id": "12:4"}]}
      ) == ["12:3", "12:4"])
check("sources 里没有 chunk_id 时返回空列表（老数据）",
      MySQLSessionStore._extract_ref_ids({"sources": [{"source": "/x/a.txt"}]}) == [])  # type: ignore[attr-defined]
check("sources 缺失时返回空列表", MySQLSessionStore._extract_ref_ids({}) == [])  # type: ignore[attr-defined]
check("**不把 None 塞进数组**（遗留切片会让引用计数虚高）",
      MySQLSessionStore._extract_ref_ids(  # type: ignore[attr-defined]
          {"sources": [{"chunk_id": None}, {"chunk_id": "12:3"}]}
      ) == ["12:3"])

sid = f"m10_{U}_ref"
mysql_store = MySQLSessionStore(ttl_seconds=3600)
mysql_store.save(
    sid,
    SessionSnapshot(
        messages=[
            {"role": "user", "content": "报销标准是什么？"},
            {"role": "assistant", "content": "市内交通实报实销。"},
        ],
        exchange_meta=[
            {"sources": [{"index": 1, "chunk_id": f"{doc_id}:0"}, {"index": 2, "chunk_id": f"{doc_id}:1"}],
             "ts": 1.0, "usage": {}},
        ],
    ),
)

with session_scope() as s:
    row = s.execute(
        select(chat_message_table.c.ref_ids)
        .where(chat_message_table.c.session_id == sid)
        .where(chat_message_table.c.seq == 1)
    ).first()
db_ref_ids = row[0] if row else None
check("ref_ids 真的落进了数据库（不再是恒为 []）",
      db_ref_ids == [f"{doc_id}:0", f"{doc_id}:1"], str(db_ref_ids))

# 用户行不该带引用元数据 —— 引用是「AI 这一答」的属性
with session_scope() as s:
    row_user = s.execute(
        select(chat_message_table.c.ref_ids)
        .where(chat_message_table.c.session_id == sid)
        .where(chat_message_table.c.seq == 0)
    ).first()
check("用户行的 ref_ids 为 NULL（引用只挂 assistant 行）", (row_user[0] if row_user else None) is None,
      str(row_user and row_user[0]))


# --------------------------------------------------------------------------- #
# 第 7 组：HTTP 接口层
# --------------------------------------------------------------------------- #
print("\n== 第 7 组：GET /api/v1/chunks/{chunk_id} ==")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app as fastapi_app  # noqa: E402
from api.routes import chunks as chunks_route  # noqa: E402

client = TestClient(fastapi_app)

# 只打这一处桩：让反查接口用本模块的临时库，而不是污染 vector_db/。
# MySQL 不打桩 —— ref_ids / 文件名 / 上传时间都要读真的表才有意义。
_orig_get_vs = chunks_route.get_vector_store_manager
chunks_route.get_vector_store_manager = lambda: vs  # type: ignore[assignment]

try:
    good_id = f"{doc_id}:0"
    print("\n  -- 命中 --")
    r = client.get(f"/api/v1/chunks/{good_id}")
    check("命中返回 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    body = r.json() if r.status_code == 200 else {}
    check("正文与直接读向量库一致", body.get("content") == (hit or {}).get("content"))
    check("带出 doc_id / chunk_index", (body.get("doc_id"), body.get("chunk_index")) == (doc_id, 0))
    check("带出文件名（来自 MySQL）", body.get("file_name") == SRC.name, str(body.get("file_name")))
    check("带出上传时间（来自 MySQL）", bool(body.get("upload_time")), str(body.get("upload_time")))
    check("带出归属项目", body.get("project_id") == "default", str(body.get("project_id")))
    check("document_exists=true", body.get("document_exists") is True)
    check("document_status 有值", body.get("document_status") == repo.STATUS_SUCCESS, str(body.get("document_status")))
    # 与 sources 里那条 snippet 做对照：同一个切片，sources 只给 200 字摘要，
    # 本接口给全文 —— 这正是「引用要能点开」的价值所在。
    _snippet = RAGChain._extract_sources(
        [Document(page_content=hit["content"], metadata={"source": "/x/a.txt"})]
    )[0]["snippet"]
    check(
        "**返回全文而不是 200 字摘要**（snippet 只是它的前缀）",
        len(body.get("content") or "") >= len(_snippet)
        and (body.get("content") or "").startswith(_snippet),
        f"全文 {len(body.get('content') or '')} 字 / 摘要 {len(_snippet)} 字",
    )

    print("\n  -- 格式非法（400，不是 404）--")
    for bad in ["abc", "12", "12:3:4", "-1:0", "1:²"]:
        rr = client.get(f"/api/v1/chunks/{bad}")
        check(f"非法 chunk_id 返回 400（{bad!r}）", rr.status_code == 400,
              f"{rr.status_code} {rr.text[:120]}")

    print("\n  -- 引用内容已随文档删除（404）--")
    gone_rel = f"upload/m10_{U}_已删.txt"
    (UPLOAD / f"m10_{U}_已删.txt").write_text("这份文档马上会被删掉。" * 20, encoding="utf-8")
    gone_id = repo.create_pending(file_name=f"m10_{U}_已删.txt", storage_path=gone_rel, file_size=10)
    parse_and_index(gone_id, gone_rel, file_name=f"m10_{U}_已删.txt", store=vs)
    check("删除前能查到（前置条件）", client.get(f"/api/v1/chunks/{gone_id}:0").status_code == 200)
    # 「三件事」：切片 + 磁盘 + 记录，全删掉才算真的删干净
    vs.delete_by_doc_id(gone_id)
    (UPLOAD / f"m10_{U}_已删.txt").unlink(missing_ok=True)
    repo.delete(gone_id)
    rr = client.get(f"/api/v1/chunks/{gone_id}:0")
    check("删除后返回 404", rr.status_code == 404, f"{rr.status_code} {rr.text[:120]}")
    check("404 的文案是「已随文档删除」（前端据此降级展示）",
          rr.status_code == 404 and "已随文档删除" in rr.json().get("detail", ""),
          rr.json().get("detail", "") if rr.status_code == 404 else "")

    print("\n  -- 文档还在但切片没了（404，文案不同）--")
    alive_id = repo.create_pending(file_name=f"m10_{U}_重解析.txt", storage_path=f"upload/m10_{U}_重解析.txt", file_size=10)
    (UPLOAD / f"m10_{U}_重解析.txt").write_text("这份文档的切片会被单独清掉。" * 20, encoding="utf-8")
    parse_and_index(alive_id, f"upload/m10_{U}_重解析.txt", file_name=f"m10_{U}_重解析.txt", store=vs)
    vs.delete_by_doc_id(alive_id)          # 只清切片，记录留着 —— 模拟「正在重新解析」
    rr = client.get(f"/api/v1/chunks/{alive_id}:0")
    check("返回 404", rr.status_code == 404, f"{rr.status_code} {rr.text[:120]}")
    check("文案提示「已失效 / 刷新」（与「已删除」区分开）",
          rr.status_code == 404 and ("失效" in rr.json().get("detail", "")),
          rr.json().get("detail", "") if rr.status_code == 404 else "")

    print("\n  -- 切片在、记录没了（脏数据：照常返回，但如实标记）--")
    orphan_id = 909090
    vs.add_documents([
        Document(page_content="孤儿切片：文档记录先没了", metadata={
            "source": "/x/orphan.txt", "doc_id": orphan_id, "chunk_index": 0,
            "chunk_id": f"{orphan_id}:0", "file_name": "orphan.txt",
        })
    ])
    rr = client.get(f"/api/v1/chunks/{orphan_id}:0")
    check("仍返回 200（正文确实还在，不给用户报错）", rr.status_code == 200, f"{rr.status_code} {rr.text[:120]}")
    check("document_exists=false（这是「该跑重建脚本了」的信号）",
          rr.status_code == 200 and rr.json().get("document_exists") is False,
          str(rr.json().get("document_exists")) if rr.status_code == 200 else "")
    check("文件名退回用向量库元数据兜底",
          rr.status_code == 200 and rr.json().get("file_name") == "orphan.txt",
          str(rr.json().get("file_name")) if rr.status_code == 200 else "")
    vs.delete_by_doc_id(orphan_id)
    repo.delete(alive_id)
finally:
    chunks_route.get_vector_store_manager = _orig_get_vs  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 第 8 组：验收 4 的离线版 —— 库里不存在缺 doc_id 的切片
# --------------------------------------------------------------------------- #
print("\n== 第 8 组：库里不存在缺 doc_id 的切片 ==")

_metas = vs._store.get(include=["metadatas"])["metadatas"]  # noqa: SLF001
_missing_doc_id = [m for m in _metas if not isinstance(m.get("doc_id"), int)]
check("**每条切片都有 int 类型的 doc_id**", not _missing_doc_id, f"{len(_missing_doc_id)} 条缺失：{_missing_doc_id[:2]}")
check("每条切片都有 chunk_index", all(isinstance(m.get("chunk_index"), int) for m in _metas))
check("每条切片都有 chunk_id", all(m.get("chunk_id") for m in _metas))
check("以 MySQL 里的文档为准时没有孤儿",
      not vs.list_orphan_chunks({r.doc_id for r in repo.list_all()}),
      str(len(vs.list_orphan_chunks({r.doc_id for r in repo.list_all()}))))


# --------------------------------------------------------------------------- #
# 清理与汇总
# --------------------------------------------------------------------------- #
# 顺序同 module9：先按记录里的 storage_path 删文件，再清表。
# ⚠️ 这里的遍历必须限定在本模块建的文档上：`repo.list_all()` 是全表，
# 里面有真实知识库文档，无差别删会连带删掉 upload/ 下的原始文件。
for _r in repo.list_all():
    if not (_r.file_name or "").startswith(f"m10_{U}_"):
        continue
    try:
        _p = resolve_storage_path(_r.storage_path)
        if settings.UPLOAD_DIR.resolve() in _p.resolve().parents and _p.is_file():
            _p.unlink()
    except OSError:
        pass
for fn in _cleanups:
    try:
        fn()
    except Exception:  # noqa: BLE001 - 清理失败不该掩盖真实断言结果
        pass
_tmp.cleanup()

if SKIPPED:
    print(f"\n跳过 {len(SKIPPED)} 项：")
    for item in SKIPPED:
        print(f"  - {item}")

print(f"\n{'=' * 50}")
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
