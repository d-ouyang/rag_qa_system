"""答案级 golden 守卫：跑真实 RAG 链路，答案命中 forbidden_patterns 即失败。

为什么单独一个脚本：
    data/eval/golden.jsonl 是**检索**评测（chunk 级 recall/MRR），
    而「答案不能把线性规则改写成区间档位」是**生成**约束，
    必须跑完整链路（检索 + 生成）才能验。

用法：
    .venv/bin/python scripts/eval_answer_guard.py            # 跑全部
    .venv/bin/python scripts/eval_answer_guard.py --id a001  # 只跑一条
    .venv/bin/python scripts/eval_answer_guard.py --keep-cache

会话归属用 owner_id=None（无主会话，不进任何用户的会话列表），
按用户要求不做任何会话删除。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.rag_chain import get_rag_chain  # noqa: E402

DEFAULT_GUARD = ROOT / "data" / "eval" / "answer_guard.jsonl"


def _norm(text: str) -> str:
    """去空白、统一破折号/波浪号，避免「5 年以下」「5—10年」漏检。"""
    t = re.sub(r"\s+", "", text or "")
    return t.replace("—", "-").replace("–", "-").replace("~", "-").replace("－", "-")


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not obj.get("id") or not obj.get("turns"):
            raise ValueError(f"answer_guard 条目缺 id/turns：{line[:80]}")
        cases.append(obj)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="答案级 golden 守卫（禁止区间档位等）")
    parser.add_argument("--file", type=Path, default=DEFAULT_GUARD)
    parser.add_argument("--id", default=None, help="只跑指定 id")
    parser.add_argument("--keep-cache", action="store_true", help="不清问答缓存（默认清）")
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 路径（默认落 data/eval/results/）")
    args = parser.parse_args()

    if not args.keep_cache:
        try:
            from core.redis_store import get_redis_client

            r = get_redis_client()
            keys = list(r.scan_iter("rag:qa_cache:*"))
            if keys:
                r.delete(*keys)
            print(f"已清问答缓存：{len(keys)} 条")
        except Exception as e:  # 清缓存失败不挡评测
            print(f"清缓存失败（继续）：{e}")

    cases = load_cases(args.file)
    if args.id:
        cases = [c for c in cases if c["id"] == args.id]
        if not cases:
            print(f"未找到 id={args.id}")
            return 2

    chain = get_rag_chain()
    failed = 0
    records: list[dict] = []
    for case in cases:
        sid = f"evalguard-{uuid.uuid4().hex}"
        answers: list[dict] = []
        print(f"\n== {case['id']} | {' → '.join(case['turns'])} ==")
        try:
            for q in case["turns"]:
                res = chain.query(q, sid, owner_id=None, persist=True)
                ans = res.get("answer") or ""
                answers.append({
                    "question": q,
                    "answer": ans,
                    "elapsed_ms": res.get("elapsed_ms"),
                    "timings": res.get("timings"),
                    "qa_trace": res.get("qa_trace"),
                })
                print(f"  Q: {q}\n  A: {ans[:240].replace(chr(10), ' ')}")
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            failed += 1
            records.append({
                "id": case["id"], "turns": case["turns"], "answers": answers,
                "passed": False, "error": f"{type(e).__name__}: {e}",
                "note": case.get("note"),
            })
            continue

        final_answer = answers[-1]["answer"] if answers else ""
        norm = _norm(final_answer)
        bad = [p for p in (case.get("forbidden_patterns") or []) if _norm(p) in norm]
        required = [p for p in (case.get("required_any") or []) if _norm(p) in norm]
        passed = not bad
        if not passed:
            failed += 1
            print(f"  [FAIL] 命中禁止模式：{bad}")
        else:
            print("  [PASS] 未命中禁止模式")
        if case.get("required_any") and not required:
            # 期望表述只做提示，不判失败：模型可能用同义说法（「逐年递增」等）。
            print(f"  [WARN] 未出现期望表述（不判失败）：{case['required_any']}")
        records.append({
            "id": case["id"],
            "turns": case["turns"],
            "answers": answers,
            "passed": passed,
            "bad_patterns": bad,
            "required_hit": required,
            "note": case.get("note"),
        })

    # —— 落盘：单次明细 + 历史索引（便于看趋势）——
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or (ROOT / "data" / "eval" / "results" / f"answer_guard-{stamp}.json")
    out = out if out.is_absolute() else (ROOT / out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "guard_file": str(args.file),
            "cases": len(cases),
            "passed": len(cases) - failed,
            "failed": failed,
        },
        "records": records,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    history = out.parent / "answer_guard-history.jsonl"
    with history.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "at": report["meta"]["generated_at"],
            "file": out.name,
            "cases": len(cases),
            "passed": len(cases) - failed,
            "failed": failed,
        }, ensure_ascii=False) + "\n")

    print(f"\n结果：{len(cases) - failed} 通过 / {failed} 失败")
    print(f"明细已写入：{out}")
    print(f"历史索引追加：{history}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
