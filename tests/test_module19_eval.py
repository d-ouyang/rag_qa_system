# pyright: basic
"""
P3-3 测试：检索评测集 + 评测脚本（golden set / recall / MRR / 拒答正确率）。

    .venv/bin/python tests/test_module19_eval.py
    .venv/bin/python tests/test_module19_eval.py --reverse

--------------------------------------------------------------------------
为什么要给「一个评测脚本」写回归
--------------------------------------------------------------------------
因为它接下来要当**裁判**：P3-4 的混合检索达标与否，完全由它打印的三个数字决定。
裁判本身错了，比没有裁判更糟 —— 它会给错误结论盖章，而且盖得很有权威感。
所以这里钉的不是「脚本能不能跑」（跑一次就知道了），而是三件会悄悄失真的事：

    ① **指标定义**：recall 是「比例型」而不是「至少命中一条算 1」的 hit 型。
       两者差着一个数量级的敏感度：五条相关只找回一条，hit 仍是 1.0。
    ② **实验协议**：k=5/10/20 **各检索一次**，不是「一次取 20 再截前缀」。
       后者会让 recall@5 变成「在 80 条候选里重排后的前 5 条」，
       比线上乐观，且乐观幅度随改动不同 —— 用它判 P3-4 会系统性高估收益。
    ③ **拒答口径**：分母只有库外 10 条（PLAN 写的是「10/10 保持」），
       库内误拒单列。混成一个分数会让两个方向互相抵消。

--------------------------------------------------------------------------
为什么**不**真跑一次检索
--------------------------------------------------------------------------
真跑要联网（远程 embedding + 远程 rerank），而 `make test-sandbox` 的定位是
离线回归。本模块用**假检索器**（按预设顺序吐 chunk_id）驱动 `evaluate_case`，
把「算得对不对」和「连不连得上」两件事分开：连不上是环境问题，不该让指标测试红。
真跑由 `make eval-retrieval` 负责，数字落 `data/eval/results/`。
"""
import io
import json
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
REVERSE = "--reverse" in sys.argv


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


import eval_retrieval as ev                                # noqa: E402
from core.vector_store import parse_chunk_id               # noqa: E402

GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
SCRIPT = ROOT / "scripts" / "eval_retrieval.py"
MAKEFILE = ROOT / "Makefile"
SRC = SCRIPT.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 第 1 组：评测集本身（读真实文件，不联网）
# --------------------------------------------------------------------------- #
section("第 1 组：golden.jsonl 的结构与配比")

cases = ev.load_golden(GOLDEN)
check("golden.jsonl 能被 load_golden 读进来且非空", len(cases) > 0, f"读到 {len(cases)} 条")
check("共 40 条（PLAN §P3-3 定的量）", len(cases) == 40, f"实际 {len(cases)}")

in_scope = [c for c in cases if c["should_answer"]]
out_scope = [c for c in cases if not c["should_answer"]]
check("库内 30 条 / 库外 10 条", len(in_scope) == 30 and len(out_scope) == 10,
      f"库内={len(in_scope)} 库外={len(out_scope)}")

counts: dict[str, int] = {}
for c in cases:
    counts[c["category"]] = counts.get(c["category"], 0) + 1
EXPECTED_COUNTS = {"term": 10, "numbered": 8, "paraphrase": 7, "cross": 5, "out_of_scope": 10}
check("分类配比 = 10 专名 / 8 编号 / 7 改写 / 5 跨文档 / 10 库外",
      counts == EXPECTED_COUNTS, f"实际 {counts}")

check("每条都有 id / question / note（note 是事后复盘的唯一线索）",
      all(c.get("id") and c.get("question") and c.get("note") for c in cases),
      f"缺 note 的：{[c['id'] for c in cases if not c.get('note')]}")

bad_format = []
for c in cases:
    for chunk_id in c["relevant_chunk_ids"]:
        try:
            doc_id, chunk_index = parse_chunk_id(str(chunk_id))
            if doc_id <= 0 or chunk_index < 0:
                bad_format.append(chunk_id)
        except ValueError:
            bad_format.append(str(chunk_id))
check("所有标注 chunk_id 都符合 '<doc_id>:<chunk_index>' 契约",
      not bad_format, f"不合规：{bad_format}")

check("库外题的 relevant_chunk_ids 必须是空数组（口径不能自相矛盾）",
      all(c["relevant_chunk_ids"] == [] for c in out_scope),
      f"非空：{[c['id'] for c in out_scope if c['relevant_chunk_ids']]}")

ids = [c["id"] for c in cases]
check("id 不重复（结果文件里靠它定位）", len(ids) == len(set(ids)),
      f"重复：{[i for i in ids if ids.count(i) > 1]}")

# --------------------------------------------------------------------------- #
# 第 2 组：load_golden 的校验能不能拦住坏标注
# --------------------------------------------------------------------------- #
section("第 2 组：坏标注必须报错，不能悄悄失真")


def _try_load(text: str) -> str:
    """写临时文件 → load_golden → 返回 'ok' 或异常类型的名字。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "g.jsonl"
        p.write_text(text, encoding="utf-8")
        try:
            ev.load_golden(p)
            return "ok"
        except ValueError:
            return "ValueError"
        except FileNotFoundError:
            return "FileNotFoundError"


GOOD = json.dumps({
    "id": "x1", "category": "term", "question": "?",
    "relevant_chunk_ids": ["1:0"], "should_answer": True,
}, ensure_ascii=False)

check("合法一行 → 正常读入", _try_load(GOOD + "\n") == "ok")


def _bad(**override) -> str:
    row = json.loads(GOOD)
    row.update(override)
    return json.dumps(row, ensure_ascii=False) + "\n"


check("should_answer=true 却不标相关切片 → ValueError（否则 recall 恒 0 还浑然不觉）",
      _try_load(_bad(relevant_chunk_ids=[])) == "ValueError")
check("should_answer=false 却标了相关切片 → ValueError",
      _try_load(_bad(should_answer=False)) == "ValueError")
check("relevant_chunk_ids 写成字符串 → ValueError",
      _try_load(_bad(relevant_chunk_ids="1:0")) == "ValueError")
check("少字段 → ValueError",
      _try_load(json.dumps({"id": "x1", "question": "?"}, ensure_ascii=False) + "\n")
      == "ValueError")
try:
    ev.load_golden(ROOT / "data" / "eval" / "__nope__.jsonl")
    _missing_raised = False
except FileNotFoundError:
    _missing_raised = True
check("文件不存在 → FileNotFoundError（给的是明确错误，不是空结果）", _missing_raised)
check("评测集指纹对内容敏感（改一个字节就变，用于拦住两套集子比数字）",
      ev.golden_fingerprint(GOLDEN) != ev.golden_fingerprint(SCRIPT),
      "指纹函数退化成了常量")

with tempfile.TemporaryDirectory() as td:
    a = Path(td) / "a.jsonl"
    b = Path(td) / "b.jsonl"
    a.write_text(GOOD + "\n", encoding="utf-8")
    b.write_text(GOOD + "\n", encoding="utf-8")
    check("同内容 → 同指纹（可复现）",
          ev.golden_fingerprint(a) == ev.golden_fingerprint(b))
    b.write_text(GOOD + "\n" + GOOD.replace("x1", "x2") + "\n", encoding="utf-8")
    check("内容变了 → 指纹变", ev.golden_fingerprint(a) != ev.golden_fingerprint(b))


# --------------------------------------------------------------------------- #
# 第 3 组：指标定义（用假检索器驱动 evaluate_case）
# --------------------------------------------------------------------------- #
section("第 3 组：recall / MRR / 拒答 的定义（假检索器，不联网）")


class FakeRetriever:
    """按预设顺序吐 Document 的替身。只提供 evaluate_case 用到的 retrieve()。"""

    def __init__(self, order: list[str]) -> None:
        # 按 k 逐档返回前缀：模拟「k 越大召回越多」的真实形态
        self.order = order

    def retrieve(self, query: str, top_k: int | None = None, **_kw):
        from langchain_core.documents import Document
        k = int(top_k or 5)
        out = []
        for chunk_id in self.order[:k]:
            doc_id, chunk_index = parse_chunk_id(chunk_id)
            out.append(Document(
                page_content=chunk_id,
                metadata={"chunk_id": chunk_id, "doc_id": doc_id, "chunk_index": chunk_index},
            ))
        return out


def run_case(order: list[str], relevant: list[str], should_answer: bool,
             k_values=(5, 10, 20)) -> dict:
    case = {
        "id": "t", "category": "term", "question": "q",
        "relevant_chunk_ids": relevant, "should_answer": should_answer,
    }
    return ev.evaluate_case(FakeRetriever(order), case, k_values)


# —— recall 是比例型，不是 hit 型 ——
r = run_case(["1:0", "9:9"], relevant=["1:0", "2:0"], should_answer=True)
check("两条相关只中一条 → recall@5 = 0.5（不是 hit 型的 1.0）",
      r["recall"]["5"] == 0.5, f"recall@5={r['recall']['5']}")
check("同一情形下 hit@5 = 1（两个指标各自独立，主指标是 recall）",
      r["hit"]["5"] == 1, f"hit@5={r['hit']['5']}")

r = run_case(["1:0", "2:0"], relevant=["1:0", "2:0"], should_answer=True)
check("全中 → recall@5 = 1.0", r["recall"]["5"] == 1.0, f"recall@5={r['recall']['5']}")

# —— 窗口越宽，recall 单调不降 ——
r = run_case(["9:9"] * 3 + ["1:0"] * 5 + ["2:0"], relevant=["1:0", "2:0"], should_answer=True)
check("recall 随 k 单调不降（recall@5 ≤ recall@10 ≤ recall@20）",
      r["recall"]["5"] <= r["recall"]["10"] <= r["recall"]["20"],
      f"{r['recall']}")

# —— MRR 看的是「第一条相关排第几」 ——
r = run_case(["9:9", "1:0"], relevant=["1:0"], should_answer=True)
check("第一条相关在第 2 位 → mrr = 0.5", r["mrr"] == 0.5, f"mrr={r['mrr']}")
r = run_case(["1:0"], relevant=["1:0"], should_answer=True)
check("第一条相关在第 1 位 → mrr = 1.0", r["mrr"] == 1.0, f"mrr={r['mrr']}")
r = run_case(["9:9"] * 10 + ["1:0"], relevant=["1:0"], should_answer=True)
check("相关切片掉出 mrr 窗口 → mrr = 0（不是 None、不是 1/rank 越界）",
      r["mrr"] == 0.0, f"mrr={r['mrr']}")
check("mrr 窗口 = 10（键名写进结果，换档会被看见）",
      r["mrr_k"] == 10, f"mrr_k={r['mrr_k']}")

# —— 拒答判定 ——
r = run_case([], relevant=[], should_answer=False)
check("库外题 + 零召回 → 判为拒答且正确", r["abstained"] and r["abstain_correct"])
r = run_case(["1:0"], relevant=[], should_answer=False)
check("库外题 + 有召回 → 没拒答，判定错误",
      (not r["abstained"]) and (not r["abstain_correct"]))
r = run_case([], relevant=["1:0"], should_answer=True)
check("库内题 + 零召回 → 误拒（abstain_correct=False）",
      r["abstained"] and (not r["abstain_correct"]))
r = run_case(["1:0"], relevant=["1:0"], should_answer=True)
check("库内题 + 有召回 → 判定正确", (not r["abstained"]) and r["abstain_correct"])

# —— 库外题的 recall/mrr 必须是 None，不能混进分母 ——
r = run_case(["1:0"], relevant=[], should_answer=False)
check("库外题的 recall / mrr 为 None（不进库内分母）",
      r["recall"]["5"] is None and r["hit"]["5"] is None and r["mrr"] is None,
      f"recall={r['recall']['5']} mrr={r['mrr']}")

# —— 单题结果要能复盘：留着命中的 chunk_id 清单 ——
r = run_case(["1:0", "2:0"], relevant=["1:0"], should_answer=True, k_values=(5, 10))
check("结果里留着每一档实际召回的 chunk_id 清单（事后复盘的唯一依据）",
      set(r["retrieved"].keys()) == {"5", "10"} and r["retrieved"]["5"] == ["1:0", "2:0"],
      f"retrieved={r['retrieved']}")

# --------------------------------------------------------------------------- #
# 第 4 组：aggregate 汇总口径
# --------------------------------------------------------------------------- #
section("第 4 组：aggregate 的汇总口径")

per_question = [
    # 库内：一条全中、一条部分命中
    {**run_case(["1:0", "2:0"], ["1:0", "2:0"], True), "id": "a", "category": "term"},
    {**run_case(["1:0"], ["1:0", "2:0"], True), "id": "b", "category": "term"},
    # 库外：一条正确拒答、一条没拒住
    {**run_case([], [], False), "id": "c", "category": "out_of_scope"},
    {**run_case(["1:0"], [], False), "id": "d", "category": "out_of_scope"},
]
metrics = ev.aggregate(per_question, (5, 10, 20), 10)
check("recall@5 = (1.0 + 0.5) / 2 = 0.75", metrics["recall@5"] == 0.75,
      f"recall@5={metrics['recall@5']}")
check("hit@5 = 1.0（两条都至少中一条）", metrics["hit@5"] == 1.0,
      f"hit@5={metrics['hit@5']}")
check("拒答正确率 = 库外 1/2 = 0.5（分母只有库外题）",
      metrics["abstain"]["accuracy"] == 0.5,
      f"accuracy={metrics['abstain']['accuracy']} detail={metrics['abstain']}")
check("拒答明细里库外 1/2、库内误拒 0（两个方向分开记）",
      metrics["abstain"]["out_of_scope_correct"] == 1
      and metrics["abstain"]["out_of_scope_total"] == 2
      and metrics["abstain"]["in_scope_false_abstain"] == 0,
      f"{metrics['abstain']}")
check("分类下钻按 category 分组且条数对得上",
      metrics["by_category"]["term"]["count"] == 2
      and metrics["by_category"]["out_of_scope"]["count"] == 2,
      f"{metrics['by_category']}")
check("库外分类的 recall 为 None（不是 0 —— 0 会假装成一个很差的成绩）",
      metrics["by_category"]["out_of_scope"]["recall@20"] is None,
      f"{metrics['by_category']['out_of_scope']}")

# --------------------------------------------------------------------------- #
# 第 5 组：实验协议（源码级：每个 k 各检索一次）
# --------------------------------------------------------------------------- #
section("第 5 组：实验协议 —— 每个 k 各跑一次，且脚本只读")

check("k=5/10/20 是本脚本的默认档位",
      ev.DEFAULT_K_VALUES == (5, 10, 20), f"{ev.DEFAULT_K_VALUES}")

body = SRC[SRC.index("def evaluate_case("):SRC.index("def _mean(")]
check("evaluate_case 里对每个 k 各调一次 retrieve（不是一次取最大 k 截前缀）",
      "for k in k_values" in body
      and re.search(r"retriever\.retrieve\(\s*\w+,?\s*top_k=k", body) is not None,
      "没找到 `for k in k_values` + `retrieve(..., top_k=k)` 的组合")
check("每个 k 的召回清单分别存下来（res_per_k 形态），不是共享一份",
      "retrieved_by_k[str(k)] = _chunk_ids_of(docs)" in body,
      "检索结果只存了一份 → 各档共用即等于截前缀")

readonly_violations = [
    token for token in ("clear_all", "delete_by_doc_id", "delete_by_source",
                        "purge_orphan", "add_documents", "parse_and_index",
                        "session.execute", "commit()")
    if token in SRC
]
check("评测脚本只读：不出现任何写库 / 清库调用",
      not readonly_violations, f"出现：{readonly_violations}")

check("拒答判定用最小档（线上 top_k）的检索结果，不是最大档",
      "production_k = min(k_values)" in body
      and "abstained = not retrieved_by_k[str(production_k)]" in body,
      "拒答口径漂移了 —— 用最大档判拒答会让「拒答正确率」虚高")

check("复用 core.retriever._candidate_brief 取 chunk_id（不写第三份契约实现）",
      "from core.retriever import RAGRetriever, _candidate_brief" in SRC,
      "自己又写了一遍 chunk_id 解析 → 与运行时迟早漂移")

check("结果 meta 里带上评测集指纹（拦住「两套集子比数字」）",
      "golden_sha1" in SRC and "golden_sha1\": golden_fingerprint" in SRC)

# --------------------------------------------------------------------------- #
# 第 6 组：落盘 / 对比 / Makefile
# --------------------------------------------------------------------------- #
section("第 6 组：结果落盘、--compare 与 Makefile 目标")

def _mk_result(tag: str, sha: str) -> dict:
    """造一份形状与真实落盘一致的结果（meta 字段必须齐，否则对比函数会 KeyError）。"""
    return {
        "meta": {
            "tag": tag, "golden_sha1": sha,
            "k_values": [5, 10, 20], "mrr_k": 10,
            "questions": len(per_question), "in_scope": 2, "out_of_scope": 2,
            "retrieval": {"top_k": 5, "hybrid": False},
        },
        "metrics": metrics,
        "per_question": per_question,
    }


with tempfile.TemporaryDirectory() as td:
    out_dir = Path(td) / "results"
    payload = _mk_result("baseline", "aaaabbbbcccc")
    p1 = ev.save_result(payload, out_dir, "baseline")
    check("落盘路径形如 <timestamp>-<tag>.json 且目录自动创建",
          p1.is_file() and p1.parent == out_dir
          and re.fullmatch(r"\d{8}-\d{6}-baseline\.json", p1.name) is not None,
          f"实际 {p1}")
    check("落盘内容是合法 JSON 且含 metrics / per_question",
          json.loads(p1.read_text(encoding="utf-8"))["metrics"]["recall@5"] == 0.75)

    p2 = ev.save_result(payload, out_dir, "hybrid")
    check("latest_result 按 tag 取，不同 tag 互不干扰",
          ev.latest_result(out_dir, "baseline") == p1
          and ev.latest_result(out_dir, "hybrid") == p2)
    check("resolve_compare 既能接文件路径也能接 tag",
          ev.resolve_compare(str(p1), out_dir) == p1
          and ev.resolve_compare("hybrid", out_dir) == p2)
    try:
        ev.resolve_compare("__不存在的tag__", out_dir)
        _raised = False
    except FileNotFoundError:
        _raised = True
    check("resolve_compare 遇到不存在的 tag 直接报错（不静默比了个寂寞）", _raised)

    # 对比打印：指纹不同要显式警告，不能假装可比
    other = _mk_result("hybrid", "deadbeef0000")
    buf = io.StringIO()
    with redirect_stdout(buf):
        ev.print_compare(json.loads(p1.read_text(encoding="utf-8")), other, p1)
    text = buf.getvalue()
    check("两份结果指纹不同 → 打印不可比警告",
          "不可比" in text, text[:200])
    check("对比表里同时列出 P3-4 的三条达标判据",
          all(s in text for s in ("recall@20", "mrr@10", "拒答正确率不降")), text[:400])

MAKE = MAKEFILE.read_text(encoding="utf-8")
check("Makefile 有 eval-retrieval 目标", "eval-retrieval:" in MAKE)
check("eval-retrieval 用 $(PY) 跑脚本（跟着 venv 走，不用系统 python）",
      re.search(r"eval-retrieval:\s*\n\t@\$\(PY\) scripts/eval_retrieval\.py", MAKE) is not None,
      "目标写法变了")
check("eval-retrieval 进了 .PHONY（否则同名目录会让它静默失效）",
      "eval-retrieval" in MAKE.split(".PHONY")[1].split("\n\n")[0])
check("make help 里能查到 eval-retrieval", "eval-retrieval" in MAKE)

check("结果目录约定写进了脚本常量（data/eval/results）",
      ev.DEFAULT_RESULTS_DIR == ROOT / "data" / "eval" / "results",
      f"{ev.DEFAULT_RESULTS_DIR}")

# --------------------------------------------------------------------------- #
# 第 7 组：反向验证（--reverse 才跑）
# --------------------------------------------------------------------------- #
if REVERSE:
    section("第 7 组：反向验证（拆掉护栏后断言必须转红）")

    # 反向 1：recall 退化成 hit 型 → 第 3 组「部分命中 = 0.5」必须转红
    _orig_evaluate = ev.evaluate_case

    def _hit_style(retriever, case, k_values):
        row = _orig_evaluate(retriever, case, k_values)
        for k in k_values:
            if row["recall"][str(k)] is not None:
                row["recall"][str(k)] = float(row["hit"][str(k)])
        return row

    ev.evaluate_case = _hit_style
    _r = ev.evaluate_case(FakeRetriever(["1:0"]), {
        "id": "t", "category": "term", "question": "q",
        "relevant_chunk_ids": ["1:0", "2:0"], "should_answer": True,
    }, (5, 10, 20))
    ev.evaluate_case = _orig_evaluate
    check("反向 1：recall 改成 hit 型后「部分命中=0.5」会变成 1.0 → 第 3 组那条转红",
          _r["recall"]["5"] == 1.0,
          f"退化后 recall@5={_r['recall']['5']} —— 若仍是 0.5 说明判据无效")

    # 反向 2：拒答正确率分母改成全部题 → 第 4 组「=0.5」必须转红
    _orig_aggregate = ev.aggregate

    def _mixed_abstain(per_question, k_values, mrr_k):
        m = _orig_aggregate(per_question, k_values, mrr_k)
        total = len(per_question)
        correct = sum(1 for q in per_question if q["abstain_correct"])
        m["abstain"]["accuracy"] = round(correct / total, 4)
        return m

    ev.aggregate = _mixed_abstain
    _m = ev.aggregate(per_question, (5, 10, 20), 10)
    ev.aggregate = _orig_aggregate
    check("反向 2：拒答分母改成全部题后变成 0.75 ≠ 0.5 → 第 4 组那条转红",
          _m["abstain"]["accuracy"] == 0.75,
          f"退化后 accuracy={_m['abstain']['accuracy']}")

    # 反向 3：改成「一次取最大 k 截前缀」→ 第 5 组的协议断言必须转红
    _prefix_src = SRC.replace(
        "        docs = retriever.retrieve(query, top_k=k, vector_query=vector_query)\n"
        "        retrieved_by_k[str(k)] = _chunk_ids_of(docs)",
        "        docs = retriever.retrieve(query, top_k=max(k_values), vector_query=vector_query)\n"
        "        retrieved_by_k[str(k)] = _chunk_ids_of(docs)[:k]",
    )
    check("反向 3：把协议改成截前缀后，源码里 `retrieve(..., top_k=k)` 消失 → 第 5 组转红",
          _prefix_src != SRC
          and re.search(r"retriever\.retrieve\(\s*\w+,?\s*top_k=k",
                        _prefix_src[_prefix_src.index("def evaluate_case("):
                                    _prefix_src.index("def _mean(")]) is None,
          "替换没生效，反向验证等于没做")

    # 反向 4：脚本里出现清库调用 → 「只读」断言转红
    check("反向 4：往脚本里塞一行 clear_all() 后，只读断言会抓到它",
          "clear_all" in (SRC + "\n    vs.clear_all()\n")
          and "clear_all" not in SRC,
          "只读扫描的判据是恒真的 —— 反向验证不成立")
else:
    print("\n== 第 7 组：反向验证（--reverse 才跑）==")
    print("  （跳过）")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 66)
print(f"  P3-3 检索评测回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
