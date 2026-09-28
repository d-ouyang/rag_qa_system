#!/usr/bin/env python3
"""
知识库全清脚本 —— 把「已经入库的一切」抹干净，从零开始。

    .venv/bin/python scripts/reset_kb.py                 # 干跑：只报告，一个字都不改（默认）
    .venv/bin/python scripts/reset_kb.py --apply          # 真做：清 Chroma + MySQL + upload/ + Redis
    .venv/bin/python scripts/reset_kb.py --apply --no-upload
    .venv/bin/python scripts/reset_kb.py --apply --clean-legacy-vector-db

--------------------------------------------------------------------------
为什么「清空」要单独成一个脚本，而不是直接删 chroma_data/ 目录
--------------------------------------------------------------------------
Chroma 的数据目录**不能直接 rm -rf**：容器 `rag-chroma` 把它 bind 挂载到
`/chroma/chroma`，服务端进程还持有这份数据的连接与 WAL；在服务端在线时
把文件抽走，等于让服务端面对一个「空但合法」的目录。轻则重启后索引自愈，
重则 sqlite 日志回放对不上，collection 直接读不出来。

所以这里走 `VectorStoreManager.clear_all()` —— 它对 Chroma 是
「删掉整张 collection 再新建一张空的」，对 FAISS 是「只删自己那两个索引文件」，
两个后端都不靠手搓文件操作。

--------------------------------------------------------------------------
清 MySQL 的边界：哪些删、哪些一个都不许碰
--------------------------------------------------------------------------
    document / chat_message / session / folder   ← 本脚本删（它们是「已入库的数据」）
    user                                          ← 不删。登录态与网关账号有关，
                                                    清掉会导致「人还在、登录不了」
    alembic_version                               ← 绝对不删。删了 alembic 会认为
                                                    迁移没做过，下次 db-upgrade 会
                                                    重复建表并报错
删表顺序按外键依赖倒着来：先 chat_message（依赖 session），再 session / folder，
最后 document（它不依赖别人，但要在切片清完之后，否则校验时看到的 orphan 会有假阳性）。

--------------------------------------------------------------------------
前置检查（与 scripts/reindex.py 同一套理由）
--------------------------------------------------------------------------
· worker 在线 + 要真改数据 → 拒绝。两进程同时改写同一批 doc_id 的切片会翻倍。
· 后端在线 + 嵌入式 Chroma → 拒绝。后端句柄会变陈旧，问答 500。
  server 模式不拦：索引由服务端单点持有，改写即刻对所有进程可见。

--------------------------------------------------------------------------
退出码
--------------------------------------------------------------------------
    0  全部完成
    1  有步骤失败
    2  被前置检查拦下
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import Any

# 与其它脚本/测试一致：把项目根塞进 sys.path，保证从任意目录执行都能 import
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core import schema  # noqa: E402
from core.db import session_scope  # noqa: E402
from core.queue import worker_alive  # noqa: E402
from core.vector_store import get_vector_store_manager  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

# 业务表。顺序即删除顺序（外键依赖倒序），别调换。
BUSINESS_TABLES = (
    ("chat_message", schema.chat_message_table),
    ("session", schema.session_table),
    ("folder", schema.folder_table),
    ("document", schema.document_table),
)


def _hr(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * 4}")


def _backend_online(timeout: float = 1.5) -> bool:
    """
    探测后端服务是否在跑。显式关掉代理：本机常配 HTTP_PROXY，
    会让 127.0.0.1 的请求也走代理、拿到来源不明的 502（等价 curl --noproxy '*'）。
    """
    url = f"http://127.0.0.1:{settings.API_PORT}/api/v1/system/health"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 探测失败一律视为「后端没在跑」
        return False


def _check_preconditions(apply_changes: bool, force: bool) -> bool:
    """返回是否允许继续。被拦下时打印原因。"""
    alive = worker_alive()
    worker_running = bool(alive.get("ok"))

    _hr("前置检查：解析 worker")
    if worker_running:
        print(f"  ⚠️  解析 worker 在线：{alive.get('workers')}")
    else:
        print(f"  未检测到 worker（{alive.get('detail')}）—— 可以安全清空")

    if worker_running and apply_changes and not force:
        print(
            "\n  拒绝执行：清空会与 worker 同时改写同一批 doc_id 的切片，\n"
            "  可能交错出「删一半写一半」的中间态。\n"
            "  请先停掉 worker，或确认风险后加 --force。"
        )
        return False

    _hr("前置检查：后端服务")
    backend_running = _backend_online()
    if backend_running:
        print(f"  ⚠️  后端服务在线（127.0.0.1:{settings.API_PORT}）")
    else:
        print(f"  后端服务未在 {settings.API_PORT} 上响应")

    if backend_running and apply_changes and not force:
        if settings.CHROMA_HOST:
            # server 模式：索引在服务端，后端句柄不会变陈旧
            print("  Chroma server 模式：在线后端不影响清空结果。")
        else:
            print(
                "\n  拒绝执行：后端进程正持有 vector_db/ 的 Chroma 句柄，\n"
                "  清空会让那个句柄指向已删除的 collection，问答接口直接 500。\n"
                "  请先停掉后端；或确认风险后加 --force。"
            )
            return False
    return True


def _count_rows() -> dict[str, int]:
    """统计业务表当前行数（只读）。"""
    counts: dict[str, int] = {}
    with session_scope() as session:
        for name, table in BUSINESS_TABLES:
            counts[name] = int(session.execute(select(func.count()).select_from(table)).scalar() or 0)
    return counts


def _step_chroma(apply_changes: bool) -> bool:
    """清空向量库。返回是否成功。"""
    _hr("① 向量库（Chroma / FAISS）")
    store = get_vector_store_manager()
    before = store.count()
    print(f"  清空前共 {before} 条切片 | 后端={store.store_type}"
          f"{'(server)' if store.chroma_host else '(embedded)'} | collection={store.collection_name}")

    if not apply_changes:
        print("  （干跑：未改动。执行时会 delete_collection 后重建空 collection）")
        return True

    try:
        store.clear_all()
    except Exception as e:  # noqa: BLE001 - 清空失败要让脚本继续报告其它步骤的败因
        print(f"  ✗ 清空向量库失败：{type(e).__name__}: {e}")
        return False

    print(f"  ✓ 已清空（clear_all 后 count()={store.count()}）")

    # 再确认一遍：用「所有 doc_id 都是孤儿」的判定把库扫一遍，
    # 即使 clear_all 没生效也能被这一步逮住（比只信 count() 可靠）
    orphans = store.list_orphan_chunks(frozenset())
    if orphans:
        print(f"  ⚠️  仍检出 {len(orphans)} 条残留切片，建议查 chroma 容器日志")
    return True


def _step_mysql(apply_changes: bool) -> bool:
    """清空 MySQL 业务表。返回是否成功。"""
    _hr("② MySQL 业务表")
    before = _count_rows()
    for name, cnt in before.items():
        print(f"  {name:<14} {cnt} 行")

    if not apply_changes:
        print("  （干跑：未改动。执行时会按 chat_message→session→folder→document 顺序删除）")
        return True

    total = 0
    try:
        with session_scope() as session:
            for name, table in BUSINESS_TABLES:
                deleted = session.execute(table.delete()).rowcount or 0
                total += int(deleted)
                print(f"  ✓ 删除 {name}：{deleted} 行")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 删除失败：{type(e).__name__}: {e}")
        return False

    _hr("  复核")
    after = _count_rows()
    leftover = {k: v for k, v in after.items() if v}
    if leftover:
        print(f"  ⚠️  仍有残留：{leftover}")
        return False
    print(f"  ✓ 四张业务表均已清空（共删 {total} 行）；user 与 alembic_version 未动")
    return True


def _step_upload(apply_changes: bool, clean_upload: bool) -> bool:
    """清空 upload/ 下的原始文件。返回是否成功。"""
    _hr("③ 原始文件 upload/")
    root: Path = settings.UPLOAD_DIR
    if not root.is_dir():
        print("  upload/ 不存在，跳过")
        return True

    files = sorted(p for p in root.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        print("  upload/ 是空的，无需清理")
        return True

    print(f"  共 {len(files)} 个文件：")
    for p in files[:12]:
        print(f"    - {p.name}")
    if len(files) > 12:
        print(f"    …… 另有 {len(files) - 12} 个")

    if not clean_upload:
        print("  （--no-upload：保留文件，只清库与向量）")
        return True
    if not apply_changes:
        print("  （干跑：未删除）")
        return True

    removed = 0
    for p in files:
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            print(f"  ✗ 删除失败 {p.name}：{e}")
    print(f"  ✓ 已删除 {removed} 个文件（二次确认：目录内剩 "
          f"{len([p for p in root.iterdir() if p.is_file()])} 个）")
    return True


def _step_legacy_vector_db(apply_changes: bool, clean_legacy: bool) -> bool:
    """删除 vector_db/（p1.5c 之前的嵌入式遗留数据，当前配置已不走它）。"""
    _hr("④ 历史遗留目录 vector_db/")
    root: Path = settings.VECTOR_DB_DIR
    if not root.is_dir():
        print("  vector_db/ 不存在，跳过")
        return True

    entries = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
    print(f"  内容：{entries}")
    if settings.CHROMA_HOST:
        print("  当前 CHROMA_HOST 非空 → 走 server 模式，本目录不参与读写，属历史遗留")
    else:
        print("  ⚠️  当前 CHROMA_HOST 为空（嵌入式模式），本目录是**在线数据**，请勿删除！")

    if not clean_legacy:
        print("  （默认不删。确认后加 --clean-legacy-vector-db）")
        return True
    if not apply_changes:
        print("  （干跑：未删除）")
        return True
    if not settings.CHROMA_HOST:
        print("  ✗ 拒绝删除：嵌入式模式下这目录就是活数据")
        return False

    removed = 0
    for p in root.iterdir():
        if p.name.startswith("."):
            continue
        if p.is_dir():
            import shutil

            shutil.rmtree(p)
        else:
            p.unlink()
        removed += 1
    print(f"  ✓ 已清空 vector_db/（{removed} 项）")
    return True


def _step_redis(apply_changes: bool) -> bool:
    """清 Redis：db0 是问答缓存，必须失效；db1 是解析队列。"""
    _hr("⑤ Redis")
    try:
        import redis  # 延迟导入：memory 模式下不装 redis 包也能跑

        from core.redis_store import get_redis_client
    except Exception as e:  # noqa: BLE001
        print(f"  · 无法导入 redis 客户端，跳过（{type(e).__name__}: {e}）")
        return True

    try:
        client = get_redis_client()          # db0：settings.REDIS_URL
        cached = client.dbsize()
        print(f"  db0（问答缓存）：{cached} 个 key")
        if apply_changes and cached:
            client.flushdb()
            print(f"  ✓ 已清空 db0（flushdb 后 dbsize={client.dbsize()}）")
        elif apply_changes:
            print("  ✓ db0 本来就是空的")

        # db1 是解析队列。core.redis_store 的单例只配了 db0，
        # 这里按 REDIS_QUEUE_URL 单独建一个客户端——不为一次清空去污染那个单例。
        q_client = redis.Redis.from_url(settings.REDIS_QUEUE_URL, socket_connect_timeout=2)
        queue_keys = [k for k in q_client.scan_iter(match="*", count=100)]
        # _kombu.binding.* 是 celery 的消费者绑定（声明的 exchange/queue 关系），
        # 不是业务数据；删掉它们只会让 worker 起来时重新声明一遍。
        jobs = [k for k in queue_keys if not k.startswith(b"_kombu")]
        print(f"  db1（解析队列）：{len(queue_keys)} 个 key"
              f"（celery 绑定 {len(queue_keys) - len(jobs)} 个属正常拓扑，业务任务 {len(jobs)} 个）")
        if jobs:
            print(f"  ⚠️  队列里有 {len(jobs)} 个待处理任务，建议先停 worker 或确认后清空")
    except Exception as e:  # noqa: BLE001 - Redis 挂了不该拖垮整次清空
        print(f"  ✗ Redis 操作失败：{type(e).__name__}: {e}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="知识库全清：Chroma + MySQL 业务表 + upload/ + Redis 缓存",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行改动。不加这个参数等于干跑（默认），只打印将要做什么。",
    )
    parser.add_argument(
        "--no-upload", action="store_true",
        help="保留 upload/ 下的原始文件（只清向量库与 MySQL）。",
    )
    parser.add_argument(
        "--clean-legacy-vector-db", action="store_true",
        help="一并删除 vector_db/ 这个历史遗留目录（仅当 CHROMA_HOST 非空时允许）。",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="worker / 后端在线时强行执行。",
    )
    args = parser.parse_args()

    apply_changes: bool = args.apply
    setup_logging()

    print("=" * 68)
    print("  知识库全清" + ("（执行模式：会改写数据）" if apply_changes else "（干跑模式：不会改动任何数据）"))
    print("=" * 68)
    print(f"  项目根    ：{settings.BASE_DIR}")
    print(f"  向量库后端：{settings.VECTOR_STORE_TYPE}"
          f"{'/server' if settings.CHROMA_HOST else '/embedded'}@{settings.CHROMA_HOST}:{settings.CHROMA_PORT}")
    print(f"  MySQL     ：{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DATABASE}")
    print(f"  上传目录  ：{settings.UPLOAD_DIR}")

    if not _check_preconditions(apply_changes, args.force):
        return EXIT_REFUSED

    results: list[tuple[str, bool]] = [
        ("向量库", _step_chroma(apply_changes)),
        ("MySQL", _step_mysql(apply_changes)),
        ("upload/", _step_upload(apply_changes, not args.no_upload)),
        ("vector_db/", _step_legacy_vector_db(apply_changes, args.clean_legacy_vector_db)),
        ("Redis", _step_redis(apply_changes)),
    ]

    print("\n" + "=" * 68)
    failed = [name for name, ok in results if not ok]
    if apply_changes:
        print("  执行结束。" + (f"  ⚠️  以下步骤失败：{failed}" if failed else "  ✅ 全部步骤完成。"))
        if not failed:
            print("")
            print("  下一步：重新灌知识库")
            print("    .venv/bin/python scripts/rechunk_kb.py --help")
        print("")
        if settings.CHROMA_HOST:
            print("  ✅ Chroma server 模式：结果即刻生效，在线后端无需重启。")
        else:
            print("  ⚠️  若后端仍在运行，重启一次再对外提供服务（陈旧句柄会导致问答 500）。")
    else:
        print("  干跑结束。确认无误后加 --apply 执行。")
    print("=" * 68)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
