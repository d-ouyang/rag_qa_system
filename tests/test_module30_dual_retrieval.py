"""module30 —— 双路检索合并 / 零召回回退（P4.14+）。

覆盖纯函数：
  · 合并：同 chunk 保留高重排分、按分降序、top1 取两路最大、候选去重；
  · 回退：改写路零召回 → 原问题；双路都召回 → 合并；双路都空 → 保持零召回。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.documents import Document  # noqa: E402

from core.rag_chain import _merge_retrieval, _reconcile_dual  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name} | {detail}")


def doc(cid: str, score: float, text: str = "x") -> Document:
    return Document(
        page_content=text,
        metadata={"chunk_id": cid, "rerank_score": score, "source": f"/upload/{cid}.md"},
    )


def rerank(candidates: list[dict], top1: float | None) -> dict:
    return {
        "candidates": candidates,
        "threshold": 0.1,
        "top1_before_filter": top1,
        "filtered_count": 0,
    }


print("\n== 1. 合并：去重 / 取高分 / 排序 / top1 ==")
a = [doc("1:0", 0.3, "a"), doc("2:0", 0.2, "b")]
b = [doc("1:0", 0.5, "a2"), doc("3:0", 0.4, "c")]
ta = {"rerank": rerank([{"chunk_id": "1:0", "rerank_score": 0.3, "passed_threshold": True}], 0.3)}
tb = {"rerank": rerank([
    {"chunk_id": "1:0", "rerank_score": 0.5, "passed_threshold": True},
    {"chunk_id": "3:0", "rerank_score": 0.4, "passed_threshold": True},
], 0.5)}
docs, trace = _merge_retrieval(a, ta, b, tb)
check("同 chunk 保留高重排分", docs[0].page_content == "a2", docs[0].page_content)
check("合并后按分降序", [d.metadata["chunk_id"] for d in docs] == ["1:0", "3:0", "2:0"],
      str([d.metadata["chunk_id"] for d in docs]))
check("top1 取两路最大", trace["rerank"]["top1_before_filter"] == 0.5)
check("候选按 chunk_id 去重", len(trace["rerank"]["candidates"]) == 2,
      str(trace["rerank"]["candidates"]))
check("合并标记 dual", trace["rerank"].get("dual") is True)

print("\n== 2. 裁决：回退 / 合并 / 零召回 ==")
d, t, s, q, dual, fb = _reconcile_dual(
    [], {}, [doc("9:0", 0.2)], {}, "那年假呢？", ["年假如何安排？"], "年假如何安排？"
)
check("改写零召回 → 回退原问题",
      len(d) == 1 and s == "那年假呢？" and q == ["那年假呢？"] and fb is True and dual is False,
      f"{len(d)} {s} {q} {dual} {fb}")

d, t, s, q, dual, fb = _reconcile_dual(
    [doc("1:0", 0.3)], {"rerank": rerank([], 0.3)},
    [doc("2:0", 0.2)], {"rerank": rerank([], 0.2)},
    "q", ["rq"], "rq",
)
check("双路都有结果 → 合并",
      len(d) == 2 and dual is True and fb is False, f"{len(d)} {dual} {fb}")

d, t, s, q, dual, fb = _reconcile_dual([], {}, [], {}, "q", ["rq"], "rq")
check("双路都空 → 保持零召回", d == [] and dual is False and fb is False)

print(f"\n结果：{_passed} 通过 / {_failed} 失败")
