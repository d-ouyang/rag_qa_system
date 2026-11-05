"""扩展资料（PDF/DOCX/XLSX）测试问题集校验。

用法：
    .venv/bin/python scripts/verify_kb_extras.py
    .venv/bin/python scripts/verify_kb_extras.py --id EXT-13
    .venv/bin/python scripts/verify_kb_extras.py --keep-cache

判定：
    · 普通题：sources 里出现任一 expected_doc_ids，且答案命中 >= min_hits 个 expected_any；
    · 负样本（expect_no_source）：无 sources，或答案命中 expected_any（拒答话术）。

默认清 qa 缓存，确保是真实检索；persist=false，不写会话。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_QUESTIONS = ROOT / "data" / "eval" / "kb_extras_questions.jsonl"
#: 拒答语义：否词（未/没有/暂无/不包含）+ 短距离内的告知类动词。
#: 例：「但未提及2027年的营收目标」「资料没有涉及该指标」「暂无相关数据」。
_REFUSAL_LIKE_RE = re.compile(
    r"(?:未|没有|暂无|查不到|不包含|未包含)[^。；;\n]{0,10}"
    r"(?:提及|涉及|提供|给出|包含|查到|规定|说明|列明|相关|数据|信息)"
)


def _norm(text: str) -> str:
    """去空白/统一大小写，避免「2 小时」和「2小时」漏检。"""
    return re.sub(r"\s+", "", text or "").lower()


def _secret() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("INTERNAL_SHARED_SECRET="):
            return line.split("=", 1)[1].strip()
    return ""


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not obj.get("id") or not obj.get("question"):
            raise ValueError(f"缺 id/question：{line[:80]}")
        cases.append(obj)
    return cases


def clear_qa_cache() -> None:
    try:
        from core.redis_store import get_redis_client

        r = get_redis_client()
        keys = list(r.scan_iter("rag:qa_cache:*"))
        if keys:
            r.delete(*keys)
        print(f"已清问答缓存：{len(keys)} 条")
    except Exception as e:
        print(f"清缓存失败（继续）：{e}")


def ask(question: str, secret: str) -> dict:
    sid = f"verifykbx-{uuid.uuid4().hex}"
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/qa/ask/stream",
        data=json.dumps({"question": question, "session_id": sid, "persist": False}).encode(),
        headers={"Content-Type": "application/json", "x-internal-auth": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        if e.code == 429 and "quota_exceeded" in detail:
            raise RuntimeError(f"本月 token 额度已用完（429）：{detail[:200]}") from e
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}") from e
    frames = [json.loads(l) for l in body.splitlines() if l.strip()]
    sources, answer, grounded = [], "", None
    for f in frames:
        if f.get("type") == "meta":
            sources = f.get("sources") or []
        elif f.get("type") == "chunk":
            answer += f.get("content") or ""
        elif f.get("type") == "grounded":
            grounded = f
    return {"sources": sources, "answer": answer, "grounded": grounded}


def main() -> int:
    parser = argparse.ArgumentParser(description="扩展资料测试问题集校验")
    parser.add_argument("--file", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--id", default=None)
    parser.add_argument("--keep-cache", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0, help="每题之间的间隔秒数，避免触发票方限流")
    args = parser.parse_args()

    if not args.keep_cache:
        clear_qa_cache()

    cases = load_cases(args.file)
    if args.id:
        cases = [c for c in cases if c["id"] == args.id]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有可执行的用例")
        return 2

    secret = _secret()
    records = []
    failed = 0
    for case in cases:
        q = case["question"]
        expected_docs = case.get("expected_doc_ids") or []
        expected_files = case.get("expected_files") or []
        expected_any = [str(x) for x in (case.get("expected_any") or [])]
        min_hits = int(case.get("min_hits") or 1)
        try:
            res = ask(q, secret)
        except Exception as e:
            failed += 1
            print(f"\n{case['id']} | {q}\n  [ERROR] {type(e).__name__}: {e}")
            records.append({**case, "passed": False, "error": str(e)})
            if "额度已用完" in str(e):
                print("  [ABORT] 本月额度已用完，停止后续用例；请调整额度或换账号")
                break
            continue

        answer = res["answer"]
        source_ids = [s.get("doc_id") for s in res["sources"]]
        source_files = [s.get("file_name") for s in res["sources"]]
        doc_hit = (
            (not expected_docs and not expected_files)
            or any(d in source_ids for d in expected_docs)
            or any(f in source_files for f in expected_files)
        )
        hits = [kw for kw in expected_any if _norm(kw) in _norm(answer)]
        no_source = len(res["sources"]) == 0
        if case.get("expect_no_source"):
            # 负样本判定**看语义不看词表**：模型每次的措辞都不同
            # （未查到/未提及/未涉及/没有关于…），追同义词追不完。
            # 这里认「否词 + 告知类动词」的结构，同时保留 expected_any 作为兜底。
            refusal_like = bool(_REFUSAL_LIKE_RE.search(answer or ""))
            if refusal_like:
                hits = hits + ["<语义:未提及类>"]
            passed = no_source or refusal_like or len(hits) >= min_hits
        else:
            passed = doc_hit and len(hits) >= min_hits
        if not passed:
            failed += 1

        print(f"\n{case['id']} | {q}")
        print(f"  sources={source_ids} files={source_files} | doc_hit={doc_hit} | hits={hits} ({len(hits)}/{min_hits}) | "
              f"grounded={(res['grounded'] or {}).get('grounded')} | {'PASS' if passed else 'FAIL'}")
        print(f"  answer: {answer[:220].replace(chr(10), ' ')}")
        records.append({
            **case,
            "passed": passed,
            "source_ids": source_ids,
            "source_files": source_files,
            "hits": hits,
            "answer": answer,
            "grounded": res["grounded"],
        })
        if args.delay > 0:
            time.sleep(args.delay)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "data" / "eval" / "results" / f"kb-extras-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cases": len(cases),
            "passed": len(cases) - failed,
            "failed": failed,
        },
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n结果：{len(cases) - failed} 通过 / {failed} 失败")
    print(f"明细已写入：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
