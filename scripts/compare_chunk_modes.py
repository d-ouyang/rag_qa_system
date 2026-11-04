#!/usr/bin/env python3
"""同一套标注下的 fixed vs title 对照（p4.15 整库重建评审用，一次性脚本）。

为什么要这个脚本
================
CHUNK_MODE 从 fixed 切到 title 后，一个旧切片会**裂成多个新切片**，golden 重映射
会把它们全标成 relevant（52 个 id → 81 个 id）。直接比 recall 有两重失真：
分母变大（要全捞回才算满分）+ 标注重叠。所以要折算回同一套标注再比。

折算靠**内容匹配**，不能靠「题级集合对集合」——
一题标了 2 个旧切片时，把该题所有新 id 都折到第一个旧 id，recall 上限就变成 0.5，
量出来的是指标缺陷而不是切分质量（本项目踩过：错误折算曾把 0.87 压成 0.67）。

两个口径都给：
  · chunk 级：新 id --内容重叠--> 旧 id，与 fixed 基线同一套 relevant 比；
  · 文档级：只问「该文档进没进 top-k」。切法不同时这个口径最稳——
    进不了文档就一定答不出，进了文档但碎片不对还有机会。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_golden(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        out[row["id"]] = list(row.get("relevant_chunk_ids") or [])
    return out


def doc_of(chunk_id: str) -> str:
    return str(chunk_id).split(":")[0]


def _norm(text: str) -> str:
    return "".join((text or "").split())


def build_new_to_old(snapshot: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """当前切片 → 快照切片（按同文档 + 内容包含度最高）。

    旧片被切开后，每个新片都是旧片正文的一个子串（除了 enrichment 前缀），
    所以「新片正文在哪个旧片正文里出现」是最直接可靠的判据。
    """
    by_doc: dict[str, list[tuple[str, str]]] = {}
    for cid, text in snapshot.items():
        by_doc.setdefault(doc_of(cid), []).append((cid, _norm(text)))

    mapping: dict[str, str] = {}
    for cid, text in current.items():
        body = _norm(text)
        best, best_score = "", 0.0
        for old_id, old_text in by_doc.get(doc_of(cid), []):
            if not old_text:
                continue
            if body and body in old_text:
                score = len(body) / max(1, len(old_text))      # 完全包含：越长越像
            else:
                # 兜底：字符集合 Jaccard（enrichment 前缀会让包含判定偶尔失效）
                a, b = set(body), set(old_text)
                score = len(a & b) / max(1, len(a | b)) * 0.5
            if score > best_score:
                best, best_score = old_id, score
        if best:
            mapping[cid] = best
    return mapping


def score(per_question, relevant_of, id_map=None, doc_level=False) -> dict:
    acc = {"5": [], "10": [], "20": []}
    mrr, hit5, hit10 = [], [], []
    for q in per_question:
        if not q.get("should_answer"):
            continue
        rel = set(relevant_of(q["id"]))
        if not rel:
            continue
        if doc_level:
            rel = {doc_of(x) for x in rel}
        got = {}
        for k in ("5", "10", "20"):
            ids = list(q["retrieved"].get(k) or [])
            if doc_level:
                seq, seen = [], set()
                for x in ids:
                    d = doc_of(x)
                    if d not in seen:
                        seen.add(d)
                        seq.append(d)
                ids = seq
            elif id_map:
                seq, seen = [], set()
                for x in ids:
                    o = id_map.get(x, x)
                    if o not in seen:
                        seen.add(o)
                        seq.append(o)
                ids = seq
            got[k] = ids
            acc[k].append(len(rel & set(ids)) / len(rel))
        ranks = [i + 1 for i, x in enumerate(got["10"]) if x in rel]
        mrr.append(1.0 / ranks[0] if ranks else 0.0)
        hit5.append(1 if set(got["5"]) & rel else 0)
        hit10.append(1 if set(got["10"]) & rel else 0)
    n = len(acc["5"]) or 1
    return {
        "n": n,
        "recall@5": sum(acc["5"]) / n,
        "recall@10": sum(acc["10"]) / n,
        "recall@20": sum(acc["20"]) / n,
        "hit@5": sum(hit5) / n,
        "hit@10": sum(hit10) / n,
        "mrr@10": sum(mrr) / n,
    }


def show(title: str, f: dict, t: dict) -> None:
    print(f"\n== {title} ==")
    print(f"{'指标':<11}{'fixed':>9}{'title':>9}{'差值':>9}")
    for m in ("recall@5", "recall@10", "recall@20", "hit@5", "hit@10", "mrr@10"):
        d = t[m] - f[m]
        flag = "" if abs(d) < 0.02 else ("  ↑" if d > 0 else "  ↓")
        print(f"{m:<11}{f[m]:>9.4f}{t[m]:>9.4f}{d:>+9.4f}{flag}")
    print(f"{'样本数':<11}{f['n']:>9}{t['n']:>9}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixed", required=True, help="fixed 模式评测结果 json")
    ap.add_argument("--title", required=True, help="title 模式评测结果 json")
    ap.add_argument("--snapshot", default="/tmp/kb_snapshot_fixed_pre_p415.json")
    ap.add_argument("--golden-fixed", default=str(ROOT / "data/eval/golden.fixed-p415.bak.jsonl"))
    args = ap.parse_args()

    golden_fixed = load_golden(Path(args.golden_fixed))
    fixed = json.loads(Path(args.fixed).read_text(encoding="utf-8"))
    title = json.loads(Path(args.title).read_text(encoding="utf-8"))

    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    from core.vector_store import get_vector_store_manager  # 局部导入：仅此处需要真库
    store = get_vector_store_manager()
    current = {}
    for item in store.get_all_chunks():
        meta = item.get("metadata") or {}
        current[item["chunk_id"]] = str(meta.get("content") or item["text"])
    id_map = build_new_to_old(snapshot, current)
    print(f"当前切片 {len(current)} 条，成功折回旧 id 的 {len(id_map)} 条"
          f"（未折回的按原样参与比较：多出现在 xlsx/pdf 等未改切法的文档）")

    rel = lambda qid: golden_fixed.get(qid, [])  # noqa: E731
    show("chunk 级（折算回旧切片）",
         score(fixed["per_question"], rel),
         score(title["per_question"], rel, id_map=id_map))
    show("文档级（只问文档进没进 top-k）",
         score(fixed["per_question"], rel, doc_level=True),
         score(title["per_question"], rel, doc_level=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
