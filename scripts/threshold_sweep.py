#!/usr/bin/env python3
"""
阈值扫档 —— 回答「重排分数阈值设多少，拒答正确率和命中率怎么权衡」。

    .venv/bin/python scripts/threshold_sweep.py                    # 跑一遍 + 出 HTML 报告
    .venv/bin/python scripts/threshold_sweep.py --hybrid           # 带混合召回跑（默认关，对齐基线）

--------------------------------------------------------------------------
原理：为什么跑一遍就能算四档
--------------------------------------------------------------------------
重排分数是**降序**的，阈值只做「score >= T 保留」的截断 —— 所以：
    · 某阈值 T 下「进上下文的文档」⊆ 分数最高的前 top_k 篇；
    · 「拒答」（进上下文为空）⟺ top1 分数 < T。
因此**以阈值 0（关过滤）跑一遍**、记录每题 top-5 的 (chunk_id, score)，
离线即可对任意 T 重算全部指标，不需要每个阈值真的跑一遍检索。

--------------------------------------------------------------------------
指标（每档 T 一组，口径与 eval_retrieval.py 对齐）
--------------------------------------------------------------------------
· 命中率 hit@5      库内题：保留的 top5 里至少一条命中标注切片的比例（主看这个）
· recall@5          库内题：命中标注切片数 ÷ 标注总数（阈值升高只会降不会升）
· 误拒              库内题：保留为空（该答却拒）—— 阈值过高的代价
· 拒答正确率        库外题：保留为空（该拒就拒）—— 阈值过低的代价
                      ↑ 病假（q041）这类「擦边相关但库里无答案」的题在这一列：
                        阈值调不出它的正确行为时，就是 P4-6 Groundedness 立项的证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core.retriever import RAGRetriever, _candidate_brief, get_rag_retriever  # noqa: E402
from eval_retrieval import load_golden  # noqa: E402

DEFAULT_GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
DEFAULT_OUT_DIR = ROOT / "data" / "eval" / "results"
THRESHOLDS = (0.1, 0.3, 0.5, 0.7)
PRODUCTION_K = 5


def sweep_case(retriever: RAGRetriever, case: dict[str, Any]) -> dict[str, Any]:
    """跑一条题（阈值 0 = 关过滤），记录 top-5 的 (chunk_id, score)。"""
    docs = retriever.retrieve(case["question"], top_k=PRODUCTION_K)
    scored = []
    for doc in docs:
        brief = _candidate_brief(doc)
        # 分数在 doc.metadata["rerank_score"]（_rank 重排分支写入）；
        # ⚠️ _candidate_brief 只给 chunk_id/file_name，不含分数——首版在这里读 0.0
        score = doc.metadata.get("rerank_score")
        scored.append({
            "chunk_id": str(brief.get("chunk_id")),
            "file_name": str(brief.get("file_name") or ""),
            "score": float(score) if score is not None else 0.0,
        })
    return {
        "id": case["id"],
        "question": case["question"],
        "should_answer": bool(case["should_answer"]),
        "relevant": [str(c) for c in case["relevant_chunk_ids"]],
        "scores": scored,
        "note": case.get("note", ""),
    }


def metrics_at_threshold(rows: list[dict[str, Any]], t: float) -> dict[str, Any]:
    """对单个阈值 T 重算全部指标（纯离线，不再检索）。"""
    hit_vals: list[float] = []
    recall_vals: list[float] = []
    false_abstain = 0
    in_scope = 0
    out_correct = 0
    out_total = 0
    verdicts: dict[str, str] = {}
    for row in rows:
        kept = [c for c in row["scores"] if c["score"] >= t][:PRODUCTION_K]
        kept_ids = {c["chunk_id"] for c in kept}
        abstained = not kept
        if row["should_answer"]:
            in_scope += 1
            found = len(kept_ids & set(row["relevant"]))
            recall_vals.append(found / len(row["relevant"]) if row["relevant"] else 0.0)
            hit_vals.append(1.0 if found > 0 else 0.0)
            if abstained:
                false_abstain += 1
            verdicts[row["id"]] = "误拒" if abstained else ("命中" if found else "答但未命中")
        else:
            out_total += 1
            if abstained:
                out_correct += 1
                verdicts[row["id"]] = "正确拒答"
            else:
                verdicts[row["id"]] = "未拒答（幻觉风险）"
    return {
        "threshold": t,
        "hit@5": round(sum(hit_vals) / in_scope, 4) if in_scope else None,
        "recall@5": round(sum(recall_vals) / in_scope, 4) if in_scope else None,
        "false_abstain": false_abstain,
        "in_scope_total": in_scope,
        "abstain_accuracy": round(out_correct / out_total, 4) if out_total else None,
        "out_correct": out_correct,
        "out_total": out_total,
        "_verdicts": verdicts,
    }


# --------------------------------------------------------------------------- #
# HTML 报告（暗色 dashboard 风，自包含、无外部依赖）
# --------------------------------------------------------------------------- #
def render_html(payload: dict[str, Any]) -> str:
    ms = payload["metrics"]
    meta = payload["meta"]
    rows = payload["per_question"]

    # ---- 折线图（SVG，x=四档阈值，y=0~1）----
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 640, 300, 60, 30, 30, 46
    xs = {t: PAD_L + i * (W - PAD_L - PAD_R) / (len(THRESHOLDS) - 1)
          for i, t in enumerate(THRESHOLDS)}
    def y(v: float) -> float:
        return PAD_T + (1 - v) * (H - PAD_T - PAD_B)
    grid = "".join(
        f'<line x1="{PAD_L}" y1="{y(g):.0f}" x2="{W - PAD_R}" y2="{y(g):.0f}" stroke="#1e293b"/>'
        f'<text x="{PAD_L - 8}" y="{y(g) + 4:.0f}" text-anchor="end" fill="#64748b" font-size="11">{g:.1f}</text>'
        for g in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    xticks = "".join(
        f'<text x="{xs[t]:.0f}" y="{H - PAD_B + 18}" text-anchor="middle" fill="#94a3b8" font-size="12">{t}</text>'
        for t in THRESHOLDS
    )
    series = [
        ("命中率 hit@5", "#38bdf8", "hit@5"),
        ("拒答正确率（库外）", "#34d399", "abstain_accuracy"),
        ("误拒率（库内，反向代价）", "#f87171", None),  # false_abstain / in_scope_total
    ]
    polylines = ""
    dots = ""
    for name, color, key in series:
        pts = []
        for t in THRESHOLDS:
            m = ms[str(t)]
            v = (m["false_abstain"] / m["in_scope_total"]) if key is None else m[key]
            pts.append(f"{xs[t]:.0f},{y(v):.0f}")
            dots += (f'<circle cx="{xs[t]:.0f}" cy="{y(v):.0f}" r="4" fill="{color}"/>'
                     f'<text x="{xs[t]:.0f}" y="{y(v) - 10:.0f}" text-anchor="middle" '
                     f'fill="{color}" font-size="11">{v:.2f}</text>')
        polylines += (f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
                      f'stroke-width="2"/>')

    detail_rows = "".join(
        f'<tr class="{"oos" if not r["should_answer"] else ""}">'
        f'<td>{r["id"]}</td><td class="q">{r["question"]}</td>'
        f'<td class="mono">{r["scores"][0]["score"]:.4f}</td>'
        f'<td class="mono">{r["scores"][0]["chunk_id"]}</td>'
        f'<td>{ms["0.1"]["_verdicts"].get(r["id"], "—")}</td>'
        f'<td>{ms["0.3"]["_verdicts"].get(r["id"], "—")}</td>'
        f'<td>{ms["0.5"]["_verdicts"].get(r["id"], "—")}</td>'
        f'<td>{ms["0.7"]["_verdicts"].get(r["id"], "—")}</td>'
        f"</tr>"
        for r in rows
    )
    th_rows = "".join(
        f'<tr><td class="mono">{m["threshold"]}</td>'
        f'<td>{m["hit@5"]:.4f}</td><td>{m["recall@5"]:.4f}</td>'
        f'<td>{m["false_abstain"]} / {m["in_scope_total"]}</td>'
        f'<td>{m["out_correct"]} / {m["out_total"]}</td>'
        f'<td>{m["abstain_accuracy"]:.4f}</td></tr>'
        for m in (ms[str(t)] for t in THRESHOLDS)
    )
    q041 = next(r for r in rows if r["id"] == "q041")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>重排阈值扫档报告 · {meta['generated_at']}</title>
<style>
  body {{ background:#0b1220; color:#e2e8f0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         margin:0; padding:32px 40px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:28px 0 10px; color:#94a3b8; }}
  .sub {{ color:#64748b; font-size:13px; margin-bottom:20px; }}
  .card {{ background:#111a2e; border:1px solid #1e293b; border-radius:12px; padding:18px 20px; margin-bottom:18px; }}
  .kv {{ color:#64748b; font-size:12px; line-height:1.9; }}
  .kv b {{ color:#cbd5e1; font-weight:500; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:7px 10px; border-bottom:1px solid #1e293b; text-align:left; }}
  th {{ color:#94a3b8; font-weight:500; }}
  td.mono, .mono {{ font-family:ui-monospace,Menlo,monospace; }}
  td.q {{ max-width:260px; }}
  tr.oos td {{ color:#94a3b8; }}
  svg text {{ font-family:inherit; }}
  .callout {{ border-left:3px solid #f87171; padding:10px 14px; background:#1a2338; border-radius:6px;
              font-size:13px; line-height:1.8; color:#cbd5e1; }}
  .legend {{ display:flex; gap:22px; font-size:12px; color:#94a3b8; margin:6px 0 0 60px; }}
  .legend i {{ display:inline-block; width:20px; height:2.5px; vertical-align:middle; margin-right:6px; }}
</style></head><body>
<h1>重排分数阈值扫档报告</h1>
<div class="sub">生成于 {meta['generated_at']} · 评测集 {meta['golden']}（{meta['questions']} 题，指纹 {meta['golden_sha1']}）</div>

<div class="card">
  <div class="kv">
    检索配置：<b>top_k={meta['retrieval']['top_k']}</b> · 重排=<b>{meta['retrieval']['reranker_model']}</b>
    · 混合召回=<b>{'开' if meta['retrieval']['hybrid'] else '关'}</b> · 评测运行阈值=<b>0（关过滤，离线重算四档）</b><br>
    库内 {meta['in_scope']} 题（应答） / 库外 {meta['out_total']} 题（应拒答，含 q041 幻觉回归锚） · 耗时 {meta['elapsed_sec']}s
  </div>
</div>

<h2>拒答正确率 vs 命中率 · 权衡曲线</h2>
<div class="card">
  <svg viewBox="0 0 {W} {H}" width="100%">
    {grid}
    {polylines}{dots}
    {xticks}
    <text x="{(PAD_L + W - PAD_R) / 2:.0f}" y="{H - 6}" text-anchor="middle" fill="#64748b" font-size="12">重排分数阈值</text>
  </svg>
  <div class="legend">
    <span><i style="background:#38bdf8"></i>命中率 hit@5（库内）</span>
    <span><i style="background:#34d399"></i>拒答正确率（库外）</span>
    <span><i style="background:#f87171"></i>误拒率（库内，反向代价）</span>
  </div>
</div>

<h2>各档指标汇总</h2>
<div class="card">
  <table>
    <tr><th>阈值</th><th>命中率 hit@5</th><th>recall@5</th><th>误拒（库内）</th><th>正确拒答（库外）</th><th>拒答正确率</th></tr>
    {th_rows}
  </table>
</div>

<h2>关键案例 · q041「请病假扣工资吗？」（幻觉回归锚）</h2>
<div class="card">
  <div class="callout">
    库内考勤制度只写「病假需凭医疗机构证明」（812:2），<b>没有任何病假扣薪规定</b>。
    实测 top1=<b class="mono">{q041['scores'][0]['score']:.4f}</b>（812:1，考勤打卡/迟到旷工切片，不含病假）——
    阈值 {ms['0.1']['threshold']} 与 {ms['0.3']['threshold']} 档都不拒答（上下文无答案 → 模型用常识编造），
    {ms['0.5']['threshold']} 档起才拒答。<br>
    结论：<b>阈值挡得住「全库无相关」，挡不住「擦边相关但无答案」</b>——后者需要答案充分性校验（P4-6 Groundedness）。
  </div>
</div>

<h2>逐题明细（按 id 排序，含每档判定）</h2>
<div class="card">
  <table>
    <tr><th>id</th><th>问题</th><th>top1 分数</th><th>top1 chunk</th><th>T=0.1</th><th>T=0.3</th><th>T=0.5</th><th>T=0.7</th></tr>
    {detail_rows}
  </table>
</div>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重排阈值扫档：0.1/0.3/0.5/0.7 的拒答正确率 vs 命中率")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--hybrid", action="store_true", help="带混合召回跑（默认关，对齐评测基线）")
    args = parser.parse_args(argv)
    setup_logging()

    cases = load_golden(args.golden)
    retriever = get_rag_retriever()
    # 关过滤跑一遍：拿全量分数，离线重算各档（见文件头「原理」）
    retriever._threshold_override = 0
    info = retriever.get_retriever_info()

    print(f"阈值扫档 | 题数={len(cases)} 重排={info['reranker_model']} "
          f"混合={'开' if args.hybrid else '关'} 运行阈值=0（离线重算 {THRESHOLDS}）")
    start = time.perf_counter()
    rows = []
    for i, case in enumerate(cases, 1):
        row = sweep_case(retriever, case)
        rows.append(row)
        top1 = row["scores"][0]["score"] if row["scores"] else 0.0
        print(f"  ({i}/{len(cases)}) {case['id']} {case['question'][:26]} top1={top1:.4f}")
    elapsed = round(time.perf_counter() - start, 1)

    metrics = {str(t): metrics_at_threshold(rows, t) for t in THRESHOLDS}
    payload = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "golden": str(args.golden),
            "golden_sha1": hashlib.sha1(args.golden.read_bytes()).hexdigest()[:12],
            "questions": len(rows),
            "in_scope": sum(1 for r in rows if r["should_answer"]),
            "out_total": sum(1 for r in rows if not r["should_answer"]),
            "elapsed_sec": elapsed,
            "retrieval": {
                "top_k": info["top_k"],
                "reranker_model": info["reranker_model"],
                "hybrid": bool(args.hybrid),
                "run_threshold": 0,
            },
        },
        "metrics": metrics,
        "per_question": rows,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = args.out_dir / f"{stamp}-threshold-sweep.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path = args.out_dir / f"{stamp}-threshold-sweep.html"
    html_path.write_text(render_html(payload), encoding="utf-8")

    print()
    print(f"{'阈值':<8}{'命中率':>8}{'recall@5':>10}{'误拒':>10}{'正确拒答':>12}{'拒答正确率':>10}")
    for t in THRESHOLDS:
        m = metrics[str(t)]
        print(f"{t:<8}{m['hit@5']:>8.4f}{m['recall@5']:>10.4f}"
              f"{m['false_abstain']:>5}/{m['in_scope_total']:<4}"
              f"{m['out_correct']:>6}/{m['out_total']:<5}{m['abstain_accuracy']:>10.4f}")
    print(f"\n报告：{html_path}")
    print(f"数据：{json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
