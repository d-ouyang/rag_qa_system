"""
可热更新业务标志的**注册表**（v4.0.0 P4-2，PLAN-v4.0.0.md）。

--------------------------------------------------------------------------
它管什么、不管什么
--------------------------------------------------------------------------
P4-1 的 ConfigService 是「机制」（存取/校验/TTL），本模块是「清单」——
哪些配置项允许通过管理端改、每个项的类型/边界/归属卡片/默认档，全在这里登记。
**没进注册表的 key，管理端一律 404**——「配置能改什么」必须是白名单，
不能是「ConfigService 里有什么就能改什么」。

默认档（`.env`/代码那一档）用 ``default`` 惰性求值而不是启动时定值：
测试会就地翻 ``settings.XXX``（module23 的既有手法），活读才能跟住。

--------------------------------------------------------------------------
读取点的约定
--------------------------------------------------------------------------
业务代码取值**必须**走 ``flag_value(key)``，不许自己拼
``config_service.get_flag(key, settings.XXX)`` —— 后者绕过了注册表，
注册表里改了边界/描述，散落的读取点不会跟着变。

v1 明确**不进**注册表的（保持重启语义，见 PLAN-v4.0.0 §1 Q4）：
连接串/密钥/端口/CORS（基础设施）；LLM 模型名（P4-3）；CHUNK_MODE 与
嵌入模型（改了=重建索引，要走显式的重建任务而不是拨开关）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from config.settings import settings
from core.config_service import ConfigError, config_service, _type_of


@dataclass(frozen=True)
class FlagSpec:
    """一个可热更新配置项的完整契约。"""

    key: str
    value_type: str              # "bool" | "int" | "float"
    card: str                    # 前端设置页的卡片分组名
    label: str                   # 中文名（UI 展示）
    description: str             # 一句话说明（UI 展示；写清改了会影响什么）
    default: Callable[[], Any]   # 默认档（惰性：活读 settings，跟住测试与 .env）
    min_value: float | None = None   # 数值型的下界（bool 型忽略）
    max_value: float | None = None   # 数值型的上界（bool 型忽略）


#: 注册表。**新增强序**：key 字典序，便于人工扫与测试断言稳定。
FLAG_REGISTRY: dict[str, FlagSpec] = {
    spec.key: spec
    for spec in (
        FlagSpec(
            key="doc_download_enabled",
            value_type="bool",
            card="知识库与文档",
            label="原件下载",
            description="关闭后下载接口直接 404（功能不存在而非无权限），前端按钮不渲染；真实收益是保问答流式带宽",
            default=lambda: settings.DOC_DOWNLOAD_ENABLED,
        ),
        FlagSpec(
            key="groundedness_check",
            value_type="bool",
            card="问答增强",
            label="答案依据校验",
            description="回答生成后用模型比对「答案论断 vs 检索资料」，无依据的回答挂警示标记（q041 幻觉治理）；判定在答案输出之后，只增标记不改答案",
            default=lambda: settings.GROUNDEDNESS_ENABLED,
        ),
        FlagSpec(
            key="memory_max_turns",
            value_type="int",
            card="会话记忆",
            label="记忆轮数",
            description="送进模型与问题改写的最近对话轮数；只限模型窗口，不删库里的历史",
            default=lambda: settings.MEMORY_MAX_TURNS,
            min_value=1,
            max_value=20,
        ),
        FlagSpec(
            key="qa_cache_enabled",
            value_type="bool",
            card="问答缓存",
            label="相同问题缓存",
            description="规范化后逐字相同的问题直接返回缓存答案；Redis 丢了只是多调一次模型，不影响正确性",
            default=lambda: settings.QA_CACHE_ENABLED,
        ),
        FlagSpec(
            key="query_hyde",
            value_type="bool",
            card="问答增强",
            label="HyDE 假设答案召回",
            description="改写后再生成一段假设答案、用它的向量去召回（BM25 仍用真实问句）；单独开启会自动先走改写",
            default=lambda: settings.QUERY_HYDE,
        ),
        FlagSpec(
            key="query_rewrite",
            value_type="bool",
            card="问答增强",
            label="单轮检索前改写",
            description="单轮提问也先经 LLM 把口语规整成检索友好句再检索（多轮指代改写始终生效，与此开关无关）",
            default=lambda: settings.QUERY_REWRITE,
        ),
        FlagSpec(
            key="rerank_enabled",
            value_type="bool",
            card="检索与重排",
            label="重排裁决",
            description="关闭后按向量相似度直接截断，不做精排。生产走硅基流动可即时热开；本地权重后端热开需重启",
            default=lambda: settings.USE_RERANKER,
        ),
        FlagSpec(
            key="rerank_score_threshold",
            value_type="float",
            card="检索与重排",
            label="重排分数阈值",
            description="重排分低于阈值的相关片直接丢弃；0 = 关闭过滤。基线实测相关≈0.99、无关≈0.0003，0.1 是保守分界",
            default=lambda: (
                settings.RERANK_SCORE_THRESHOLD
                if settings.RERANK_SCORE_THRESHOLD is not None
                else 0.1
            ),
            min_value=0.0,
            max_value=1.0,
        ),
        FlagSpec(
            key="search_top_k",
            value_type="int",
            card="检索与重排",
            label="召回条数 top_k",
            description="最终送进问答链的切片条数（候选池会按倍数自动放大）；调大费 token、调小丢上下文",
            default=lambda: settings.SEARCH_TOP_K,
            min_value=1,
            max_value=20,
        ),
        FlagSpec(
            key="session_summary",
            value_type="bool",
            card="会话记忆",
            label="长会话滚动摘要",
            description="窗口之外的对话由模型压缩成摘要后随上下文带下去（长会话跨轮记忆）；摘要失败不影响问答",
            default=lambda: settings.SESSION_SUMMARY_ENABLED,
        ),
    )
}


def flag_value(key: str) -> Any:
    """读取一个**注册**标志的当前生效值（DB > `.env`/代码默认档）。

    未注册的 key 直接 KeyError——那是代码 bug（读取点忘了登记），不是运行时错误，
    让它炸出来比静默回落好。
    """
    spec = FLAG_REGISTRY[key]
    return config_service.get_flag(key, spec.default())


def validate_flag_value(key: str, value: Any) -> Any:
    """PATCH 入口的类型与边界校验。非法抛 ConfigError（路由层转 422）。"""
    spec = FLAG_REGISTRY.get(key)
    if spec is None:
        raise ConfigError(f"未登记的配置项：{key!r}")
    vtype = _type_of(value)
    if vtype != spec.value_type:
        raise ConfigError(
            f"配置 {key!r} 期望类型 {spec.value_type}，收到 {vtype}（值 {value!r}）"
        )
    if spec.value_type in ("int", "float"):
        if spec.min_value is not None and value < spec.min_value:
            raise ConfigError(f"配置 {key!r} 不能小于 {spec.min_value:g}（收到 {value!r}）")
        if spec.max_value is not None and value > spec.max_value:
            raise ConfigError(f"配置 {key!r} 不能大于 {spec.max_value:g}（收到 {value!r}）")
    return value


def describe_flags() -> list[dict[str, Any]]:
    """GET /admin/config 的数据源：注册表全量 + 当前生效值。

    只含注册表里的标量开关——密钥/连接串这类敏感项**结构性**进不来
    （它们根本不在注册表里），不靠「记得别返回」。
    """
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "card": spec.card,
            "value_type": spec.value_type,
            "description": spec.description,
            "value": config_service.get_flag(spec.key, spec.default()),
            "default": spec.default(),
            "min": spec.min_value,
            "max": spec.max_value,
        }
        for spec in FLAG_REGISTRY.values()
    ]
