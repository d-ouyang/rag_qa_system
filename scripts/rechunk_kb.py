#!/usr/bin/env python3
"""
重新切分并灌库 —— 把一个目录下的文档全部重建为知识库切片。

    .venv/bin/python scripts/rechunk_kb.py                          # 干跑
    .venv/bin/python scripts/rechunk_kb.py --apply                  # 真做（默认源 docs/guoquan-kb）
    .venv/bin/python scripts/rechunk_kb.py --apply --src docs/其它目录

--------------------------------------------------------------------------
为什么照抄 reindex.py 的骨架，而不是把它改成通用脚本
--------------------------------------------------------------------------
两件事看着像，其实不是一回事：
· reindex.py      是「救火」——磁盘上已有文件、MySQL 里已有记录，补登记 + 重灌 + 清孤儿。
· 本脚本          是「重建」——数据源整个换掉：先把旧知识库清干净，再从零灌一批。
复用它的**写法**（干跑/--apply、前置检查、逐文档幂等重灌、最后对账），
不合并成一个脚本 —— 合并之后，「只想重灌一篇」和「整个知识库换血」两种意图
会共用同一套参数，出问题时分不清是哪一步动的。

--------------------------------------------------------------------------
为什么解析仍然只走 core.parsing.parse_and_index()
--------------------------------------------------------------------------
项目铁律（见 core/parsing.py 文件头）：任何「把文件变成向量库切片」的地方
都调那个唯一实现。本脚本**不出现**任何 loader / splitter / 嵌入调用 ——
抄一份出来漏写 `doc_id`，后果是「按文档删除静默失效」，而且不报错。

--------------------------------------------------------------------------
为什么每一步都「先登记 → 再解析 → 最后对账」
--------------------------------------------------------------------------
    · 先登记：MySQL 里的 document 行是 doc_id 的来源，而 doc_id 必须写进
      切片 metadata —— 顺序颠倒就会出现「有切片没身份」。
    · 逐文档幂等：parse_and_index 内部先按 doc_id 删旧再写新，
      中断后重跑结果一致，不会切片翻倍。
    · 最后对账：把 MySQL 声称的 chunk_count 与向量库实地数出来的
      count_by_doc_id() 比一遍。不一致说明写入中途出过错，
      这种错不会报错，只会让检索悄悄变差 —— 必须在这里抓住。

--------------------------------------------------------------------------
退出码
--------------------------------------------------------------------------
    0  全部成功且对账通过
    1  有文档失败或对账不一致
    2  被前置检查拦下
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core import document_repo as repo  # noqa: E402
from core.parsing import ParseError, parse_and_index  # noqa: E402
from core.queue import worker_alive  # noqa: E402
from core.vector_store import get_vector_store_manager  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

DEFAULT_SRC = ROOT / "docs" / "guoquan-kb"
DEFAULT_PROJECT = "default"


def _hr(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * 4}")


def _scan_sources(src: Path) -> list[Path]:
    """
    扫描源目录下的全部 .md。

    只收 `.md`：本次重建的知识库本身就是 markdown，混进 pdf/docx 会把
    解析耗时翻几倍，而它们的内容后面随时可以再单独灌。
    跳过 `.` 开头的文件（macOS 的 .DS_Store 之类）。

    ⚠️ 还要跳过 `README.md` —— 它是**目录索引**（一张「编号 | 文件 | 主题」的表），
    不是知识内容。实测它会被当成一份「文档」灌进去，然后在问答里被检索到，
    回答里出现「| 编号 | 文件 | 主题 |---|---|」这种表格碎片。索引该被检索到的是
    **人翻文件夹时**，不是大模型。
    """
    if not src.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(src.rglob("*.md")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.upper() == "README.MD":
            continue
        files.append(path)
    return files


def _display_name(path: Path, src: Path) -> str:
    """
    「公司知识库/01-公司简介.md」这种相对路径当展示名。

    用相对路径而不是纯文件名：知识库里有 20 多份制度文档，
    只看 basename 会出现一堆「24_员工手册摘要.md」，在列表页分不清是谁。
    后缀保留原样（不去 .md）：展示名就该是文件本来的名字，
    类型信息丢了的话，列表页看不出这是制度还是表格。
    """
    try:
        rel = path.relative_to(src)
    except ValueError:  # 源目录之外（理论上不会发生）
        return path.name
    return rel.as_posix() or path.name


def _check_worker(apply_changes: bool, force: bool) -> bool:
    alive = worker_alive()
    running = bool(alive.get("ok"))
    _hr("前置检查：解析 worker")
    if running:
        print(f"  ⚠️  解析 worker 在线：{alive.get('workers')}")
    else:
        print("  未检测到 worker —— 可以安全重建")
    if running and apply_changes and not force:
        print(
            "\n  拒绝执行：新入库的文档若被 worker 并发解析，同一 doc_id 会"
            "「删旧写新」交错，结果是切片翻倍。\n"
            "  请先停掉 worker，或确认风险后加 --force。"
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="重新切分：把目录下文档重建进知识库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="真正执行。默认干跑。")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="源目录（默认 docs/guoquan-kb）")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"归属项目（默认 {DEFAULT_PROJECT}）")
    parser.add_argument("--force", action="store_true", help="worker 在线时强行执行。")
    args = parser.parse_args()

    apply_changes: bool = args.apply
    setup_logging()

    src: Path = args.src
    print("=" * 68)
    print("  重新切分知识库" + ("（执行模式：会写入数据）" if apply_changes else "（干跑模式：不会改动任何数据）"))
    print("=" * 68)
    print(f"  源目录    ：{src}")
    print(f"  上传目录  ：{settings.UPLOAD_DIR}")
    print(f"  项目      ：{args.project}")

    files = _scan_sources(src)
    if not files:
        print(f"\n  ✗ 源目录里没有 .md 文件（{src}），没有可灌的内容。")
        return EXIT_REFUSED
    print(f"  待处理    ：{len(files)} 个 .md 文件")

    if not _check_worker(apply_changes, args.force):
        return EXIT_REFUSED

    store = get_vector_store_manager()

    rows: list[dict[str, Any]] = []
    fails: list[str] = []
    mismatched: list[str] = []

    for path in files:
        # 磁盘名必须是「uuid + 原后缀」，不能只有 uuid：
        # DocumentLoader 认的是**文件后缀**而不是 mimetype，落盘名丢后缀会直接
        # 报「不支持的文件类型 ''」（36 篇一次全军覆没过，别再犯）。
        suffix = path.suffix.lower()
        rel = f"upload/{uuid.uuid4().hex}{suffix}"
        display = _display_name(path, src)
        size = path.stat().st_size

        if not apply_changes:
            rows.append({"name": display, "plan": f"{rel}（{size} 字节）", "chunks": "—"})
            continue

        # ① 落盘：document.storage_path 指向的是 upload/ 下的 uuid 名（防重名覆盖）
        target = settings.UPLOAD_DIR / Path(rel).name
        shutil.copy2(path, target)

        # ② 先登记建行（doc_id 从这里来）
        try:
            doc_id = repo.create_pending(
                file_name=display,
                storage_path=rel,
                file_size=size,
                project_id=args.project,
            )
        except Exception as e:  # noqa: BLE001
            fails.append(f"{display}（建行失败 {type(e).__name__}: {e}）")
            print(f"    ✗ {display}  建行失败：{type(e).__name__}: {e}")
            continue

        # ③ 走唯一入口解析入库
        try:
            chunk_count = parse_and_index(
                doc_id, rel, file_name=display, project_id=args.project
            )
        except ParseError as e:
            repo.mark_fail(doc_id, str(e))
            fails.append(f"{display}（{e}）")
            print(f"    ✗ {display}  解析失败：{e}")
            continue
        except Exception as e:  # noqa: BLE001
            repo.mark_fail(doc_id, f"{type(e).__name__}: {e}")
            fails.append(f"{display}（{type(e).__name__}: {e}）")
            print(f"    ✗ {display}  未预期异常：{type(e).__name__}: {e}")
            continue

        # ④ 对账：MySQL 声称的片数 vs 向量库实地数出来的
        actual = store.count_by_doc_id(doc_id)
        if actual != chunk_count:
            repo.mark_fail(doc_id, f"对账不一致：声称 {chunk_count} 片，实际 {actual} 片")
            mismatched.append(f"{display}（声称 {chunk_count} / 实际 {actual}）")
            rows.append({"name": display, "plan": rel, "chunks": f"{chunk_count} ⚠️ 实际 {actual}"})
            continue

        repo.mark_success(doc_id, chunk_count)
        rows.append({"name": display, "plan": rel, "chunks": str(chunk_count)})
        print(f"    ✓ {display}  doc_id={doc_id}  {chunk_count} 片")

    # ------------------------------------------------------------------ #
    # 报告
    # ------------------------------------------------------------------ #
    _hr("汇总")
    print(f"  {'文档':<44}{'切片':>6}")
    for r in rows:
        print(f"  {r['name'][:42]:<44}{r['chunks']:>6}")
    total_chunks = sum(int(r["chunks"]) for r in rows if r["chunks"].isdigit())
    print(f"\n  共 {len(rows)} 篇文档，{total_chunks} 条切片"
          f"{'（另有失败/对账不一致，见上）' if fails or mismatched else '，无失败'}")

    if not apply_changes:
        print("\n  干跑结束。确认无误后加 --apply 执行。")
        return EXIT_OK

    # ------------------------------------------------------------------ #
    # 清孤儿 + 失效问答缓存
    # ------------------------------------------------------------------ #
    _hr("清孤儿切片")
    valid_ids = {r.doc_id for r in repo.list_all()}
    orphans = store.list_orphan_chunks(valid_ids)
    if orphans:
        print(f"  检出 {len(orphans)} 条孤儿，清理中……")
        print(f"  已清理 {store.purge_orphan_chunks(valid_ids)} 条，剩余 {store.count()} 条")
    else:
        print(f"  ✓ 无孤儿切片（向量库共 {store.count()} 条，全部指向有效文档）")

    _hr("失效问答缓存")
    try:
        from core.qa_cache import bump_kb_version

        print(f"  知识库版本号已更新：{bump_kb_version()}")
    except Exception as e:  # noqa: BLE001
        print(f"  · 跳过（{type(e).__name__}: {e}）")

    print("\n" + "=" * 68)
    print(f"  执行结束：{len(rows)} 篇 / 失败 {len(fails)} / 对账不一致 {len(mismatched)}")
    print(f"  向量库当前共 {store.count()} 条切片")
    if fails or mismatched:
        print("  ⚠️  有问题的文档已标记为 fail，可在知识库页面或命令行重传")
    else:
        print("  ✅ 全部成功，对账一致")
    print("=" * 68)
    return EXIT_FAILED if (fails or mismatched) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
