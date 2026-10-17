"""
RAG 链模块 —— 用 LCEL 把「问题重写 → 检索 → 提示词 → 大模型 → 解析器」串成完整链路，
并集成多轮记忆（MemoryManager）与意图路由（IntentClassifier），是系统的业务核心。

--------------------------------------------------------------------------
整体数据流（一次问答的完整旅程）
--------------------------------------------------------------------------
    用户问题 + session_id
        │
        ├─① 意图识别（core/intent_router.py）
        │      chitchat → 闲聊链（不检索，直接 LLM 回答）
        │      rag_qa   ↓
        ├─② 取记忆（core/memory_manager.py）：该会话的历史消息
        │
        ├─③ 问题重写（condense question）：
        │      「它多少钱？」+ 历史 → 「XX 产品多少钱？」
        │      多轮始终改写；单轮默认跳过（省一次 LLM 调用），
        │      QUERY_REWRITE 打开时单轮也过一次改写（§6.4 D 检索增强）
        │
        ├─④ 检索（core/retriever.py）：向量召回 + CrossEncoder 精排
        │
        ├─⑤ 拼 prompt：system(含检索到的 context) + 历史 + 当前问题
        │
        ├─⑥ LLM 生成 → StrOutputParser 转纯文本
        │
        └─⑦ 写回记忆：本轮 (question, answer) 存入该 session

--------------------------------------------------------------------------
为什么用 LCEL 管道（| 运算符）而不是函数串调（面试常问）
--------------------------------------------------------------------------
· 每个环节都是 Runnable：invoke / stream / batch / astream 自动全部支持，
  写流式接口时不用重写链路（.stream() 天然逐 token 产出）；
· 管道可以整体打日志、加回调、接 LangSmith 追踪，函数串调做不到；
· 声明式组合让「数据怎么流」一目了然：context 从哪来、history 在哪进。

关键技巧：RunnablePassthrough.assign(...)
    作用是「在字典上追加一个 key，其余 key 原样透传」。
    靠它把 standalone_question / docs / context 逐步补进数据包，
    最后 RunnableParallel 一次性取出「答案 + 溯源文档」两个结果，
    避免「链跑完只剩字符串、引用来源丢失」的经典问题。
"""

from langchain_core.language_models.chat_models import BaseChatModel
import logging
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from operator import itemgetter
from typing import Any

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import (
    Runnable,
    RunnableBranch,
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)

from config.settings import settings
from core.config_flags import flag_value
from core.config_service import config_service
from core.intent_router import Intent, IntentResult, get_intent_classifier
from core.llm_client import LLMClient, get_llm_client
from core.memory_manager import MemoryManager, get_memory_manager
from core.qa_cache import is_standalone_question, looks_anaphoric, lookup_answer, store_answer
from core.groundedness import (
    answer_admits_no_basis,
    append_groundedness_correction,
    check_groundedness,
    is_low_confidence,
)
from core import qa_flow
from core import qa_trace_repo
from core.retriever import RAGRetriever, get_rag_retriever
from core.vector_store import build_chunk_id, chunk_id_of_metadata

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
# ① 问题重写提示词：让模型把「依赖上下文的追问」改写成「独立成立的问题」。
#    只改写、不回答 —— 输出会直接喂给检索器，多一个字都污染检索。
#    ⚠️ 实测部分模型（如 Qwen3-8B）会违反「不要回答」的指令直接作答，
#    所以除了这里加强措辞，还有 _sanitize_rewrite 做输出消毒兜底。
_CONTEXTUALIZE_Q_SYSTEM_PROMPT = (
    "你是一个问题改写专家，"
    "给定一段对话历史和一个用户的最新问题，"
    "这个问题可能引用了对话历史中的内容（如「它」「这个」「上面说的」）。\n"
    "你的唯一任务是【改写问题】，绝对禁止回答问题。\n"
    "输出要求：\n"
    "- 只输出改写后的问题本身，不要任何前言、解释或多余内容\n"
    "- 不要输出答案，不要输出「无法回答」之类的回应\n"
    "- 如果问题本身已经是独立的，原样返回该问题（一行）\n"
    "- 如果一个问题里包含多个可以**各自独立回答**的子问题"
    "（例如「怎么请事假？事假扣不扣钱？」），把每个子问题改写成独立的一行，"
    "每行一个、按原顺序，最多 3 行；不要合并成一个长句"
)

_contextualize_q_prompt = ChatPromptTemplate.from_messages([
    ("system", _CONTEXTUALIZE_Q_SYSTEM_PROMPT),
    MessagesPlaceholder("chat_history"),     # 历史消息插在这里
    ("human", "{input}"),
])


def _sanitize_rewrite(rewritten: str, original: str) -> str:
    """
    问题重写输出的消毒兜底。

    重写模型偶尔会违反指令：直接回答问题、输出「无法回答」类回应、
    或附带大段解释。这些输出进检索器会污染查询，透传到接口会误导前端
    （standalone_question 字段显示成一段回答，实测 Qwen3-8B 就复现过）。

    判定规则（命中任一回退原问题 —— 改写失败最多损失一点检索精度，
    带着垃圾查询去检索才是真的灾难）：
        · 输出为空
        · 输出比原问题长得多（改写只该补全指代，不该长篇大论）
        · 含「无法回答 / 根据现有资料 / 抱歉」等作答痕迹
        · 作答形态：含句号/感叹号且不以问号结尾 —— 合法改写几乎总是疑问句
          （以 ？ 结尾或无句读），陈述句基本可以断定模型在「作答」。
          实测漏网案例：「根据公司规定，员工每年享有10天带薪年假。」
          命中规则前三条全部不触发，但陈述句形态一眼可辨。
          副作用：极少数陈述式改写（如「请介绍一下报销流程。」）会回退原问题，
          只损失一点改写质量，不影响正确性 —— 这是可接受的代价。
    """
    text = (rewritten or "").strip()
    if not text:
        return original
    if len(text) > max(len(original) * 4, len(original) + 30):
        logger.warning("重写输出超长，回退原问题 | 原问题=%.24s 输出长度=%d", original, len(text))
        return original
    for marker in ("无法回答", "根据现有资料", "抱歉", "对不起"):
        if marker in text:
            logger.warning("重写输出含作答痕迹，回退原问题 | 原问题=%.24s 输出=%.32s", original, text)
            return original
    has_declarative = "。" in text or "！" in text or "!" in text
    ends_question = text.endswith("？") or text.endswith("?")
    if has_declarative and not ends_question:
        logger.warning("重写输出为陈述句（疑似作答），回退原问题 | 原问题=%.24s 输出=%.32s", original, text)
        return original
    return text


# --------------------------------------------------------------------------- #
# 检索前 Query 增强（路线图 §6.4 D，v3.1.0-p3.7d）
# --------------------------------------------------------------------------- #
# 对外暴露的纯函数：生产链路（_prepare_parallel）与评测脚本（eval_retrieval.py
# 的 --enhance / --hyde）共用同一份改写与 HyDE 逻辑，避免两处各写一份漂移。
# 两者都复用主答模型（settings.LLM_*），不另起模型——改写延迟已被
# _prepare_parallel 的并行管线消化（改写与意图识别 / 投机检索并发执行）。
_HYDE_SYSTEM_PROMPT = (
    "你是一个严谨的企业知识库助手。用户会给你一个问题，"
    "请想象「如果知识库里有这个问题的标准答案，那段答案最可能长什么样」，"
    "然后只写出那段假设性答案文本本身（2~4 句，简明、贴近知识库文风）。\n"
    "绝对不要回答问题，不要输出「根据资料」之类的开场白，"
    "只输出那段假设答案文本本身。"
)


#: 疑问标记（不计问号本身——「怎么申请事假？」只有一个疑问标记，是单问句）
_QUESTION_MARKS: tuple[str, ...] = (
    "怎么", "如何", "多少", "几", "是否", "能不能", "会不会", "可不可以",
    "哪些", "什么", "吗", "呢",
)
#: 显式并列连词/分隔符
_PARALLEL_MARKS: tuple[str, ...] = (
    "以及", "另外", "还有", "同时", "并且", "而且", ";", "；", "且",
)


def _looks_compound(question: str) -> bool:
    """
    零成本判断：这个问句**疑似**包含多个可独立回答的子问题（P4-9b）。

    判定只是「要不要花一次 LLM 去做改写/分解」的门，不是分解本身——
    真正的拆分由改写模型按行输出。判宽一点的成本只是多一次调用，
    判窄了则复合问题根本得不到分解（q042 实测：复合问句不分解 → 流程切片缺席）。

    三条命中其一：
        ① 问号 ≥2（「怎么请事假？扣不扣钱？」）；
        ② 疑问标记 ≥2（「事假怎么申请，工资怎么算」——**无问号**也能触发）；
        ③ 有并列连词且 ≥1 个疑问标记（「怎么申请年假？以及需要哪些材料」）。
    刻意保守：单问号 + 单疑问标记（「怎么申请事假？」）不触发，避免误伤。
    """
    q = question or ""
    if q.count("？") + q.count("?") >= 2:
        return True
    marks = sum(q.count(m) for m in _QUESTION_MARKS)
    if marks >= 2:
        return True
    return marks >= 1 and any(m in q for m in _PARALLEL_MARKS)


def _sub_queries_from(raw: str, original: str) -> list[str]:
    """
    把改写模型的输出解析成 1~3 个子查询（P4-9）。

    口径：
        · 按行拆（模型被要求每行一个子问题）；剥掉「1. 」「- 」等列表前缀；
        · 每行过一遍 _sanitize_rewrite（作答痕迹 / 超长 / 陈述句 → 弃行）；
        · 去重、最多 3 行；全军覆没 → 回退 [original]（改写失败不致命）。
    """
    text = (raw or "").strip()
    if not text:
        return [original]
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"^(?:\d+[.、)]\s*|[-*•]\s*)", "", line)   # 剥列表前缀
        if not line:
            continue
        line = _sanitize_rewrite(line, original).strip()
        if line and line not in lines:
            lines.append(line)
    if not lines:
        return [original]
    return lines[:3]


def rewrite_to_sub_queries(
    question: str,
    chat_history: list[Any] | None = None,
    llm: Any = None,
) -> list[str]:
    """
    问题改写 + 复合问题分解（P4-9），输出 1~3 个独立子查询。

    · 单子问题：与旧行为完全一致（含 P4-7b 的指代重试）；
    · 复合问题：模型按行输出子问题，每行独立改写。
    供生产链路（RAGChain._rewrite_question）与评测脚本共用同一份逻辑。

    :param llm: 可选注入（测试桩）；缺省走全局客户端。
    """
    llm = llm or get_llm_client().get_llm()
    history = list(chat_history or [])
    message = (_contextualize_q_prompt | llm).invoke(
        {"input": question, "chat_history": history}
    )
    sub_queries = _sub_queries_from(str(message.content), question)
    # P4-7b：原样返回 ≠ 一定合法——原问题含指代却原样返回 = 改写失败，重试一次
    if len(sub_queries) == 1 and sub_queries[0].strip() == question.strip() \
            and looks_anaphoric(question):
        logger.warning("改写原样返回但问题含指代，带强化指令重试一次 | q=%.32s", question)
        retry_message = (_contextualize_q_prompt | llm).invoke(
            {
                "input": (
                    f"{question}\n\n"
                    "（注意：上一轮你没有输出改写结果。这个问题依赖对话历史，必须消解指代——"
                    "例如「那病假呢？」应改写为「病假如何申请？」。只输出改写后的那一个问题。）"
                ),
                "chat_history": history,
            }
        )
        sub_queries = _sub_queries_from(str(retry_message.content), question)
    return sub_queries


def rewrite_query(
    question: str,
    chat_history: list[Any] | None = None,
    llm: Any = None,
) -> str:
    """
    `rewrite_to_sub_queries` 的字符串包装（评测脚本 --enhance 用）：
    多子查询以空格拼接成一个检索串；单子问题行为与旧版完全一致。
    """
    return " ".join(rewrite_to_sub_queries(question, chat_history, llm=llm))


def generate_hypo_answer(question: str) -> str:
    """
    为 HyDE 生成「假设答案」文本，用其 embedding 去做向量召回。

    BM25 仍用真实问句关键词（在 _prepare_parallel / retriever 里分离处理），
    所以这里只负责产出一段贴近知识库文风的假设答案。
    退化兜底：生成失败（空 / 异常）退回原问题，绝不带脏文本去 embedding。
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", _HYDE_SYSTEM_PROMPT),
        ("human", "{input}"),
    ])
    llm = get_llm_client().get_llm()
    message = (prompt | llm).invoke({"input": question})
    text = str(message.content).strip()
    return text or question


# ② 问答提示词：system 里带检索到的上下文，强约束「基于资料回答」。
#    「不知道就说不知道」是 RAG 防幻觉的第一道护栏。
#    {intent_instruction} 是意图专属的回答组织指令（见下面 INTENT_INSTRUCTIONS），
#    由运行时按当次意图填入 —— 链只建一次，模板随意图切换。
_QA_SYSTEM_PROMPT = (
    "你是一个严谨的企业知识库问答助手。请基于下面检索到的资料回答用户的问题。\n"
    "要求：\n"
    "1. 只能依据给定资料作答。资料里没有的信息，禁止用你的常识、经验或通用知识补充"
    "——即使你觉得答案显而易见；\n"
    "2. 制度 / 规则 / 流程类问题，如果资料中没有明确规定，必须回答"
    "「根据现有制度未查到相关规定，请补充对应制度文档或联系人力资源部门确认」，禁止推测；\n"
    "3. {intent_instruction}\n"
    "4. 如果资料与问题无关，忽略资料并如实告知。\n"
    "5. 不要输出任何乱码、特殊符号或者不连贯的句子。\n"
    "6. 回答引用了资料中的具体条款时，在答案末尾另起一行列出依据，"
    "格式：「依据：文件名 第X条」（多个依据用顿号分隔）；"
    "「未查到」类回答不加此行。\n"
    "7. 用户的问题包含多个子问题时，分子问题组织回答（每个子问题一小节）；"
    "任一子问题在资料中找不到依据的，在该子问题下明确说明「根据现有制度未查到相关规定」，"
    "不要把多个子问题混在一起含糊作答。\n\n"
    "检索到的资料：\n{context}"
)

# 意图 → 回答组织指令：同一个「基于资料回答」的底座，
# 按意图换「怎么组织答案」——这是细粒度意图分类的核心收益。
# chitchat 不在表里（走闲聊链，不进检索问答）。
INTENT_INSTRUCTIONS: dict[str, str] = {
    "knowledge_query": (
        "侧重讲清概念与原理，定义先行，再展开说明，必要时举例"
    ),
    "operation_guide": (
        "按操作步骤分条回答（1. 2. 3.），说明每一步要做什么、注意什么"
    ),
    "policy_consult": (
        "回答要包含政策依据、适用条件/范围、关键数字（如额度、时限）"
    ),
    "comparison_analysis": (
        "逐个维度对比，优先用表格或分条对照呈现相同点与不同点"
    ),
    "data_statistics": (
        "以数字为核心回答，给出具体数值、单位与口径（统计范围、时间区间）"
    ),
    "troubleshooting": (
        "按「可能原因 → 排查方法 → 解决步骤」的结构回答，可执行优先"
    ),
    # 兜底：模型给了不在表里的意图标签（理论不会发生，防御性默认）
    "default": "回答简洁准确，可以用条目列出要点",
}

_qa_prompt = ChatPromptTemplate.from_messages([
    ("system", _QA_SYSTEM_PROMPT),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}"),
])

# ③ 闲聊提示词：不挂检索资料，只带历史自由对话。
_CHITCHAT_SYSTEM_PROMPT = (
    "你是一个友好的企业知识库助手。用户刚才在与你寒暄或闲聊，"
    "请自然地回应，保持简洁亲切，并可以顺带提示用户可以向你提问知识库相关的问题。"
)

_chitchat_prompt = ChatPromptTemplate.from_messages([
    ("system", _CHITCHAT_SYSTEM_PROMPT),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}"),
])


# --------------------------------------------------------------------------- #
# 并行管线基础设施
# --------------------------------------------------------------------------- #
# 意图识别 / 投机检索 / 问题重写三者互不依赖，串行执行是纯等待浪费。
# 模块级线程池：跨请求复用线程（每次新建 ThreadPoolExecutor 反而更贵）。
_PIPELINE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rag-pipe")


def _usage_zero() -> dict[str, int]:
    """一次 LLM 调用的 token 用量零值结构。"""
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}


def _merge_usage(target: dict[str, int], source: dict[str, int]) -> None:
    """把一次调用的用量合并进累计桶（一次问答 = 重写 + 主回答多次调用）。"""
    for key in target:
        target[key] += source.get(key, 0)


def _extract_usage(message: Any) -> dict[str, int]:
    """
    从 AIMessage / AIMessageChunk 提取 token 用量（统一 OpenAI 兼容与 ollama 格式）。

    langchain-core 的 usage_metadata 结构：
        {"input_tokens": N, "output_tokens": M, "total_tokens": N+M,
         "input_token_details": {"cache_read": K}}   # 命中 prompt 缓存时才有
    非流式 invoke 的响应默认带；流式需要 ChatOpenAI(stream_usage=True) 末帧才带。
    """
    meta = getattr(message, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    return {
        "input_tokens": int(meta.get("input_tokens") or 0),
        "output_tokens": int(meta.get("output_tokens") or 0),
        "cache_read_tokens": int(details.get("cache_read") or 0),
    }


# 空库/零召回时的标准拒答话术。
# 为什么不用 prompt 约束而要短路：意图指令（如「以数字为核心回答」）
# 会与「不知道就说不知道」的护栏竞争，实测空上下文时模型可能编造数字。
# 检索结果为空 → 压根不进 LLM，直接返回固定话术，幻觉概率归零。
_NO_CONTEXT_ANSWER = "根据现有资料无法回答该问题。请先向知识库录入相关资料，或换个问法重试。"


def _resolve_chunk_id(metadata: dict[str, Any]) -> str | None:
    """
    从切片元数据里取出引用键；元数据里没有就用 doc_id + chunk_index 现拼一个。

    存在的意义：P0-3a 与 P0-4a 之间入库的那批切片只有 doc_id / chunk_index，
    没有 chunk_id。它们的正文完好、反查通道也通，唯一缺的就是「键」。
    在这里补一次，等于让那批切片不必为了一个派生字段整库重建。

    两级都拿不到（P0-3 之前的遗留切片）返回 None —— 调用方（前端）据此
    把引用渲染成不可点击的纯文本。**不要**退化成返回 source 路径：
    那会让前端以为「有键可查」，点下去必然 404。
    """
    existing = metadata.get("chunk_id")
    if isinstance(existing, str) and existing:
        return existing

    doc_id = metadata.get("doc_id")
    chunk_index = metadata.get("chunk_index")
    if isinstance(doc_id, bool) or not isinstance(doc_id, int):
        return None
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
        return None
    return build_chunk_id(doc_id, chunk_index)


def format_docs(docs: list[Document]) -> str:
    """
    把检索到的文档片段格式化成注入 prompt 的 context 文本。

    带编号和来源：模型可以据此组织回答，人也方便在日志里对照
    「模型看到的资料」和「接口返回的 sources」是不是同一份。
    """
    blocks: list[str] = []
    for index, doc in enumerate(docs, start=1):
        source = doc.metadata.get("source", "未知来源")
        blocks.append(f"【资料{index}】（来源：{source}）\n{doc.page_content}")
    return "\n\n".join(blocks)


def _has_chat_history(inputs: dict[str, Any]) -> bool:
    """
    RunnableBranch 的分支条件：有没有历史消息。

    单独写成函数而不是 lambda，是为了让「输入是 dict」这件事有类型注解：
    LCEL 内部的 Input 类型对 pyright 是不透明的，lambda 的形参会被推断成未知类型，
    后面的 inputs.get(...) 就成了「访问未知属性」。
    """
    return bool(inputs.get("chat_history"))


def _has_no_docs(inputs: dict[str, Any]) -> bool:
    """
    RunnableBranch 的分支条件：检索结果是不是空的（零召回）。

    同 _has_chat_history：写成带注解的函数而不是 lambda，
    否则 lambda 形参会被推断成 object，inputs["docs"] 就成了「对 object 取下标」。
    """
    return not inputs.get("docs")


def _empty_context_result(inputs: dict[str, Any]) -> dict[str, Any]:
    """
    零召回分支的返回值：固定拒答，并把 docs / standalone_question 原样透传。

    透传是为了让上层拿到的结果结构与正常回答一致（接口层与流式都要读这两个字段），
    用 .get + 默认值是因为这两个键由上游 assign 写入，理论上必有、仍按缺失兜底。
    """
    return {
        "answer": _NO_CONTEXT_ANSWER,
        "docs": inputs.get("docs", []),
        "standalone_question": inputs.get("standalone_question", ""),
    }


def _log_rejection(
    standalone_question: str | None,
    trace: dict[str, Any] | None,
) -> None:
    """
    拒答分支的结构化 INFO 日志（P2-23）。

    立项动机：p2.22 验收时 Q2 被阈值短路拒答，但被全过滤时
    `重排完成` 的分数区间退化成 [0, 0]，原始 top1 不可见 ——
    「差多少分」这个最关键的问题答不了。top1 从重排轨迹取
    （retriever 在阈值过滤前抢救出来的那份），取不到时如实写「不可得」，
    不编造 0.0（0.0 会被误读成「模型给了 0 分」）。
    """
    top1 = trace.get("top1_before_filter") if trace else None
    threshold = trace.get("threshold") if trace else None
    logger.info(
        "拒答 | 原因=检索为空 | 改写后查询=%s | top1分数=%s | 阈值=%s",
        (standalone_question or "").strip() or "（无）",
        f"{top1:.4f}" if isinstance(top1, (int, float)) else "不可得",
        f"{threshold:.4f}" if isinstance(threshold, (int, float)) else "未启用",
    )


def _retrieval_debug_event(
    intent_result: IntentResult,
    prepared: dict[str, Any],
    trace: dict[str, Any] | None,
    hybrid: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    组装 NDJSON `retrieval` 调试事件（P2-23，/ask/stream debug=true 时下发）。

    候选清单的来源优先级：
        · 有重排轨迹 —— 直接用（含被阈值过滤掉的候选，passed_threshold=false，
          这是「拒答差多少分」的核心数据）；
        · 没有轨迹（重排关闭）—— 用返回 docs 的溯源信息凑，
          全部标 passed_threshold=true（没有阈值就谈不上没过）。

    :param hybrid: 混合召回轨迹（P3-4）。开关关闭时为 None，
                   此时 `bm25_hits` / `rrf_top` 一律下发 None 而不是 0 / ""：
                   「没开混合检索」和「开了但 BM25 一条都没命中」是两回事，
                   用同一份 0 表示会让排查的人往错误的方向上查。
    """
    if trace:
        candidates = [dict(c) for c in trace.get("candidates") or []]
        threshold = trace.get("threshold")
        top1 = trace.get("top1_before_filter")
        filtered_count = trace.get("filtered_count", 0)
    else:
        candidates = [
            {
                "chunk_id": s.get("chunk_id"),
                "file_name": s.get("file_name"),
                "rerank_score": s.get("rerank_score"),
                "vector_similarity": s.get("vector_similarity"),
                "passed_threshold": True,
            }
            for s in RAGChain._extract_sources(list(prepared.get("docs") or []))
        ]
        threshold = None
        top1 = None
        filtered_count = 0
    return {
        "type": "retrieval",
        "rewritten_query": prepared.get("standalone_question"),
        # HyDE 假设答案（向量召回实际用的 embedding 文本）；未启用时为 null（不是空串）
        "vector_query": prepared.get("vector_query"),
        "intent": intent_result.intent.value,
        "route": intent_result.route,
        "candidates": candidates,
        "threshold": threshold,
        "top1_before_filter": top1,
        "filtered_count": filtered_count,
        "docs_returned": len(prepared.get("docs") or []),
        # —— P3-4 混合召回：关时为 None（不是 0）——
        "bm25_hits": hybrid.get("bm25_hits") if hybrid else None,
        "bm25_only": hybrid.get("bm25_only") if hybrid else None,
        "rrf_top": hybrid.get("rrf_top") if hybrid else None,
    }


def _cache_qa_trace(
    question: str, intent: str, route: str, elapsed_ms: float, sources_count: int
) -> dict[str, Any]:
    """
    缓存命中轮的回溯轨迹（P4-5）。

    复用缓存不跑检索，所以没有候选 / 阈值 / top1 —— 这些一律写 None 而不是 0：
    「没跑检索」和「跑了但一条没捞到」是两种完全不同的排查方向，
    与 `_retrieval_debug_event` 里 bm25_hits 的口径同款。
    """
    return {
        "question": question,
        "decision": "cache_hit",
        "intent": intent,
        "route": route,
        "intent_source": "cache",
        "rewritten_query": None,
        "vector_query": None,
        "candidates": [],
        "threshold": None,
        "top1_before_filter": None,
        "filtered_count": 0,
        "docs_returned": sources_count,
        "bm25_hits": None,
        "bm25_only": None,
        "rrf_top": None,
        "hyde_used": False,
        "elapsed_ms": elapsed_ms,
        "sources_count": sources_count,
        "rejected": False,
    }


def _build_qa_trace(
    question: str,
    intent_result: IntentResult,
    prepared: dict[str, Any] | None,
    decision: str,
    elapsed_ms: float,
    sources_count: int,
) -> dict[str, Any]:
    """
    组装本轮的流程回溯轨迹（P4-5），随 turn_meta 落进 `chat_message`。

    `decision` 是这一轮最终走到的**终端状态**（四选一，见 PLAN-v4.0.0 P4-5）：
        cache_hit / chitchat / zero_recall / rag_qa

    检索细节**复用 `_retrieval_debug_event`**（同一份口径，只剥掉流式用的 type 键）——
    「流式面板看到的」和「落库里存的」必须是同一份数据，否则两套口径迟早漂移。
    """
    if prepared is None:
        # 闲聊：压根没检索。同上，一律 None 而不是 0。
        retrieval: dict[str, Any] = {
            "rewritten_query": None,
            "vector_query": None,
            "candidates": [],
            "threshold": None,
            "top1_before_filter": None,
            "filtered_count": 0,
            "docs_returned": 0,
            "bm25_hits": None,
            "bm25_only": None,
            "rrf_top": None,
        }
    else:
        rtrace = prepared.get("retrieval_trace") or {}
        event = _retrieval_debug_event(
            intent_result, prepared, rtrace.get("rerank"), rtrace.get("hybrid")
        )
        event.pop("type", None)
        retrieval = event

    trace: dict[str, Any] = {
        "question": question,
        "decision": decision,
        "intent": intent_result.intent.value,
        "route": intent_result.route,
        "intent_source": intent_result.source,
        "hyde_used": bool(prepared and prepared.get("vector_query")),
        "elapsed_ms": elapsed_ms,
        "sources_count": sources_count,
        "rejected": decision == "zero_recall",
    }
    trace.update(retrieval)
    return trace


def _record_qa_trace(
    session_id: str, owner_id: int | None, trace: dict[str, Any]
) -> None:
    """
    轨迹镜像进 `qa_trace` 分析表（P4-5b），**fail-open**。

    为什么吞异常而 quota / 会话不吞：这份表是运营分析用的镜子，
    镜子碎了该有的症状是「溯源页缺数据」，绝不是「用户问不了」。
    与 qa_cache.store_answer 的姿态一致（缓存写失败只记日志）；
    与 audit_repo.record 的分工是「仓库层抛、调用方定姿态」——
    测试要断言镜像内容时，让 record 原样炸出来比吞掉更好查。
    """
    try:
        qa_trace_repo.record(session_id=session_id, owner_id=owner_id, trace=trace)
    except Exception:
        logger.warning("qa_trace 镜像落库失败（不影响本轮回答）", exc_info=True)


def _strip_leading_blank(text: str) -> str:
    """
    裁掉答案**开头**的空白，中间与结尾一律不动。

    为什么要这一步：模型偶尔以 `\\n\\n` 开头（2026-09 真实使用中出现过一次）。
    前端气泡是 `white-space: pre-wrap` 的纯文本容器，前导换行会渲染成空首行 ——
    看起来像「气泡顶部凭空多了一块空白」，首帧恰为 `\\n\\n` 时甚至只剩一个空气泡。
    答案开头的空白没有任何语义（本项目气泡不渲染 Markdown），所以在这里裁掉。

    只在「答案成为结果的那一处」调用，让**流式增量 / 缓存回放 / 同步返回 / 落库正文**
    四个出口口径一致 —— 否则会出现「当下看着对、刷新后冒空行」这类只在历史里复现的错。
    """
    return text.lstrip()


class RAGChain:
    """
    生产级 RAG 链：意图路由 + 多轮记忆 + LCEL 检索问答。

    对外只有两个方法：
        query(question, session_id)   —— 同步问答，返回完整结构化结果
        stream(question, session_id)  —— 流式问答，逐段产出文本增量
    """

    def __init__(
        self,
        retriever: RAGRetriever | None = None,
        llm_client: LLMClient | None = None,
        memory: MemoryManager | None = None,
    ) -> None:
        """
        三个依赖全部缺省用全局单例：
        检索器（向量库连接）/ LLM 客户端（HTTP 连接池）/ 记忆管理器都是
        「进程级共享资源」，每个请求新建会反复建连，必须用单例。
        """
        self.retriever: RAGRetriever = retriever or get_rag_retriever()
        self.llm_client: LLMClient = llm_client or get_llm_client()
        self.memory: MemoryManager = memory or get_memory_manager()

        # LCEL 链延迟到首次问答时构建：
        # 构建会触发底层 LLM 连接初始化，服务启动阶段不该背这个成本
        # LCEL 链的 Input 统一是 {"input": ..., "chat_history": ...}，
        # 两条链的区别只在 Output：主链返回结构化 dict，闲聊链返回纯文本
        self._rag_chain: Runnable[dict[str, Any], dict[str, Any]] | None = None
        self._chitchat_chain: Runnable[dict[str, Any], str] | None = None
        # 「重写→消毒→检索→格式化」前置管道：构建主链时顺带产出，
        # 流式路径复用它（先同步跑 prepare 拿 docs，再单独流式生成）
        self._prepare_chain: Runnable[dict[str, Any], dict[str, Any]] | None = None
        self._build_lock = threading.Lock()

        logger.info(
            "RAGChain 初始化完成 | llm=%s/%s 检索重排=%s",
            self.llm_client.provider,
            self.llm_client.model_name,
            "开" if self.retriever.reranker else "关",
        )

    # ------------------------------------------------------------------ #
    # LCEL 链构建
    # ------------------------------------------------------------------ #
    def _build_prepare_chain(self) -> Runnable[dict[str, Any], dict[str, Any]]:
        """
        构建「问题重写 → 检索 → 格式化」前置管道。

        同步链与流式链共用这一段：两条路径对「重写 + 检索」的要求完全一致，
        分开写两份迟早改一处漏一处 —— 本次重写消毒就是两处都要挂，
        曾经就出现过「主链有消毒、流式链漏了」的版本冲突事故。

        输入：{"input": 问题, "chat_history": [...]}
        输出：原输入 + standalone_question / docs / context 三个新键
        """
        llm = self.llm_client.get_llm()
        retriever = self.retriever.as_retriever()

        # 问题重写子链：prompt → LLM → 纯文本 → 消毒（模型违反指令作答时回退原问题）
        contextualize_chain = _contextualize_q_prompt | llm | StrOutputParser()

        def rewrite_with_guard(inputs: dict[str, Any]) -> str:
            """重写 + 消毒：闭包捕获原问题，交给 _sanitize_rewrite 判定。"""
            return _sanitize_rewrite(contextualize_chain.invoke(inputs), inputs["input"])

        # 分支：有历史才重写；没历史直接用原问题（省一次 LLM 往返）
        rewrite_or_passthrough = RunnableBranch(
            (_has_chat_history, RunnableLambda(rewrite_with_guard)),
            itemgetter("input"),
        )

        prepare_chain: Runnable[dict[str, Any], dict[str, Any]] = (
            RunnablePassthrough.assign(standalone_question=rewrite_or_passthrough)
            | RunnablePassthrough.assign(docs=itemgetter("standalone_question") | retriever)
            | RunnablePassthrough.assign(context=lambda inputs: format_docs(inputs["docs"]))
        )
        return prepare_chain

    def _build_rag_chain(self) -> Runnable[dict[str, Any], dict[str, Any]]:
        """
        构建检索问答主链（LCEL 管道）。

        输入字典：{"input": 用户问题, "chat_history": [Message, ...]}
        输出字典：{"answer": str, "docs": [Document], "standalone_question": str}

        管道拆解（每个 | 是一级）：
            第一级（prepare，见 _build_prepare_chain）
                有历史 → 重写 prompt → LLM → 消毒 → 独立问题
                无历史 → 原问题透传（省一次 LLM 往返）
            第二级 检索：用独立问题去检索，保留原始 Document 列表做溯源
            第三级 格式化：docs → prompt 用的 context 文本
            第四级 零召回短路 或 RunnableParallel 生成
                answer：context + 历史 + 问题 → 问答 prompt → LLM → 字符串
                docs / standalone_question：原样透传，随答案一起返回
        """
        llm: BaseChatModel = self.llm_client.get_llm()
        prepare_chain = self._build_prepare_chain()
        # 存一份引用给流式路径用：流式需要先跑完 prepare 拿 docs（meta 帧要带溯源），
        # 再单独对生成段做流式，不能直接复用整条主链
        self._prepare_chain = prepare_chain

        rag_chain = prepare_chain | RunnableBranch(
            # 零召回短路：检索结果为空时跳过 LLM，直接返回固定拒答。
            # 这样既省一次（可能编造的）生成调用，也保证所有意图下拒答口径一致。
            (_has_no_docs, _empty_context_result),
            RunnableParallel(
                answer=_qa_prompt | llm | StrOutputParser(),
                docs=itemgetter("docs"),
                standalone_question=itemgetter("standalone_question"),
            ),
        )
        logger.info("RAG 主链构建完成（LCEL：重写→消毒→检索→格式化→生成）")
        return rag_chain

    def _build_chitchat_chain(self) -> Runnable[dict[str, Any], str]:
        """构建闲聊链：不检索，历史 + 问题直接给 LLM。"""
        chain = _chitchat_prompt | self.llm_client.get_llm() | StrOutputParser()
        logger.info("闲聊链构建完成")
        return chain

    def _get_chains(self) -> tuple[Runnable[dict[str, Any], dict[str, Any]], Runnable[dict[str, Any], str]]:
        """
        双检锁延迟构建两条链。

        两条链是一次性成对构建的，所以用局部变量接住结果再一次性赋给实例属性，
        返回值也就天然是「非空」的，不必再靠 type: ignore 掩盖 Optional。
        """
        rag_chain = self._rag_chain
        chitchat_chain = self._chitchat_chain
        if rag_chain is None or chitchat_chain is None:
            with self._build_lock:
                rag_chain = self._rag_chain
                chitchat_chain = self._chitchat_chain
                if rag_chain is None or chitchat_chain is None:
                    rag_chain = self._build_rag_chain()
                    chitchat_chain = self._build_chitchat_chain()
                    self._rag_chain = rag_chain
                    self._chitchat_chain = chitchat_chain
        return rag_chain, chitchat_chain

    def _rewrite_question(
        self, question: str, chat_history: list[Any]
    ) -> tuple[list[str], dict[str, int]]:
        """
        问题重写 + 复合分解 + 指代重试，返回 (子查询列表 1~3 个, 重写用量)。

        P4-7c：必须走模块级单一入口（本方法在 P4-7b 曾内联自己的调用，
        指代重试在生产链路完全没生效——修逻辑不修入口 = 没修）。
        P4-9：入口升级为 rewrite_to_sub_queries（复合问题按行拆子查询）。
        """
        llm = self.llm_client.get_llm()
        sub_queries = rewrite_to_sub_queries(question, chat_history, llm=llm)
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        return sub_queries, usage

    def _prepare_parallel(
        self, question: str, chat_history: list[Any]
    ) -> tuple[IntentResult, dict[str, Any] | None]:
        """
        并行管线前段：意图识别 ∥ 投机检索（原问题）∥ 问题重写（多轮或开关开启时）。

        三个环节互不依赖，串行执行是纯浪费（实测串行多花 ~2s）：
            · 意图识别（ollama 小模型 ~2s）决定路由；
            · 投机检索：先用原问题检索。首轮必中（无重写）；改写后若与原问题一致也可复用；
            · 问题重写（有历史，或 QUERY_REWRITE / QUERY_HYDE 开关开启时）与意图/检索并行。
               开关经 ConfigService 读取（DB > .env > 代码默认），管理端可热更新。

        检索前 Query 增强（§6.4 D，v3.1.0-p3.7d）：
            开关关闭时行为与原版完全一致（无历史 → 不重写、不 HyDE）；
            开关打开时，单轮问题也过一次改写 prompt，可选再做 HyDE
            （用假设答案的 embedding 去做向量召回，BM25 仍用真实问句）。
        chitchat 路由时 prepared=None（投机检索白跑一次本地检索，成本可忽略）。

        :return: (意图结果, prepared) —— prepared 含 standalone_question/docs/context/
                 rewrite_usage，以及 HyDE 用的 vector_query（None = 未启用）
                 与 P4-5 的 retrieval_trace（本次检索轨迹，随轮落库）
        """
        classifier = get_intent_classifier()
        retriever = self.retriever  # 直接用 RAGRetriever：retrieve 支持 vector_query（HyDE 透传）

        # 检索前 Query 增强：多轮（有历史）或任一开关开启时都跑改写
        # 开关改走 ConfigService（v4.0.0 P4-1）：取值优先级 DB > .env > 代码默认，
        # 管理端改了即时生效，不再需要 recreate 容器。settings.XXX 仅作默认档。
        rewrite_on = config_service.get_flag("query_rewrite", settings.QUERY_REWRITE)
        hyde_on = config_service.get_flag("query_hyde", settings.QUERY_HYDE)
        enhance_on = rewrite_on or hyde_on
        # P4-9：复合问题在**首轮**就会出现（无历史），不能依赖「有历史才改写」。
        # 零成本预检 → 触发改写/分解（真正的分解由改写模型按行输出，不是按符号切）。
        looks_compound = _looks_compound(question)
        should_rewrite = bool(chat_history) or enhance_on or looks_compound

        # P4-5：本次调用的检索轨迹（按请求一份），随轮落库做流程回溯
        trace: dict[str, Any] = {}
        intent_future = _PIPELINE_EXECUTOR.submit(classifier.classify, question)
        # 投机检索用原问题（与改写并行；改写无变化时复用其结果，与原行为一致）
        retrieve_future = _PIPELINE_EXECUTOR.submit(
            retriever.retrieve, question, trace_out=trace
        )
        rewrite_future = (
            _PIPELINE_EXECUTOR.submit(self._rewrite_question, question, chat_history)
            if should_rewrite
            else None
        )

        intent_result: IntentResult = intent_future.result()
        if intent_result.route == "chitchat":
            # 投机任务不取消（已在跑），结果丢弃即可；线程池复用，不会泄漏
            return intent_result, None

        rewrite_usage = _usage_zero()
        vector_query: str | None = None
        if rewrite_future is not None:
            sub_queries, rewrite_usage = rewrite_future.result()
            standalone = " / ".join(sub_queries)   # 展示口径：多个子查询一并可见
            if hyde_on and len(sub_queries) == 1:
                # HyDE 只服务单子查询（复合问题的每路各自真实检索，不掺假设答案）
                vector_query = generate_hypo_answer(sub_queries[0])
            if sub_queries == [question] and not hyde_on:
                # 改写无变化 → 复用投机结果（省 ~1s 检索+重排）
                docs: list[Document] = retrieve_future.result()
            elif len(sub_queries) == 1:
                # 单子查询：与旧行为一致，重新检索并换新轨迹
                trace = {}
                docs = retriever.retrieve(
                    sub_queries[0], vector_query=vector_query, trace_out=trace
                )
            else:
                # P4-9 复合问题：每个子查询各检索一路，候选池去重合并
                trace = {}
                docs = self._multi_retrieve(sub_queries, trace)
        else:
            sub_queries = [question]
            standalone = question
            docs = retrieve_future.result()

        return intent_result, {
            "standalone_question": standalone,
            "sub_queries": sub_queries,    # P4-9：本轮实际检索的子查询（1~3 个）
            "docs": docs,
            "context": format_docs(docs),
            "rewrite_usage": rewrite_usage,
            "vector_query": vector_query,  # HyDE 假设答案；None = 未启用
            "retrieval_trace": trace,      # P4-5：本次检索轨迹（rerank / hybrid 两段）
        }

    def _multi_retrieve(
        self, sub_queries: list[str], trace: dict[str, Any]
    ) -> list[Document]:
        """
        复合问题的多路检索（P4-9）：每个子查询各走一遍完整检索
        （混合召回 + 重排 + 阈值），候选池按 chunk_id 去重合并。

        合并口径：同一切片多路命中 → 保留 rerank 分最高的那份 Document；
        合并后按 rerank 分降序输出。轨迹合成：rerank.top1 取各路最大值
        （低置信带判定用它），candidates 为合并池全量（均过阈值），
        hybrid 的 bm25_hits 逐路累加——「每路都看到」比「精确到路」更有用。
        """
        merged: dict[str, tuple[Document, float]] = {}
        best_top1 = 0.0
        threshold: float | None = None
        bm25_total = 0
        rrf_top: str | None = None
        for sq in sub_queries:
            sub_trace: dict[str, Any] = {}
            docs = self.retriever.retrieve(sq, trace_out=sub_trace)
            for d in docs:
                cid = chunk_id_of_metadata(d.metadata)
                if cid is None:
                    continue
                score = float(d.metadata.get("rerank_score") or 0.0)
                if cid not in merged or score > merged[cid][1]:
                    merged[cid] = (d, score)
            rr = sub_trace.get("rerank") or {}
            best_top1 = max(best_top1, float(rr.get("top1_before_filter") or 0.0))
            if threshold is None and rr.get("threshold") is not None:
                threshold = float(rr["threshold"])
            hy = sub_trace.get("hybrid") or {}
            bm25_total += int(hy.get("bm25_hits") or 0)
            if rrf_top is None and hy.get("rrf_top"):
                rrf_top = str(hy["rrf_top"])

        ordered = sorted(merged.values(), key=lambda t: t[1], reverse=True)
        trace["rerank"] = {
            "top1_before_filter": best_top1,
            "threshold": threshold,
            "filtered_count": 0,   # 各路已各自过阈值，合并池不再过滤
            "candidates": [
                {
                    "chunk_id": chunk_id_of_metadata(d.metadata),
                    "file_name": d.metadata.get("file_name"),
                    "rerank_score": round(score, 6),
                    "vector_similarity": d.metadata.get("vector_similarity"),
                    "passed_threshold": True,
                }
                for d, score in ordered
            ],
        }
        trace["hybrid"] = {
            "bm25_hits": bm25_total,
            "bm25_only": None,     # 多路合并后「独有」语义模糊，不下发（None = 不适用）
            "rrf_top": rrf_top,
        }
        logger.info(
            "复合检索 | 子查询=%d 合并候选=%d best_top1=%.4f | %.24s",
            len(sub_queries), len(ordered), best_top1, " / ".join(sub_queries),
        )
        return [d for d, _ in ordered]

    @staticmethod
    def _stream_answer(
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        llm: BaseChatModel,
        usage_box: dict[str, int],
    ) -> Iterator[str]:
        """
        流式生成回答；捕获到的 usage_metadata 实时写入 usage_box（调用方迭代结束后读）。

        不走 prompt | llm | StrOutputParser() 的 LCEL 写法：StrOutputParser 会把
        AIMessageChunk 拍扁成字符串，usage_metadata（stream_usage=True 末帧携带）
        就丢了。直接用 llm.stream(PromptValue)，文本与用量都要。
        """
        chunks = llm.stream(prompt.invoke(variables))
        started = False  # 是否已吐出过正文：答案开头的空白只裁一次，中间换行照旧保留
        try:
            for chunk in chunks:
                text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                if text:
                    if started:
                        yield text
                    else:
                        # 首包可能整段都是空白（实测模型会以 "\n\n" 开头）：这一帧既不发、
                        # 也不算「已开始」，等真正的内容到达。否则前端会先渲染出空首行。
                        text = _strip_leading_blank(text)
                        if text:
                            started = True
                            yield text
                # 用量仍逐帧收：末帧常带 usage 而 content 为空，跳过它就漏账
                captured = _extract_usage(chunk)
                if captured["input_tokens"] or captured["output_tokens"]:
                    usage_box.update(captured)
        except GeneratorExit:
            # 客户端断开：停在当前 yield，关掉上游流，避免继续扣 token
            closer = getattr(chunks, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    logger.warning("关闭模型流失败", exc_info=True)
            raise

    # ------------------------------------------------------------------ #
    # 溯源信息整理
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_sources(docs: list[Document]) -> list[dict[str, Any]]:
        """
        从检索结果整理接口层需要的溯源信息。

        同时透传两种分数（重排分 / 向量相似度）：两者量纲不同但方向一致，
        前端展示「相关度」用 rerank_score 优先，退化到 vector_similarity。

        `chunk_id` 是 P0-4a 加的**引用键**：前端据此调
        `GET /api/v1/chunks/{chunk_id}` 反查切片正文，`core/mysql_store.py`
        的 `_extract_ref_ids()` 也据它往 `chat_message.ref_ids` 落库 ——
        在那个键出现之前，`ref_ids` 列一直是空的。

        取值是「先读元数据、读不到再拼」的两级：
            · 元数据里有 chunk_id —— P0-4a 之后入库的切片，直接用；
            · 没有 —— P0-3a 期间入库的切片（那时只写了 doc_id 与 chunk_index），
              由这两个字段现拼一个。
        不这么做的话，那批切片在界面上会变成「点不开的引用」，
        而它们的正文其实好端端地躺在库里 —— 纯粹是键没带上而已。

        两级都拿不到（P0-3 之前的遗留切片，连 doc_id 都没有）就返回 None：
        这类切片**本来就无法反查**，前端应当把它渲染成不可点击的纯文本，
        而不是给一个点了报错的按钮。
        """
        sources: list[dict[str, Any]] = []
        for index, doc in enumerate(docs, start=1):
            metadata = doc.metadata
            source = metadata.get("source", "未知来源")
            # source 是落盘路径，文件名是 uuid。展示用入库时写入的原始文件名。
            raw_name = metadata.get("file_name")
            file_name = str(raw_name) if raw_name else Path(str(source)).name
            # 片段摘要：接口不把全文吐出去，前端需要全文可以按 chunk_id 反查。
            # P3-7a：title 模式下存储正文是 enriched（header_path 前缀），
            # 摘要优先取 metadata["content"] 原文，别让路径前缀占掉 200 字配额。
            stored_meta = metadata if isinstance(metadata, dict) else {}
            snippet_source = stored_meta.get("content") or doc.page_content
            sources.append({
                "index": index,
                "chunk_id": _resolve_chunk_id(metadata),
                "source": source,
                "file_name": file_name,
                "snippet": snippet_source[:200],
                "rerank_score": metadata.get("rerank_score"),
                "vector_similarity": metadata.get("vector_similarity"),
            })
        return sources

    # ------------------------------------------------------------------ #
    # 相同问题缓存
    # ------------------------------------------------------------------ #
    def _replay_cache(self, question: str, session_id: str, start: float, *,
                       owner_id: int | None) -> dict[str, Any] | None:
        """命中则写进当前会话并返回完整结果。未命中返回 None。"""
        hit = lookup_answer(question)
        if hit is None:
            return None
        return self._finish_cached(question, session_id, start, hit, owner_id=owner_id)

    def _finish_cached(
        self, question: str, session_id: str, start: float, hit: dict[str, Any], *,
        owner_id: int | None,
    ) -> dict[str, Any]:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        usage = _usage_zero()
        answer = _strip_leading_blank(str(hit.get("answer") or ""))
        sources = list(hit.get("sources") or [])
        intent = str(hit.get("intent") or "knowledge_query")
        route = str(hit.get("route") or "rag_qa")
        standalone = hit.get("standalone_question") or question
        exchange_meta = {
            "sources": sources,
            "intent": intent,
            "route": route,
            "standalone_question": standalone,
            "usage": dict(usage),
            "elapsed_ms": elapsed_ms,
            "ts": time.time(),
            "cache_hit": True,
            # P4-5：缓存命中也是一个终端状态，同样留一条轨迹（无检索细节）
            "qa_trace": _cache_qa_trace(
                question, intent, route, elapsed_ms, len(sources)
            ),
        }
        self.memory.add_exchange(session_id, question, answer, meta=exchange_meta,
                                 owner_id=owner_id)
        # P4-5b：缓存命中轮同样镜像进分析表（decision=cache_hit，无检索细节）
        _record_qa_trace(session_id, owner_id, exchange_meta["qa_trace"])
        logger.info(
            "问答缓存复用 | session_id=%s 耗时=%.0fms | query=%.24s",
            session_id, elapsed_ms, question,
        )
        return {
            "answer": answer,
            "intent": intent,
            "route": route,
            "intent_source": "cache",
            "standalone_question": standalone,
            "sources": sources,
            "session_id": session_id,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
            "cache_hit": True,
        }

    def _stream_cached(
        self, question: str, session_id: str, start: float, hit: dict[str, Any], *,
        owner_id: int | None,
    ) -> Iterator[dict[str, Any]]:
        result = self._finish_cached(question, session_id, start, hit, owner_id=owner_id)
        yield {
            "type": "meta",
            "intent": result["intent"],
            "route": result["route"],
            "intent_source": "cache",
            "standalone_question": result["standalone_question"],
            "sources": result["sources"],
            "cache_hit": True,
        }
        yield {"type": "chunk", "content": result["answer"]}
        yield {
            "type": "done",
            "elapsed_ms": result["elapsed_ms"],
            "usage": result["usage"],
            "session_usage": self.memory.get_usage(session_id, owner_id=owner_id),
            "cache_hit": True,
        }

    def _maybe_store_cache(self, question: str, result: dict[str, Any], history_used: bool) -> None:
        """
        把用户打出来的原问题写入公共缓存。

        有上文时，重写经常会把完整问句再扩写一遍，不能因此拒绝缓存。
        只拒绝短句和指代句（「它呢」「继续」）。重写结果和原问题不同时，
        仍按原问题存储：下次任何人打出同一句，直接复用这次回答。
        """
        if result.get("route") != "rag_qa" or not result.get("sources"):
            return
        if not is_standalone_question(question):
            logger.info(
                "跳过问答缓存 | 问题依赖上文或过短 | history=%s | q=%.24s",
                history_used, question,
            )
            return
        standalone = str(result.get("standalone_question") or question)
        store_answer(
            question,
            str(result.get("answer") or ""),
            list(result.get("sources") or []),
            str(result.get("intent") or ""),
            str(result.get("route") or ""),
            standalone,
        )

    # ------------------------------------------------------------------ #
    # 对外：同步问答
    # ------------------------------------------------------------------ #
    def query(self, question: str, session_id: str, *,
             owner_id: int | None) -> dict[str, Any]:
        """
        执行一次完整问答。

        :param question: 用户问题
        :param session_id: 会话 id（多轮记忆的隔离键）
        :param owner_id: 提问者归属（P2-12c）。决定这段历史记在谁名下；
                         与session_id **一起**构成隔离 —— session_id 只是
                         一个随机串，光有它不构成任何权限。
        :return: {
            answer, intent, route, intent_source, session_id,
            standalone_question, sources, elapsed_ms
        }
        :raises ValueError: 问题为空
        :raises SessionOwnershipError: session_id 属于别人（写了半截会被接口层拦下，
                                        所以这里是干净失败 —— 记忆层一个字节都没写）
        :raises Exception:  LLM 调用失败（接口层转成 502）
        """
        if not question or not question.strip():
            raise ValueError("问题不能为空")

        start = time.perf_counter()

        cached = self._replay_cache(question, session_id, start, owner_id=owner_id)
        if cached is not None:
            return cached

        # ①②③④ 并行前段：意图识别 ∥ 投机检索 ∥ 问题重写（有历史时），见 _prepare_parallel
        chat_history = self.memory.get_recent_messages(session_id, owner_id=owner_id)
        intent_result, prepared = self._prepare_parallel(question, chat_history)
        llm = self.llm_client.get_llm()
        usage = _usage_zero()

        if prepared is None:
            # 闲聊链：无检索、无资料，answer 之后同样写记忆
            message = llm.invoke(
                _chitchat_prompt.invoke({"input": question, "chat_history": chat_history})
            )
            answer = _strip_leading_blank(str(message.content))
            _merge_usage(usage, _extract_usage(message))
            result: dict[str, Any] = {
                "answer": answer,
                "intent": intent_result.intent.value,
                "route": intent_result.route,
                "intent_source": intent_result.source,
                "standalone_question": None,    # 闲聊链不做问题重写
                "sources": [],                  # 没检索就没有溯源
            }
        else:
            _merge_usage(usage, prepared["rewrite_usage"])
            if not prepared["docs"]:
                # 零召回短路：不进 LLM，固定拒答（防幻觉护栏，与流式同口径）
                # P2-23：结构化日志留下「差多少分」的证据
                # P4-5：轨迹取本次调用那份（不再读实例级「最近一次」）
                _log_rejection(
                    prepared["standalone_question"],
                    (prepared.get("retrieval_trace") or {}).get("rerank"),
                )
                answer = _NO_CONTEXT_ANSWER
            else:
                # 知识型意图（六类）统一走 RAG 生成；意图标签换回答组织指令
                message = llm.invoke(
                    _qa_prompt.invoke({
                        "input": question,
                        "chat_history": chat_history,
                        "context": prepared["context"],
                        "intent_instruction": INTENT_INSTRUCTIONS.get(
                            intent_result.intent.value, INTENT_INSTRUCTIONS["default"]
                        ),
                    })
                )
                answer = _strip_leading_blank(str(message.content))
                _merge_usage(usage, _extract_usage(message))
            result = {
                "answer": answer,
                "intent": intent_result.intent.value,
                "route": intent_result.route,
                "intent_source": intent_result.source,
                "standalone_question": prepared["standalone_question"],
                "sources": self._extract_sources(prepared["docs"]),
            }

        # 每轮详情先算好再统一写记忆：sources/usage/耗时/时间戳随轮持久化，
        # 刷新页面后前端从历史接口原样恢复（引用不再丢失）
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        # P4-5：终端状态 —— 这一轮最终走的是哪条分支（四选一）。
        # P4-5b 起判定收口到 qa_flow.resolve_decision（唯一出处，module27 穷举守着）
        decision = qa_flow.resolve_decision(
            prepared_present=prepared is not None,
            docs_count=len(prepared["docs"]) if prepared else 0,
        )
        # P4-6：低置信带判定 + 依据校验（只在 rag_qa 轮；校验热开关可关）
        low_confidence = False
        grounded: dict[str, Any] | None = None
        if decision == "rag_qa":
            low_confidence = is_low_confidence(
                (prepared.get("retrieval_trace") or {}).get("rerank", {}).get("top1_before_filter")
            )
            if flag_value("groundedness_check"):
                grounded = check_groundedness(
                    question=question,
                    context=prepared["context"],
                    answer=answer,
                )
        # P4-6b：依据校验未通过 / 模型自认未查到 → 据实更正。
        # 追加更正块而不是静默替换：答案已流式流出，撤回做不到；
        # 最诚实的做法是把「没有依据、请勿采信」钉进正文并随轮落库。
        if answer_admits_no_basis(answer) and (grounded is None or grounded.get("grounded") is not False):
            # 模型自己在依据行承认了未查到 —— 覆盖 judge 结论（保守）
            grounded = {"grounded": False,
                        "unsupported": ["模型自述未在资料中查到依据"],
                        "note": "依据行自认未查到"}
        if grounded is not None and grounded.get("grounded") is False:
            answer = append_groundedness_correction(answer, grounded)
            result["answer"] = answer  # 缓存与调用方拿到的必须是更正后的版本
        exchange_meta = {
            "sources": result["sources"],
            "intent": result["intent"],
            "route": result["route"],
            "standalone_question": result["standalone_question"],
            "usage": dict(usage),
            "elapsed_ms": elapsed_ms,
            "ts": time.time(),
            "cache_hit": False,
            # P4-6：依据校验与低置信徽标（前端据此后挂警示标记）
            "low_confidence": low_confidence,
            "grounded": grounded,
            # 流程回溯轨迹（P4-5）：随 turn_meta 落库，刷新后仍可回看整条链路
            "qa_trace": _build_qa_trace(
                question, intent_result, prepared, decision,
                elapsed_ms, len(result["sources"]),
            ),
        }
        if grounded is not None:
            # 依据校验结果同时写进轨迹（镜像表经 _TRACE_KEYS 透传到 grounded/unsupported 列）
            exchange_meta["qa_trace"]["grounded"] = grounded.get("grounded")
            exchange_meta["qa_trace"]["unsupported"] = grounded.get("unsupported") or []
        self.memory.add_exchange(session_id, question, answer, meta=exchange_meta,
                                 owner_id=owner_id)
        # P4-5b：镜像进 qa_trace 分析表（fail-open —— 分析挂了不能挡问答）
        _record_qa_trace(session_id, owner_id, exchange_meta["qa_trace"])
        self._maybe_store_cache(question, result, bool(chat_history))
        # token 用量：本次问答（重写 + 主回答）累加进会话
        self.memory.add_usage(
            session_id, usage["input_tokens"], usage["output_tokens"],
            usage["cache_read_tokens"], owner_id=owner_id,
        )

        result["session_id"] = session_id
        result["elapsed_ms"] = elapsed_ms
        result["usage"] = usage
        result["cache_hit"] = False
        result["low_confidence"] = low_confidence
        result["grounded"] = grounded
        logger.info(
            "问答完成 | session_id=%s 意图=%s(%s) 引用=%d条 耗时=%.0fms tokens=%d+%d | query=%.24s",
            session_id,
            result["intent"],
            result["intent_source"],
            len(result["sources"]),
            elapsed_ms,
            usage["input_tokens"],
            usage["output_tokens"],
            question,
        )
        return result

    # ------------------------------------------------------------------ #
    # 对外：流式问答
    # ------------------------------------------------------------------ #
    def stream(self, question: str, session_id: str, *,
              owner_id: int | None, debug: bool = False) -> Iterator[dict[str, Any]]:
        """
        流式问答：逐段产出，最后产出一次完成事件。

        产出序列（dict，接口层序列化为 NDJSON 一行一个）：
            {"type": "meta",  ...}      第一帧：意图/溯源/重写后的问题
            {"type": "retrieval", ...}  仅 debug=True：检索调试事件（P2-23，
                                        meta 之后、chunk 之前；缓存命中与闲聊不下发）
            {"type": "chunk", "content": "..."}  × N：答案文本增量
            {"type": "done",  "elapsed_ms": ...} 最后一帧

        记忆在流结束后写回：必须等答案拼完整再写，
        半途写入会把「残缺答案」存进历史，污染下一轮。
        客户端断开时生成器在 yield 处退出，不写记忆、不再往下拉 token。

        :param owner_id: 见 query()。归属越权会在**吐字之前**（读历史那一次
                         load）就抛 `SessionOwnershipError`，所以不会出现
                         「已经吐了半屏才告诉你是别人的会话」。
        """
        if not question or not question.strip():
            raise ValueError("问题不能为空")

        start = time.perf_counter()
        cached = lookup_answer(question)
        if cached is not None:
            yield from self._stream_cached(question, session_id, start, cached,
                                           owner_id=owner_id)
            return

        chat_history = self.memory.get_recent_messages(session_id, owner_id=owner_id)
        # 并行前段：意图识别 ∥ 投机检索 ∥ 问题重写（有历史时）
        intent_result, prepared = self._prepare_parallel(question, chat_history)
        llm = self.llm_client.get_llm()
        usage = _usage_zero()

        if prepared is None:
            yield {
                "type": "meta",
                "intent": intent_result.intent.value,
                "route": intent_result.route,
                "intent_source": intent_result.source,
                "standalone_question": None,
                "sources": [],
            }
            answer_parts: list[str] = []
            for text in self._stream_answer(
                _chitchat_prompt,
                {"input": question, "chat_history": chat_history},
                llm,
                usage,
            ):
                answer_parts.append(text)
                yield {"type": "chunk", "content": text}
            answer = "".join(answer_parts)
        else:
            _merge_usage(usage, prepared["rewrite_usage"])
            yield {
                "type": "meta",
                "intent": intent_result.intent.value,
                "route": intent_result.route,
                "intent_source": intent_result.source,
                "standalone_question": prepared["standalone_question"],
                "sources": self._extract_sources(prepared["docs"]),
            }
            # P2-23：检索调试事件（生成开始前下发；不传 debug 的调用方不会走到这）
            # P4-5：与落库用同一份本次调用轨迹（流式面板和库里存的是同一份数据）
            if debug:
                rtrace = prepared.get("retrieval_trace") or {}
                yield _retrieval_debug_event(
                    intent_result, prepared, rtrace.get("rerank"), rtrace.get("hybrid"),
                )
            if not prepared["docs"]:
                # 零召回短路：与同步链路同口径，不进 LLM，拒答作为唯一 chunk 下发
                _log_rejection(
                    prepared["standalone_question"],
                    (prepared.get("retrieval_trace") or {}).get("rerank"),
                )
                yield {"type": "chunk", "content": _NO_CONTEXT_ANSWER}
                answer = _NO_CONTEXT_ANSWER
            else:
                answer_parts = []
                for text in self._stream_answer(
                    _qa_prompt,
                    {
                        "input": question,
                        "chat_history": chat_history,
                        "context": prepared["context"],
                        "intent_instruction": INTENT_INSTRUCTIONS.get(
                            intent_result.intent.value, INTENT_INSTRUCTIONS["default"]
                        ),
                    },
                    llm,
                    usage,
                ):
                    answer_parts.append(text)
                    yield {"type": "chunk", "content": text}
                answer = "".join(answer_parts)

        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        # 每轮详情随轮写入记忆（同 query 口径），历史接口按轮回填
        # 溯源只算一次：add_exchange / _maybe_store_cache / qa_trace 三处共用
        sources = [] if prepared is None else self._extract_sources(prepared["docs"])
        # P4-5：终端状态 —— 这一轮最终走的是哪条分支（四选一）。
        # P4-5b 起判定收口到 qa_flow.resolve_decision（与 query() 同一出处）
        decision = qa_flow.resolve_decision(
            prepared_present=prepared is not None,
            docs_count=len(prepared["docs"]) if prepared else 0,
        )
        # P4-6：低置信带判定 + 依据校验（与 query() 同口径；校验在答案流完之后）
        low_confidence = False
        grounded: dict[str, Any] | None = None
        if decision == "rag_qa":
            low_confidence = is_low_confidence(
                (prepared.get("retrieval_trace") or {}).get("rerank", {}).get("top1_before_filter")
            )
            if flag_value("groundedness_check"):
                grounded = check_groundedness(
                    question=question,
                    context=prepared["context"],
                    answer=answer,
                )
                # 校验结果作为独立帧下发（答案已流出，不撤回，只挂标记）
                yield {
                    "type": "grounded",
                    "grounded": grounded.get("grounded"),
                    "unsupported": grounded.get("unsupported") or [],
                    "low_confidence": low_confidence,
                }
        # P4-6b：同 query 口径——未通过/自认未查到 → 更正块作为额外 chunk 下发并随轮落库
        if answer_admits_no_basis(answer) and (grounded is None or grounded.get("grounded") is not False):
            grounded = {"grounded": False,
                        "unsupported": ["模型自述未在资料中查到依据"],
                        "note": "依据行自认未查到"}
        if grounded is not None and grounded.get("grounded") is False:
            correction = append_groundedness_correction("", grounded).strip()
            answer = (answer.rstrip() + "\n\n" + correction) if answer else correction
            yield {"type": "chunk", "content": "\n\n" + correction}
        exchange_meta = {
            "sources": sources,
            "intent": intent_result.intent.value,
            "route": intent_result.route,
            "standalone_question": None if prepared is None else prepared["standalone_question"],
            "usage": dict(usage),
            "elapsed_ms": elapsed_ms,
            "ts": time.time(),
            "cache_hit": False,
            # P4-6：依据校验与低置信徽标（前端据此后挂警示标记）
            "low_confidence": low_confidence,
            "grounded": grounded,
            # 流程回溯轨迹（P4-5）：随 turn_meta 落库，刷新后仍可回看整条链路
            "qa_trace": _build_qa_trace(
                question, intent_result, prepared, decision, elapsed_ms, len(sources),
            ),
        }
        if grounded is not None:
            exchange_meta["qa_trace"]["grounded"] = grounded.get("grounded")
            exchange_meta["qa_trace"]["unsupported"] = grounded.get("unsupported") or []
        self.memory.add_exchange(session_id, question, answer, meta=exchange_meta,
                                 owner_id=owner_id)
        # P4-5b：镜像进 qa_trace 分析表（fail-open —— 分析挂了不能挡问答）
        _record_qa_trace(session_id, owner_id, exchange_meta["qa_trace"])
        self._maybe_store_cache(
            question,
            {
                "answer": answer,
                "intent": intent_result.intent.value,
                "route": intent_result.route,
                "standalone_question": None if prepared is None else prepared["standalone_question"],
                "sources": sources,
            },
            bool(chat_history),
        )
        self.memory.add_usage(
            session_id, usage["input_tokens"], usage["output_tokens"],
            usage["cache_read_tokens"], owner_id=owner_id,
        )
        session_usage = self.memory.get_usage(session_id, owner_id=owner_id)
        logger.info(
            "流式问答完成 | session_id=%s 意图=%s 耗时=%.0fms tokens=%d+%d | query=%.24s",
            session_id,
            intent_result.intent.value,
            elapsed_ms,
            usage["input_tokens"],
            usage["output_tokens"],
            question,
        )
        # done 帧：本次用量 + 会话累计，前端据此展示 token 统计
        yield {
            "type": "done",
            "elapsed_ms": elapsed_ms,
            "usage": usage,
            "session_usage": session_usage,
        }

    # ------------------------------------------------------------------ #
    # 状态信息
    # ------------------------------------------------------------------ #
    def get_chain_info(self) -> dict[str, Any]:
        """返回链路当前配置（健康检查/状态页用，不含密钥）。"""
        return {
            "llm": self.llm_client.get_model_info(),
            "retriever": self.retriever.get_retriever_info(),
            "memory": {
                "max_turns": self.memory.max_turns,
                "ttl_seconds": self.memory.ttl_seconds,
                "session_count": self.memory.session_count(),
            },
            "intent_classifier": {
                "provider": get_intent_classifier().provider,
                "llm_available": not get_intent_classifier()._llm_broken,
                "intents": [intent.value for intent in Intent],
            },
        }


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_rag_chain: RAGChain | None = None
_rag_chain_lock = threading.Lock()


def get_rag_chain() -> RAGChain:
    """获取全局唯一的 RAG 链实例（双检锁，与项目内其他单例同风格）。"""
    global _rag_chain
    if _rag_chain is None:
        with _rag_chain_lock:
            if _rag_chain is None:
                _rag_chain = RAGChain()
                logger.debug("RAGChain 单例已创建")
    return _rag_chain


def reset_rag_chain() -> None:
    """重置单例（测试切换配置时用）。"""
    global _rag_chain
    with _rag_chain_lock:
        _rag_chain = None
        logger.info("RAGChain 单例已重置")
