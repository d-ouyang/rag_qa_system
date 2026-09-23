# pyright: basic
"""
P0-4a 验收脚本 —— 按 `docs/PLAN-v2.0.0.md` §P0-4 的 4 条验收标准**逐条实测**。

运行：
    .venv/bin/python tests/acceptance_p0_4a.py

与 `tests/test_module10_chunk_refs.py` 的分工（**别混**）：

    module10       回归测试。向量库换成临时 Chroma，只验「契约与逻辑正确」，
                   离线可跑、快、必须永远全绿。
    本脚本         验收。**一处都不打桩** —— 真 MySQL、真向量库目录、
                   真检索链路（真嵌入 + 真重排）、真重建脚本（subprocess），
                   走 HTTP 接口。目的是「计划书里承诺的那四件事真的成立吗」。

为什么验收里要真的跑一次重建脚本：
    验收第 3、4 条说的就是「重建」。用一段等价代码代替它，等于验收自己写的代码
    而不是用户真正会敲的那条命令 —— 而重建脚本最容易出问题的地方
    （补登记口径、清理范围）恰恰在脚本内部，不在它调用的那几个函数里。

本脚本会改动生产向量库（vector_db/）
    这是 P0-4 的一次性成本：库里现有的遗留切片（没有 doc_id）会被清掉，
    upload/ 下没有记录的文件会被补登记。这正是计划书要的结果。
    自己的测试产物会在结束时清掉；补登记进来的真实文件会**保留**。

需要：MySQL + Redis（`make infra`）。**要求没有 worker 在跑**（重建脚本自己会拒绝）。
"""

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging

setup_logging()

ROOT = Path(__file__).parent.parent

PASS = 0
FAIL = 0
CRITERIA: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    """统一断言输出。返回条件本身，方便在循环里顺手用。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"    [PASS] {name}")
    else:
        FAIL += 1
        print(f"    [FAIL] {name} | {detail}")
    return bool(condition)


def criterion(title: str, ok: bool, note: str = "") -> None:
    """登记一条「计划书验收标准」的结论 —— 这是本脚本的输出主体。"""
    CRITERIA.append((title, ok, note))
    print(f"\n  >>> 验收结论：{title} → {'成立' if ok else '不成立'} {note}")


# --------------------------------------------------------------------------- #
# 前置：依赖可用性（验收脚本不允许 SKIP）
# --------------------------------------------------------------------------- #
from config.settings import settings  # noqa: E402
from core import document_repo as repo  # noqa: E402
from core.db import check_connection  # noqa: E402
from core.parsing import parse_and_index, resolve_storage_path  # noqa: E402
from core.queue import queue_depth, worker_alive  # noqa: E402
from core.vector_store import get_vector_store_manager  # noqa: E402

_conn = check_connection()
if not _conn.get("ok"):
    print(f"\n[不可验收] MySQL 不可达：{_conn.get('detail')}")
    print("           起中间件：make infra")
    sys.exit(1)

_depth = queue_depth()
if not _depth.get("ok"):
    print(f"\n[不可验收] 队列 Redis 不可达：{_depth.get('detail')}")
    print("           起中间件：make infra")
    sys.exit(1)

_alive = worker_alive()
if _alive["ok"]:
    print(f"\n[不可验收] 已经有一个 worker 在跑：{_alive['workers']}")
    print("           重建脚本会拒绝在 worker 运行时执行（并发重灌会让切片翻倍），")
    print("           先把它停掉再跑（Ctrl-C 那个 make worker 的终端）。")
    sys.exit(1)

U = uuid.uuid4().hex[:8]
UPLOAD = ROOT / "upload"
VEC = get_vector_store_manager()
REINDEX = ROOT / "scripts" / "reindex.py"

print(f"\nMySQL ：{_conn.get('detail')}")
print(f"队列  ：{_depth.get('queue')} | 当前积压={_depth.get('depth')}")
print(f"向量库：{settings.VECTOR_STORE_TYPE} | 重建前总量={VEC.count()}")
print(f"本次标记：{U}（产物名都带它，便于清理与定位）")


# --------------------------------------------------------------------------- #
# 前置 2：后端服务必须**停着**
# --------------------------------------------------------------------------- #
# 这条不是洁癖，是 P0-4a 实测撞出来的硬约束（根因见 scripts/reindex.py 文件头）：
# 重建脚本会在另一个进程里改写 vector_db/，而后端进程正持有同一个目录的
# Chroma 句柄。两个进程同时动它，HNSW 索引会进入不一致状态 ——
# 实测表现是：查询返回 documents=None（问答接口 500），
# 以及 hnswlib 抛 "ef or M is too small"。两种都不是「少召回几条」这种温和降级。
def _backend_online() -> bool:
    """探测后端是否在跑（与 scripts/reindex.py 里那道检查同一个口径）。"""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 关掉本机代理
    try:
        with opener.open(f"http://127.0.0.1:{settings.API_PORT}/api/v1/qa/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 附属：chunk_id 里的冒号能否穿过鉴权网关
# --------------------------------------------------------------------------- #
# `doc_id:chunk_index` 用了冒号。冒号在 URL 路径里合法（RFC 3986 的 pchar 含 ":"），
# 但本项目前面还挡着一个 NestJS 代理（@All('api/v1/*')）。这里从网络层再验一次，
# 确认「直连后端能过」不等于「经网关也能过」—— 记忆里那条「该放的放了」就是这个意思。
#
# 放在最前面做，是因为它恰恰需要后端**开着**；而下面的重建要求后端**停着**。
# 一个脚本没法同时满足两者，所以顺序是「先趁它开着把网关验了，再要求你停掉」。
print("\n== 附属：冒号路径穿过鉴权网关（3000）==")

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_gw_ok = False
try:
    _gw_ok = _opener.open("http://127.0.0.1:3000/api/health", timeout=3).status == 200
except Exception:  # noqa: BLE001 - 网关没起不算验收失败，只是这一项没验到
    _gw_ok = False

_api_up = _backend_online()

if not _gw_ok or not _api_up:
    print(f"  [未验证] 网关 3000 在线={_gw_ok} / 后端 {settings.API_PORT} 在线={_api_up}")
    print("           这项要两端都在跑才验得到，而下面的重建要求后端停着 ——")
    print("           所以在「推荐姿势」（停后端再跑）下它会显示未验证，属预期。")
else:
    try:
        _req = urllib.request.Request(
            "http://127.0.0.1:3000/api/auth/login",
            data=json.dumps({"username": os.environ.get("GATEWAY_USER") or "admin",
                             "password": os.environ.get("GATEWAY_PASS") or "admin123"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        _token = json.loads(_opener.open(_req, timeout=5).read().decode("utf-8")).get("access_token")
        check("网关登录拿到 token", bool(_token), str(_token)[:40])
        if _token:
            # 挑一条真实存在的引用键去穿透代理
            _probe_id = None
            for _rec in repo.list_all():
                _chunk = VEC.get_chunk_by_position(_rec.doc_id, 0)
                if _chunk:
                    _probe_id = _chunk["chunk_id"]
                    break
            if not _probe_id:
                print("  [未验证] 库里还没有可反查的切片，跳过代理穿透")
            else:
                _req2 = urllib.request.Request(
                    f"http://127.0.0.1:3000/api/v1/chunks/{_probe_id}",
                    headers={"Authorization": f"Bearer {_token}"},
                )
                _r2 = _opener.open(_req2, timeout=8)
                check("**冒号路径能穿过网关**（代理没有吃掉它）", _r2.status == 200, f"{_r2.status}")
                _payload = json.loads(_r2.read().decode("utf-8"))
                check("经网关拿到的正文非空", bool(_payload.get("content")), str(_payload)[:120])
    except urllib.error.HTTPError as e:
        check("冒号路径穿过网关", False, f"HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:  # noqa: BLE001
        check("冒号路径穿过网关", False, f"{type(e).__name__}: {e}")

if _api_up:
    # 先顺手证明「重建脚本自己也会拦」—— 这道闸门是 P0-4a 才加的，
    # 不能只在这里读代码相信它，要真的看它拦一次。
    _guard = subprocess.run([sys.executable, str(REINDEX), "--apply"], cwd=str(ROOT),
                            capture_output=True, text=True)
    check("**后端在线时重建被拒绝**（退出码 2）", _guard.returncode == 2, f"code={_guard.returncode}")
    check("拒绝文案说清了后果（问答接口 500）", "问答接口 500" in _guard.stdout, _guard.stdout[-200:])
    print(f"\n[不可验收] 后端服务仍在 {settings.API_PORT} 上运行。")
    print("           重建必须在后端停着时跑（原因见 scripts/reindex.py 文件头）。")
    print("           请 Ctrl-C 掉那个 make api / make dev 的终端，再重跑本脚本。")
    print(f"           （本次已验完网关穿透那一项：{PASS} 通过 / {FAIL} 失败）")
    sys.exit(1 if FAIL else 0)

print("  · 后端未运行 —— 重建可以安全执行")


# --------------------------------------------------------------------------- #
# 检索探针：在**全新进程**里做一次检索并回报库的体检结果
# --------------------------------------------------------------------------- #
# 为什么必须是新进程，而不是「重置单例」：
# chromadb 在同一进程内按 (目录, collection) 共享集合实例，
# `reset_vector_store_manager()` 之后拿到的其实还是**同一个**集合对象 ——
# 它的 HNSW 索引仍然指向已被重建脚本删掉的 id，`query` 会为这些 id 返回
# documents=None，langchain 拿着 None 构造 Document 直接 pydantic 报错。
# 实测：重置单例之后检索照样崩，换成新进程就正常。
#
# 真运维里对应的动作是「重启后端服务」—— 换的是进程，不是句柄。
# 所以本脚本也用子进程来等价这一步，而不是假装在进程内重启。
_PROBE_SRC = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from config.logging_config import setup_logging
setup_logging()
from core import document_repo as repo
from core.retriever import get_rag_retriever
from core.rag_chain import RAGChain
from core.vector_store import get_vector_store_manager

question = sys.argv[2]
store = get_vector_store_manager()
hits = get_rag_retriever().retrieve(question, return_score=True)
docs = [d for d, _ in hits]
sources = RAGChain._extract_sources(docs)

# 库体检（验收第 4 条的判据）：只看元数据，用 get()，不碰 HNSW
valid_ids = {r.doc_id for r in repo.list_all()}
metas = store._store.get(include=["metadatas"])["metadatas"] or []
missing_doc_id = [m for m in metas if not isinstance(m.get("doc_id"), int)]
orphans = store.list_orphan_chunks(valid_ids)

sys.stdout.write("@@RESULT@@" + json.dumps({
    "ids": [s.get("chunk_id") for s in sources],
    "texts": [d.page_content for d in docs],
    "total": len(metas),
    "missing_doc_id": len(missing_doc_id),
    "orphans": len(orphans),
    "docs": len(repo.list_all()),
}, ensure_ascii=False) + "\\n")
'''


def probe(question: str) -> dict:
    """起一个全新进程做一次检索 + 库体检，返回结果 dict。"""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(_PROBE_SRC)
        path = f.name
    try:
        p = subprocess.run([sys.executable, path, str(ROOT), question],
                           cwd=str(ROOT), capture_output=True, text=True)
    finally:
        os.unlink(path)

    marker = "@@RESULT@@"
    lines = [ln for ln in p.stdout.splitlines() if marker in ln]
    if not lines:
        raise RuntimeError(f"探针没返回结果：exit={p.returncode}\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return json.loads(lines[-1].split(marker, 1)[1])


# --------------------------------------------------------------------------- #
# 准备：造 2 篇真文档，用真解析链路入库
# --------------------------------------------------------------------------- #
def make_doc(name: str, body: str) -> tuple[int, str]:
    """
    写一个真文件 → 建真记录 → 用**真解析链路**入库 → 标记 success。
    返回 (doc_id, storage_path)。

    为什么这里自己 mark_success 而不是入队等 worker：
    本脚本要求「没有 worker 在跑」（重建的硬性前提），
    而解析本身是同步可调的 —— run_parse_task 与 parse_and_index 是同一段代码。
    """
    path = UPLOAD / name
    path.write_text(body, encoding="utf-8")
    rel = f"upload/{name}"
    doc_id = repo.create_pending(file_name=name, storage_path=rel, file_size=path.stat().st_size)
    n = parse_and_index(doc_id, rel, file_name=name)
    repo.mark_success(doc_id, n)
    return doc_id, rel


DOC_A = f"p04a_{U}_差旅.txt"
DOC_B = f"p04a_{U}_设备.txt"
# ⚠️ 正文**不能**用「同一句话重复 N 遍」来凑长度：那样切出来的多个切片正文
# 完全相同，重排分并列，两次检索之间它们的返回顺序会互换 ——
# 于是「重建前后一致」这类断言会假失败（看起来像引用键漂移，其实是并列）。
# 每条都带上序号，保证切出来的每一片内容都不同。
BODY_A = " ".join(f"差旅报销标准第{i}条：市内交通实报实销，住宿按职级上限，一线城市每晚 600 元。" for i in range(1, 41))
BODY_B = " ".join(f"设备借用流程第{i}步：先在系统提交申请，管理员审批通过后到库房领用，归还时签字。" for i in range(1, 41))

print("\n== 准备：清掉上一轮的 p04a_* 产物 ==")
# 上一轮若是崩在中途，会把 p04a_* 的文档/文件留在库里。而重建脚本会把
# upload/ 下没有记录的文件**补登记**，残留就会混进本次重建，
# 让「重建前后一致」这类断言失去意义（还会出现正文重复的并列）。
# 放在造文档之前跑：此时本轮还没产生任何 p04a_* 的东西。
_purged = 0
for _rec in repo.list_all():
    if _rec.file_name.startswith("p04a_") or "/p04a_" in _rec.storage_path:
        VEC.delete_by_doc_id(_rec.doc_id)
        _p = resolve_storage_path(_rec.storage_path)
        if settings.UPLOAD_DIR.resolve() in _p.resolve().parents and _p.is_file():
            _p.unlink()
        repo.delete(_rec.doc_id)
        _purged += 1
for _left in UPLOAD.glob("p04a_*"):
    _left.unlink(missing_ok=True)
print(f"  · 清理上一轮残留 {_purged} 条记录；当前文档数={len(repo.list_all())}，切片数={VEC.count()}")

print("\n== 准备：造 2 篇真文档并入库 ==")
id_a, rel_a = make_doc(DOC_A, BODY_A)
id_b, rel_b = make_doc(DOC_B, BODY_B)
n_a = VEC.count_by_doc_id(id_a)
n_b = VEC.count_by_doc_id(id_b)
check("文档 A 已入库", n_a > 0, str(n_a))
check("文档 B 已入库", n_b > 0, str(n_b))
check("两篇互不串味（按 doc_id 隔离）", n_a != 0 and n_b != 0 and n_a + n_b == VEC.count_by_doc_id(id_a) + VEC.count_by_doc_id(id_b))


# --------------------------------------------------------------------------- #
# 验收 1：删除文档后三处无残留
# --------------------------------------------------------------------------- #
print("\n== 验收 1：删除文档后「磁盘 / Chroma / MySQL」三处无残留 ==")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app as fastapi_app  # noqa: E402

client = TestClient(fastapi_app)

# 删除前先把要用的 chunk_id 记下来 —— 删完就用它去反查，验「真的取不到了」
meta_a = VEC._store.get(where={"doc_id": id_a}, include=["metadatas"])["metadatas"]  # noqa: SLF001
cid_a0 = meta_a[0]["chunk_id"]
check("删除前：切片带 chunk_id", bool(cid_a0), str(cid_a0))
check("删除前：反查接口取得到", client.get(f"/api/v1/chunks/{cid_a0}").status_code == 200)
disk_before = resolve_storage_path(rel_a)

r = client.delete(f"/api/v1/documents/{id_a}")
check("DELETE 返回 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
del_body = r.json() if r.status_code == 200 else {}

third_disk = (not disk_before.is_file())
third_chroma = (VEC.count_by_doc_id(id_a) == 0)
third_mysql = (repo.get(id_a) is None)

check("① 磁盘原文件已删", third_disk, str(disk_before))
check("② Chroma 该 doc_id 的切片已清", third_chroma, f"剩余 {VEC.count_by_doc_id(id_a)} 条")
check("③ MySQL 记录已删", third_mysql)
check("删除接口如实汇报三件事",
      bool(del_body.get("file_removed")) and bool(del_body.get("record_removed"))
      and del_body.get("deleted_chunks", 0) > 0,
      str(del_body))

rr = client.get(f"/api/v1/chunks/{cid_a0}")
check("④ 删掉之后引用反查返回 404（死链，不是报错）", rr.status_code == 404, f"{rr.status_code}")
check("⑤ 404 文案是「已随文档删除」", "已随文档删除" in (rr.json().get("detail") if rr.status_code == 404 else ""),
      rr.json().get("detail") if rr.status_code == 404 else "")

# 成对断言：该删的删了，不该删的还在（白名单类机制必须正反都验，见 MEMORY 第 9 条）
check("⑥ 兄弟文档 B 一个切片都没少", VEC.count_by_doc_id(id_b) == n_b, f"{VEC.count_by_doc_id(id_b)} vs {n_b}")
check("⑦ 兄弟文档 B 的引用仍然可反查",
      client.get(f"/api/v1/chunks/{VEC.get_chunk_by_position(id_b, 0)['chunk_id']}").status_code == 200)

criterion(
    "删除文档后三处无残留",
    third_disk and third_chroma and third_mysql,
    f"（磁盘={third_disk} 向量={third_chroma} 记录={third_mysql}）",
)


# --------------------------------------------------------------------------- #
# 验收 2：点引用 → 通过 chunk_id 拿到切片正文与源文档信息
# --------------------------------------------------------------------------- #
print("\n== 验收 2：历史回答点引用 → chunk_id → 切片正文 + 源文档信息 ==")

from core.rag_chain import RAGChain  # noqa: E402
from core.retriever import get_rag_retriever  # noqa: E402

QUESTION = "设备借用要走什么流程？"
retriever = get_rag_retriever()
hits = retriever.retrieve(QUESTION, return_score=True)
check("真检索链路有召回（真嵌入 + 真重排）", len(hits) > 0, str(len(hits)))

docs = [d for d, _ in hits]
sources = RAGChain._extract_sources(docs)

# 重建**之前**，库里还混着 P0-3 之前的遗留切片（连 doc_id 都没有）。
# 它们的 chunk_id 必然是 None —— 这不是缺陷，恰恰是「必须重建一次」的理由：
# 那种切片既删不掉也反查不了。所以这里分开断言，而不是笼统地要求「全部都有」。
_pairs = list(zip(docs, sources))
_new_refs = [s for d, s in _pairs if isinstance(d.metadata.get("doc_id"), int)]
_legacy_refs = [s for d, s in _pairs if not isinstance(d.metadata.get("doc_id"), int)]
check("新入库文档的引用**都带** chunk_id（引用入口成立）",
      bool(_new_refs) and all(s.get("chunk_id") for s in _new_refs),
      str([s.get("chunk_id") for s in _new_refs]))
print(f"    · 另有 {len(_legacy_refs)} 条引用来自遗留切片 → chunk_id 为 None（本次重建要清掉的对象）")
check("遗留切片的引用键为 None（前端会渲染成不可点击，而不是给个必然报错的按钮）",
      all(s.get("chunk_id") is None for s in _legacy_refs))
check("sources 里的 snippet 仍是摘要（反查接口才是全文）",
      all(len(s["snippet"]) <= 200 for s in sources))

# 下面的反查要挑一条**真正有 chunk_id** 的引用
_indexed = [(d, s) for d, s in _pairs if s.get("chunk_id")]
top_doc, top = _indexed[0]
r = client.get(f"/api/v1/chunks/{top['chunk_id']}")
check("点引用 → 反查接口 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
body = r.json() if r.status_code == 200 else {}

check("拿到的正文与检索到的那一块一致",
      body.get("content") == top_doc.page_content, f"{len(body.get('content') or '')} vs {len(top_doc.page_content)}")
check("正文是全文（比 snippet 长，或至少相等）", len(body.get("content") or "") >= len(top["snippet"]))
check("chunk_id 往返一致", body.get("chunk_id") == top["chunk_id"], str(body.get("chunk_id")))
check("带出 doc_id / chunk_index",
      (body.get("doc_id"), body.get("chunk_index")) is not None and body.get("doc_id") > 0, str(body))

# 「源文档信息」：计划书点名要的三个字段 —— 文件名 / 上传时间 / 归属项目
check("拿到**源文件名**（来自 MySQL）", bool(body.get("file_name")), str(body.get("file_name")))
check("拿到**上传时间**（来自 MySQL）", bool(body.get("upload_time")), str(body.get("upload_time")))
check("拿到**归属项目**（来自 MySQL）", bool(body.get("project_id")), str(body.get("project_id")))
check("document_exists=true（切片与记录对得上）", body.get("document_exists") is True)

# 该拒的拒了：反查接口对垃圾输入必须 400 而不是 500
for bad in ["abc", "12:3:4", "-1:0"]:
    rbad = client.get(f"/api/v1/chunks/{bad}")
    check(f"非法 chunk_id → 400（{bad!r}）", rbad.status_code == 400, f"{rbad.status_code}")

criterion(
    "点引用 → 通过 chunk_id 拿到切片正文与源文档信息",
    r.status_code == 200 and body.get("content") == top_doc.page_content
    and bool(body.get("file_name")) and bool(body.get("upload_time")) and bool(body.get("project_id")),
)


# --------------------------------------------------------------------------- #
# 验收 3 + 4：真跑一次重建脚本，比对重建前后
# --------------------------------------------------------------------------- #
print("\n== 验收 3 / 4：真跑一次重建脚本 ==")

# 先注入一条「P0-3 之前的遗留切片」（没有 doc_id）—— 否则验收 4 没有对照物，
# 「库里不存在缺 doc_id 的切片」会变成一句永远成立、什么也没验的空话。
from langchain_core.documents import Document  # noqa: E402

VEC.add_documents([
    Document(
        page_content="这是 P0-3 之前入库的遗留切片，元数据里只有 source，没有 doc_id。",
        metadata={"source": f"/x/p04a_{U}_legacy.txt", "file_name": f"p04a_{U}_legacy.txt"},
    )
])
legacy_before = len([m for m in VEC._store.get(include=["metadatas"])["metadatas"]  # noqa: SLF001
                     if not isinstance(m.get("doc_id"), int)])
check("已注入遗留切片（作为验收 4 的对照物）", legacy_before >= 1, str(legacy_before))

# 重建前：同一个问题的检索结果快照（走探针 = 独立进程，与重建后口径完全一致）
before = probe(QUESTION)
before_ids, before_texts = before["ids"], before["texts"]
check("重建前有召回", len(before_ids) > 0, str(len(before_ids)))
print(f"    · 重建前：库内 {before['total']} 条 / 缺 doc_id {before['missing_doc_id']} 条")

print("\n  -- 干跑（先确认它打算做什么）--")
dry = subprocess.run(
    [sys.executable, str(REINDEX)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("干跑退出码为 0", dry.returncode == 0, f"code={dry.returncode} {dry.stdout[-300:]}")
print(f"    干跑报告行数：{len(dry.stdout.splitlines())}")
if dry.returncode != 0:
    print("    ---- 干跑 stdout ----")
    print(dry.stdout[-2000:])
    print("    ---- 干跑 stderr ----")
    print(dry.stderr[-2000:])

print("\n  -- 真执行（--apply）--")
# 到这里后端一定是停着的（开头那条前置检查保证了），所以不需要 --force，
# 重建脚本的两道闸门都自然通过 —— 这才是推荐的操作姿势。
run = subprocess.run(
    [sys.executable, str(REINDEX), "--apply"],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("重建脚本退出码为 0（没有文档解析失败）", run.returncode == 0, f"code={run.returncode}")
if run.returncode != 0:
    print("    ---- stdout ----")
    print(run.stdout[-2500:])
    print("    ---- stderr ----")
    print(run.stderr[-2500:])

# ---- 关键一步：重建之后的检索必须放在**新进程**里做 ----
# 重建脚本是在另一个进程里改写 vector_db/ 的。本进程的 chromadb 集合实例是共享的，
# 它的 HNSW 索引仍指向已被删除的 id，紧接着的 query 会返回 documents=None，
# langchain 拿 None 构造 Document 直接报错 —— 表现是问答接口 500。
# 重置单例没用（拿到的还是同一个集合对象，实测照样崩），只有换进程才行，
# 真运维里这一步就是「重启后端服务」。详见 probe() 上方的注释。
after = probe(QUESTION)
after_ids, after_texts = after["ids"], after["texts"]
print(f"    · 重建后：库内 {after['total']} 条 / 缺 doc_id {after['missing_doc_id']} 条")

check("**重建后全部引用都带 chunk_id**（遗留切片已被清掉，不再有点不开的引用）",
      bool(after_ids) and all(after_ids), str(after_ids))

check("重建后仍有召回（不是把库清空了）", len(after_ids) > 0, str(len(after_ids)))
check("**重建前后命中的是同一批切片**（引用键多重集一致）",
      sorted(before_ids) == sorted(after_ids),
      f"前 {before_ids} / 后 {after_ids}")
check("**重建前后正文逐字一致**", sorted(before_texts) == sorted(after_texts),
      f"前 {len(before_texts)} 条 / 后 {len(after_texts)} 条")
# 为什么按**多重集**而不是按原顺序比：
# 两块正文完全相同的切片（同名文件重复入库、或文档里有整段重复内容）
# 重排分并列，两次检索之间它们的先后顺序是任意的 —— 那是并列，不是漂移。
# 真正要守住的是「重建没有换掉任何一片」，也就是多重集相等。
print(f"    · 顺序是否也完全一致：{before_ids == after_ids}"
      f"（正文相同的切片会并列，顺序互换属正常）")

criterion(
    "重建后同一问题检索到的内容与重建前一致",
    bool(before_texts) and sorted(before_texts) == sorted(after_texts)
    and sorted(before_ids) == sorted(after_ids),
    f"（正文一致={sorted(before_texts) == sorted(after_texts)} 引用键一致={sorted(before_ids) == sorted(after_ids)}）",
)

# 验收 4：判据取自探针（新进程、干净句柄），不用本进程那个陈旧的 VEC
missing = after["missing_doc_id"]
orphans = after["orphans"]
check("**Chroma 里不存在缺 doc_id 的切片**", missing == 0, f"{missing} 条缺失")
check("**以 MySQL 为准时没有孤儿切片**", orphans == 0, f"{orphans} 条孤儿")
check("刚注入的那条遗留切片已经被清掉", legacy_before >= 1 and missing == 0,
      f"注入 {legacy_before} 条 / 仍缺 {missing} 条")

criterion(
    "Chroma 里不存在缺 doc_id 的切片",
    missing == 0 and orphans == 0,
    f"（缺 doc_id={missing} 条，孤儿={orphans} 条，总量={after['total']}）",
)


# --------------------------------------------------------------------------- #
# 清理（只清本脚本的产物；补登记进来的真实文件保留）
# --------------------------------------------------------------------------- #
print("\n== 清理 ==")
try:
    for doc_id in (id_b,):
        rec = repo.get(doc_id)
        if rec is None:
            continue
        VEC.delete_by_doc_id(doc_id)
        path = resolve_storage_path(rec.storage_path)
        if settings.UPLOAD_DIR.resolve() in path.resolve().parents and path.is_file():
            path.unlink()
        repo.delete(doc_id)
        print(f"  · 清理 doc_id={doc_id}（{rec.file_name}）")
    for leftover in UPLOAD.glob(f"p04a_{U}_*"):
        leftover.unlink(missing_ok=True)
        print(f"  · 清理残留文件 {leftover.name}")
except Exception as e:  # noqa: BLE001 - 清理失败要报出来，但不该盖住验收结论
    print(f"  [警告] 清理未完全成功：{type(e).__name__}: {e}")

print(f"  · 向量库总量：{VEC.count()}")
print(f"  · 剩余文档记录：{len(repo.list_all())}")


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
print("\n" + "=" * 66)
print("计划书 4 条验收标准的结论")
print("=" * 66)
for i, (title, ok, note) in enumerate(CRITERIA, 1):
    print(f"  {i}. [{'成立' if ok else '不成立'}] {title} {note}")

print(f"\n断言：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 66}")
sys.exit(1 if FAIL else 0)
