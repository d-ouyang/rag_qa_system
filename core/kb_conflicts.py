"""知识库口径冲突台账：让「这条回答不准，是因为两份文档互相矛盾」能被看见。

--------------------------------------------------------------------------
为什么需要
--------------------------------------------------------------------------
2026-11-24 出跨文档考题时撞到：出差报销的提交时限，
《差旅管理制度》(GQ-AD-2026-006) 第四条写「返回后 15 个工作日」，
《费用报销管理制度》(GQ-FI-2026-002) 第四条写「费用发生后 30 个工作日」。

模型答 15 天，判分器给了 5 分——因为答案确实忠于它检索到的那一片。
**这不是模型错，是知识库错**，而当时的评测体系没有任何一层能发现：

  · 检索层（recall/hit/mrr）只问「对的片段进没进前 5」；
  · 生成层（answer_guard / judge）只问「有没有编、够不够用」；
  · 没有任何一层问「这几份资料互相打不打架」。

结果就是：线上给用户一个数字，谁都以为它是对的。

--------------------------------------------------------------------------
为什么不做全自动检测
--------------------------------------------------------------------------
「自动找冲突」两条路都不通：只比对数字会漏掉文义冲突（「原则上不予报销」
vs 「可以报销」，没有数字）；交给模型自由比对则会误报成灾——任意两份制度
在措辞上都可能有差异。误报一多，这个提示就会像依据校验那样被用户学会无视，
比不做还糟。

所以定位是：**人确认，机器提示**。
  · 台账（data/kb_conflicts.json）是唯一事实源，由人登记与关闭；
  · 机器只做一件事：本轮检索命中台账里的任一文档，就在答案末尾说一句。

只命中**一侧**也要提示——cf-001 的实况就是模型只捞到差旅制度那一片，
要求两侧都出现才提示的话，正好把最该提示的情况漏掉。

--------------------------------------------------------------------------
怎么新增一条
--------------------------------------------------------------------------
1. 跑评测或人工发现两份文档对同一件事说法不同；
2. 在 data/kb_conflicts.json 的 conflicts 里加一条，status=open；
3. 业务方确认口径后修改文档，把 status 改成 resolved（**保留记录不删**，
   下次有人问「为什么以前答 15 天」还能查到原因）；
4. 重新跑 make eval-judge，确认答案不再只给一个数字。

--------------------------------------------------------------------------
姿态
--------------------------------------------------------------------------
· fail-open：台账读不到 / 格式坏 / 没有文件 → 一律当没有冲突，不挡回答。
  冲突提示是「锦上添的警告」，不是回答的前置条件。
· 按文件 mtime 缓存：改完台账不用重启后端。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFLICTS_FILE = Path(__file__).resolve().parents[1] / "data" / "kb_conflicts.json"

#: (mtime, 解析结果)。台账是极小文件，但每轮问答都会读——没有缓存就是
#: 每轮一次 stat + 一次 JSON 解析，纯属浪费。
_cache: tuple[float, list[dict[str, Any]]] | None = None

_NOTICE_HEAD = "⚠️ **知识库口径冲突**"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def load_conflicts(path: Path | None = None) -> list[dict[str, Any]]:
    """读台账里 status=open 的冲突。任何异常都返回空列表（fail-open）。"""
    global _cache
    target = path or CONFLICTS_FILE
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return []

    if _cache and _cache[0] == mtime and path is None:
        return _cache[1]

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        items = [
            c for c in (data.get("conflicts") or [])
            if isinstance(c, dict) and str(c.get("status", "")).lower() == "open"
        ]
    except (OSError, ValueError):
        logger.warning("冲突台账读取失败（按无冲突处理）| %s", target, exc_info=True)
        return []

    if path is None:
        _cache = (mtime, items)
    return items


def match_conflicts(
    sources: list[dict[str, Any]],
    conflicts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """本轮检索到的文档命中哪几条冲突。

    只命中一侧也算命中（见模块注释：cf-001 的实况就是只捞到一侧）。
    匹配用文件名（完全相等或 basename 相等），不做模糊匹配——宁可漏，
    也不要因为文件名相似而给不相干的回答挂警告。
    """
    items = conflicts if conflicts is not None else load_conflicts()
    if not items or not sources:
        return []

    hit_files: set[str] = set()
    for s in sources:
        name = str(s.get("file_name") or "").strip()
        if name:
            hit_files.add(name)
            hit_files.add(_basename(name))

    matched: list[dict[str, Any]] = []
    for cf in items:
        for side in cf.get("sides") or []:
            f = str(side.get("file") or "").strip()
            if f and (f in hit_files or _basename(f) in hit_files):
                matched.append(cf)
                break
    return matched


def format_conflict_notice(hits: list[dict[str, Any]]) -> str:
    """把命中的冲突渲染成一段可读提示（追加在答案末尾）。"""
    if not hits:
        return ""
    lines = ["", "---", ""]
    for cf in hits:
        topic = str(cf.get("topic") or "相关规定")
        sides = cf.get("sides") or []
        parts = []
        for side in sides:
            ref = str(side.get("ref") or side.get("file") or "")
            value = str(side.get("value") or "")
            parts.append(f"《{_basename(str(side.get('file') or '')).removesuffix('.md')}》{ref}为「{value}」")
        lines.append(f"{_NOTICE_HEAD}：{topic}存在两种口径——" + "；".join(parts) + "。")
        if cf.get("action"):
            lines.append(f"已登记待确认（{cf.get('owner') or '归口部门'}），最终以归口部门口径为准。")
    return "\n".join(lines)


def append_conflict_notice(answer: str, sources: list[dict[str, Any]]) -> str:
    """答案 + 冲突提示。没有命中就原样返回（字节级不变）。"""
    hits = match_conflicts(sources)
    if not hits:
        return answer
    notice = format_conflict_notice(hits)
    return f"{answer.rstrip()}\n{notice}" if answer else notice.strip()


def all_conflicts(path: Path | None = None) -> list[dict[str, Any]]:
    """不管 status 全量返回（管理端/排查用；回答侧只用 open 的）。"""
    target = path or CONFLICTS_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in (data.get("conflicts") or []) if isinstance(c, dict)]
