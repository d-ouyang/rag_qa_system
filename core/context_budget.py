"""上下文注入预算（v4.1.0-p1.2 / Phase ①2 回灌规范）——检索结果进 prompt 的统一收口。

背景：format_docs（正式资料）与 _format_candidate_docs（judge 候选补充）此前只有
条数限制、没有字符预算——单个长切片（整章 PDF 切片）或大候选池能把 prompt 撑爆，
token 成本与截断风险都不可控。

规范：
  · **保序**：资料块按检索排名顺序保留（排名靠前的信息密度最高）；
  · **整块丢弃**：预算耗尽后丢弃尾部整块（半截资料比没有资料更危险——模型会
    对着半句话编）；
  · **可观测**：返回 dropped 明细（丢弃块数/字符数），调用方写 trace；
  · **可配**：热开关 `context_inject_max_chars`（config_flags 注册表，
    0 = 不限制——预算器直通）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BudgetResult:
    text: str
    kept: int = 0
    dropped: int = 0
    dropped_chars: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return self.dropped > 0


def budget_blocks(blocks: list[str], max_chars: int, *, separator: str = "\n\n") -> BudgetResult:
    """把资料块按预算拼成 context 文本。

    max_chars <= 0 表示不限制（原样拼接）；预算按「块全文 + 分隔符」累计，
    第一块放不下的整块也被丢（不产生半截资料）。
    """
    if max_chars is None or max_chars <= 0:
        return BudgetResult(text=separator.join(blocks), kept=len(blocks))

    kept: list[str] = []
    used = 0
    dropped = 0
    dropped_chars = 0
    for block in blocks:
        cost = len(block) + (len(separator) if kept else 0)
        if used + cost > max_chars and kept:
            dropped += 1
            dropped_chars += len(block)
            continue
        if used + cost > max_chars and not kept:
            # 单块就超预算：截断这一块（保底给模型看到排名最高的资料），其余丢。
            # 截断长度给"…（超长截断）"标记留空间——总长必须仍 ≤ 预算。
            marker = "…（超长截断）"
            body_len = max(0, max_chars - used - len(marker))
            kept.append(block[:body_len] + marker)
            dropped += len(blocks) - 1
            dropped_chars = sum(len(b) for b in blocks[1:])
            used = max_chars
            break
        kept.append(block)
        used += cost

    if dropped:
        logger.info("上下文预算：%d/%d 块注入（丢弃 %d 块 / %d 字符，预算 %d）",
                    len(kept), len(blocks), dropped, dropped_chars, max_chars)
    return BudgetResult(
        text=separator.join(kept),
        kept=len(kept),
        dropped=dropped,
        dropped_chars=dropped_chars,
    )


# --------------------------------------------------------------------------- #
# 分段预算注册表（v4.1.0-p4.2 / Phase ④2，对齐 office A4 Policy）
# --------------------------------------------------------------------------- #
#: prompt 的各注入段与其预算来源。新增注入段必须在这里登记（可枚举），并在
#: 消费点接 budget_blocks ——「各段预算可配、可观测」的单一契约面。
#: memory_turns 是轮数维度（既有热开关 memory_max_turns），不走字符预算。
SEGMENTS: dict[str, dict[str, str]] = {
    "retrieval": {
        "flag": "context_inject_max_chars",
        "desc": "检索资料注入（p1.2）：超限丢弃尾部整块",
    },
    "summary": {
        "flag": "context_summary_max_chars",
        "desc": "会话滚动摘要注入（p4.2）：超长硬截断",
    },
    "memory_turns": {
        "flag": "memory_max_turns",
        "desc": "最近对话轮数（轮数维度，非字符预算）",
    },
}


def get_segment_limit(name: str) -> int:
    """读某段的当前生效预算（字符段返回旗标值；轮数段返回轮数）。0 = 不限制。"""
    from core.config_flags import flag_value

    if name not in SEGMENTS:
        raise KeyError(f"get_segment_limit：未登记的预算段 {name}")
    return int(flag_value(SEGMENTS[name]["flag"]) or 0)


def describe_budgets() -> list[dict[str, str]]:
    """各段预算全景（管理端/日志可观测：一段都不能漏登记）。"""
    return [{"segment": k, "flag": v["flag"], "desc": v["desc"]} for k, v in SEGMENTS.items()]
