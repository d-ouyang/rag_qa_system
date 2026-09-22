"""
意图识别模块 —— 小模型做多类意图分类，加载/调用失败时降级为本地规则映射（打分制）。

--------------------------------------------------------------------------
为什么需要细粒度意图（而不只是「要不要检索」二分类）
--------------------------------------------------------------------------
路由（检索 or 不检索）只是意图的最基本用途。同一个知识库，
「怎么申请年假」需要分步骤回答、「A和B有什么区别」适合用对照表、
「服务报错了」需要 症状→原因→解决 动作路径 —— 不同意图对应不同的
回答组织方式。意图标签直接驱动 prompt 模板选择（见 core/rag_chain.py）。

意图体系（六类知识型 + 一类闲聊）：
    knowledge_query      知识查询   概念/定义/原理类问题
    operation_guide      操作指导   怎么做/流程/步骤类问题
    policy_consult       政策咨询   制度/规定/福利类问题
    comparison_analysis  对比分析   区别/优劣/差异类问题
    data_statistics      数据统计   数量/占比/趋势类问题
    troubleshooting      故障排查   报错/异常/失败类问题
    chitchat             闲聊寒暄   不需要检索，直接对话

路由规则：前六类统一走 RAG 检索链路（route="rag_qa"），chitchat 走闲聊链。
知识型意图内部的差别不改变「要检索」这个事实，只改变回答模板。

--------------------------------------------------------------------------
两级方案：小模型主力 + 规则兜底（面试常问）
--------------------------------------------------------------------------
· 纯规则：快、零成本，但覆盖不了千变万化的表达；
· 纯大模型：准，但多一次网络往返，且模型挂的时候分类也挂，链路全断。
工业界常见做法：小模型（本地 ollama 3B，输出一个词）做主力，
失败时本地规则兜底 —— 任何一层挂了服务都不中断，只是精度下降。

--------------------------------------------------------------------------
规则兜底的打分制（对比朴素关键词包含的改进）
--------------------------------------------------------------------------
朴素做法（关键词在不在句子里就算命中）有两个硬伤：
    ① 冲突无解：「政策执行失败怎么修复」同时命中政策/故障/操作三类，
      结果取决于字典遍历顺序 —— 随机的；
    ② 误报高：子串误命中（「方案」含「方」、「失败的原因分析」归属不清）。
本模块的打分制：
    score = 命中词数 × 100 + 类别优先级
    · 命中词数是主信号（「报错+无法启动」命中 2 个故障词，压倒只命中 1 个的其他类）；
    · 优先级是平手裁决：越「具体」的类别优先级越高
      （故障排查 60 > 对比分析 50 > 数据统计 40 > 政策咨询 30 > 操作指导 20 > 知识查询 10），
      「知识查询」最泛化，天然垫底 —— 只有别的类都不命中时才兜底；
    · 平手时最高分 wins，不再依赖字典顺序。

兜底默认意图是 knowledge_query 而不是 chitchat：
误把知识问题当闲聊 → 模型可能编造答案（危害大）；
误把闲聊当知识问题 → 只是多检索一次（危害小）。
兜底策略永远向「安全的一侧」倾斜。
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum

from config.settings import settings

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    """
    意图枚举（继承 str：JSON 序列化时直接变成字符串，接口层不用特殊处理）。

    value     —— 对外的标准标签（LLM 也输出这个）
    keywords  —— 规则兜底的信号词表
    priority  —— 打分平手时的裁决优先级（越大越具体）
    """

    KNOWLEDGE_QUERY = "knowledge_query"
    OPERATION_GUIDE = "operation_guide"
    POLICY_CONSULT = "policy_consult"
    COMPARISON_ANALYSIS = "comparison_analysis"
    DATA_STATISTICS = "data_statistics"
    TROUBLESHOOTING = "troubleshooting"
    CHITCHAT = "chitchat"

    @property
    def route(self) -> str:
        """路由目标：前六类走 RAG 检索链，chitchat 走闲聊链。"""
        return "chitchat" if self is Intent.CHITCHAT else "rag_qa"

    @property
    def is_rag(self) -> bool:
        return self is not Intent.CHITCHAT


# 类别元数据不放 enum 成员里（enum 成员只能是简单值），用外部字典管理。
# 顺序即打分平手时的优先级顺序（列表前面的优先级高，见 _CATEGORY_PRIORITY）。
INTENT_KEYWORDS: dict[Intent, tuple[str, ...]] = {
    Intent.TROUBLESHOOTING: (
        "报错", "错误", "失败", "异常", "故障", "无法", "不能", "崩溃",
        "闪退", "卡死", "修复", "解决", "排查", "报错信息",
    ),
    Intent.COMPARISON_ANALYSIS: (
        "对比", "区别", "比较", "差异", "优缺点", "优劣势", "哪个更",
        "哪个好", "不同", "vs",
    ),
    Intent.DATA_STATISTICS: (
        # 注意不放「数据」这种泛化词：它会子串命中「向量数据库」等知识型问题
        # （打分制防不了子串误报，只能靠词表规避高风险词）
        "统计", "数量", "占比", "比例", "排名", "排行",
        "趋势", "增长", "销售额", "销量", "月度", "季度", "年度",
    ),
    Intent.POLICY_CONSULT: (
        "政策", "规定", "制度", "条例", "标准", "要求", "补贴",
        "福利", "假期", "年假", "报销", "考勤", "薪酬", "入职", "离职",
    ),
    Intent.OPERATION_GUIDE: (
        "怎么", "如何", "步骤", "流程", "操作", "申请", "设置",
        "配置", "办理", "使用", "安装", "开通", "注册", "登录",
    ),
    Intent.KNOWLEDGE_QUERY: (
        "什么是", "是什么", "概念", "定义", "介绍", "含义",
        "原理", "说明", "解释", "是指",
    ),
}

# 打分平手裁决：值越大越「具体」，越应该赢
_CATEGORY_PRIORITY: dict[Intent, int] = {
    Intent.TROUBLESHOOTING: 60,
    Intent.COMPARISON_ANALYSIS: 50,
    Intent.DATA_STATISTICS: 40,
    Intent.POLICY_CONSULT: 30,
    Intent.OPERATION_GUIDE: 20,
    Intent.KNOWLEDGE_QUERY: 10,
}

# 知识型意图清单（供 LLM 提示词 / 链路展示用）
KNOWLEDGE_INTENTS: tuple[Intent, ...] = tuple(
    intent for intent in Intent if intent.is_rag
)

# 意图 -> 一句话语义说明（拼进小模型提示词，教它分类）
_INTENT_DESCRIPTIONS: dict[Intent, str] = {
    Intent.KNOWLEDGE_QUERY: "询问概念、定义、原理等知识（什么是X、X的含义）",
    Intent.OPERATION_GUIDE: "询问操作方法、步骤、流程（怎么做、如何申请）",
    Intent.POLICY_CONSULT: "询问公司制度、规定、福利政策（报销规定、年假政策）",
    Intent.COMPARISON_ANALYSIS: "要求比较、对比、分析差异（A和B的区别、哪个好）",
    Intent.DATA_STATISTICS: "询问数量、占比、排名、趋势等数据（销量多少、占比统计）",
    Intent.TROUBLESHOOTING: "描述故障、报错并求助（无法登录、执行失败怎么办）",
    Intent.CHITCHAT: "打招呼、寒暄、表示感谢或闲聊（你好、谢谢、再见）",
}


@dataclass
class IntentResult:
    """
    意图识别结果。

    · source 记录「这次判断是模型给的还是规则给的」—— 必须透传到接口层：
      排查「为什么这次回答没引用资料」时，第一眼看的就是意图和来源。
    · route 是路由结论（rag_qa / chitchat），链路和接口直接消费它，
      不用各自再写一遍 if/else。
    """

    intent: Intent
    source: str             # "llm" | "rule"
    route: str = ""         # "rag_qa" | "chitchat"（从 intent 推导，见 __post_init__）
    elapsed_ms: float = 0.0
    detail: str = ""        # 模型原始输出 / 命中的规则，供日志排查

    def __post_init__(self) -> None:
        if not self.route:
            self.route = self.intent.route


# 给小模型的分类提示词：只输出一个标签词，最大限度压缩输出空间
# （输出越长，3B 小模型越容易跑偏、越慢；一个词最稳）
def _build_intent_system_prompt() -> str:
    """把意图清单 + 每类一句话语义 + 输出约束拼成系统提示词。"""
    lines = [
        "你是意图分类器。判断用户消息属于以下哪一类，只回答标签本身，不要任何解释："
    ]
    for intent in Intent:
        lines.append(f"- {intent.value}：{_INTENT_DESCRIPTIONS[intent]}")
    lines.append(f"只输出以下标签之一：{' / '.join(i.value for i in Intent)}。")
    return "\n".join(lines)


_INTENT_SYSTEM_PROMPT = _build_intent_system_prompt()

# --------------------------------------------------------------------------- #
# 本地规则映射（小模型不可用时的兜底）
# --------------------------------------------------------------------------- #
# 寒暄类用「整句匹配」而不是「包含匹配」，
# 防止「你好，请问报销流程是什么」被误判成闲聊
_CHITCHAT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"^\s*(你好|您好|hi|hello|嗨|哈喽|喂|在吗|在么|早上好|晚上好|下午好)[!！~。\s]*$",
        r"^\s*(谢谢|感谢|多谢|辛苦了|thank\s*you|thanks)[!！~。\s]*$",
        r"^\s*(再见|拜拜|bye|回聊|下次聊)[!！~。\s]*$",
        r"^\s*(你是谁|你叫什么|你能做什么|你会什么)[?？\s]*$",
    ]
]


class IntentClassifier:
    """
    意图分类器：小模型优先，本地规则兜底（打分制）。

    线程安全：分类本身无状态，多线程可共享一个实例（单例见模块底部）。
    """

    def __init__(
        self,
        provider: str | None = None,
        model_name: str | None = None,
        timeout: int | None = None,
    ) -> None:
        """
        :param provider: 意图识别用的 provider，缺省读 settings.INTENT_LLM_PROVIDER
        :param model_name: 缺省读 settings.INTENT_LLM_MODEL_NAME（再空则用该 provider 的默认模型）
        :param timeout: 分类超时秒数，缺省读 settings.INTENT_LLM_TIMEOUT
        """
        self.provider: str = (provider or settings.INTENT_LLM_PROVIDER).strip().lower()
        self.model_name: str | None = (
            model_name or settings.INTENT_LLM_MODEL_NAME or None
        )
        self.timeout: int = int(timeout or settings.INTENT_LLM_TIMEOUT)

        # 预编译「意图 -> 信号词元组」为有序列表：打分时按固定顺序遍历，
        # 顺序不影响结果（得分是唯一的裁决依据），但固定顺序让日志可复现
        self._scored_categories: list[tuple[Intent, tuple[str, ...]]] = [
            (intent, words) for intent, words in INTENT_KEYWORDS.items()
        ]

        # 小模型客户端延迟构建：首次分类时才连接；
        # 构建/调用失败过一次就永久标记不可用，避免每次请求都重试拖慢接口
        self._llm_client = None
        self._llm_broken: bool = False
        self._build_lock = threading.Lock()
        self._last_llm_output: str = ""     # 最近一次模型原始输出（调试日志用）

        logger.info(
            "意图分类器初始化完成 | provider=%s model=%s timeout=%ds 意图数=%d",
            self.provider,
            self.model_name or "(provider 默认模型)",
            self.timeout,
            len(Intent),
        )

    # ------------------------------------------------------------------ #
    # 小模型分类
    # ------------------------------------------------------------------ #
    def _get_llm_client(self):
        """
        获取意图识别小模型客户端（延迟构建）。

        与主 LLM 分开一个客户端实例：分类任务参数完全不同
        （temperature=0 求稳定、max_tokens 只要几个、超时更短、失败不重试），
        复用主 LLM 客户端会把这些参数污染到主链路。
        """
        if self._llm_broken:
            return None
        if self._llm_client is not None:
            return self._llm_client
        with self._build_lock:
            if self._llm_client is not None:
                return self._llm_client
            try:
                from core.llm_client import get_llm_client

                self._llm_client = get_llm_client(
                    provider=self.provider,
                    model_name=self.model_name,
                    temperature=0,          # 分类要确定性，不要采样
                    max_tokens=16,          # 只需输出一个标签词
                    timeout=self.timeout,
                    max_retries=0,          # 分类失败直接降级，重试只会拖慢接口
                )
                logger.info("意图识别小模型客户端构建完成")
            except Exception as e:
                # 常见原因：ollama 服务没起、模型没拉取、配置错误
                logger.warning(
                    "意图识别小模型构建失败，将降级为规则映射：%s", e
                )
                self._llm_broken = True
                self._llm_client = None
        return self._llm_client

    def _classify_by_llm(self, question: str) -> Intent | None:
        """
        用小模型分类。

        :return: Intent；失败（连接/超时/输出非法）返回 None，交给规则兜底
        """
        client = self._get_llm_client()
        if client is None:
            return None
        try:
            raw = client.invoke(question, system_prompt=_INTENT_SYSTEM_PROMPT)
        except Exception as e:
            logger.warning("意图识别小模型调用失败，降级为规则映射：%s", e)
            return None

        # 归一化输出：小模型可能多带标点/解释/大小写问题，
        # 只要在输出里找到标签词就算命中；找不到视为非法输出，降级
        normalized = raw.strip().lower()
        self._last_llm_output = raw
        # 匹配顺序按优先级（具体的标签在前），防止输出里混入多个标签时误匹配
        for intent in sorted(Intent, key=lambda i: -_priority_of(i)):
            if intent.value in normalized:
                return intent
        logger.warning("意图识别小模型输出非法（%r），降级为规则映射", raw[:80])
        return None

    # ------------------------------------------------------------------ #
    # 规则兜底（打分制）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _score(question: str, words: tuple[str, ...]) -> int:
        """
        单个类别的得分：命中词数 × 100（优先级在 _classify_by_rule 里统一加）。

        命中数是主信号：一句话同时命中故障类 2 个词和操作类 1 个词，
        故障类胜出 —— 主信号比优先级裁决更有说服力。
        """
        return sum(1 for word in words if word in question)

    def _classify_by_rule(self, question: str) -> tuple[Intent, str]:
        """
        本地规则映射（零依赖、零延迟、永远可用）。

        :return: (意图, 判决说明)

        判断顺序：
            ① 寒暄整句匹配（整句都是寒暄才算，防止带问题的寒暄开头被误杀）
            ② 六类知识意图打分：命中数×100 + 优先级，最高分 wins
            ③ 全不命中 → knowledge_query（安全侧默认，见模块 docstring）
        """
        for pattern in _CHITCHAT_PATTERNS:
            if pattern.match(question):
                return Intent.CHITCHAT, f"命中寒暄规则：{pattern.pattern[:30]}"

        best_intent: Intent | None = None
        best_score = -1
        best_detail = ""
        for intent, words in self._scored_categories:
            hits = self._score(question, words)
            if hits <= 0:
                continue
            score = hits * 100 + _CATEGORY_PRIORITY[intent]
            if score > best_score:
                best_score = score
                best_intent = intent
                # 记录命中的词，日志里能直接看出「为什么判成这一类」
                best_detail = f"命中{hits}个信号词({score}分)"

        if best_intent is not None:
            return best_intent, best_detail

        # 默认安全侧：宁可多检索一次，也不让知识问题漏过检索
        return Intent.KNOWLEDGE_QUERY, "无信号词命中，默认 knowledge_query（安全侧）"

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def classify(self, question: str) -> IntentResult:
        """
        判断问题意图。

        :param question: 用户问题（空串直接按 chitchat 处理，不浪费模型调用）
        :return: IntentResult（intent + route + 判断来源 + 耗时）
        """
        if not question or not question.strip():
            return IntentResult(intent=Intent.CHITCHAT, source="rule", detail="空输入")

        start = time.perf_counter()
        intent = self._classify_by_llm(question)
        if intent is not None:
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "意图识别完成 | 来源=llm 意图=%s 路由=%s 耗时=%.0fms | query=%.24s",
                intent.value, intent.route, elapsed, question,
            )
            return IntentResult(
                intent=intent, source="llm",
                elapsed_ms=round(elapsed, 1),
                detail=self._last_llm_output[:80],
            )

        intent, rule_detail = self._classify_by_rule(question)
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "意图识别完成 | 来源=rule 意图=%s 路由=%s 耗时=%.0fms 规则=%s | query=%.24s",
            intent.value, intent.route, elapsed, rule_detail, question,
        )
        return IntentResult(
            intent=intent, source="rule",
            elapsed_ms=round(elapsed, 1), detail=rule_detail,
        )


def _priority_of(intent: Intent) -> int:
    """取类别优先级（chitchat 不参与打分，给 0 即可）。"""
    return _CATEGORY_PRIORITY.get(intent, 0)


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_intent_classifier: IntentClassifier | None = None
_intent_classifier_lock = threading.Lock()


def get_intent_classifier() -> IntentClassifier:
    """获取全局唯一的意图分类器（双检锁）。"""
    global _intent_classifier
    if _intent_classifier is None:
        with _intent_classifier_lock:
            if _intent_classifier is None:
                _intent_classifier = IntentClassifier()
                logger.debug("IntentClassifier 单例已创建")
    return _intent_classifier


def reset_intent_classifier() -> None:
    """重置单例（测试切换配置时用）。"""
    global _intent_classifier
    with _intent_classifier_lock:
        _intent_classifier = None
        logger.info("IntentClassifier 单例已重置")
