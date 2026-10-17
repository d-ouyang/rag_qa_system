"""答案依据校验（Groundedness）—— v4.0.0 P4-6，q041 病假幻觉的根治方案。

--------------------------------------------------------------------------
它回答的问题
--------------------------------------------------------------------------
检索管线回答的是「哪个片段和问题最相关」，**从来不回答「片段里有没有答案」**。
q041 案例：考勤制度切片 top1=0.3232 通过阈值，但里面没有病假扣薪规定——
模型用常识编造了答案，引用面板却挂着文档出处。

本模块在**答案生成之后**做一次依据比对：「答案的关键论断能否在检索资料里
找到依据」。判定不通过 → 结果随轮落库（qa_trace.grounded / unsupported）
并在前端挂警示；**不撤回已流出的答案**（流式无法撤回，撒谎式召回比
误报更危险，一期只做标记，改写留给二期）。

--------------------------------------------------------------------------
姿态约定
--------------------------------------------------------------------------
· **fail-open**：judge 调用失败 / JSON 解析失败 → grounded=None（未判定），
  绝不挡回答——校验器坏了的症状是「没有校验标记」，不是「问不了」。
· **只校验 rag_qa 轮**：缓存命中轮没有新答案、闲聊轮没有资料、
  零召回轮本身就是拒答——三种终端状态都不需要校验。
· 低置信带：扫档实测库内最低 0.2569 与库外最高 0.3232 犬牙交错，
  (0.25, 0.35) 落在带内的 top1 挂「低置信」徽标（只提示，不改行为）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

#: 低置信带（扫档报告 2026-10-16：库内最低 0.2569 / 库外最高 0.3232）。
#: top1_before_filter 落在 [min, max] 内 → 答案挂「低置信」徽标。
LOW_CONFIDENCE_BAND: tuple[float, float] = (0.25, 0.35)

_GROUNDED_SYSTEM_PROMPT = (
    "你是答案依据审核员。给定【用户问题】【检索资料】【候选答案】，"
    "判断候选答案中的实质性论断是否都能在检索资料里找到原文依据。\n"
    "输出要求：只输出一个 JSON 对象，不要任何解释或代码块标记，格式：\n"
    '{"grounded": true 或 false, "unsupported": ["未被资料支持的论断", ...]}\n'
    "判定规则：\n"
    "- grounded=true 当且仅当答案的所有实质性论断都有资料依据；\n"
    "- 答案末尾的「依据：…」引用行不算论断，忽略它；\n"
    "- 拿不准的论断按 unsupported 处理（宁可误报不可漏报）；\n"
    "- unsupported 为空列表时 grounded 必须为 true，反之亦然。"
)


def is_low_confidence(top1_before_filter: Any) -> bool:
    """top1 重排分是否落在低置信带。None（未重排/未跑检索）恒为 False。"""
    if top1_before_filter is None:
        return False
    lo, hi = LOW_CONFIDENCE_BAND
    return lo <= float(top1_before_filter) <= hi


import re  # noqa: E402

#: 模型在「依据：…」行里自认未查到 —— 强信号：它自己都承认没找到依据，
#: 却可能已经在上面编完了（q041 实测复现：正文编造扣薪规则 + 依据行写「未查到」）。
_ADMITS_NO_BASIS_RE = re.compile(r"依据[:：]\s*未查到")


def answer_admits_no_basis(answer: str) -> bool:
    """答案的依据行是否自认「未查到」。命中即视为未通过，无论 judge 怎么判。"""
    return bool(_ADMITS_NO_BASIS_RE.search(answer or ""))


def append_groundedness_correction(answer: str, grounded: dict[str, Any] | None) -> str:
    """
    依据校验未通过时，在答案尾部追加「未查到依据」更正块（P4-6b）。

    为什么是**追加**而不是替换：答案已经流式输出给用户，撤回做不到；
    能做的最诚实的事是把「这段回答没有依据、请勿采信」钉在正文里，
    连同未支撑论断清单和补充资料建议一起落库——刷新后看到的和当时看到的一致。

    grounded 非 False（通过 / 未判定）时原样返回，一个字不加。
    """
    if not grounded or grounded.get("grounded") is not False:
        return answer
    unsupported = [str(x) for x in (grounded.get("unsupported") or []) if str(x).strip()]
    lines = [
        "---",
        "⚠️ **依据校验：以上回答未能在检索资料中找到依据，请勿采信。**",
    ]
    if unsupported:
        lines.append("未能找到依据的论断：" + "；".join(unsupported) + "。")
    lines.append(
        "根据现有制度未查到相关规定，请补充对应制度文档，或联系人力资源部门确认。"
    )
    return (answer or "").rstrip() + "\n\n" + "\n".join(lines)


def check_groundedness(
    *, question: str, context: str, answer: str
) -> dict[str, Any]:
    """
    依据比对。返回 `{"grounded": bool | None, "unsupported": list[str], "note": str}`。

    grounded=None 表示**未判定**（调用失败 / 输出无法解析）——fail-open，
    调用方照常落库与展示，只是没有通过/未通过的标记。
    """
    from core.rag_chain import get_llm_client  # 局部导入避免环（rag_chain 导入本模块）

    result: dict[str, Any] = {"grounded": None, "unsupported": [], "note": ""}
    try:
        llm = get_llm_client().get_llm()
        prompt = (
            f"【用户问题】\n{question}\n\n"
            f"【检索资料】\n{context}\n\n"
            f"【候选答案】\n{answer}"
        )
        message = llm.invoke(_GROUNDED_SYSTEM_PROMPT + "\n\n" + prompt)
        text = str(message.content).strip()
        # 剥掉模型偶尔裹的 ```json 围栏
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        data = json.loads(text)
        grounded = bool(data.get("grounded"))
        unsupported = [str(x) for x in (data.get("unsupported") or [])]
        if grounded and unsupported:
            # 口径自相矛盾（说有依据又列了未支撑论断）→ 按保守处理
            logger.warning("judge 输出口径矛盾（grounded=true 但有 unsupported），按未通过处理")
            grounded = False
        result["grounded"] = grounded
        result["unsupported"] = unsupported
    except Exception:
        # fail-open：judge 挂了不挡回答，留 None 让前端不展示校验标记
        logger.warning("依据校验失败（grounded=None，不影响回答）", exc_info=True)
        result["note"] = "校验调用失败"
    return result
