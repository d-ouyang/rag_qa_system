#!/usr/bin/env python3
"""
知识库重建脚本 —— 把 `upload/` 里的文件重灌一遍向量库，并把历史遗留切片清干净。

    .venv/bin/python scripts/reindex.py            # 干跑：只报告，一个字都不改（默认）
    .venv/bin/python scripts/reindex.py --apply    # 真做
    .venv/bin/python scripts/reindex.py --apply --no-purge
    .venv/bin/python scripts/reindex.py --apply --only-doc 12

--------------------------------------------------------------------------
为什么需要这个脚本（P0-4 的一次性成本）
--------------------------------------------------------------------------
P0-3 之前入库的切片，元数据里只有 `source`（磁盘绝对路径），没有 `doc_id`。
后果是「按文档删除」与「引用反查」两件事都缺一个可靠的身份键：
删文档只能靠路径字符串匹配，文件一改名就静默失效。

那些数据**无法就地修复** —— doc_id 没法从切片正文反推出来。只能重灌。
重灌的前提是「写入时就把 doc_id / chunk_index / chunk_id 写全」，
那件事在 P0-3a 与 P0-4a 已经做完了，所以现在跑一次就能对齐。

--------------------------------------------------------------------------
为什么复用 `core.parsing.parse_and_index()` 而不是在这里重现一套
--------------------------------------------------------------------------
这是硬性要求（见 core/parsing.py 的文件头）。重建脚本最容易犯的错，
恰恰是「就为了重灌一下，顺手把解析逻辑抄一遍」—— 抄出来的那版漏写
`doc_id`，于是重建之后「按文档删除」静默失效；漏写 `chunk_index`，
于是片段顺序变成随机。两种错都不报错，只让人几周后对着检索结果困惑。
所以这里**只**调用那个唯一实现，本文件不出现任何 loader / splitter / 嵌入调用。

--------------------------------------------------------------------------
为什么不先 `clear_all()` 再重灌
--------------------------------------------------------------------------
那样做在「第 3 个文件解析失败」时，前 2 个进了库、其余全没了，
而且**不可续跑**（清库这个动作已经发生过，旧数据回不来）。
本脚本改成「先补登记 → 逐文档幂等重灌 → 最后清孤儿」：
每一步都可中断、可重跑，`parse_and_index` 内部先按 doc_id 删旧再写新，
所以重跑任意多次结果都一样。

--------------------------------------------------------------------------
为什么 --apply 时拒绝在 worker 运行时执行
--------------------------------------------------------------------------
`parse_and_index` 是「先按 doc_id 删旧切片，再写新切片」。两个进程同时对
同一个 doc_id 做这件事，交错顺序可能是「A 删 → B 删 → A 写 → B 写」，
结果是**切片翻倍**（内容相同的两份），检索时互相挤占 Top-K。
这不是理论风险：重建脚本跑几分钟，而 worker 只要队列里有活就会动同一批文档。
所以默认拒绝执行，除非显式 `--force`（或者先停掉 worker）。

--------------------------------------------------------------------------
为什么 --apply 时也拒绝在**后端服务**运行时执行
--------------------------------------------------------------------------
这一条是验收时实测撞出来的，不是理论推演：

    重建脚本在另一个进程里改写了 vector_db/（补登记、重灌、清孤儿），
    后端进程里那个 Chroma 句柄就成了**陈旧的** —— 它的 HNSW 索引仍然指向
    已被删掉的 id，`query` 会为这些 id 返回 `documents=None`，
    langchain 拿着 None 去构造 Document 直接 pydantic 报错，
    于是**问答接口 500**（而不是「少召回几条」这种温和的降级）。

    Chroma 官方也不支持多进程共享同一个 persist_directory。
    所以：重建必须在后端停着的时候跑，跑完再重启后端拿一个干净句柄。
    这个检查与 worker 那道并列，两道都是「别让另一个进程同时动这个目录」。

--------------------------------------------------------------------------
退出码
--------------------------------------------------------------------------
    0  全部成功
    1  有文档解析失败（部分成功）
    2  被前置检查拦下（worker 在跑 / 参数有问题 / 目标不存在）
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

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core import document_repo as repo  # noqa: E402
from core.document_loader import DocumentLoader  # noqa: E402
from core.parsing import ParseError, parse_and_index, resolve_storage_path  # noqa: E402
from core.queue import worker_alive  # noqa: E402
from core.vector_store import get_vector_store_manager  # noqa: E402

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_REFUSED = 2

# 补登记时给这批「从磁盘捡回来」的文档归的项目名。
# 用 default 而不是造一个 "imported" 之类的名字：原始归属已经不可考，
# 编一个假的项目名只会让「按项目过滤」多出一个没人认识的分组。
_DEFAULT_PROJECT = "default"


def _hr(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * 4}")


def _to_rel(path: Path) -> str:
    """
    把磁盘绝对路径转成库里存的形态（相对项目根，如 `upload/xxx.pdf`）。

    必须与 `core.parsing.resolve_storage_path` 互为逆运算 —— 那边负责
    「相对根 → 绝对」的还原，这边负责「绝对 → 相对根」的登记。
    两边只要有一处不一致，重建出来的记录就指向不存在的文件，
    而症状只是「文件缺失，跳过」，很容易被当成「文件真没了」。
    """
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        # 不在项目根下（例如有人配了 UPLOAD_DIR 到别处）：退回绝对路径。
        # 库里存绝对路径是允许的（resolve_storage_path 兼容这种历史写法）。
        return str(path)


def _scan_upload_files() -> list[Path]:
    """
    扫描 upload/ 下的全部文件。

    跳过两类：
        · 以 `.` 开头 —— 主要是 macOS 的 `.DS_Store`
        · 后缀不在 DocumentLoader 支持列表里 —— 它们注定解析失败，
          给它们建一条 `fail` 记录只是往知识库列表里塞噪音
    """
    root = settings.UPLOAD_DIR
    if not root.is_dir():
        return []

    supported = DocumentLoader.SUPPORTED_EXTENSIONS
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in supported:
            print(f"    · 跳过（类型不支持 {path.suffix or '无后缀'}）：{path.name}")
            continue
        files.append(path)
    return files


def _legacy_names(store: Any) -> dict[str, str]:
    """
    从现有向量库里捞一份 `{磁盘路径: 原始文件名}` 映射。

    用途：补登记时给文件起对名字。P0-3 之后磁盘名是 uuid（防重名覆盖），
    而 P0-3 **之前**的落盘名就是原始名 —— 光看磁盘分不出这两类文件，
    但老切片的元数据里存着 `file_name`，能查。
    查不到（比如那文件从没入过库）才退回用磁盘名。
    """
    try:
        return {
            str(item.get("source")): str(item.get("file_name"))
            for item in store.list_documents()
            if item.get("source") and item.get("file_name")
        }
    except Exception as e:  # noqa: BLE001 - 拿不到只是名字差点，不该中断重建
        print(f"    · 读取现有向量库元数据失败（将退回磁盘名）：{e}")
        return {}


def _check_worker(apply_changes: bool, force: bool) -> bool:
    """返回是否允许继续。worker 在线且要动数据时，除非 --force 一律拒绝。"""
    alive = worker_alive()
    running = bool(alive.get("ok"))
    _hr("前置检查：解析 worker")
    if running:
        print(f"  ⚠️  检测到解析 worker 在线：{alive.get('workers')}")
    else:
        print(f"  未检测到 worker（{alive.get('detail')}）—— 可以安全重建")

    if running and apply_changes and not force:
        print(
            "\n  拒绝执行：重建会与 worker 同时写同一批 doc_id 的切片，\n"
            "  交错执行会导致切片翻倍（内容重复、互相挤占检索 Top-K）。\n"
            "\n  请先停掉 worker（Ctrl-C 那个 make worker 的终端），\n"
            "  或者确认风险后加 --force。"
        )
        return False
    return True


def _backend_online(timeout: float = 1.5) -> bool:
    """
    探测后端服务是否在跑（默认 8000 的 /api/v1/qa/health）。

    只做一次轻量 GET，不引入依赖；任何异常（没起、端口被占、被代理拦）
    都当成「没在跑」—— 这道检查的目的是**拦住真实风险**，
    宁可漏拦（用户自己知道没起后端）也不要误拦（把别的东西当成后端）。

    显式关掉代理：本机常有 HTTP_PROXY 环境变量，会让 127.0.0.1 的请求
    也走代理、拿到来源不明的 502（等价 curl --noproxy '*'）。
    """
    url = f"http://127.0.0.1:{settings.API_PORT}/api/v1/qa/health"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 探测失败一律视为「后端没在跑」
        return False


def _check_backend(apply_changes: bool, force: bool) -> bool:
    """
    与 worker 那道并列：后端进程持有 Chroma 句柄，重建会让它变陈旧。

    后果不是「少召回几条」，而是**问答接口直接 500**（见文件头），
    所以这里按「会破坏线上服务」来拦，同样留 --force 出口。
    """
    online = _backend_online()
    _hr("前置检查：后端服务")
    if online:
        print(f"  ⚠️  后端服务在线（127.0.0.1:{settings.API_PORT}）")
    else:
        print(f"  后端服务未在 {settings.API_PORT} 上响应 —— 可以安全重建")

    if online and apply_changes and not force:
        if settings.CHROMA_HOST:
            # server 模式：索引由 chroma 服务端单点持有，重灌经 HTTP 写入，
            # 在线后端立刻看见新索引，不必停。
            print("  Chroma server 模式：在线后端无需停，重灌结果即刻可见。")
            return True
        print(
            "\n  拒绝执行：后端进程正持有 vector_db/ 的 Chroma 句柄。\n"
            "  重建会在另一个进程里改写同一个目录，后端那个句柄会变陈旧，\n"
            "  HNSW 索引仍指向已删除的 id，查询返回 None 正文 —— 结果是**问答接口 500**。\n"
            "\n  请先停掉后端（Ctrl-C 那个 make api / make dev 的终端），\n"
            "  跑完重建再把它起回来；或者确认风险后加 --force。"
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="知识库重建：补登记磁盘文件 → 逐文档重灌 → 清理孤儿切片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行改动。不加这个参数等于干跑（默认），只打印将要做什么。",
    )
    parser.add_argument(
        "--no-purge", action="store_true",
        help="跳过第 ③ 步（不清理孤儿切片）。想先只重灌、观察一轮时用。",
    )
    parser.add_argument(
        "--only-doc", type=int, metavar="DOC_ID", default=None,
        help="只重灌这一篇文档（跳过补登记与清孤儿）。用于定向修复某一篇。",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="worker 或后端在线时也强行执行（有切片翻倍 / 问答 500 的风险，确认过再用）。",
    )
    args = parser.parse_args()

    apply_changes: bool = args.apply
    setup_logging()

    print("=" * 68)
    print("  知识库重建" + ("（执行模式：会改写数据）" if apply_changes else "（干跑模式：不会改动任何数据）"))
    print("=" * 68)
    print(f"  项目根    ：{settings.BASE_DIR}")
    print(f"  upload/   ：{settings.UPLOAD_DIR}")
    print(f"  向量库后端：{settings.VECTOR_STORE_TYPE}")

    if not _check_worker(apply_changes, args.force):
        return EXIT_REFUSED
    if not _check_backend(apply_changes, args.force):
        return EXIT_REFUSED

    store = get_vector_store_manager()
    records = repo.list_all()
    files = _scan_upload_files()
    # 补登记要用它给文件起对名字，先灌一次（只有一个文件时也无所谓，成本是扫一遍元数据）
    legacy_names = _legacy_names(store) if args.only_doc is None else {}

    # ------------------------------------------------------------------ #
    # ① 补登记：磁盘上有、MySQL 里没有的文件
    # ------------------------------------------------------------------ #
    registered: list[tuple[int, str, int]] = []       # (doc_id, file_name, size)
    if args.only_doc is not None:
        _hr("① 补登记（--only-doc 模式，跳过）")
    else:
        _hr("① 补登记：磁盘上有、MySQL 里没有的文件")
        known_paths = {r.storage_path for r in records}
        candidates: list[tuple[str, str, int]] = []   # (rel_path, file_name, size)
        for path in files:
            rel = _to_rel(path)
            if rel in known_paths:
                continue
            file_name = legacy_names.get(str(path)) or path.name
            candidates.append((rel, file_name, path.stat().st_size))

        if not candidates:
            print(f"  无需补登记（MySQL 已有 {len(records)} 条记录，磁盘 {len(files)} 个文件）")
        else:
            print(f"  待补登记 {len(candidates)} 个：")
            for rel, file_name, size in candidates:
                print(f"    + {file_name}  ← {rel}（{size} 字节）")
            if apply_changes:
                for rel, file_name, size in candidates:
                    doc_id = repo.create_pending(
                        file_name=file_name,
                        storage_path=rel,
                        file_size=size,
                        project_id=_DEFAULT_PROJECT,
                    )
                    registered.append((doc_id, file_name, size))
                # 重新取一次：让第 ② 步覆盖到刚登记的行
                records = repo.list_all()
                print(f"  已补登记 {len(registered)} 条")
            else:
                print("  （干跑：未写入。执行时会为它们建 pending 记录）")

    # ------------------------------------------------------------------ #
    # ② 重灌
    # ------------------------------------------------------------------ #
    if args.only_doc is not None:
        targets = [r for r in records if r.doc_id == args.only_doc]
        _hr(f"② 重灌（仅 doc_id={args.only_doc}）")
        if not targets:
            print(f"  没有 doc_id={args.only_doc} 的记录，没什么可做的。")
            return EXIT_REFUSED
    else:
        targets = records
        _hr(f"② 重灌（{len(targets)} 篇文档）")

    ok_count = 0
    fail_count = 0
    skipped: list[str] = []

    for rec in targets:
        path = resolve_storage_path(rec.storage_path)
        if not path.is_file():
            skipped.append(f"{rec.file_name}（磁盘文件不存在：{rec.storage_path}）")
            print(f"    - {rec.file_name}  文件缺失，跳过")
            continue

        if not apply_changes:
            print(f"    → {rec.file_name}  （将重新解析并覆盖该 doc_id 的切片）")
            ok_count += 1
            continue

        try:
            chunk_count = parse_and_index(
                rec.doc_id,
                rec.storage_path,
                file_name=rec.file_name,
                project_id=rec.project_id,
            )
        except ParseError as e:
            # 预期的失败（文件坏 / 类型不支持 / 内容为空）：如实写进 MySQL，
            # 让知识库页面能直接看到原因，而不是只在终端里刷一行字。
            repo.mark_fail(rec.doc_id, str(e))
            fail_count += 1
            print(f"    ✗ {rec.file_name}  解析失败：{e}")
        except Exception as e:  # noqa: BLE001 - 任何未预期异常都要让脚本跑完剩下的
            repo.mark_fail(rec.doc_id, f"{type(e).__name__}: {e}")
            fail_count += 1
            print(f"    ✗ {rec.file_name}  未预期异常：{type(e).__name__}: {e}")
        else:
            repo.mark_success(rec.doc_id, chunk_count)
            ok_count += 1
            print(f"    ✓ {rec.file_name}  {chunk_count} 片")

    if apply_changes:
        print(f"\n  重灌完成：成功 {ok_count} 篇 / 失败 {fail_count} 篇 / 跳过 {len(skipped)} 篇")
    else:
        print(f"\n  干跑：将重灌 {ok_count} 篇 / 跳过 {len(skipped)} 篇")
    for reason in skipped:
        print(f"    · 跳过：{reason}")

    # ------------------------------------------------------------------ #
    # ③ 清理孤儿切片
    # ------------------------------------------------------------------ #
    # 这一步才是「Chroma 里不存在缺 doc_id 的切片」这条验收的保证：
    # ② 只覆盖「MySQL 里有的文档」，而遗留切片压根没有对应的 document 行，
    # 靠 ② 永远碰不到它们。
    if args.only_doc is not None:
        _hr("③ 清孤儿（--only-doc 模式，跳过）")
    elif args.no_purge:
        _hr("③ 清孤儿（--no-purge，跳过）")
    else:
        _hr("③ 清理孤儿切片")
        valid_ids = {r.doc_id for r in repo.list_all()}
        orphans = store.list_orphan_chunks(valid_ids)

        if not orphans:
            print(f"  没有孤儿切片（向量库共 {store.count()} 条，全部指向有效文档）")
        else:
            no_doc_id = [m for _, m in orphans if not isinstance(m.get("doc_id"), int)]
            stale = len(orphans) - len(no_doc_id)
            print(f"  向量库共 {store.count()} 条，其中孤儿 {len(orphans)} 条：")
            print(f"    · 元数据缺 doc_id（P0-3 之前的遗留）：{len(no_doc_id)} 条")
            print(f"    · doc_id 指向已删除的文档          ：{stale} 条")
            for _, meta in orphans[:10]:
                print(f"      - {meta.get('source')}  doc_id={meta.get('doc_id')!r}")
            if len(orphans) > 10:
                print(f"      …… 另有 {len(orphans) - 10} 条")
            if apply_changes:
                deleted = store.purge_orphan_chunks(valid_ids)
                print(f"  已清理 {deleted} 条，向量库剩余 {store.count()} 条")
            else:
                print(f"  （干跑：未删除。执行时会清掉这 {len(orphans)} 条）")

    print("\n" + "=" * 68)
    if apply_changes:
        print(f"  执行结束：重灌成功 {ok_count} / 失败 {fail_count}" + ("（有失败，请查看上方日志）" if fail_count else ""))
        print("")
        if settings.CHROMA_HOST:
            # server 模式（p1.5c 起）：索引由 chroma 服务端单点持有，
            # 重灌结果对所有在线进程即刻可见，**不需要重启后端**
            print("  ✅ Chroma server 模式：重灌结果即刻生效，在线后端无需重启。")
        else:
            print("  ⚠️  下一步：把后端服务重启一遍再对外提供服务。")
            print("     重建是在另一个进程里改写 vector_db/ 的，仍在运行的后端进程")
            print("     持有的是**陈旧句柄**，它的查询会拿到 None 正文并直接 500")
            print("     （根因见本文件头）。重启之后才是干净句柄。")
    else:
        print("  干跑结束。确认无误后加 --apply 执行。")
        print("          提醒：执行前请先停掉 worker 与后端服务（本脚本会自动拦下）。")
    print("=" * 68)
    return EXIT_PARTIAL_FAILURE if fail_count else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
