"""答案级语义评测：用模型当裁判，给「忠实度」和「要点覆盖」打分。

为什么需要这个脚本
    `eval_retrieval.py` 量的是**检索层**（对的 chunk 有没有进前 5），
    `eval_answer_guard.py` 只拦**已知的**错误说法（forbidden_patterns 逐条枚举）。
    两者之间有个洞：检索对了、也没有踩到已知禁语，但模型仍可能用常识补库外的
    内容（q041 病假扣薪就是这么编出来的），或者答得切题但漏掉关键步骤。
    这个洞只能用「读懂答案」的方式判，所以请模型当裁判。

判据（两条，都由裁判给，不给人工打标）
    faithful   答案里的事实性陈述（数字、条款、流程步骤）能否在检索片段里找到。
               推断可以，但「资料没写却说得像真的」一律 false。
    score 0-5  切题与要点覆盖：
               5 = 要点全覆盖且无错；4 = 主要要点对、漏次要点；
               3 = 方向对但漏关键细节；2 = 部分错；1 = 答非所问；0 = 编造。

为什么每条跑 repeat 次取中位数
    temperature=0 在多数推理后端**不等于**确定性（浮点累加顺序、批处理分组都会变），
    单次采样会把抖动当成质量变化。默认 3 次：score 取中位数、faithful 取多数票。
    报的是「这条题稳定能拿几分」，不是「运气最好那次几分」。

已知偏差（有意接受）
    `sources[].snippet` 是接口侧的 200 字截断（`_extract_sources`），裁判看不到全文。
    截断会**低估** faithful（本来有据、但依据被切掉了），方向是保守的：
    宁可误报「不忠实」，也不会放过真的编造。需要更准时改用 chunk 全文反查，
    代价是每条题多一次向量库读——等这个指标真的被误报顶住了再上。

用法
    .venv/bin/python scripts/eval_judge.py                      # 全部内域题，每题 3 次
    .venv/bin/python scripts/eval_judge.py --id q041            # 单题复现
    .venv/bin/python scripts/eval_judge.py --repeat 1 --limit 5 # 冒烟（省 token）
    .venv/bin/python scripts/eval_judge.py --init-baseline      # 首版基线：实测值写进 gate_baseline.json
    .venv/bin/python scripts/eval_judge.py --min-score 4.0 --min-faithful 0.9
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 注意：`core.rag_chain` 会连锁拉起 Chroma / MySQL / LLM 客户端，
#: 放在模块顶层会让「只想测纯函数」的测试也背上全部基础设施依赖。
#: 所以真链路的 import 落在 main() 里，本模块顶层只依赖标准库。

DEFAULT_GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
BASELINE = ROOT / "data" / "eval" / "gate_baseline.json"

#: 最多喂几个片段。检索层 top_k 通常 4~6，全喂会让裁判注意力分散；
#: 第 7 条之后基本是噪声，且白烧 token。
MAX_CONTEXTS = 6
#: 单个片段喂给裁判的字符上限。
#: 取 chunk 全文时会用到——表格类切片（入职登记表 20 多个字段）在 200 字处被切断，
#: 裁判看不到后半截，就会把答案里如实列出的字段判成「编造」。这是最早踩到的坑。
MAX_SNIPPET_CHARS = 1200

RUBRIC = """你是企业知识库问答的阅卷老师。只输出 JSON，不要解释、不要 markdown。

【三条铁律，先读这三条再判】
1. 答案**省略**资料里的部分细节，是正常且正确的。用户要的是能用的回答，不是原文复述。
   省略**不影响 faithful**，最多让 score 降 1 分。
2. 答案说「资料未涉及 / 没有规定 X」是诚实表现。若片段确实没写，这是正确回答，不是遗漏。
3. 只判答案**说出口的内容**对不对，不判它「应该多说多少」。

【1) faithful —— 只判有没有编造】
- true：答案里的每一条事实（数字、条款、流程、责任主体）都能在片段找到依据，
  或者它明确说明了「资料未涉及」。
- false：存在片段里没有的事实性陈述，而答案把它当事实说了（编造、数字错、
  把 A 的规定安到 B 头上）。
- 遗漏不是 false。措辞不同不是 false。答案比资料短不是 false。

【2) score —— 判这份回答能不能直接用】
5 = 直接可用：要点齐、无错
4 = 可用：有轻微省略，用户不需要追问
3 = 大意对，但漏了关键步骤或关键数字，用户得再问一次才完整
2 = 有明确事实错误，或答非所问
1 = 基本不可用
0 = 全篇编造

【例子】
片段：「一线城市住宿 ≤500元/晚，其他城市 ≤350元/晚。」
- 答案「一线 ≤500，其他 ≤350」→ faithful=true，score=5（无错且够用，没说「每晚」不算问题）
- 答案「一线 ≤800，其他 ≤500」→ faithful=false，score=2（数字编造）
- 答案「住宿按城市分级，具体金额资料未列」→ faithful=true，score=3（诚实，但漏了关键数字）

输出（严格）：{"faithful": true 或 false, "score": 0-5 的整数, "reason": "不超过 40 字，指出依据或问题"}
"""

_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def parse_judge(text: str) -> dict[str, Any]:
    """宽容解析裁判输出。

    真实模型会包 ```json 围栏、会在 JSON 前后加一句客套话、偶尔把 score 写成字符串。
    这里只认「第一个花括号到最后一个花括号」，解析失败就抛——**不猜分数**：
    猜出来的分混进聚合，比缺一条更危险。
    """
    match = _JSON_OBJ.search(text or "")
    if not match:
        raise ValueError(f"裁判输出里没有 JSON：{text[:120]!r}")
    obj = json.loads(match.group(0))

    faithful = obj.get("faithful")
    if isinstance(faithful, str):
        token = faithful.strip().lower()
        if token in {"true", "是", "yes", "1"}:
            faithful = True
        elif token in {"false", "否", "no", "0"}:
            faithful = False
        else:
            # 不能把认不出的词一味当 False：那是「裁判没看懂」被记成「答案不忠实」，
            # 分数会平白变差，而且没人能看出是解析问题。
            raise ValueError(f"faithful 取值无法识别：{faithful!r}")
    if not isinstance(faithful, bool):
        raise ValueError(f"faithful 不是布尔：{obj.get('faithful')!r}")

    raw_score = obj.get("score")
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"score 不是数字：{raw_score!r}") from exc
    if not 0 <= score <= 5:
        raise ValueError(f"score 越界：{score}")

    return {"faithful": faithful, "score": score, "reason": str(obj.get("reason") or "")[:120]}


def build_contexts(sources: list[dict[str, Any]], contents: dict[str, str] | None = None) -> str:
    """把检索片段拼成裁判能读的编号块。

    用编号而不是纯拼接：裁判要能在 reason 里指出「第 2 段有依据」，
    排查时人能按编号回 `chunk_id` 反查原文。

    `contents` 是 `{chunk_id: 全文}`（来自 `fetch_chunk_contents`）。给了就用全文，
    没给或取不到就退回接口的 200 字 snippet —— caller 负责把「取不到」记进日志，
    这里不静默改变判据强度。
    """
    blocks: list[str] = []
    for item in sources[:MAX_CONTEXTS]:
        chunk_id = str(item.get("chunk_id") or "")
        full = (contents or {}).get(chunk_id)
        snippet = (full or str(item.get("snippet") or "")).strip()
        if not snippet:
            continue
        blocks.append(
            f"[{item.get('index')}] 文件：{item.get('file_name') or '未知'}"
            f"（chunk {chunk_id}）\n{snippet[:MAX_SNIPPET_CHARS]}"
        )
    return "\n\n".join(blocks)


def contexts_for(sources: list[dict[str, Any]], *, snippet_only: bool = False) -> str:
    """给裁判准备片段：默认取切片全文，取不到才退回 200 字摘要。

    为什么必须取全文：接口 sources 的 snippet 是 200 字截断，
    而表格类切片（入职登记表二十多个字段）在第 200 字处正好被切断，
    答案如实列出后面的字段，裁判看不到 → 判成「编造」。这会把忠实率
    压低一大截，且方向无法从数字上察觉。降级必须打日志，不能静默。
    """
    if snippet_only:
        return build_contexts(sources)

    ids = [str(s.get("chunk_id")) for s in sources[:MAX_CONTEXTS] if s.get("chunk_id")]
    if not ids:
        return build_contexts(sources)

    try:
        from core.vector_store import fetch_chunk_contents

        raw = fetch_chunk_contents(ids)
        contents = {k: v["content"] for k, v in raw.items() if v and v.get("content")}
    except Exception as e:
        print(f"  [WARN] 取切片全文失败，本次退回 200 字摘要：{type(e).__name__}: {e}")
        return build_contexts(sources)

    missing = [i for i in ids if i not in contents]
    if missing:
        print(f"  [WARN] {len(missing)}/{len(ids)} 个切片取不到全文，退回摘要：{missing[:3]}")
    return build_contexts(sources, contents)


def judge_once(question: str, contexts: str, answer: str, llm: Any) -> dict[str, Any]:
    """一次裁判调用。答案为空时不必花钱——直接判 1 分/不忠实。"""
    if not answer.strip():
        return {"faithful": False, "score": 1, "reason": "答案为空，未产出内容"}

    from langchain_core.messages import HumanMessage, SystemMessage

    payload = f"【问题】{question}\n\n【检索片段】\n{contexts or '（本轮没有检索到任何片段）'}\n\n【模型答案】\n{answer}"
    resp = llm.invoke([SystemMessage(content=RUBRIC), HumanMessage(content=payload)])
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    return parse_judge(content)


def aggregate(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一条题的多次裁决压成一个结论。

    score 取中位数（抗单次抖动），faithful 取多数票（偶数平票时判不忠实——保守侧）。
    """
    if not verdicts:
        return {"score": None, "faithful": None, "votes": 0, "reasons": []}
    scores = [v["score"] for v in verdicts]
    faithful_votes = sum(1 for v in verdicts if v["faithful"])
    return {
        "score": statistics.median(scores),
        "faithful": faithful_votes * 2 > len(verdicts),
        "votes": len(verdicts),
        "scores": scores,
        "faithful_votes": faithful_votes,
        "reasons": [v["reason"] for v in verdicts if v["reason"]],
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合全量指标。分母只算「有效裁决」——判分报错的题单独计数，不摊进平均分。"""
    ok = [r for r in records if r["judge"]["score"] is not None]
    errors = [r["id"] for r in records if r.get("error") or r["judge"]["score"] is None]
    if not ok:
        return {"cases": len(records), "judged": 0, "errors": errors}

    scores = [r["judge"]["score"] for r in ok]
    faithful_n = sum(1 for r in ok if r["judge"]["faithful"])
    return {
        "cases": len(records),
        "judged": len(ok),
        "judge_score_avg": round(statistics.fmean(scores), 4),
        "judge_score_min": min(scores),
        "faithful_rate": round(faithful_n / len(ok), 4),
        "low_score_ids": [r["id"] for r in ok if r["judge"]["score"] <= 2],
        "unfaithful_ids": [r["id"] for r in ok if not r["judge"]["faithful"]],
        "errors": errors,
    }


def evaluate_gate(metrics: dict[str, Any], *, min_score: float, min_faithful: float) -> list[str]:
    """对照阈值，返回不达标的原因列表（空列表 = 过）。

    门禁只看**下限**：avg 高但有一条编造，仍然算不过——用户看到的是一条具体答案，
    不是平均分。
    """
    problems: list[str] = []
    if metrics.get("judged", 0) == 0:
        return ["没有一条拿到有效裁决（全部判分失败），无法判定"]
    if metrics.get("errors"):
        # 没拿到裁决的题等于没测过。门禁不替它猜「大概没问题」。
        problems.append(f"{len(metrics['errors'])} 题未取得有效裁决：{metrics['errors']}")
    score = metrics.get("judge_score_avg")
    if score is None or score < min_score:
        problems.append(f"judge_score_avg {score} < {min_score}")
    rate = metrics.get("faithful_rate")
    if rate is None or rate < min_faithful:
        problems.append(f"faithful_rate {rate} < {min_faithful}")
    return problems


def load_cases(path: Path, *, case_id: str | None, limit: int | None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not obj.get("id") or not obj.get("question"):
            raise ValueError(f"golden 条目缺 id/question：{line[:80]}")
        # 外域题（should_answer=false）的正确行为是拒答，属于检索层 abstain 指标；
        # 拿「忠实度」量它没有意义（拒答本来就没有可核对的事实）。
        if obj.get("should_answer") is False:
            continue
        cases.append(obj)
    if case_id:
        cases = [c for c in cases if c["id"] == case_id]
        if not cases:
            raise SystemExit(f"未找到 id={case_id}（或该题是外域题，不属于本评测）")
    if limit:
        cases = cases[:limit]
    return cases


def write_baseline(metrics: dict[str, Any], *, tag: str, judge_model: str) -> Path:
    """把本次实测写成 judge 段基线。

    阈值 = 实测值留一档余地（分 −0.3、忠实率 −0.05），和检索基线同一套逻辑：
    拦「变差」，不要求「满分」。已有的别的键一律不动。

    `judge_model` 必须记进基线：换裁判就是换尺子，分数不可跨模型比较。
    """
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    data["judge"] = {
        "baseline_tag": tag,
        "baseline_date": datetime.now().strftime("%Y-%m-%d"),
        "golden_file": str(DEFAULT_GOLDEN.relative_to(ROOT)),
        "judge_model": judge_model,
        "context_mode": "chunk_full",
        "thresholds": {
            "judge_score_avg": round(max(0.0, float(metrics["judge_score_avg"]) - 0.3), 2),
            "faithful_rate": round(max(0.0, float(metrics["faithful_rate"]) - 0.05), 2),
        },
        "_baseline_metrics": (
            f"judge_score_avg {metrics['judge_score_avg']} / "
            f"faithful_rate {metrics['faithful_rate']}（{metrics['judged']} 题有效裁决）"
        ),
        "_why": "首版建立：阈值 = 实测留一档余地，不是为通过而放宽。",
    }
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return BASELINE


def main() -> int:
    parser = argparse.ArgumentParser(description="答案级语义评测（模型当裁判）")
    parser.add_argument("--file", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--id", default=None, help="只跑指定题号")
    parser.add_argument("--ids", default=None, help="只跑指定题号，逗号分隔（诊断用）")
    parser.add_argument(
        "--replay", type=Path, default=None,
        help="复用一份历史结果里的 question/answer/contexts，只重跑裁判（比 rubric 时必须用它控变量）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--repeat", type=int, default=3, help="每题裁判次数，取中位数/多数票")
    parser.add_argument("--workers", type=int, default=4, help="裁判并发数")
    parser.add_argument(
        "--snippet-only", action="store_true",
        help="只用接口的 200 字摘要当片段（默认取切片全文；仅用于复现历史结果的判据）",
    )
    parser.add_argument("--keep-cache", action="store_true", help="不清问答缓存（默认清，避免缓存答案掩盖变化）")
    parser.add_argument("--judge-model", default=None, help="裁判模型名（默认用配置里的主模型）")
    parser.add_argument("--min-score", type=float, default=None, help="低于该均分即退出码 1")
    parser.add_argument("--min-faithful", type=float, default=None, help="低于该忠实率即退出码 1")
    parser.add_argument(
        "--use-baseline", action="store_true",
        help="阈值取 gate_baseline.json 的 judge 段（显式 --min-* 优先）",
    )
    parser.add_argument("--init-baseline", action="store_true", help="把本次实测写入 gate_baseline.json 的 judge 段")
    parser.add_argument("--tag", default="", help="写入结果文件名与基线的版本标记")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.repeat < 1:
        raise SystemExit("--repeat 至少为 1")

    if not args.keep_cache:
        try:
            from core.redis_store import get_redis_client

            r = get_redis_client()
            keys = list(r.scan_iter("rag:qa_cache:*"))
            if keys:
                r.delete(*keys)
            print(f"已清问答缓存：{len(keys)} 条")
        except Exception as e:  # 清缓存失败不挡评测（和 answer_guard 同一取舍）
            print(f"清缓存失败（继续）：{e}")

    cases: list[dict[str, Any]] = []
    if args.replay:
        print(f"回放模式：复用 {args.replay} 的答案与片段，只重跑裁判")
    else:
        cases = load_cases(args.file, case_id=args.id, limit=args.limit)
        if args.ids:
            wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
            cases = [c for c in cases if c["id"] in wanted]
            if not cases:
                raise SystemExit(f"未找到 ids={args.ids}")
        print(f"待评 {len(cases)} 题，每题 {args.repeat} 次裁决，裁判并发 {args.workers}")

    from core.llm_client import get_llm_client

    # 裁判固定 temperature=0：同一条题反复跑，抖动只应来自后端而非采样参数。
    llm = get_llm_client(
        model_name=args.judge_model,
        temperature=0.0,
        max_tokens=256,
        timeout=60,
    ).get_llm()

    if args.replay:
        prev = json.loads(args.replay.read_text(encoding="utf-8"))
        drafts = [
            {**r, "judge": {"score": None, "faithful": None, "votes": 0, "reasons": []}}
            for r in prev.get("records") or []
        ]
        if args.ids:
            wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
            drafts = [d for d in drafts if d["id"] in wanted]
        # 回放时不连链路：这个模式存在的意义就是「答案不变，只换判据」。
        print(f"回放 {len(drafts)} 条既有答案")
    else:
        from core.rag_chain import get_rag_chain

        chain = get_rag_chain()

        # —— 阶段 1：串行跑真实链路 ——
        # 不并发：chain 内部有会话记忆和共享执行器，并发跑会让「同一题的答案」互相影响，
        # 评测结果不再可复现。慢几倍换确定性，值得。
        drafts: list[dict[str, Any]] = []
        for idx, case in enumerate(cases, start=1):
            sid = f"evaljudge-{uuid.uuid4().hex}"
            record: dict[str, Any] = {
                "id": case["id"],
                "category": case.get("category"),
                "question": case["question"],
                "answer": "",
                "contexts": "",
                "sources": [],
                "error": None,
                "judge": {"score": None, "faithful": None, "votes": 0, "reasons": []},
            }
            try:
                res = chain.query(case["question"], sid, owner_id=None, persist=False)
                sources = list(res.get("sources") or [])
                record.update({
                    "answer": res.get("answer") or "",
                    "sources": [
                        {"index": s.get("index"), "chunk_id": s.get("chunk_id"), "file_name": s.get("file_name")}
                        for s in sources
                    ],
                    "contexts": contexts_for(sources, snippet_only=args.snippet_only),
                    "route": res.get("route"),
                    "intent": res.get("intent"),
                    "elapsed_ms": res.get("elapsed_ms"),
                })
                print(f"[{idx}/{len(cases)}] {case['id']} 链路完成（{len(sources)} 片段）")
            except Exception as e:
                record["error"] = f"{type(e).__name__}: {e}"
                print(f"[{idx}/{len(cases)}] {case['id']} 链路失败：{record['error']}")
            drafts.append(record)

    # —— 阶段 2：裁判并发（纯网络 IO，和链路无关） ——
    tasks = [
        (d, rep)
        for d in drafts if d["error"] is None
        for rep in range(args.repeat)
    ]
    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(judge_once, d["question"], d["contexts"], d["answer"], llm): d
                for d, _ in tasks
            }
            by_case: dict[str, list[dict[str, Any]]] = {}
            # 失败的那一次不参与聚合，但记进 judge_errors 便于发现裁判接口不稳。
            judge_errors: dict[str, str] = {}
            for fut in futures:
                draft = futures[fut]
                try:
                    verdict = fut.result()
                    by_case.setdefault(draft["id"], []).append(verdict)
                except Exception as e:
                    judge_errors[draft["id"]] = f"{type(e).__name__}: {e}"
        for draft in drafts:
            if draft["error"] is None:
                draft["judge"] = aggregate(by_case.get(draft["id"], []))
                votes = draft["judge"]["votes"]
                if votes < args.repeat:
                    # 少投了票说明裁判接口在抖。不判失败（有票就还能取中位数），
                    # 但必须显式记下来——静默按 1 票处理会把「接口不稳」伪装成「结论稳」。
                    draft["judge_partial"] = True
                    print(f"  [WARN] {draft['id']} 只拿到 {votes}/{args.repeat} 次裁决")
                if votes == 0:
                    draft["error"] = f"裁判失败：{judge_errors.get(draft['id'], '无有效裁决')}"

    metrics = summarize(drafts)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{args.tag}" if args.tag else ""
    out = args.out or (ROOT / "data" / "eval" / "results" / f"judge-{stamp}{suffix}.json")
    out = out if out.is_absolute() else (ROOT / out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "golden_file": str(args.file),
            "cases": len(cases),
            "repeat": args.repeat,
            "judge_model": args.judge_model or "（配置主模型）",
            "metrics": metrics,
        },
        "records": drafts,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n== 汇总 ==")
    print(f"有效裁决 {metrics.get('judged', 0)}/{metrics.get('cases', 0)} 题")
    if metrics.get("judged"):
        print(f"均分 {metrics['judge_score_avg']}（最低 {metrics['judge_score_min']}）")
        print(f"忠实率 {metrics['faithful_rate']}")
        if metrics["low_score_ids"]:
            print(f"低分题（<=2）：{', '.join(metrics['low_score_ids'])}")
        if metrics["unfaithful_ids"]:
            print(f"不忠实题：{', '.join(metrics['unfaithful_ids'])}")
    if metrics.get("errors"):
        print(f"异常题：{', '.join(metrics['errors'])}")
    print(f"明细已写入：{out}")

    if args.init_baseline:
        if not metrics.get("judged"):
            print("没有有效裁决，拒绝写基线")
            return 1
        path = write_baseline(
            metrics,
            tag=args.tag or "judge-first",
            judge_model=args.judge_model or "（配置主模型）",
        )
        print(f"基线已写入：{path}")

    if args.min_score is not None or args.min_faithful is not None or args.use_baseline:
        min_score = args.min_score
        min_faithful = args.min_faithful
        if args.use_baseline:
            thresholds = (json.loads(BASELINE.read_text(encoding="utf-8")).get("judge") or {}).get("thresholds") or {}
            if not thresholds:
                print("\n[GATE FAIL] gate_baseline.json 里没有 judge 段，先跑一次 --init-baseline")
                return 1
            # 显式参数优先于基线：临时排查时允许放宽，但必须写在命令行里留痕。
            min_score = min_score if min_score is not None else thresholds.get("judge_score_avg")
            min_faithful = min_faithful if min_faithful is not None else thresholds.get("faithful_rate")
        problems = evaluate_gate(
            metrics,
            min_score=min_score if min_score is not None else 0.0,
            min_faithful=min_faithful if min_faithful is not None else 0.0,
        )
        if problems:
            print("\n[GATE FAIL] " + "；".join(problems))
            return 1
        print("\n[GATE PASS]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
