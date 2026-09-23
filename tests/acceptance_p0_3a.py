# pyright: basic
"""
P0-3a 验收脚本 —— 按 `docs/PLAN-v2.0.0.md` §P0-3 的 5 条验收标准**逐条实测**。

运行：
    .venv/bin/python tests/acceptance_p0_3a.py

与 `tests/test_module9_async_pipeline.py` 的分工（**别混**）：

    module9        回归测试。把队列、worker、向量库都换成桩或临时目录，
                   目的是「改动不破坏既有行为」，离线可跑、快、必须永远全绿。
    本脚本         验收。**一处都不打桩** —— 真 MySQL、真 Redis db1、
                   真 Worker 进程、真向量库目录，全部走 HTTP 接口。
                   目的是「计划书里承诺的那五件事真的成立吗」。

为什么验收不能复用回归测试：
    回归测试打桩的地方，恰好就是验收要验的东西。把 `enqueue_parse` 换成
    一个 lambda，就永远验不出「worker 真的把任务消费掉了」；
    把向量库换成临时目录，就永远验不出「关掉浏览器后切片真的进了生产的那个库」。
    打桩让回归测试稳定，也让验收失去意义 —— 所以这是两份东西。

本脚本自己管 Worker 生命周期
    验收第 3 条要求「kill Worker 后重启」。为了能确定地 kill 和重启，
    脚本要求**启动时没有别的 worker 在跑**（否则会误杀用户的），
    然后自己 `subprocess` 起一个、kill 掉、再起一个。
    跑完一定会把它停掉（try/finally），不留后台进程。

需要：MySQL + Redis（`make infra`）。不需要提前起 Worker 或后端服务。
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

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
from core.parsing import resolve_storage_path  # noqa: E402
from core.queue import enqueue_parse, queue_depth, worker_alive  # noqa: E402
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
    print("           本脚本要自己 kill / 重启 worker 来验第 3 条，")
    print("           先把它停掉再跑（本地直接 Ctrl-C / 关掉 make worker 那个终端）。")
    sys.exit(1)

U = uuid.uuid4().hex[:8]
UPLOAD = ROOT / "upload"
VEC = get_vector_store_manager()
WORKER_LOG = ROOT / f"upload/_acceptance_worker_{U}.log"

print(f"\nMySQL：{_conn.get('detail')}")
print(f"队列 ：{_depth.get('queue')} | 当前积压={_depth.get('depth')}")
print(f"向量库：{settings.VECTOR_STORE_TYPE} | 当前总量={VEC.count()}")
print(f"本次标记：{U}（产物名都带它，便于清理与定位）")


# --------------------------------------------------------------------------- #
# Worker 生命周期管理
# --------------------------------------------------------------------------- #
def start_worker() -> subprocess.Popen:
    """
    起一个真 worker。日志重定向到文件（验收要看日志里的证据）。

    为什么直接 `python -m celery` 而不是 `make worker`：
    `make` 会多一层进程（make → sh → celery），kill 掉 make 不一定会
    把 celery 一起带走 —— 而本脚本恰恰依赖「kill 之后 worker 真的没了」。
    直接持有 celery 进程的句柄，语义才确定。
    """
    log = open(WORKER_LOG, "ab", buffering=0)  # noqa: SIM115 - 需要跨调用持有直到进程退出
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "worker.app", "worker", "--loglevel=info"],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    proc._log_handle = log  # type: ignore[attr-defined] - 记着句柄，退出时关掉
    return proc


def stop_worker(proc: subprocess.Popen | None, *, hard: bool = False) -> None:
    """停 worker 并**确认它真的停了**。不确认的 kill 会让后面的断言变成猜谜。"""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate() if not hard else proc.kill()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    handle = getattr(proc, "_log_handle", None)
    if handle is not None:
        handle.close()


def wait_worker_up(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if worker_alive()["ok"]:
            return True
        time.sleep(1.0)
    return False


def wait_status(doc_id: int, targets: tuple[str, ...], timeout: float = 120.0) -> str:
    """轮询 MySQL 里的 document.status 直到落进目标集合（超时返回当前值）。"""
    deadline = time.time() + timeout
    current = "?"
    while time.time() < deadline:
        rec = repo.get(doc_id)
        current = rec.status if rec else "(记录已删)"
        if current in targets:
            return current
        time.sleep(0.5)
    return current


def log_tail(from_offset: int) -> str:
    """读 worker 日志在 from_offset 之后新增的部分（用于断言日志里的证据）。"""
    if not WORKER_LOG.exists():
        return ""
    data = WORKER_LOG.read_text(encoding="utf-8", errors="replace")
    return data[from_offset:]


def log_size() -> int:
    return WORKER_LOG.stat().st_size if WORKER_LOG.exists() else 0


def one_mb_block() -> bytes:
    """造一段**恰好 1MB** 的 UTF-8 文本，用来精确控制大文件尺寸。"""
    unit = "公司差旅报销制度：员工出差前需先在系统提交申请，凭票据在五个工作日内完成报销。\n".encode()
    return (unit * (1024 * 1024 // len(unit) + 1))[: 1024 * 1024]


# --------------------------------------------------------------------------- #
# 从 TestClient 出发（进程内起 ASGI，不需要额外起 uvicorn）
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app as fastapi_app  # noqa: E402


def new_client() -> TestClient:
    """
    新建一个「浏览器」。

    刻意不 `with TestClient(...)`：那会跑 lifespan，而验收里
    「重开页面」不该伴随一次应用重启 —— 要验的恰恰是「关掉再打开，
    状态从 MySQL 读回来」，中途重启应用会把这件事搅成另一件事。
    """
    return TestClient(fastapi_app)


WORKER: subprocess.Popen | None = None
CREATED: list[int] = []  # 本次创建的 doc_id，退出时统一清理


def upload_http(client: TestClient, name: str, body: bytes, mime: str = "text/plain"):
    r = client.post("/api/v1/documents/upload", files={"file": (name, body, mime)})
    if r.status_code == 202:
        CREATED.append(r.json()["doc_id"])
    return r


try:
    # ======================================================================= #
    # 验收 1：上传 50MB 文件，HTTP 响应立即返回（不含解析耗时）
    # ======================================================================= #
    print("\n" + "=" * 66)
    print("验收 1：上传 50MB 文件，HTTP 响应立即返回（不含解析耗时）")
    print("=" * 66)
    print("  · worker 此刻是**停的** —— 这样能顺带证明「响应快不是因为解析很快」")

    client = new_client()

    # 小文件基线：证明「耗时与文件大小几乎无关」需要一个对照
    small_name = f"p03a_{U}_小文件.txt"
    small_body = "检索增强生成的基本流程是先检索再生成。".encode() * 20
    t0 = time.perf_counter()
    r_small = upload_http(client, small_name, small_body)
    small_elapsed = time.perf_counter() - t0
    check("小文件上传 202", r_small.status_code == 202, f"{r_small.status_code} {r_small.text[:150]}")

    MAX_BYTES = int(settings.DOC_UPLOAD_MAX_BYTES)
    big_body = one_mb_block() * (MAX_BYTES // (1024 * 1024))
    check(
        f"造出的文件正好是上限（{MAX_BYTES} 字节 = 50MB）",
        len(big_body) == MAX_BYTES,
        f"{len(big_body)}",
    )

    big_name = f"p03a_{U}_大文件.txt"
    t0 = time.perf_counter()
    r_big = upload_http(client, big_name, big_body)
    big_elapsed = time.perf_counter() - t0

    print(f"    · 小文件（{len(small_body) // 1024}KB）HTTP 耗时 {small_elapsed:.3f}s")
    print(f"    · 大文件（{len(big_body) / 1024 / 1024:.0f}MB）HTTP 耗时 {big_elapsed:.3f}s")

    ok1 = True
    ok1 &= check("50MB 上传返回 202（已受理）", r_big.status_code == 202, f"{r_big.status_code} {r_big.text[:200]}")
    ub = r_big.json() if r_big.status_code == 202 else {}
    big_id = ub.get("doc_id")
    ok1 &= check("HTTP 立即返回（< 5 秒）", big_elapsed < 5.0, f"{big_elapsed:.3f}s")
    ok1 &= check("响应 status=pending（不是 success）", ub.get("status") == "pending", str(ub.get("status")))
    ok1 &= check("响应 queued=True（任务已投进队列）", ub.get("queued") is True, str(ub.get("queued")))
    ok1 &= check("响应不含 chunks_added（此刻它根本不存在）", "chunks_added" not in ub, str(sorted(ub)))
    ok1 &= check(
        "文件确实写全了盘（50MB 都在）",
        resolve_storage_path(repo.get(big_id).storage_path).stat().st_size == MAX_BYTES
        if big_id is not None
        else False,
    )
    ok1 &= check(
        "**上传过程中没有任何解析发生**（该 doc_id 在向量库里 0 片）",
        VEC.count_by_doc_id(big_id) == 0 if big_id is not None else False,
    )
    ok1 &= check(
        "耗时由磁盘写决定，不随文件大小爆炸（大/小文件同为亚秒~秒级）",
        big_elapsed < max(2.0, small_elapsed * 20),
        f"small={small_elapsed:.3f}s big={big_elapsed:.3f}s",
    )

    # 超大文件应被拦（边界是「> 上限」而不是「>= 上限」）
    r_over = client.post(
        "/api/v1/documents/upload",
        files={"file": (f"p03a_{U}_超大.txt", big_body + b"x", "text/plain")},
    )
    ok1 &= check("超过上限 1 字节 → 413", r_over.status_code == 413, f"{r_over.status_code} {r_over.text[:120]}")

    # 立刻删掉 50MB 文件：验收 1 只关心 HTTP 那一段，不让它去挤 worker
    r = client.delete(f"/api/v1/documents/{big_id}")
    ok1 &= check(
        "清理 50MB 文档（三件事）",
        r.status_code == 200 and r.json()["record_removed"] is True and r.json()["file_removed"] is True,
        str(r.json()),
    )
    CREATED.remove(big_id)
    client.delete(f"/api/v1/documents/{r_small.json()['doc_id']}")
    CREATED.remove(r_small.json()["doc_id"])
    del big_body  # 尽早释放 50MB

    criterion("上传 50MB 文件，HTTP 响应立即返回（不含解析耗时）", ok1, f"（实测 {big_elapsed:.2f}s）")

    # ======================================================================= #
    # 起 worker，进入后面四条验收
    # ======================================================================= #
    print("\n" + "=" * 66)
    print("启动真 Worker（后面四条验收都要它）")
    print("=" * 66)
    WORKER = start_worker()
    if not wait_worker_up(90.0):
        print("  [致命] 90 秒内 worker 没有应答，后续验收无法进行")
        print(f"         看日志：{WORKER_LOG}")
        sys.exit(1)
    print(f"  worker 已就绪：{worker_alive()['workers']}")
    print(f"  日志：{WORKER_LOG}")

    # ======================================================================= #
    # 验收 2：提交后关闭浏览器，任务照常跑完，重开页面状态正确
    # ======================================================================= #
    print("\n" + "=" * 66)
    print("验收 2：提交后关闭浏览器，任务照常跑完，重开页面状态正确")
    print("=" * 66)

    doc2_name = f"p03a_{U}_关闭浏览器.txt"
    doc2_body = ("考勤制度：上班时间九点到十八点，午休一小时，迟到三次记一次警告。\n" * 60).encode()
    browser_a = new_client()
    r = upload_http(browser_a, doc2_name, doc2_body)
    ok2 = check("提交上传 → 202", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
    doc2_id = r.json()["doc_id"]
    browser_a.close()  # ← 「关闭浏览器」：连接断了，没有任何客户端在等结果
    print("    · 浏览器已关闭（连接断开，本进程不再持有该请求）")

    # 「重开页面」= 新开一个客户端，只从 MySQL 读状态
    time.sleep(3.0)
    browser_b = new_client()
    final = wait_status(doc2_id, (repo.STATUS_SUCCESS, repo.STATUS_FAIL), timeout=150.0)

    listing = browser_b.get(f"/api/v1/documents/?project_id=default").json()
    mine = [d for d in listing["documents"] if d["doc_id"] == doc2_id]
    chunks = browser_b.get(f"/api/v1/documents/{doc2_id}/chunks").json()

    ok2 &= check("关浏览器后任务仍然跑完（status=success）", final == repo.STATUS_SUCCESS, f"status={final}")
    ok2 &= check("chunk_count > 0", repo.get(doc2_id).chunk_count > 0, str(repo.get(doc2_id).chunk_count))
    ok2 &= check("重开页面能从 MySQL 读到该文档", len(mine) == 1, str(listing["counts"]))
    ok2 &= check("列表里的状态已是最新（success）", mine and mine[0]["status"] == "success", str(mine[:1]))
    ok2 &= check("切片可按 doc_id 查回，条数与 chunk_count 一致",
                 chunks["chunk_count"] == repo.get(doc2_id).chunk_count,
                 f"{chunks['chunk_count']} vs {repo.get(doc2_id).chunk_count}")
    ok2 &= check("切片带 chunk_index 且从 0 连续编号",
                 [c.get("chunk_index") for c in chunks["chunks"]] == list(range(chunks["chunk_count"])),
                 str([c.get("chunk_index") for c in chunks["chunks"]][:8]))
    criterion("提交后关闭浏览器，任务照常跑完，重开页面状态正确", ok2, f"（{repo.get(doc2_id).chunk_count} 片）")

    # ======================================================================= #
    # 验收 4：同一 doc_id 入队两次 → Chroma 不出现重复切片
    # ======================================================================= #
    print("\n" + "=" * 66)
    print("验收 4：人为让同一 doc_id 入队两次 → Chroma 不出现重复切片")
    print("=" * 66)

    baseline = VEC.count_by_doc_id(doc2_id)  # 同一份内容单独跑一次得到的切片数
    print(f"    · 对照：doc_id={doc2_id} 单次解析得 {baseline} 片")

    # 顺带钉住一条状态机约定：success 的文档**不允许** reset。
    # 这不是本条的验收目标，但它决定了下面必须换一份**新**文档来做重复入队 ——
    # 对一份已成功的文档重复入队，两条消息都会被状态机挡掉，
    # 那样「没重复切片」是因为压根没跑，证不出幂等。
    ok4 = check("success 的文档不允许 reset（重解析语义不模糊）",
                repo.reset_for_reparse(doc2_id) is False)
    ok4 &= check("被拒后状态仍是 success、切片数未变",
                 repo.get(doc2_id).status == repo.STATUS_SUCCESS
                 and VEC.count_by_doc_id(doc2_id) == baseline)

    # 用一份**全新**文档，在 worker 还没抢到它之前连推两条 ——
    # 这才是「重复入队」的真实形态（用户连点两次上传按钮 / Redis 重投）。
    doc4_name = f"p03a_{U}_重复入队.txt"
    doc4_body = doc2_body  # 同样内容 → 切片数应当与 baseline 一致，便于对照
    total_before = VEC.count()
    r = upload_http(client, doc4_name, doc4_body)  # ← 上传自带第 1 条消息
    ok4 &= check("新文档上传 → 202", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
    doc4_id = r.json()["doc_id"]

    log_offset = log_size()  # 只读这之后新增的日志，避免被前面阶段干扰
    queued_2nd = enqueue_parse(doc4_id)  # ← 人为第 2 条，与第 1 条抢同一个 doc_id
    ok4 &= check("人为再投一条同 doc_id 的消息", queued_2nd is True)

    final = wait_status(doc4_id, (repo.STATUS_SUCCESS, repo.STATUS_FAIL), timeout=150.0)
    rec4 = repo.get(doc4_id)
    after = VEC.count_by_doc_id(doc4_id)
    idx = sorted(int(c.get("chunk_index", -1)) for c in VEC.get_chunks_by_doc_id(doc4_id))
    seg = log_tail(log_offset)
    print(f"    · 两条消息跑完：status={final} | 本文件 {after} 片 | attempt_count={rec4.attempt_count}")

    ok4 &= check("两条消息跑完后 status=success", final == repo.STATUS_SUCCESS, f"status={final}")
    ok4 &= check("切片数与单次解析一致（没有翻倍）", after == baseline, f"单次 {baseline} -> 本次 {after}")
    ok4 &= check("chunk_index 无重复且从 0 连续", idx == list(range(after)), str(idx))
    ok4 &= check("向量库总量只多了这一份（不存在查不到也删不掉的孤儿重复）",
                 VEC.count() == total_before + after, f"{total_before} -> {VEC.count()}")
    ok4 &= check("attempt_count == 1（只有一条消息真的抢到了任务）",
                 rec4.attempt_count == 1, str(rec4.attempt_count))
    ok4 &= check("worker 日志里能看到第 2 条被状态机挡掉",
                 "未抢到任务" in seg, "（新增日志里没有该记录）")

    # 再把「挡掉」这件事直接验一遍：success 状态下调任务本身
    from core.parsing import run_parse_task  # noqa: E402

    dup = run_parse_task(doc4_id)
    ok4 &= check("success 状态下再跑同一任务 → claimed=False，不干活",
                 dup.get("claimed") is False and dup.get("ok") is False, str(dup))
    ok4 &= check("拒之后切片数仍不变", VEC.count_by_doc_id(doc4_id) == baseline,
                 f"{baseline} -> {VEC.count_by_doc_id(doc4_id)}")
    criterion("人为让同一 doc_id 入队两次 → Chroma 不出现重复切片", ok4,
              f"（{after} 片，与单次解析一致）")

    # ======================================================================= #
    # 验收 5：上传损坏文件 → status=fail + fail_reason 可读，前端能重试
    # ======================================================================= #
    print("\n" + "=" * 66)
    print("验收 5：上传损坏文件 → status=fail + fail_reason 可读，前端能重试")
    print("=" * 66)

    bad_name = f"p03a_{U}_坏文档.docx"
    bad_body = b"this is definitely not a zip container" * 200  # .docx 本质是 zip
    r = upload_http(client, bad_name, bad_body, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    ok5 = check("损坏文件也能被受理（202 而非 500）", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
    bad_id = r.json()["doc_id"] if r.status_code == 202 else None

    final = wait_status(bad_id, (repo.STATUS_FAIL,), timeout=150.0)
    rec_bad = repo.get(bad_id)
    reason = (rec_bad.fail_reason or "") if rec_bad else ""
    print(f"    · fail_reason = 「{reason}」")

    ok5 &= check("status=fail", final == repo.STATUS_FAIL, f"status={final}")
    ok5 &= check("fail_reason 非空", bool(reason.strip()))
    ok5 &= check("fail_reason 里有中文（是人话不是裸类名）",
                 any("\u4e00" <= ch <= "\u9fff" for ch in reason), reason)
    ok5 &= check("fail_reason 指明是哪个文件（用户要知道自己在传什么）", "坏文档" in reason, reason)
    ok5 &= check("fail_reason 不含 Traceback（不把栈丢给用户）", "Traceback" not in reason, reason[:80])
    ok5 &= check("fail_reason 长度在列宽内（512）", len(reason) <= 512, str(len(reason)))
    ok5 &= check("失败状态不会污染向量库", VEC.count_by_doc_id(bad_id) == 0, str(VEC.count_by_doc_id(bad_id)))

    # 「前端能重试」= 失败的文档允许再次入队，而不是被状态机锁死
    attempts_before = rec_bad.attempt_count
    r = client.post(f"/api/v1/documents/{bad_id}/reparse")
    ok5 &= check("失败文档可重试 → 202", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
    ok5 &= check("重试真的重新入队了", r.json().get("queued") is True, str(r.json()))
    final2 = wait_status(bad_id, (repo.STATUS_FAIL,), timeout=150.0)
    ok5 &= check("重试后仍然 fail（坏文件重试不会自己变好，但流程闭环）", final2 == repo.STATUS_FAIL, f"status={final2}")
    ok5 &= check("attempt_count 增加（能看到试过几次）",
                 repo.get(bad_id).attempt_count > attempts_before,
                 f"{attempts_before} -> {repo.get(bad_id).attempt_count}")
    criterion("上传损坏文件 → status=fail + fail_reason 可读，前端能重试", ok5)

    # ======================================================================= #
    # 验收 3：手动 kill Worker 后重启，pending 任务继续被消费
    # ======================================================================= #
    print("\n" + "=" * 66)
    print("验收 3：手动 kill Worker 后重启，pending 任务继续被消费")
    print("=" * 66)

    print("  · kill worker")
    stop_worker(WORKER, hard=True)  # SIGKILL：模拟「被强行杀掉」而不是优雅退出
    WORKER = None
    time.sleep(2.0)
    ok3 = check("worker 确实没了", worker_alive()["ok"] is False, str(worker_alive()))

    doc3_name = f"p03a_{U}_杀worker期间提交.txt"
    doc3_body = ("设备借用流程：先提申请，再找管理员领用，用完当天归还。\n" * 60).encode()
    r = upload_http(client, doc3_name, doc3_body)
    ok3 &= check("无 worker 时上传仍然受理（202）", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
    doc3_id = r.json()["doc_id"]
    ok3 &= check("响应带 queued=True（消息进了队列，只是没人消费）",
                 r.json().get("queued") is True, str(r.json()))
    ok3 &= check("上传瞬间没有任何切片进库", VEC.count_by_doc_id(doc3_id) == 0)

    time.sleep(8.0)
    stuck = repo.get(doc3_id).status
    depth = queue_depth()
    print(f"    · 8 秒后 status={stuck} | 队列积压 depth={depth['depth']} unacked={depth['unacked']}")
    ok3 &= check("任务**没有**自己跑掉（仍然是 pending，等 worker 回来）", stuck == repo.STATUS_PENDING, stuck)
    ok3 &= check("队列里能看到积压 ≥ 1（积压可见，不是黑盒）",
                 depth["ok"] and int(depth["depth"]) + int(depth["unacked"]) >= 1, str(depth))
    ok3 &= check("MySQL 仍是状态真相源（记录完好、可查）", repo.get(doc3_id) is not None)

    # 顺带看一眼监控接口在 worker 不在时的表现：不能 5xx
    sq = client.get("/api/v1/system/queue")
    ok3 &= check("worker 不在时 /system/queue 仍 200", sq.status_code == 200, f"{sq.status_code}")
    ok3 &= check("监控如实报告 worker 不在", sq.json()["worker"]["ok"] is False, str(sq.json()["worker"]))

    print("  · 重启 worker")
    WORKER = start_worker()
    if not wait_worker_up(90.0):
        print("  [致命] worker 重启失败，验收 3 无法完成")
        sys.exit(1)
    print(f"    · worker 已重启：{worker_alive()['workers']}")

    final = wait_status(doc3_id, (repo.STATUS_SUCCESS, repo.STATUS_FAIL), timeout=180.0)
    ok3 &= check("重启后 pending 任务被继续消费（status=success）",
                 final == repo.STATUS_SUCCESS, f"status={final}")
    ok3 &= check("切片已入向量库（按 doc_id 查得到）", VEC.count_by_doc_id(doc3_id) > 0,
                 str(VEC.count_by_doc_id(doc3_id)))
    ok3 &= check("attempt_count 只加了 1（没有重复消费）",
                 repo.get(doc3_id).attempt_count == 1, str(repo.get(doc3_id).attempt_count))
    criterion("手动 kill Worker 后重启，pending 任务继续被消费", ok3)

finally:
    # ----------------------------------------------------------------------- #
    # 清理：一定把 worker 停掉、把本次创建的文档删干净、把临时日志删掉
    # ----------------------------------------------------------------------- #
    print("\n" + "=" * 66)
    print("清理")
    print("=" * 66)
    stop_worker(WORKER, hard=False)

    try:
        cleaner = new_client()
        for doc_id in list(CREATED):
            rec = repo.get(doc_id)
            if rec is None:
                continue
            VEC.delete_by_doc_id(doc_id)  # 先清向量（记录没了就再也找不到该删哪些）
            path = resolve_storage_path(rec.storage_path)
            if settings.UPLOAD_DIR.resolve() in path.resolve().parents and path.is_file():
                path.unlink()
            repo.delete(doc_id)
            print(f"  · 清理 doc_id={doc_id}（{rec.file_name}）")
        cleaner.close()
    except Exception as e:  # noqa: BLE001 - 清理失败要报出来，但不该盖住验收结论
        print(f"  [警告] 清理未完全成功：{type(e).__name__}: {e}")

    for leftover in UPLOAD.glob(f"p03a_{U}_*"):
        leftover.unlink(missing_ok=True)
    if WORKER_LOG.exists():
        if os.environ.get("KEEP_WORKER_LOG") == "1":
            print(f"  · 保留 worker 日志：{WORKER_LOG}")
        else:
            WORKER_LOG.unlink(missing_ok=True)

    print(f"  · worker 已停止：{worker_alive()['ok'] is False}")
    print(f"  · 向量库总量：{VEC.count()}")


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
print("\n" + "=" * 66)
print("计划书 5 条验收标准的结论")
print("=" * 66)
for i, (title, ok, note) in enumerate(CRITERIA, 1):
    print(f"  {i}. [{'成立' if ok else '不成立'}] {title} {note}")

print(f"\n断言：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 66}")
sys.exit(1 if FAIL else 0)
