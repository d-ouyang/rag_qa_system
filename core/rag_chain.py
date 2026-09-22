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
        │      没有历史时跳过重写，直接用原问题（省一次 LLM 调用）
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
import threading
import time
from collections.abc import Iterator
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
from core.intent_router import Intent, IntentResult, get_intent_classifier
from core.llm_client import LLMClient, get_llm_client
from core.memory_manager import MemoryManager, get_memory_manager
from core.retriever import RAGRetriever, get_rag_retriever

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
    "- 只输出改写后的那一个问题本身，不要任何前言、解释或多余内容\n"
    "- 不要输出答案，不要输出「无法回答」之类的回应\n"
    "- 如果问题本身已经是独立的，原样返回该问题"
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

# ② 问答提示词：system 里带检索到的上下文，强约束「基于资料回答」。
#    「不知道就说不知道」是 RAG 防幻觉的第一道护栏。
#    {intent_instruction} 是意图专属的回答组织指令（见下面 INTENT_INSTRUCTIONS），
#    由运行时按当次意图填入 —— 链只建一次，模板随意图切换。
_QA_SYSTEM_PROMPT = (
    "你是一个严谨的企业知识库问答助手。请基于下面检索到的资料回答用户的问题。\n"
    "要求：\n"
    "1. 只能依据给定资料作答，资料里没有的信息不要编造，直接说明「根据现有资料无法回答该问题」；\n"
    "2. {intent_instruction}\n"
    "3. 如果资料与问题无关，忽略资料并如实告知。\n\n"
    "4. 不要输出任何乱码、特殊符号或者不连贯的句子。\n\n"
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
    ) -> tuple[str, dict[str, int]]:
        """
        问题重写 + 消毒，返回 (standalone_question, 本次重写的 token 用量)。

        生产路径（query/stream）与 LCEL prepare 链共用同一 prompt 与消毒函数，
        行为差异只剩「是否记录 token 用量」这一观察性维度。
        """
        llm = self.llm_client.get_llm()
        chain = _contextualize_q_prompt | llm
        message = chain.invoke({"input": question, "chat_history": chat_history})
        return _sanitize_rewrite(str(message.content), question), _extract_usage(message)

    def _prepare_parallel(
        self, question: str, chat_history: list[Any]
    ) -> tuple[IntentResult, dict[str, Any] | None]:
        """
        并行管线前段：意图识别 ∥ 投机检索（原问题）∥ 问题重写（有历史时）。

        三个环节互不依赖，串行执行是纯浪费（实测串行多花 ~2s）：
            · 意图识别（ollama 小模型 ~2s）决定路由；
            · 投机检索：先用原问题检索。首轮必中（无重写）；多轮时若重写结果
              与原问题一致也可直接复用；
            · 问题重写（有历史才跑，LLM ~2-5s）与意图/检索并行，重叠等待。
        chitchat 路由时 prepared=None（投机检索白跑一次本地检索，成本可忽略）。

        :return: (意图结果, prepared) —— prepared 含 standalone_question/docs/context/rewrite_usage
        """
        classifier = get_intent_classifier()
        retriever = self.retriever.as_retriever()

        intent_future = _PIPELINE_EXECUTOR.submit(classifier.classify, question)
        retrieve_future = _PIPELINE_EXECUTOR.submit(retriever.invoke, question)
        rewrite_future = (
            _PIPELINE_EXECUTOR.submit(self._rewrite_question, question, chat_history)
            if chat_history
            else None
        )

        intent_result: IntentResult = intent_future.result()
        if intent_result.route == "chitchat":
            # 投机任务不取消（已在跑），结果丢弃即可；线程池复用，不会泄漏
            return intent_result, None

        rewrite_usage = _usage_zero()
        if rewrite_future is not None:
            standalone, rewrite_usage = rewrite_future.result()
            # 重写改变了问题才重新检索；否则复用投机结果（省 ~1s 检索+重排）
            if standalone.strip() == question.strip():
                docs: list[Document] = retrieve_future.result()
            else:
                docs = retriever.invoke(standalone)
        else:
            standalone = question
            docs = retrieve_future.result()

        return intent_result, {
            "standalone_question": standalone,
            "docs": docs,
            "context": format_docs(docs),
            "rewrite_usage": rewrite_usage,
        }

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
        for chunk in llm.stream(prompt.invoke(variables)):
            text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
            if text:
                yield text
            captured = _extract_usage(chunk)
            if captured["input_tokens"] or captured["output_tokens"]:
                usage_box.update(captured)

    # ------------------------------------------------------------------ #
    # 溯源信息整理
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_sources(docs: list[Document]) -> list[dict[str, Any]]:
        """
        从检索结果整理接口层需要的溯源信息。

        同时透传两种分数（重排分 / 向量相似度）：两者量纲不同但方向一致，
        前端展示「相关度」用 rerank_score 优先，退化到 vector_similarity。
        """
        sources: list[dict[str, Any]] = []
        for index, doc in enumerate(docs, start=1):
            metadata = doc.metadata
            sources.append({
                "index": index,
                "source": metadata.get("source", "未知来源"),
                # 片段摘要：接口不把全文吐出去，前端需要全文可以按 source 再查
                "snippet": doc.page_content[:200],
                "rerank_score": metadata.get("rerank_score"),
                "vector_similarity": metadata.get("vector_similarity"),
            })
        return sources

    # ------------------------------------------------------------------ #
    # 对外：同步问答
    # ------------------------------------------------------------------ #
    def query(self, question: str, session_id: str) -> dict[str, Any]:
        """
        执行一次完整问答。

        :param question: 用户问题
        :param session_id: 会话 id（多轮记忆的隔离键）
        :return: {
            answer, intent, route, intent_source, session_id,
            standalone_question, sources, elapsed_ms
        }
        :raises ValueError: 问题为空
        :raises Exception:  LLM 调用失败（接口层转成 502）
        """
        if not question or not question.strip():
            raise ValueError("问题不能为空")

        start = time.perf_counter()

        # ①②③④ 并行前段：意图识别 ∥ 投机检索 ∥ 问题重写（有历史时），见 _prepare_parallel
        chat_history = self.memory.get_messages(session_id)
        intent_result, prepared = self._prepare_parallel(question, chat_history)
        llm = self.llm_client.get_llm()
        usage = _usage_zero()

        if prepared is None:
            # 闲聊链：无检索、无资料，answer 之后同样写记忆
            message = llm.invoke(
                _chitchat_prompt.invoke({"input": question, "chat_history": chat_history})
            )
            answer = str(message.content)
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
                answer = str(message.content)
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
        exchange_meta = {
            "sources": result["sources"],
            "intent": result["intent"],
            "route": result["route"],
            "standalone_question": result["standalone_question"],
            "usage": dict(usage),
            "elapsed_ms": elapsed_ms,
            "ts": time.time(),
        }
        self.memory.add_exchange(session_id, question, answer, meta=exchange_meta)
        # token 用量：本次问答（重写 + 主回答）累加进会话
        self.memory.add_usage(
            session_id, usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"]
        )

        result["session_id"] = session_id
        result["elapsed_ms"] = elapsed_ms
        result["usage"] = usage
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
    def stream(self, question: str, session_id: str) -> Iterator[dict[str, Any]]:
        """
        流式问答：逐段产出，最后产出一次完成事件。

        产出序列（dict，接口层序列化为 NDJSON 一行一个）：
            {"type": "meta",  ...}      第一帧：意图/溯源/重写后的问题
            {"type": "chunk", "content": "..."}  × N：答案文本增量
            {"type": "done",  "elapsed_ms": ...} 最后一帧

        记忆在流结束后写回：必须等答案拼完整再写，
        半途写入会把「残缺答案」存进历史，污染下一轮。
        """
        if not question or not question.strip():
            raise ValueError("问题不能为空")

        start = time.perf_counter()
        chat_history = self.memory.get_messages(session_id)
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
            if not prepared["docs"]:
                # 零召回短路：与同步链路同口径，不进 LLM，拒答作为唯一 chunk 下发
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
        self.memory.add_exchange(session_id, question, answer, meta={
            "sources": (
                [] if prepared is None
                else self._extract_sources(prepared["docs"])
            ),
            "intent": intent_result.intent.value,
            "route": intent_result.route,
            "standalone_question": None if prepared is None else prepared["standalone_question"],
            "usage": dict(usage),
            "elapsed_ms": elapsed_ms,
            "ts": time.time(),
        })
        self.memory.add_usage(
            session_id, usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"]
        )
        session_usage = self.memory.get_usage(session_id)
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
