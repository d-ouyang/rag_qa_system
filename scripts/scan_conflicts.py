"""知识库口径冲突粗筛：把「同一件事、两个数值」的段落对捞出来，交人工确认。

为什么需要它
    data/kb_conflicts.json 是人工台账，只能记「已经知道」的冲突。
    2026-11-24 那条 15/30 是靠出跨文档考题偶然撞到的——下次未必这么巧。
    这个脚本负责把候选**摊到桌面上**，人不看它就永远不知道。

它不做什么
    不做判断、不写台账、不进 CI 门禁。阈值刻意放松（宁可多报），
    因为它的产物是「请人看一眼」的清单，不是结论。误报的代价是人扫一眼，
    漏报的代价是线上给用户一个错数字。

判据（全部确定性，不调模型）
    1. 同一段落里同时出现「数字 + 计量单位」；
    2. 两条来自**不同文档**；
    3. 单位相同、数值不同；
    4. 去掉停用词后的实词重合 >= MIN_SHARED（默认 2）——
       靠这条把「年假 5 天」和「病假 3 天」这类不同主题排除掉。

用法
    .venv/bin/python scripts/scan_conflicts.py
    .venv/bin/python scripts/scan_conflicts.py --unit 工作日 --top 20
    .venv/bin/python scripts/scan_conflicts.py --out docs/冲突候选.md
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = ROOT / "upload"

#: 条款里真正有约束力的计量单位。顺序有意义：先匹配「个工作日」再匹配「天」，
#: 否则「15 个工作日」会被切成「15 个」+「工作日」两截。
UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(个工作日|工作日|个自然日|自然日|个月|小时|分钟|天|元|%|年)"
)

#: 只用来去掉虚词，不做语义判断。列长一点没关系——多删几个字不会漏冲突，
#: 少删会让「须/在/的」这种高频词把所有句子连成一片。
STOPWORDS = {
    "公司", "员工", "应当", "不得", "可以", "须于", "按照", "依据", "相关", "有关",
    "以下", "上述", "本条", "本制度", "规定", "执行", "进行", "提交", "办理", "情况",
    "工作", "时间", "标准", "要求", "需要", "如果", "或者", "以及", "并且", "其他",
}


def _doc_code(path: Path) -> str:
    """取文档编号（GQ-HR-2026-005 这类）。取不到返回空串。

    为什么用它做过滤而不是文件名：upload/ 下的文件名是 uuid，看不出是什么文档；
    正文第一屏的「文件编号」是可靠且稳定的分类信号。
    """
    try:
        head = path.read_text(encoding="utf-8")[:500]
    except OSError:
        return ""
    m = re.search(r"GQ-[A-Z]{2}-\d{4}-\d{3}", head)
    return m.group(0) if m else ""


def _clauses(path: Path) -> list[dict]:
    """按行切条款。制度文本一条一行，按行切比按句号切更贴合「条」的边界。"""
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or len(line) < 8:
            continue
        hits = UNIT_RE.findall(line)
        if not hits:
            continue
        for value, unit in hits:
            out.append({
                "file": path.name,
                "text": line,
                "value": float(value),
                "unit": unit,
            })
    return out


def _content_words(text: str) -> set[str]:
    """切出实词。jieba 不可用时退回「连续汉字 2 字以上」的粗略切分。"""
    try:
        import jieba  # 项目已依赖（bm25_index 在用）

        tokens = [t.strip() for t in jieba.cut(text)]
    except Exception:  # noqa: BLE001 - 粗筛脚本不该因分词库缺失而不可用
        tokens = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
    return {
        t for t in tokens
        if len(t) >= 2 and t not in STOPWORDS and re.search(r"[\u4e00-\u9fa5]", t)
    }


def scan(*, unit: str | None = None, min_shared: int = 2, skip_prefix: tuple[str, ...] = ("GQ-KB-",)) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(UPLOAD.glob("*.md")):
        # GQ-KB-* 是公司介绍 / 发展历程 / 上市信息这类叙述性文档。
        # 它们天然会反复提到同一个年份和百分比，却是「同一事实的不同表述」，
        # 不是「同一规定给了两个口径」。放进来只会把制度冲突埋进噪声里。
        if any(_doc_code(path).startswith(p) for p in skip_prefix):
            continue
        rows.extend(_clauses(path))

    if unit:
        rows = [r for r in rows if r["unit"] == unit]

    for r in rows:
        r["words"] = _content_words(r["text"])

    # 按单位分桶：不同单位（天 vs 元）本就不可能冲突，先切开。
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[r["unit"]].append(r)

    candidates: list[dict] = []
    for unit_key, items in buckets.items():
        for a, b in combinations(items, 2):
            if a["file"] == b["file"]:
                continue
            if a["value"] == b["value"]:
                continue
            # 同一行里抽出的两个数字（「一线 500 / 其他 350」）不是冲突，
            # 它们是同一个规定的两个档位。不排掉会让这类条目刷屏。
            if a["text"] == b["text"]:
                continue
            shared = a["words"] & b["words"]
            if len(shared) < min_shared:
                continue
            candidates.append({
                "unit": unit_key,
                "shared": sorted(shared),
                "a": a,
                "b": b,
                "score": len(shared),
            })

    candidates.sort(key=lambda c: -c["score"])

    # 同一对段落可能在多个单位上都命中（「15 个工作日」同时抽出「个工作日」和「天」），
    # 按（文件A, 行A, 文件B, 行B）去重，只留共享词最多的那条。
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for c in candidates:
        key = (c["a"]["file"], c["a"]["text"], c["b"]["file"], c["b"]["text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def render(candidates: list[dict], *, top: int) -> str:
    if not candidates:
        return "未发现候选冲突（判据见脚本注释；漏报时把 --min-shared 调低再跑一次）。"
    lines = [
        "# 知识库口径冲突候选",
        "",
        f"> 共 {len(candidates)} 对候选，下列前 {min(top, len(candidates))} 条。",
        "> **这是候选，不是结论**——脚本只保证「不同文档、同一单位、数值不同、实词重合」，",
        "> 语义冲突必须人看。确认后登记到 `data/kb_conflicts.json`。",
        "",
        "| # | 单位 | 共享词 | 文档 A | 文档 B |",
        "|---|---|---|---|---|",
    ]
    for i, c in enumerate(candidates[:top], start=1):
        a, b = c["a"], c["b"]
        lines.append(
            f"| {i} | {c['unit']} | {', '.join(c['shared'])} "
            f"| {a['file']}：{a['text'][:46]} "
            f"| {b['file']}：{b['text'][:46]} |"
        )
    lines += ["", "---", "", "## 明细", ""]
    for i, c in enumerate(candidates[:top], start=1):
        a, b = c["a"], c["b"]
        lines += [
            f"### {i}. {c['unit']}（共享词：{', '.join(c['shared'])}）",
            "",
            f"- **A** `{a['file']}` {a['value']:g}{a['unit']}：{a['text']}",
            f"- **B** `{b['file']}` {b['value']:g}{b['unit']}：{b['text']}",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库口径冲突粗筛（候选清单，交人工确认）")
    parser.add_argument("--unit", default=None, help="只看某个单位，如 工作日")
    parser.add_argument("--min-shared", type=int, default=2, help="共享实词下限，调低更宽松")
    parser.add_argument("--top", type=int, default=30, help="清单条数")
    parser.add_argument("--out", type=Path, default=None, help="写入文件（默认只打印）")
    args = parser.parse_args()

    candidates = scan(unit=args.unit, min_shared=args.min_shared)
    text = render(candidates, top=args.top)
    if args.out:
        out = args.out if args.out.is_absolute() else ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {out}（{len(candidates)} 对候选）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
