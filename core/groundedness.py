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
    "- 数字、档位、期限、比例、金额必须与资料原文逐字/逐档一致："
    "资料写「每增 1 年增 1 天」，答案写成「5-10 年 10 天」即 unsupported；\n"
    "- 【补充候选资料】是被阈值过滤、未进入回答的检索结果，但同样是知识库原文；\n"
    "  论断能在其中找到原文依据也算有依据；\n"
    "- 「资料只按类别汇总 / 没有按人拆分的明细」「资料只到城市级别」「资料未涉及审批时限」"
    "这类**关于资料范围或不确定性的说明**属于如实告知，不是实质论断，不要计入 unsupported；"
    "只有答案给出了资料中没有的数字、档位、期限或结论才算未支撑；\n"
    "- 答案末尾的「依据：…」引用行不算论断，忽略它；\n"
    "- unsupported 只列**候选答案里**的实质性论断；"
    "用户问题本身、或答案里复述问题的疑问句（如「XX 是多少」「XX 有哪些」）不是论断，不得列入；\n"
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
        "⚠️ **依据校验：以下论断未能在检索资料中找到依据，请勿采信。**",
    ]
    if unsupported:
        lines.append("未能找到依据的论断：" + "；".join(unsupported) + "。")
    lines.append(
        # 这里**不能**再写「根据现有制度未查到相关规定」：那句话是「整条资料无关」的
        # 专用话术（见 _QA_SYSTEM_PROMPT 规则 2），而更正块的场景恰恰相反——
        # 资料里**有**内容，只是答案有一句没站住。2026-11-05 生产实测：
        # 同一条回答里先是规则 2 的话术、中间是正确数据、末尾又被这里重复一遍，
        # 用户看到的是「查不到 / 有数据 / 请勿采信 / 查不到」四段自相矛盾。
        "以上论断请以原始文件为准；如需确认，请联系资料归口部门。"
    )
    return (answer or "").rstrip() + "\n\n" + "\n".join(lines)


def check_groundedness(
    *,
    question: str,
    context: str,
    answer: str,
    candidate_context: str | None = None,
) -> dict[str, Any]:
    """
    依据比对。返回 `{"grounded": bool | None, "unsupported": list[str], "note": str}`。

    :param candidate_context: 被阈值过滤、未进入回答的候选原文（top-N）。
        它们同样是知识库原文，作为补充依据交给 judge —— 避免「本轮只召回一条
        表头切片、细节在另一条被过滤的切片里」时把正确答案误判成无依据。

    grounded=None 表示**未判定**（调用失败 / 输出无法解析）——fail-open，
    调用方照常落库与展示，只是没有通过/未通过的标记。
    """
    from core.rag_chain import get_llm_client  # 局部导入避免环（rag_chain 导入本模块）

    result: dict[str, Any] = {"grounded": None, "unsupported": [], "note": ""}
    # 可观测：judge 到底看到了多少段「被阈值过滤的候选原文」。
    # 这是「阈值 0.1 以下的答案细节有没有进 judge」的唯一实证，排查误报时先看这条。
    result["candidate_count"] = candidate_context.count("【候选") if candidate_context else 0
    logger.info(
        "依据校验输入 | 候选资料=%d 段 | 检索上下文=%d 字 | 答案=%d 字",
        result["candidate_count"], len(context or ""), len(answer or ""),
    )
    try:
        llm = get_llm_client().get_llm()
        prompt = (
            f"【用户问题】\n{question}\n\n"
            f"【检索资料】\n{context}\n\n"
        )
        if candidate_context:
            prompt += f"【补充候选资料（未进入回答，仅供核对依据）】\n{candidate_context}\n\n"
        prompt += f"【候选答案】\n{answer}"
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
