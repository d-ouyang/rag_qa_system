#!/usr/bin/env python3
"""
检索评测 —— 用固定评测集（golden.jsonl）给检索质量打分，结果落盘可对比。

    .venv/bin/python scripts/eval_retrieval.py                      # 跑一遍，tag=baseline
    .venv/bin/python scripts/eval_retrieval.py --tag hybrid         # 换 tag 再跑一遍
    .venv/bin/python scripts/eval_retrieval.py --tag hybrid --compare baseline
    .venv/bin/python scripts/eval_retrieval.py --validate-only      # 只校验标注，不检索
    .venv/bin/python scripts/eval_retrieval.py --category term --category cross

--------------------------------------------------------------------------
为什么要这个脚本（它解决的不是「跑得通」，是「改了能不能证明变好」）
--------------------------------------------------------------------------
检索链路的参数（候选池倍数、重排阈值、是否混合召回）在代码里都是几个常量，
改任何一个都只能靠「我问两句感觉一下」来判断好坏 —— 那不是判断，是掷骰子。
本脚本把「好」变成三个可复现的数字（recall / MRR / 拒答正确率）：
改之前跑一次、改之后跑一次，`--compare` 直接给出差值，够不够一目了然。

--------------------------------------------------------------------------
指标定义（改定义必须先改这里，否则历史结果不可比）
--------------------------------------------------------------------------
· recall@k   （主指标）对每条**库内**题：前 k 条里命中的相关切片数 ÷ 标注的相关切片总数；
              再对所有库内题求平均。取值 0~1。
              ⚠️ 不是「至少命中一条就算 1」的 hit@k —— 后者会掩盖
              「五条相关只找回一条」的退化。两种都打印，主指标是 recall。
· hit@k      辅助指标：至少命中一条的题占库内题的比例（直觉更好读，但对部分命中不敏感）。
· mrr@10     对每条库内题：第一条相关切片出现在第 r 位就记 1/r（r>10 或未出现记 0），求平均。
              衡量「好东西排得够不够前」—— 只有 reranker 之类的排序改动才动得了它，
              纯召回改动主要动 recall。
· 拒答正确率 判定口径与线上完全同构：**top_k=SEARCH_TOP_K 那一路检索结果为空 == 拒答**
              （rag_chain 的零召回短路就是这个判据，不进 LLM）。
              分母只取**库外 10 条**（PLAN 的达标判据写作「10/10 保持」）——
              答「拒」即正确。库内题的反向风险（该答却拒）单列为「误拒数」，
              不并进这个分数：两个方向一涨一跌会互相抵消，看着没变化其实都变了。

--------------------------------------------------------------------------
为什么每个 k 各跑一次检索，而不是「一次取 20 条再截前缀」
--------------------------------------------------------------------------
看着省事，其实是两个不同的实验：
    retrieve(top_k=20) 的候选池是 20 × multiplier = 80，
    线上 retrieve(top_k=5) 的候选池只有 5 × multiplier = 20。
截前缀得到的「recall@5」是**在 80 条候选里重排后的前 5 条**，
比线上真实的前 5 条乐观，而且乐观幅度随改动不同 —— 用它做 A/B 会系统性
高估「扩大候选池」类改动（P3-4 的混合召回正好是这一类）。
所以 k=5 / 10 / 20 各跑一次，各自用自己的候选池。代价是远程重排调用 ×3，
换来的结论是「这个数字就是线上那个数字」。

--------------------------------------------------------------------------
不做什么（边界）
--------------------------------------------------------------------------
· 不评测生成质量（答案写得对不对、像不像人话）—— 那需要另一套人工标注集。
  本脚本只回答「该找的切片找回来了吗、排得靠前吗」。
· 默认不做多轮改写：直接拿 question 原文检索。线上的 standalone_question
  改写只在多轮对话里生效，单轮评测用原文才对得上标注。
  （检索前 Query 增强 §6.4 D 的 A/B 用 `--enhance` / `--hyde` 显式开启，
  开启后会先过一次改写 / HyDE 再检索——这是受控的实验变量，不是默认行为。）
· 不改任何库里的数据（只读检索）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core.retriever import RAGRetriever, _candidate_brief, get_rag_retriever  # noqa: E402
from core.vector_store import parse_chunk_id  # noqa: E402

DEFAULT_GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
DEFAULT_RESULTS_DIR = ROOT / "data" / "eval" / "results"
# 主指标三档。k=5 与线上 SEARCH_TOP_K 对齐；10/20 用来看「召回池放大后能救回多少」
DEFAULT_K_VALUES = (5, 10, 20)
# MRR 用哪一档。固定 10（键名 mrr@10），k 列表里没有 10 时降级到最大的一档并在键名里写明
MRR_K = 10

EXIT_OK = 0
EXIT_ERROR = 1


# --------------------------------------------------------------------------- #
# 评测集读写
# --------------------------------------------------------------------------- #
def load_golden(path: Path) -> list[dict[str, Any]]:
    """
    读取 golden.jsonl，顺手做结构校验。

    为什么在这里校验而不是「跑起来再说」：标注写错（漏 should_answer、
    relevant_chunk_ids 是字符串而不是数组）不会报错，只会让指标悄悄失真，
    而失真的评测集比没有评测集更危险 —— 它会给错误结论盖章。
    """
    if not path.is_file():
        raise FileNotFoundError(f"评测集不存在：{path}")

    rows: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON：{e}") from e

        missing = [k for k in ("id", "category", "question", "relevant_chunk_ids", "should_answer")
                   if k not in item]
        if missing:
            raise ValueError(f"{path}:{lineno} 缺字段 {missing}")
        if not isinstance(item["relevant_chunk_ids"], list):
            raise ValueError(f"{path}:{lineno} relevant_chunk_ids 必须是数组")
        if not isinstance(item["should_answer"], bool):
            raise ValueError(f"{path}:{lineno} should_answer 必须是布尔值")
        if bool(item["should_answer"]) and not item["relevant_chunk_ids"]:
            raise ValueError(
                f"{path}:{lineno} should_answer=true 却没标相关切片 —— "
                f"这种题 recall 恒为 0，会污染分母"
            )
        if not item["should_answer"] and item["relevant_chunk_ids"]:
            raise ValueError(
                f"{path}:{lineno} should_answer=false 却标了相关切片 —— 口径自相矛盾"
            )
        rows.append(item)
    if not rows:
        raise ValueError(f"{path} 里没有任何题目")
    return rows


def golden_fingerprint(path: Path) -> str:
    """评测集指纹。--compare 时用它拦住「两套评测集比数字」这种无效对比。"""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 标注校验（--validate-only）
# --------------------------------------------------------------------------- #
def validate_golden(cases: Sequence[dict[str, Any]]) -> tuple[int, list[str]]:
    """
    逐条反查：标注里的 chunk_id 在向量库里真的存在吗？

    chunk_id 是按 "<doc_id>:<chunk_index>" 拼出来的，知识库一重建 doc_id 就换，
    标注会整批失效（不报错，只是 recall 悄悄掉到 0）。跑评测前先校验这一步。
    """
    vs = get_rag_retriever().vector_store
    problems: list[str] = []
    checked = 0
    for case in cases:
        for chunk_id in case["relevant_chunk_ids"]:
            checked += 1
            try:
                doc_id, chunk_index = parse_chunk_id(str(chunk_id))
            except ValueError as e:
                problems.append(f"{case['id']} {chunk_id} 格式非法：{e}")
                continue
            if vs.get_chunk_by_position(doc_id, chunk_index) is None:
                problems.append(f"{case['id']} {chunk_id} 在库里不存在")
    return checked, problems


# --------------------------------------------------------------------------- #
# 单题评测
# --------------------------------------------------------------------------- #
def _chunk_ids_of(docs: Iterable[Any]) -> list[str]:
    """
    从检索结果里取 chunk_id 序列（保持顺序）。

    ⚠️ 复用 core.retriever._candidate_brief 而不是自己再写一遍解析：
    chunk_id 契约 "<doc_id>:<chunk_index>" 在运行时有两处同构实现
    （retriever 与 rag_chain），再加第三处迟早漂移 —— 由
    tests/test_module5_retrieval_observability.py 守着的那两处不漂移，
    本脚本直接借其中一处。
    """
    out: list[str] = []
    for doc in docs:
        chunk_id = _candidate_brief(doc).get("chunk_id")
        if chunk_id:
            out.append(str(chunk_id))
    return out


def evaluate_case(
    retriever: RAGRetriever,
    case: dict[str, Any],
    k_values: Sequence[int],
    enhance: bool = False,
    hyde: bool = False,
) -> dict[str, Any]:
    """
    跑一条题：按 k_values 各检索一次，算出这条题的 recall / MRR / 是否拒答。

    检索前 Query 增强（路线图 §6.4 D，v3.1.0-p3.7d）：
        默认用 question 原文检索（与标注对齐，单轮评测就该用原文）；
        enhance=True 时先过一次改写 prompt，hyde=True 时再生成假设答案做 HyDE 检索。
        二者都只在本脚本内生效，不改变任何库里的数据。

    :return: 单题结果（含每个 k 的命中清单，写进结果文件供事后复盘）
    """
    relevant = [str(c) for c in case["relevant_chunk_ids"]]
    should_answer = bool(case["should_answer"])

    # 检索前 Query 增强：--enhance / --hyde 时改写（+ 可选 HyDE），用增强后的 query 检索
    query = case["question"]
    vector_query: str | None = None
    if enhance or hyde:
        from core.rag_chain import rewrite_query, generate_hypo_answer
        query = rewrite_query(case["question"])
        if hyde:
            vector_query = generate_hypo_answer(query)

    retrieved_by_k: dict[str, list[str]] = {}
    for k in k_values:
        docs = retriever.retrieve(query, top_k=k, vector_query=vector_query)
        retrieved_by_k[str(k)] = _chunk_ids_of(docs)

    # recall / hit：只在库内题上有意义（库外题没有相关切片，分母为 0）
    recall_by_k: dict[str, float | None] = {}
    hit_by_k: dict[str, int | None] = {}
    for k in k_values:
        got = retrieved_by_k[str(k)][:k]
        if not should_answer or not relevant:
            recall_by_k[str(k)] = None
            hit_by_k[str(k)] = None
            continue
        found = len(set(got) & set(relevant))
        recall_by_k[str(k)] = found / len(relevant)
        hit_by_k[str(k)] = 1 if found > 0 else 0

    # MRR：第一条相关切片的位置（1-based），超出窗口或未命中记 0
    mrr_k = MRR_K if MRR_K in k_values else max(k_values)
    mrr: float | None = None
    if should_answer and relevant:
        window = retrieved_by_k[str(mrr_k)][:mrr_k]
        mrr = 0.0
        for rank, chunk_id in enumerate(window, start=1):
            if chunk_id in relevant:
                mrr = 1.0 / rank
                break

    # 拒答：与线上同构 —— 线上 top_k 那一路结果为空即拒答
    production_k = min(k_values)
    abstained = not retrieved_by_k[str(production_k)]
    if should_answer:
        abstain_correct = not abstained          # 库内题：答了才对
    else:
        abstain_correct = abstained              # 库外题：拒了才对

    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "should_answer": should_answer,
        "relevant": relevant,
        "retrieved": retrieved_by_k,
        "recall": recall_by_k,
        "hit": hit_by_k,
        "mrr_k": mrr_k,
        "mrr": mrr,
        "abstained": abstained,
        "abstain_correct": abstain_correct,
        "enhanced_query": query if (enhance or hyde) else None,
        "note": case.get("note", ""),
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(
    per_question: Sequence[dict[str, Any]],
    k_values: Sequence[int],
    mrr_k: int,
) -> dict[str, Any]:
    """把单题结果汇总成对外汇报的几个数字。"""
    in_scope = [q for q in per_question if q["should_answer"]]
    out_scope = [q for q in per_question if not q["should_answer"]]

    recall: dict[str, float] = {}
    hit: dict[str, float] = {}
    for k in k_values:
        vals = [q["recall"][str(k)] for q in in_scope if q["recall"][str(k)] is not None]
        hits = [q["hit"][str(k)] for q in in_scope if q["hit"][str(k)] is not None]
        recall[f"recall@{k}"] = round(_mean(vals), 4)
        hit[f"hit@{k}"] = round(_mean(hits), 4)

    mrr_vals = [q["mrr"] for q in in_scope if q["mrr"] is not None]
    mrr = round(_mean(mrr_vals), 4)

    # ⚠️ 口径对齐 PLAN §P3-3：拒答正确率 = **库外 10 条**里被正确拒答的比例
    # （PLAN 的达标判据写的是「10/10 保持」，分母就是库外题）。
    # 库内题的「误拒」单独记 —— 它是另一个方向的风险（该答却拒了），
    # 不该和「该拒就拒」混成一个分数，否则一个涨一个跌会互相抵消。
    out_correct = sum(1 for q in out_scope if q["abstain_correct"])
    in_false = sum(1 for q in in_scope if q["abstained"])
    abstain = {
        "accuracy": round(out_correct / len(out_scope), 4) if out_scope else 0.0,
        "out_of_scope_correct": out_correct,
        "out_of_scope_total": len(out_scope),
        "in_scope_false_abstain": in_false,
        "in_scope_total": len(in_scope),
    }

    # 分类下钻：混合召回对不同题型的影响不一样，看总数会互相抵消
    by_category: dict[str, Any] = {}
    for category in sorted({q["category"] for q in per_question}):
        rows = [q for q in per_question if q["category"] == category]
        vals20 = [q["recall"][str(max(k_values))] for q in rows
                  if q["recall"][str(max(k_values))] is not None]
        mvals = [q["mrr"] for q in rows if q["mrr"] is not None]
        by_category[category] = {
            "count": len(rows),
            f"recall@{max(k_values)}": round(_mean(vals20), 4) if vals20 else None,
            f"mrr@{mrr_k}": round(_mean(mvals), 4) if mvals else None,
            "abstain_correct": sum(1 for q in rows if q["abstain_correct"]),
        }

    return {
        **recall,
        **hit,
        f"mrr@{mrr_k}": mrr,
        "abstain": abstain,
        "by_category": by_category,
    }


# --------------------------------------------------------------------------- #
# 结果落盘与对比
# --------------------------------------------------------------------------- #
def save_result(payload: dict[str, Any], out_dir: Path, tag: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{stamp}-{tag}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def latest_result(out_dir: Path, tag: str) -> Path | None:
    """按 tag 找最新一份结果（文件名格式 <timestamp>-<tag>.json）。"""
    if not out_dir.is_dir():
        return None
    hits = sorted(out_dir.glob(f"*-{tag}.json"))
    return hits[-1] if hits else None


def resolve_compare(raw: str, out_dir: Path) -> Path:
    """
    --compare 既能接文件路径，也能接 tag（找该 tag 最新一份）。
    """
    direct = Path(raw)
    if direct.is_file():
        return direct
    found = latest_result(out_dir, raw)
    if found is None:
        raise FileNotFoundError(
            f"--compare {raw} 既不是存在的文件，也没有在 {out_dir} 下找到 tag={raw} 的结果"
        )
    return found


def metric_keys(metrics: dict[str, Any]) -> list[str]:
    """对比表要按顺序展示的指标键（不含 by_category）。"""
    return [k for k in metrics if k != "by_category"]


# --------------------------------------------------------------------------- #
# 打印
# --------------------------------------------------------------------------- #
def print_report(result: dict[str, Any]) -> None:
    metrics = result["metrics"]
    meta = result["meta"]
    print()
    print("=" * 72)
    print(f"检索评测 | tag={meta['tag']} | 题数={meta['questions']}"
          f"（库内 {meta['in_scope']} / 库外 {meta['out_of_scope']}）")
    print("=" * 72)
    print(f"检索配置：top_k={meta['retrieval']['top_k']} "
          f"候选池×{meta['retrieval']['candidate_multiplier']} "
          f"重排={meta['retrieval']['reranker_model'] or '关'} "
          f"阈值={meta['retrieval']['score_threshold']} "
          f"混合召回={meta['retrieval']['hybrid']}")
    print(f"耗时：{meta['elapsed_sec']}s   评测集指纹：{meta['golden_sha1']}")
    print("-" * 72)
    for key in metric_keys(metrics):
        if key.startswith("abstain") and isinstance(metrics[key], dict):
            continue
        print(f"  {key:<14} {metrics[key]:.4f}")
    a = metrics["abstain"]
    print(f"  {'拒答正确率':<14} {a['accuracy']:.4f}   "
          f"(库外正确拒答 {a['out_of_scope_correct']}/{a['out_of_scope_total']}；"
          f"辅助：库内误拒 {a['in_scope_false_abstain']}/{a['in_scope_total']})")
    print("-" * 72)
    print("  分类下钻（recall 取最大 k 档）：")
    for category, row in metrics["by_category"].items():
        rkey = f"recall@{max(meta['k_values'])}"
        mkey = f"mrr@{meta['mrr_k']}"
        rval = "  -  " if row[rkey] is None else f"{row[rkey]:.4f}"
        mval = "  -  " if row[mkey] is None else f"{row[mkey]:.4f}"
        print(f"    {category:<14} n={row['count']:<3} {rkey}={rval} {mkey}={mval} "
              f"拒答判定正确={row['abstain_correct']}")
    print("=" * 72)


def print_compare(base: dict[str, Any], new: dict[str, Any], base_path: Path) -> None:
    bm, nm = base["metrics"], new["metrics"]
    print()
    print("=" * 72)
    print(f"A/B 对比：{base['meta']['tag']}  →  {new['meta']['tag']}")
    print(f"基线文件：{base_path}")
    print("=" * 72)

    if base["meta"]["golden_sha1"] != new["meta"]["golden_sha1"]:
        print("  ⚠️ 两份结果的评测集指纹不同，数字不可比（标注改过或知识库重建过）")

    print(f"  {'指标':<14}{'基线':>9}{'本次':>9}{'差值':>10}")
    print("-" * 72)
    for key in metric_keys(nm):
        if key == "abstain":
            continue
        b = bm.get(key)
        n = nm.get(key)
        if not isinstance(b, (int, float)) or not isinstance(n, (int, float)):
            continue
        print(f"  {key:<14}{b:>9.4f}{n:>9.4f}{n - b:>+10.4f}")

    ba, na = bm["abstain"], nm["abstain"]
    delta = na["accuracy"] - ba["accuracy"]
    print(f"  {'拒答正确率':<14}{ba['accuracy']:>9.4f}{na['accuracy']:>9.4f}{delta:>+10.4f}")

    # P3-4 的达标判据写在这里，避免每次翻 PLAN：三条全中才算赢
    max_k = max(new["meta"]["k_values"])
    rk = f"recall@{max_k}"
    mk = f"mrr@{new['meta']['mrr_k']}"
    c1 = nm[rk] - bm[rk] >= 0.05
    c2 = nm[mk] - bm[mk] >= 0.05
    c3 = na["accuracy"] >= ba["accuracy"]
    print("-" * 72)
    print("  P3-4 达标判据（三条全中才算过）：")
    print(f"    {'✅' if c1 else '❌'} {rk} 提升 ≥ 5pp     实际 {nm[rk] - bm[rk]:+.4f}")
    print(f"    {'✅' if c2 else '❌'} {mk} 提升 ≥ 0.05    实际 {nm[mk] - bm[mk]:+.4f}")
    print(f"    {'✅' if c3 else '❌'} 拒答正确率不降       实际 {delta:+.4f}")
    print(f"    结论：{'达标' if (c1 and c2 and c3) else '不达标'}")
    print("=" * 72)


def print_misses(result: dict[str, Any]) -> None:
    """列出全部未命中的库内题 —— 数字只说「变差了」，清单才说「差在哪」。"""
    misses = [q for q in result["per_question"]
              if q["should_answer"] and (q["hit"][str(min(result["meta"]["k_values"]))] == 0)]
    if not misses:
        return
    print()
    print(f"  线上 top_k 档未命中 {len(misses)} 条：")
    for q in misses:
        got = q["retrieved"][str(min(result["meta"]["k_values"]))][:3]
        print(f"    [{q['id']}][{q['category']}] {q['question']}")
        print(f"        标注={q['relevant']} 实际前3={got}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用 golden.jsonl 评测检索质量（recall / MRR / 拒答正确率）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN,
                        help=f"评测集路径（默认 {DEFAULT_GOLDEN.relative_to(ROOT)}）")
    parser.add_argument("--tag", default="baseline",
                        help="本轮结果的标签，进文件名（默认 baseline）")
    parser.add_argument("--k", default=",".join(str(k) for k in DEFAULT_K_VALUES),
                        help="要测的 top_k 档位，逗号分隔（默认 5,10,20）")
    parser.add_argument("--category", action="append", default=None,
                        help="只跑指定分类，可重复传（term/numbered/paraphrase/cross/out_of_scope）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 条（调试用，0=全部）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR,
                        help="结果落盘目录")
    parser.add_argument("--compare", default=None,
                        help="对比基线：可传结果文件路径，也可传 tag（取该 tag 最新一份）")
    parser.add_argument("--no-save", action="store_true",
                        help="不落盘（只看一眼数字）")
    parser.add_argument("--validate-only", action="store_true",
                        help="只校验标注里的 chunk_id 在库里是否存在，不检索")
    parser.add_argument("--enhance", action="store_true",
                        help="启用检索前 Query 增强：每条题先过一次改写（§6.4 D）再检索")
    parser.add_argument("--hyde", action="store_true",
                        help="在 --enhance 基础上再做 HyDE：用生成的假设答案 embedding 做向量召回")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()

    try:
        cases = load_golden(args.golden)
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ {e}")
        return EXIT_ERROR

    if args.validate_only:
        checked, problems = validate_golden(cases)
        print(f"校验 {len(cases)} 条标注 / {checked} 个 chunk_id：", end="")
        if problems:
            print(f"❌ {len(problems)} 处问题")
            for p in problems:
                print(f"   - {p}")
            return EXIT_ERROR
        print("✅ 全部命中库内真实切片")
        return EXIT_OK

    try:
        k_values = tuple(int(x) for x in args.k.split(",") if x.strip())
    except ValueError:
        print(f"❌ --k 解析失败：{args.k}")
        return EXIT_ERROR
    if not k_values or any(k <= 0 for k in k_values):
        print(f"❌ --k 必须是正整数列表：{args.k}")
        return EXIT_ERROR
    k_values = tuple(sorted(set(k_values)))

    if args.category:
        wanted = set(args.category)
        unknown = wanted - {c["category"] for c in cases}
        if unknown:
            print(f"❌ 未知分类：{sorted(unknown)}")
            return EXIT_ERROR
        cases = [c for c in cases if c["category"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("❌ 筛完之后没有题目可跑")
        return EXIT_ERROR

    retriever = get_rag_retriever()
    info = retriever.get_retriever_info()
    mrr_k = MRR_K if MRR_K in k_values else max(k_values)

    print(f"开始评测 | 题数={len(cases)} k={list(k_values)} "
          f"重排={'开(' + str(info['reranker_model']) + ')' if info['use_reranker'] else '关'}"
          f"{' | 增强=改写' if args.enhance else ''}{' +HyDE' if args.hyde else ''}")
    start = time.perf_counter()
    per_question: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        row = evaluate_case(retriever, case, k_values, enhance=args.enhance, hyde=args.hyde)
        per_question.append(row)
        flag = "" if row["abstain_correct"] else "  ← 拒答判定错"
        print(f"  ({index}/{len(cases)}) {case['id']} {case['question'][:28]}{flag}")
    elapsed = round(time.perf_counter() - start, 1)

    metrics = aggregate(per_question, k_values, mrr_k)
    result: dict[str, Any] = {
        "meta": {
            "tag": args.tag,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "golden": str(args.golden.relative_to(ROOT)) if args.golden.is_absolute() else str(args.golden),
            "golden_sha1": golden_fingerprint(args.golden),
            "questions": len(cases),
            "in_scope": sum(1 for q in per_question if q["should_answer"]),
            "out_of_scope": sum(1 for q in per_question if not q["should_answer"]),
            "k_values": list(k_values),
            "mrr_k": mrr_k,
            "elapsed_sec": elapsed,
            "retrieval": {
                "top_k": info["top_k"],
                "candidate_multiplier": info["candidate_multiplier"],
                "use_reranker": info["use_reranker"],
                "reranker_model": info["reranker_model"],
                "score_threshold": info["score_threshold"],
                "vector_store_type": info["vector_store_type"],
                # P3-4 才会真的有这个开关；用 getattr 取，改动前后同一份脚本都能跑
                "hybrid": bool(getattr(settings, "RETRIEVAL_HYBRID", False)),
                # P3-7a：切分模式进 meta，A/B（fixed vs title）对比时看得见是哪种形态的库
                "chunk_mode": str(getattr(settings, "CHUNK_MODE", "fixed")),
            },
        },
        "metrics": metrics,
        "per_question": per_question,
    }

    print_report(result)
    print_misses(result)

    saved: Path | None = None
    if not args.no_save:
        saved = save_result(result, args.out_dir, args.tag)
        print(f"\n结果已落盘：{saved.relative_to(ROOT)}")

    if args.compare:
        try:
            base_path = resolve_compare(args.compare, args.out_dir)
        except FileNotFoundError as e:
            print(f"❌ {e}")
            return EXIT_ERROR
        base = json.loads(base_path.read_text(encoding="utf-8"))
        print_compare(base, result, base_path)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
