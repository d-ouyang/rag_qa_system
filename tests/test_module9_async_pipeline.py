# pyright: basic
"""
模块9测试文件：验证异步解析链路（core/document_repo.py、core/parsing.py、
core/queue.py、worker/、api/routes/documents.py）—— v2.0.0 P0-3a。

运行：
    .venv/bin/python tests/test_module9_async_pipeline.py      （或 make test）

依赖：
    **需要 MySQL**（document 表的真相源）。连不上时整模块 SKIP 并返回 0，
    让 `make test` 在没起中间件的机器上仍然能全绿。
    设 `REQUIRE_MYSQL=1` 可把 SKIP 变成失败（CI / 发布前自检用）。
    **不需要 Redis / worker**：入队被替换成桩函数，队列与 worker 只看结构不依赖在线。

覆盖点：
1. 状态机：create / get / claim / success / fail / reset / delete / counts
2. 原子抢任务：**多线程同时抢同一条，只能有一个成功**（幂等的根）
3. 孤儿 parsing 回收：worker 被 kill 后留下的 parsing 记录能被重新抢
4. 幂等：同一 doc_id 重复解析，向量库切片不翻倍
5. 向量库 doc_id 维度：delete_by_doc_id / get_chunks_by_doc_id / count_by_doc_id
6. run_parse_task：正常 / 损坏文件 / 文件不存在三条路径
7. 接口层：upload 立即返回、列表读 MySQL、chunks / reparse / download / delete
8. 队列抽象：入队失败降级为 False、队列深度与 worker 存活的结构
9. 真 worker 端到端（**仅当 worker 在线时执行**，否则打印 SKIP）

与相邻测试的分工（别混）：
    module6  会话存储的**行为契约**（内存 / Redis 两后端）
    module7  fakeredis over TCP：进程重启不丢历史
    module8  MySQL 会话存储：迁移零漂移 / 行级事实 / 并发 / 软 TTL / 与内存版 parity
    module9  文档解析链路：**文档**状态机 + 队列 + 接口（本文件）
"""

import os
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import timedelta
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
from core.schema import document_table  # noqa: E402
from sqlalchemy import delete as sa_delete  # noqa: E402

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
from core.db import now_db  # noqa: E402
from core.parsing import parse_and_index, resolve_storage_path, run_parse_task, ParseError  # noqa: E402
from core.parsing import _describe_unexpected  # noqa: E402
from core.vector_store import VectorStoreManager  # noqa: E402

print(f"\nMySQL 已连通：{conn.get('detail')}")

U = uuid.uuid4().hex[:8]
UPLOAD = ROOT / "upload"


def _cleanup_documents() -> None:
    """清空 document 表。本模块的断言依赖「表是干净的」。"""
    with session_scope() as s:
        s.execute(sa_delete(document_table))


def _cleanup_files() -> None:
    for p in UPLOAD.glob(f"m9_{U}*"):
        p.unlink(missing_ok=True)


_cleanup_documents()


# --------------------------------------------------------------------------- #
# 第 1 组：状态机（仓储层）
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：document 状态机 ==")

NAME = f"m9_{U}_制度.txt"
PATH_REL = f"upload/m9_{U}_doc.txt"

doc_id = repo.create_pending(file_name=NAME, storage_path=PATH_REL, file_size=100)
check("create_pending 返回正整数 doc_id", isinstance(doc_id, int) and doc_id > 0, str(doc_id))

rec = repo.get(doc_id)
check("get 拿到记录", rec is not None)
check("初始 status=pending", rec is not None and rec.status == repo.STATUS_PENDING)
check("初始 chunk_count=0", rec is not None and rec.chunk_count == 0)
check("初始 attempt_count=0", rec is not None and rec.attempt_count == 0)
check("初始 parse_started_at 为空", rec is not None and rec.parse_started_at is None)
check("get 不存在的 id 返回 None", repo.get(2**62) is None)

check("首次抢任务成功", repo.try_claim(doc_id) is True)
rec = repo.get(doc_id)
check("抢到后 status=parsing", rec.status == repo.STATUS_PARSING)
check("抢到后 attempt_count=1", rec.attempt_count == 1)
check("抢到后 parse_started_at 非空", rec.parse_started_at is not None)
check("未超时的 parsing 不可再抢（防重复解析）", repo.try_claim(doc_id) is False)

check("mark_success 生效", repo.mark_success(doc_id, 9) is True)
rec = repo.get(doc_id)
check("status=success", rec.status == repo.STATUS_SUCCESS)
check("chunk_count=9", rec.chunk_count == 9)
check("成功后 parse_started_at 清空", rec.parse_started_at is None)
check("success 不可再被抢", repo.try_claim(doc_id) is False)
check("success 不允许 reset_for_reparse", repo.reset_for_reparse(doc_id) is False)

check("mark_fail 生效", repo.mark_fail(doc_id, "y" * 600) is True)
rec = repo.get(doc_id)
check("status=fail", rec.status == repo.STATUS_FAIL)
check("fail_reason 截断到列宽 512", len(rec.fail_reason) == 512, f"实际 {len(rec.fail_reason)}")
check("失败时 chunk_count 归零", rec.chunk_count == 0)
check("fail 可被重新抢（省一次状态迁移）", repo.try_claim(doc_id) is True)
check("attempt_count 累加到 2", repo.get(doc_id).attempt_count == 2)

check("reset_for_reparse 生效", repo.reset_for_reparse(doc_id) is True)
rec = repo.get(doc_id)
check("重置回 pending", rec.status == repo.STATUS_PENDING)
check("重置清空 fail_reason", rec.fail_reason == "")
check("reset 不存在的 id 返回 False", repo.reset_for_reparse(2**62) is False)

counts = repo.counts_by_status()
check("counts_by_status 四种状态齐全", all(k in counts for k in repo.ALL_STATUSES), str(counts))
check("counts.total 自洽", counts["total"] == sum(counts[k] for k in repo.ALL_STATUSES), str(counts))

check("find_by_name 命中", repo.find_by_name(NAME) is not None)
check("find_by_name 未命中返回 None", repo.find_by_name(f"不存在的名字_{U}") is None)
check("list_documents 过滤 status", all(d.status == "pending" for d in repo.list_documents(status="pending")))
check("list_documents 支持 offset", isinstance(repo.list_documents(limit=1, offset=0), list))

check("delete 生效", repo.delete(doc_id) is True)
check("删除后 get 为 None", repo.get(doc_id) is None)
check("重复 delete 返回 False", repo.delete(doc_id) is False)


# --------------------------------------------------------------------------- #
# 第 2 组：原子抢任务（并发）
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：原子抢任务（16 线程抢同一条）==")

_cleanup_documents()
conc_id = repo.create_pending(file_name=f"m9_{U}_并发.txt", storage_path=f"upload/m9_{U}_conc.txt", file_size=1)

N_THREADS = 16
wins: list[int] = []
_barier = threading.Barrier(N_THREADS)
_lock = threading.Lock()


def _racer() -> None:
    _barier.wait()          # 尽量同时出发，把竞态窗口拉到最大
    if repo.try_claim(conc_id):
        with _lock:
            wins.append(1)


_threads = [threading.Thread(target=_racer) for _ in range(N_THREADS)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()

check("只有一个线程抢到", len(wins) == 1, f"抢到 {len(wins)} 个")
check("attempt_count 只加了一次", repo.get(conc_id).attempt_count == 1, str(repo.get(conc_id).attempt_count))


# --------------------------------------------------------------------------- #
# 第 3 组：孤儿 parsing 回收
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：孤儿 parsing 的识别与回收 ==")
# 场景：worker 解析途中被 kill -9，记录冻在 parsing，没有任何进程会去改它。
# 若抢任务的条件只看 pending/fail，这条就永远捡不回来 —— 这正是需要
# parse_started_at 这一列的原因（见 alembic/versions/0002 的文件头）。
from sqlalchemy import update  # noqa: E402


def _age_parsing(doc_id: int, seconds: float) -> None:
    """把 parse_started_at 往回拨，模拟「已经解析了很久」。"""
    with session_scope() as s:
        s.execute(
            update(document_table)
            .where(document_table.c.id == doc_id)
            .values(parse_started_at=now_db() - timedelta(seconds=seconds))
        )


# 先设成「100 秒前开始解析」，然后验证**阈值边界两侧**的行为：
# 阈值 900 秒时它还不算孤儿（可能真的在跑），阈值 50 秒时它算。
_age_parsing(conc_id, 100)
check("阈值 900s：不算孤儿（可能真在跑）", conc_id not in repo.list_orphan_parsing(stale_seconds=900))
check("阈值 50s：算孤儿", conc_id in repo.list_orphan_parsing(stale_seconds=50))
check("默认阈值（settings 里的 900s）下不算孤儿", conc_id not in repo.list_orphan_parsing())
check("因此默认阈值下抢不到（保护正在跑的任务）", repo.try_claim(conc_id) is False)

# 再拨到「很久以前」，越过默认阈值
_age_parsing(conc_id, 100000)
check("越过默认阈值后出现在孤儿列表", conc_id in repo.list_orphan_parsing())
check("越过阈值后可被重新抢（worker 被 kill 的记录能救回来）", repo.try_claim(conc_id) is True)

check("刚抢到的记录不会立刻被列为孤儿", conc_id not in repo.list_orphan_parsing())

_cleanup_documents()


# --------------------------------------------------------------------------- #
# 第 4 组：向量库的 doc_id 维度
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：向量库按 doc_id 删/查 ==")

from langchain_core.documents import Document  # noqa: E402

_tmp = tempfile.TemporaryDirectory()
vs = VectorStoreManager(store_type="chroma", persist_dir=Path(_tmp.name), collection_name=f"m9_{U}")

docs_a = [
    Document(page_content=f"A{i}", metadata={"source": "/x/a.txt", "doc_id": 11, "chunk_index": i})
    for i in range(3)
]
docs_b = [
    Document(page_content=f"B{i}", metadata={"source": "/x/b.txt", "doc_id": 22, "chunk_index": i})
    for i in range(2)
]
check("写入两篇共 5 片", vs.add_documents(docs_a + docs_b) == 5)
check("total=5", vs.count() == 5)
check("count_by_doc_id(11)=3", vs.count_by_doc_id(11) == 3)
check("count_by_doc_id(22)=2", vs.count_by_doc_id(22) == 2)
chunks_a = vs.get_chunks_by_doc_id(11)
check("get_chunks_by_doc_id 按 chunk_index 升序", [c["chunk_index"] for c in chunks_a] == [0, 1, 2],
      str([c["chunk_index"] for c in chunks_a]))
check("切片正文正确", chunks_a[0]["content"] == "A0", chunks_a[0]["content"])
check("不存在的 doc_id 返回空", vs.get_chunks_by_doc_id(99999) == [])
check("字符串形式的 doc_id 也可用", vs.count_by_doc_id("11") == 3)
try:
    vs.delete_by_doc_id(True)
    check("bool 作为 doc_id 应被拒绝", False)
except TypeError:
    check("bool 作为 doc_id 应被拒绝", True)
try:
    vs.delete_by_doc_id("abc")
    check("非数字字符串应被拒绝", False)
except TypeError:
    check("非数字字符串应被拒绝", True)
check("删 doc_id=11 返回 3", vs.delete_by_doc_id(11) == 3)
check("剩余 2（B 未受影响）", vs.count() == 2)
check("重复删返回 0", vs.delete_by_doc_id(11) == 0)


# --------------------------------------------------------------------------- #
# 第 5 组：parse_and_index（metadata 注入 + 幂等）
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：parse_and_index 的 metadata 与幂等 ==")

GOOD = UPLOAD / f"m9_{U}_good.txt"
BAD = UPLOAD / f"m9_{U}_bad.docx"
GOOD.write_text("差旅报销标准：市内交通实报实销，住宿按职级上限。" * 40, encoding="utf-8")
BAD.write_bytes(b"definitely not a zip, so docx parsing must fail")

good_rel = f"upload/{GOOD.name}"
bad_rel = f"upload/{BAD.name}"

check("相对路径解析到项目根", resolve_storage_path(good_rel) == ROOT / good_rel)
check("绝对路径原样使用", resolve_storage_path(str(GOOD)) == GOOD)

d1 = repo.create_pending(file_name=f"m9_{U}_原始名.txt", storage_path=good_rel, file_size=GOOD.stat().st_size)
n1 = parse_and_index(d1, good_rel, file_name=f"m9_{U}_原始名.txt", store=vs)
check("解析返回切片数 > 0", n1 > 0, str(n1))

raw = vs._store.get(where={"doc_id": d1}, include=["metadatas"])  # noqa: SLF001
metas = raw["metadatas"]
check("每个切片都带 doc_id", all(m.get("doc_id") == d1 for m in metas))
check("doc_id 的**类型**是 int（Chroma 的 where 按类型精确匹配）",
      all(isinstance(m.get("doc_id"), int) for m in metas))
check("metadata.project_id 存在", all(m.get("project_id") == "default" for m in metas))
check("metadata.file_name 是原始名（不是 uuid 落盘名）",
      all(m.get("file_name") == f"m9_{U}_原始名.txt" for m in metas),
      str(metas[0].get("file_name")))
check("metadata.source 仍是磁盘绝对路径（兼容按来源删除与老数据）",
      all(str(m.get("source")) == str(GOOD) for m in metas))
check("chunk_index 从 0 连续", sorted(m.get("chunk_index") for m in metas) == list(range(n1)),
      str(sorted(m.get("chunk_index") for m in metas)))

n2 = parse_and_index(d1, good_rel, file_name=f"m9_{U}_原始名.txt", store=vs)
check("重复解析返回同样片数", n2 == n1, f"{n2} vs {n1}")
check("**重复解析后库里切片未翻倍**", vs.count_by_doc_id(d1) == n1, str(vs.count_by_doc_id(d1)))

d_other_name = f"m9_{U}_另一篇.txt"
try:
    # storage_path 有唯一约束：同一个磁盘文件不能被两条记录指向。
    # 这条约束是 uuid 落盘方案的配套 —— 若允许共用，删除其中一条就会把
    # 另一条变成幽灵记录（记录在、文件没了）。
    d_other = repo.create_pending(file_name=d_other_name, storage_path=good_rel, file_size=1)
    parse_and_index(d_other, good_rel, file_name="另一篇", store=vs)
    check("同一磁盘文件挂第二条记录应被拒（uk_doc_storage_path）", False)
except Exception as e:  # noqa: BLE001
    check("同一磁盘文件挂第二条记录应被拒（uk_doc_storage_path）",
          "Duplicate" in str(e) or "1062" in str(e), f"{type(e).__name__}: {e}")

try:
    parse_and_index(99999, bad_rel, file_name="坏.docx", store=vs)
    check("损坏文件应抛 ParseError", False)
except ParseError as e:
    check("损坏文件应抛 ParseError", True)
    check("ParseError 的文案可读且带文件名",
          ("损坏" in str(e) or "解析" in str(e)) and "坏.docx" in str(e), str(e))


# --------------------------------------------------------------------------- #
# 第 6 组：run_parse_task 完整状态机
# --------------------------------------------------------------------------- #
print("\n== 第 6 组：run_parse_task 状态机 ==")

_cleanup_documents()
dA = repo.create_pending(file_name=f"m9_{U}_好.txt", storage_path=good_rel, file_size=GOOD.stat().st_size)
rA = run_parse_task(dA, store=vs)
check("正常文件 claimed=True", rA["claimed"] is True, str(rA))
check("正常文件 ok=True", rA["ok"] is True, str(rA))
recA = repo.get(dA)
check("status=success", recA.status == repo.STATUS_SUCCESS)
check("chunk_count 与返回值一致", recA.chunk_count == rA["chunk_count"], f"{recA.chunk_count} vs {rA['chunk_count']}")
check("切片确实写进了向量库", vs.count_by_doc_id(dA) == recA.chunk_count)

rA2 = run_parse_task(dA, store=vs)
check("重复执行同一任务 → claimed=False（幂等）", rA2["claimed"] is False, str(rA2))
check("重复执行后切片未翻倍", vs.count_by_doc_id(dA) == recA.chunk_count)

dB = repo.create_pending(file_name=f"m9_{U}_坏.docx", storage_path=bad_rel, file_size=BAD.stat().st_size)
rB = run_parse_task(dB, store=vs)
check("损坏文件 ok=False", rB["ok"] is False, str(rB))
recB = repo.get(dB)
check("损坏文件 status=fail", recB.status == repo.STATUS_FAIL)
check("fail_reason 可读（含文件名）", "坏.docx" in recB.fail_reason, recB.fail_reason)
check("损坏文件 chunk_count=0", recB.chunk_count == 0)

# 「先不存在、后补上」——模拟「补传文件后点重试」
LATE = UPLOAD / f"m9_{U}_late.txt"
LATE.unlink(missing_ok=True)
late_rel = f"upload/{LATE.name}"
dC = repo.create_pending(file_name=f"m9_{U}_晚到.txt", storage_path=late_rel, file_size=0)
rC = run_parse_task(dC, store=vs)
check("文件不存在 → ok=False", rC["ok"] is False, str(rC))
check("fail_reason 指出文件不存在", "不存在" in repo.get(dC).fail_reason, repo.get(dC).fail_reason)
check("fail 状态本身可被直接重跑（无需先 reset）", repo.get(dC).status == repo.STATUS_FAIL)

LATE.write_text("补上的内容。" * 30, encoding="utf-8")
rC2 = run_parse_task(dC, store=vs)
check("补上文件后重跑成功", rC2["ok"] is True, str(rC2))
check("重跑后 status=success", repo.get(dC).status == repo.STATUS_SUCCESS)
check("重跑清空 fail_reason", repo.get(dC).fail_reason == "")
check("重跑写入切片", vs.count_by_doc_id(dC) > 0, str(vs.count_by_doc_id(dC)))

n_before_reset = vs.count_by_doc_id(dC)
repo.mark_fail(dC, "人为置失败")
check("fail 允许 reset", repo.reset_for_reparse(dC) is True)
rC3 = run_parse_task(dC, store=vs)
check("reset 后重跑成功", rC3["ok"] is True, str(rC3))
check("reset 后重跑切片未翻倍", vs.count_by_doc_id(dC) == n_before_reset,
      f"{vs.count_by_doc_id(dC)} vs {n_before_reset}")


# --------------------------------------------------------------------------- #
# 第 7 组：基础设施异常的可读化
# --------------------------------------------------------------------------- #
print("\n== 第 7 组：异常文案可读化 ==")
check("SoftTimeLimitExceeded → 中文提示",
      "超时" in _describe_unexpected(type("SoftTimeLimitExceeded", (Exception,), {})()),
      _describe_unexpected(type("SoftTimeLimitExceeded", (Exception,), {})()))
check("未知异常保留类名（排查线索不能丢）",
      _describe_unexpected(KeyError("pages")).startswith("KeyError"),
      _describe_unexpected(KeyError("pages")))
check("无消息的异常不写成「类名: 」",
      not _describe_unexpected(ValueError()).endswith(": "),
      _describe_unexpected(ValueError()))


# --------------------------------------------------------------------------- #
# 第 8 组：队列抽象
# --------------------------------------------------------------------------- #
print("\n== 第 8 组：队列抽象（core/queue.py）==")

from core import queue as queue_mod  # noqa: E402
import worker.app as worker_app  # noqa: E402

check("import core.queue 不把 celery 拉进来（延迟导入生效）", "celery" not in getattr(queue_mod, "__dict__", {}))
check("队列名与 settings 一致", queue_mod.queue_depth()["queue"] == settings.TASK_QUEUE_NAME)

depth = queue_mod.queue_depth()
check("queue_depth 键恒定出现", set(depth) == {"ok", "queue", "depth", "unacked", "detail"}, str(sorted(depth)))
if depth["ok"]:
    check("队列可达时 depth 是整数", isinstance(depth["depth"], int), str(depth["depth"]))
    check("队列可达时 unacked 是整数", isinstance(depth["unacked"], int), str(depth["unacked"]))
else:
    skip("队列深度数值", f"Redis 不可达：{depth['detail']}")

alive = queue_mod.worker_alive()
check("worker_alive 键恒定出现", set(alive) == {"ok", "workers", "detail"}, str(sorted(alive)))
check("worker_alive 的 workers 是列表", isinstance(alive["workers"], list), str(type(alive["workers"])))

# 入队失败必须降级为 False，而不是抛异常 —— 状态真相在 MySQL，
# 抛出去会把「文件已保存成功」这件事一起抹掉。
_orig_send = worker_app.celery_app.send_task


def _boom(*_a: object, **_k: object) -> None:
    raise ConnectionError("模拟 broker 不可用")


worker_app.celery_app.send_task = _boom  # type: ignore[assignment]
try:
    check("broker 不可用时 enqueue_parse 返回 False 而不抛", queue_mod.enqueue_parse(1) is False)
finally:
    worker_app.celery_app.send_task = _orig_send  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 第 9 组：接口层（入队打桩，不依赖 worker）
# --------------------------------------------------------------------------- #
print("\n== 第 9 组：HTTP 接口层 ==")

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app as fastapi_app  # noqa: E402
from api.routes import documents as docs_route  # noqa: E402

client = TestClient(fastapi_app)

# 打桩两处，让接口测试不碰真实队列、不污染真实向量库：
#   · enqueue_parse  → 记录投递，不起 worker
#   · get_vector_store_manager → 用本模块的临时库，不动 vector_db/
QUEUE_LOG: list[int] = []
_orig_enqueue = docs_route.enqueue_parse
_orig_get_vs = docs_route.get_vector_store_manager
docs_route.enqueue_parse = lambda doc_id: (QUEUE_LOG.append(int(doc_id)) or True)  # type: ignore[assignment]
docs_route.get_vector_store_manager = lambda: vs  # type: ignore[assignment]

try:
    _cleanup_documents()
    vs_before = vs.count()

    UP_NAME = f"m9_{U}_上传.txt"
    UP_BODY = ("设备借用流程：先提申请，再找管理员领用。" * 30).encode("utf-8")

    print("\n  -- 上传 --")
    r = client.post("/api/v1/documents/upload", files={"file": (UP_NAME, UP_BODY, "text/plain")})
    check("upload 返回 202（已受理，不是已完成）", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    ub = r.json()
    new_id = ub.get("doc_id")
    check("响应含 doc_id", isinstance(new_id, int), str(new_id))
    check("响应 status=pending", ub.get("status") == "pending", str(ub.get("status")))
    check("响应 queued=True", ub.get("queued") is True, str(ub.get("queued")))
    check("**响应不含 chunks_added**（异步后此刻无法知道）", "chunks_added" not in ub, str(sorted(ub)))
    check("file_name 是原始名（展示用）", ub.get("file_name") == UP_NAME, str(ub.get("file_name")))
    check("file_size 正确", ub.get("file_size") == len(UP_BODY), str(ub.get("file_size")))
    check("上传过程**没有**解析（向量库切片数未变）", vs.count() == vs_before, f"{vs_before} -> {vs.count()}")
    check("已投递一次任务", QUEUE_LOG == [new_id], str(QUEUE_LOG))

    rec_up = repo.get(new_id)
    check("落盘用 uuid 名（不是原始名）", UP_NAME not in rec_up.storage_path, rec_up.storage_path)
    check("storage_path 是相对路径（容器化后仍可解析）", not Path(rec_up.storage_path).is_absolute(), rec_up.storage_path)
    check("磁盘文件真实存在", resolve_storage_path(rec_up.storage_path).is_file())

    r = client.post("/api/v1/documents/upload", files={"file": ("bad.bin", b"xx", "application/octet-stream")})
    check("不支持的后缀 → 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = client.post("/api/v1/documents/upload", files={"file": ("empty.txt", b"", "text/plain")})
    check("空文件 → 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    print("\n  -- 列表（读 MySQL）--")
    r = client.get("/api/v1/documents/")
    check("列表 200", r.status_code == 200, str(r.status_code))
    lb = r.json()
    check("total_documents=1", lb["total_documents"] == 1, str(lb["total_documents"]))
    check("counts 四态齐全", all(k in lb["counts"] for k in repo.ALL_STATUSES), str(lb["counts"]))
    check("列表项带 status（前端要靠它轮询）", lb["documents"][0]["status"] == "pending")
    check("列表项带 attempt_count", "attempt_count" in lb["documents"][0], str(sorted(lb["documents"][0])))
    r = client.get("/api/v1/documents/?status=success")
    check("按 status 过滤生效", r.json()["total_documents"] == 0, str(r.json()["total_documents"]))
    r = client.get("/api/v1/documents/?status=乱填的")
    check("非法 status → 400（而不是静默返回全部）", r.status_code == 400, str(r.status_code))

    print("\n  -- 切片 --")
    r = client.get(f"/api/v1/documents/{new_id}/chunks")
    check("未解析时 chunks 返回 200 + 0 片", r.status_code == 200 and r.json()["chunk_count"] == 0,
          f"{r.status_code} {r.text[:120]}")
    check("chunks 带 file_name 与 status",
          r.json()["file_name"] == UP_NAME and r.json()["status"] == "pending", str(r.json()))
    check("不存在的 doc_id → 404", client.get("/api/v1/documents/999999/chunks").status_code == 404)

    print("\n  -- 下载 --")
    r = client.get(f"/api/v1/documents/download?doc_id={new_id}")
    check("下载 200", r.status_code == 200, str(r.status_code))
    check("字节与上传一致", r.content == UP_BODY, f"{len(r.content)} vs {len(UP_BODY)}")
    disposition = urllib.parse.unquote(r.headers.get("content-disposition") or "")
    # Starlette 对非 ASCII 文件名用 RFC 5987 百分号编码，所以必须先 unquote
    check("下载文件名是原始名（不是 uuid）", UP_NAME in disposition, disposition)
    check("下载不存在的 doc_id → 404", client.get("/api/v1/documents/download?doc_id=999999").status_code == 404)

    print("\n  -- 重解析 --")
    QUEUE_LOG.clear()
    r = client.post(f"/api/v1/documents/{new_id}/reparse")
    check("pending 可重解析 → 202", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
    check("重解析重新入队一次", QUEUE_LOG == [new_id], str(QUEUE_LOG))
    repo.mark_success(new_id, 3)
    r = client.post(f"/api/v1/documents/{new_id}/reparse")
    check("success 拒绝重解析 → 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    check("不存在的 doc_id 重解析 → 404", client.post("/api/v1/documents/999999/reparse").status_code == 404)

    print("\n  -- 同名替换 --")
    r2 = client.post("/api/v1/documents/upload", files={"file": (UP_NAME, UP_BODY + b"more", "text/plain")})
    check("同名再传 → 202", r2.status_code == 202, str(r2.status_code))
    newer_id = r2.json()["doc_id"]
    check("生成新的 doc_id", newer_id != new_id, f"{newer_id} vs {new_id}")
    check("旧记录已被清掉", repo.get(new_id) is None)
    check("旧磁盘文件已删除", not resolve_storage_path(rec_up.storage_path).exists())
    check("列表里仍只有一条（不会积累重复内容）", client.get("/api/v1/documents/").json()["total_documents"] == 1)

    print("\n  -- 删除（三件事）--")
    newer_rec = repo.get(newer_id)
    r = client.delete(f"/api/v1/documents/{newer_id}")
    check("删除 200", r.status_code == 200, str(r.status_code))
    db = r.json()
    check("record_removed=True", db.get("record_removed") is True, str(db))
    check("file_removed=True", db.get("file_removed") is True, str(db))
    check("MySQL 记录已删", repo.get(newer_id) is None)
    check("磁盘文件已删", not resolve_storage_path(newer_rec.storage_path).exists())
    check("列表已空", client.get("/api/v1/documents/").json()["total_documents"] == 0)
    r = client.delete(f"/api/v1/documents/{newer_id}")
    check("重复删除幂等 → 200 且 record_removed=False",
          r.status_code == 200 and r.json()["record_removed"] is False, str(r.json()))

    print("\n  -- 越权路径拦截 --")
    # 用一个真实存在、但**不在 upload/ 里**的项目文件当靶子。
    # 不用 /etc/passwd 之类的系统文件：那要求测试拥有删除它们的权限才能
    # 「证明没被删」，而断言一个恒为真的条件等于没断言。
    outside_target = ROOT / "README.md"
    check("靶子文件存在（前置条件）", outside_target.is_file())
    evil_id = repo.create_pending(file_name="越权.txt", storage_path="README.md", file_size=10)

    r = client.get(f"/api/v1/documents/download?doc_id={evil_id}")
    check("storage_path 指向 upload/ 之外 → 403（读不到）", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
    r = client.delete(f"/api/v1/documents/{evil_id}")
    check("删除越权路径时不删项目外的文件",
          r.status_code == 200 and r.json()["file_removed"] is False, str(r.json()))
    check("靶子文件**仍然存在**（确实没被删）", outside_target.is_file())

    # 目录穿越写法也要拦：先 resolve 再判断，而不是比字符串前缀
    evil_id2 = repo.create_pending(file_name="穿越.txt", storage_path="upload/../../README.md", file_size=10)
    r = client.get(f"/api/v1/documents/download?doc_id={evil_id2}")
    check("`upload/../../README.md` 也被拦下（先 resolve 再判断）", r.status_code == 403, str(r.status_code))
    repo.delete(evil_id)
    repo.delete(evil_id2)
finally:
    docs_route.enqueue_parse = _orig_enqueue  # type: ignore[assignment]
    docs_route.get_vector_store_manager = _orig_get_vs  # type: ignore[assignment]

print("\n  -- GET /api/v1/system/queue --")
r = client.get("/api/v1/system/queue")
check("queue 接口 200（即使 broker/worker 不在也不 5xx）", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
qb = r.json()
check("含 queue 区段", isinstance(qb.get("queue"), dict), str(type(qb.get("queue"))))
check("含 worker 区段", isinstance(qb.get("worker"), dict), str(type(qb.get("worker"))))
check("含 documents 区段", isinstance(qb.get("documents"), dict), str(type(qb.get("documents"))))
check("queue.name 与配置一致", qb["queue"]["name"] == settings.TASK_QUEUE_NAME, str(qb["queue"]["name"]))
check("暴露 ping 超时（调用方要知道本接口有多慢）",
      qb.get("ping_timeout_seconds") == settings.QUEUE_WORKER_PING_TIMEOUT_SECONDS,
      str(qb.get("ping_timeout_seconds")))


# --------------------------------------------------------------------------- #
# 第 10 组：真 worker 端到端（有 worker 才跑）
# --------------------------------------------------------------------------- #
print("\n== 第 10 组：真 worker 端到端 ==")

# 先把队列里可能残留的测试消息清掉，免得污染判断
_alive = queue_mod.worker_alive()
if not _alive["ok"]:
    skip("真 worker 端到端", "没有 worker 在应答（make worker 可起）")
else:
    _cleanup_documents()
    e2e_name = f"m9_{U}_e2e.txt"
    e2e_path = UPLOAD / e2e_name
    e2e_path.write_text("考勤制度：上班时间为九点到十八点，午休一小时。" * 40, encoding="utf-8")
    e2e_rel = f"upload/{e2e_name}"

    e2e_id = repo.create_pending(file_name=e2e_name, storage_path=e2e_rel, file_size=e2e_path.stat().st_size)
    check("真投递到队列", queue_mod.enqueue_parse(e2e_id) is True)

    deadline = time.time() + 120
    final = "pending"
    while time.time() < deadline:
        final = repo.get(e2e_id).status
        if final in (repo.STATUS_SUCCESS, repo.STATUS_FAIL):
            break
        time.sleep(1.0)

    check("worker 真的把任务跑完了", final == repo.STATUS_SUCCESS, f"最终 status={final}")
    e2e_rec = repo.get(e2e_id)
    check("chunk_count > 0", e2e_rec.chunk_count > 0, str(e2e_rec.chunk_count))
    check("attempt_count == 1", e2e_rec.attempt_count == 1, str(e2e_rec.attempt_count))
    check("parse_started_at 已清空", e2e_rec.parse_started_at is None)
    check("切片可按 doc_id 查回", len(vs.get_chunks_by_doc_id(e2e_id)) >= 0)

    # 用真实向量库（worker 用的是 settings.VECTOR_DB_DIR）清掉刚才的痕迹
    from core.vector_store import get_vector_store_manager as _real_vs

    _real_vs().delete_by_doc_id(e2e_id)
    e2e_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 清理与汇总
# --------------------------------------------------------------------------- #
# 顺序很重要：先按记录里的 storage_path 删文件，再清表。
# 反过来的话，表一清就再也找不到那些 uuid 命名的落盘文件了
# （它们的名字里没有本模块的标记 U，glob 不到）。
for _r in repo.list_documents(limit=1000):
    try:
        _p = resolve_storage_path(_r.storage_path)
        if settings.UPLOAD_DIR.resolve() in _p.resolve().parents and _p.is_file():
            _p.unlink()
    except OSError:
        pass
_cleanup_documents()
_cleanup_files()
_tmp.cleanup()

if SKIPPED:
    print(f"\n跳过 {len(SKIPPED)} 项：")
    for item in SKIPPED:
        print(f"  - {item}")

print(f"\n{'=' * 50}")
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
