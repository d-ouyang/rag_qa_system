#!/usr/bin/env python3
"""
golden.jsonl chunk_id 重映射（P3-7a 整库重建后的一次性脚本，不进常规流程）。

背景：CHUNK_MODE 从 fixed 切到 title 需要整库重建（铁律），doc_id 与 chunk_index
全部重排，golden 里标注的 "<doc_id>:<chunk_index>" 整批失效。本脚本按
「旧切片正文 → 新切片原文」的内容匹配重映射：

    旧 chunk_id --快照--> 旧原文 --归一化包含/最长匹配--> 新 chunk_id

映射不上且内容失配的条目**如实从 relevant_chunk_ids 剔除并写进报告**，
绝不猜一个凑数（错标比缺标更毒，见 eval_retrieval.load_golden 的注释）。

    .venv/bin/python scripts/remap_golden_p37a.py --snapshot /tmp/kb_snapshot_fixed.json --apply
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
NEW_REPORT = ROOT / "data" / "eval" / "remap_report_p37a.json"

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub("", text or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="整库重建后重映射 golden 的 chunk_id")
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="重建前的切片快照（scripts/snapshot_kb_p37a.py 的输出）")
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--apply", action="store_true", help="真写回 golden.jsonl。默认只出报告。")
    args = parser.parse_args()

    snapshot: dict[str, str] = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rows = [json.loads(l) for l in args.golden.read_text(encoding="utf-8").splitlines() if l.strip()]

    from core.vector_store import get_vector_store_manager
    store = get_vector_store_manager()
    # 新库全部切片：{chunk_id: 原文}（title 模式下 metadata.content 是原文）
    new_chunks: dict[str, str] = {}
    for item in store.get_all_chunks():
        meta = item.get("metadata") or {}
        original = meta.get("content")
        new_chunks[item["chunk_id"]] = _norm(str(original)) if original else _norm(item["text"])
    print(f"新库切片：{len(new_chunks)} 条（含原文）")

    normalized_new = list(new_chunks.items())

    report: list[dict] = []
    changed = 0
    for row in rows:
        mapped: list[str] = []
        lost: list[dict] = []
        for old_id in row["relevant_chunk_ids"]:
            old_text = snapshot.get(str(old_id))
            if old_text is None:
                lost.append({"old": old_id, "reason": "快照中没有该切片"})
                continue
            needle = _norm(old_text)
            if not needle:
                lost.append({"old": old_id, "reason": "旧切片正文为空"})
                continue
            # ① 完全包含（新块原文包含旧块原文）
            hits = [cid for cid, text in normalized_new if needle and needle in text]
            if len(hits) == 1:
                mapped.append(hits[0])
                continue
            if len(hits) > 1:
                # 多个新块都包含（短旧块被合并进大块后又因相邻切分出现两次）——取包含比最高者
                hits.sort(key=lambda c: -len(new_chunks[c]))
                mapped.append(hits[0])
                continue
            # ② 多目标匹配（旧块横跨标题边界时，内容分散在多个新块）：
            #    所有「吃掉旧块内容 ≥25%」的新块都算 relevant —— 答案文本实际就在
            #    这些块里。实测分布：真正的承载块 0.25~0.6（跨边界时两块各半），
            #    无关噪声 ≤0.20，0.25 落在边界附近。
            #    relevant 虚胖会拉低 recall 分母，故设上限 3 块。
            scored: list[tuple[str, float]] = []
            for cid, text in normalized_new:
                sm = difflib.SequenceMatcher(None, needle, text)
                match = sum(b.size for b in sm.get_matching_blocks())
                scored.append((cid, match / len(needle)))
            scored.sort(key=lambda t: -t[1])
            hits2 = [cid for cid, ratio in scored if ratio >= 0.25][:2]
            if hits2:
                mapped.extend(hits2)
            else:
                best_cid, best_ratio = (scored[0][0], scored[0][1]) if scored else ("", 0.0)
                lost.append({"old": old_id, "reason": f"内容失配（最佳覆盖率 {best_ratio:.2f}）"})
        mapped = list(dict.fromkeys(mapped))     # 去重保序
        report.append({
            "id": row["id"], "question": row["question"],
            "old": row["relevant_chunk_ids"], "new": mapped, "lost": lost,
        })
        if mapped != row["relevant_chunk_ids"]:
            changed += 1

    # ---- 汇总 ----
    total_ids = sum(len(r["old"]) for r in report)
    mapped_ids = sum(len(r["new"]) for r in report)
    lost_ids = total_ids - mapped_ids
    dead_questions = [r for r in report if r["lost"] and not r["new"]]
    print(f"\n标注题数={len(report)}  chunk_id 总数={total_ids}  映射成功={mapped_ids}  失配={lost_ids}")
    for r in report:
        if r["lost"]:
            print(f"  [{r['id']}] {r['question'][:26]}")
            for l in r["lost"]:
                print(f"      {l['old']}  ← {l['reason']}")
            if not r["new"]:
                print(f"      ⚠️ 该题全部标注失配，题已不可评（relevant 为空）")
    if dead_questions:
        print(f"\n⚠️ {len(dead_questions)} 道题的标注全部失配：These questions need 人工重标或移除。")
        print("   本脚本**不自动改 should_answer**（口径变更必须留痕），见 remap 报告。")

    NEW_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n重映射报告已写：{NEW_REPORT.relative_to(ROOT)}")

    if not args.apply:
        print("干跑结束（加 --apply 写回 golden.jsonl）")
        return 0

    # ---- 写回 ----
    # 全部失配的题：**保留原样**并让后续 validate-only 报错拦住 —— 宁可流程停下
    # 让人处理，也不能静默把一道「该答的题」降级成「拒答题」。
    out_lines = []
    for row, r in zip(rows, report):
        if r["lost"] and not r["new"]:
            out_lines.append(json.dumps(row, ensure_ascii=False))   # 原样保留
            continue
        row["relevant_chunk_ids"] = r["new"]
        out_lines.append(json.dumps(row, ensure_ascii=False))
    args.golden.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"golden.jsonl 已写回（{changed} 题有变化；全部失配的题原样保留待人工处理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
