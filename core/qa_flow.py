"""问答流程状态机（QaFlowMachine）—— 把隐式分支收口成**可测的决策点**。

--------------------------------------------------------------------------
为什么它存在（P4-5 二期）
--------------------------------------------------------------------------
一次问答的分支逻辑原本**散在** `rag_chain.query()` / `stream()` /
`_finish_cached()` 里：缓存查没查到、prepared 是不是 None、docs 是不是空 ——
三处各写一份三元式，靠 module26 的端到端断言间接守着。

「散着写」的代价在 P4-5 一期就露过头：query() 和 stream() 各算一遍
`decision`，两处写法必须逐字一致才不漂。本模块把「这一轮走到哪个终端状态、
沿途经过哪些状态」收口成**纯函数**：

    · `resolve_decision()`  —— 三个非缓存终端状态的唯一判定处；
    · `flow_path()`         —— 给定终端状态，返回按序经过的状态名列表。

判定不查库、不起模型，输入输出都是值 —— module27 直接穷举断言。
溯源页（admin-console「问答溯源」）的路径高亮也从 `flow_path()` 派生：
**后端算好下发**，前端只负责把命中的节点点亮，不自己再推一遍分支。

--------------------------------------------------------------------------
状态清单与真实链路的对应
--------------------------------------------------------------------------
    received      收到问题（入口，每条路径都有）
    cache_check   查问答缓存（规范化原问题逐字匹配，见 core/qa_cache.py）
    cache_hit     【终端】命中缓存，直接复用答案 —— 不检索、不调模型
    prepare       并行前段：意图识别 ∥ 投机检索 ∥ 问题改写
    intent_route  意图路由（7 类意图 → rag_qa / chitchat 两条路）
    chitchat      【终端】闲聊：不检索、无资料，走闲聊链
    retrieve      检索：向量(+HyDE) + 可选 BM25/RRF 混合
    rerank        重排（CrossEncoder）
    threshold     阈值过滤
    zero_recall   【终端】过滤后 0 条 → 固定拒答，不进 LLM
    generate      RAG 生成
    rag_qa        【终端】正常检索 + 生成

刻意**不**把 HyDE / BM25 混合做成独立状态：它们是 retrieve 节点内部的
召回手段，不是分支——开了 HyDE 的 retrieve 和没开的 retrieve 走的是
同一条路。溯源页从 trace 的 `hyde_used` / `bm25_hits` 字段给 retrieve
节点挂徽章，路径本身不变。
"""

from __future__ import annotations

from enum import Enum


class QaFlowState(str, Enum):
    """问答流程的状态。四个 `*_hit/_recall/_qa/chitchat` 是**终端状态**。"""

    RECEIVED = "received"
    CACHE_CHECK = "cache_check"
    CACHE_HIT = "cache_hit"
    PREPARE = "prepare"
    INTENT_ROUTE = "intent_route"
    CHITCHAT = "chitchat"
    RETRIEVE = "retrieve"
    RERANK = "rerank"
    THRESHOLD = "threshold"
    ZERO_RECALL = "zero_recall"
    GENERATE = "generate"
    RAG_QA = "rag_qa"


#: 终端状态。一轮问答**恰好**落在其中一个上；qa_trace.decision 只取这里的值。
TERMINAL_STATES: frozenset[str] = frozenset({
    QaFlowState.CACHE_HIT.value,
    QaFlowState.CHITCHAT.value,
    QaFlowState.ZERO_RECALL.value,
    QaFlowState.RAG_QA.value,
})

#: 状态 → 中文短名。溯源页与日志都用这份，**不另写第三份清单**。
STATE_LABELS: dict[str, str] = {
    QaFlowState.RECEIVED.value: "收到问题",
    QaFlowState.CACHE_CHECK.value: "查问答缓存",
    QaFlowState.CACHE_HIT.value: "缓存命中",
    QaFlowState.PREPARE.value: "并行前段",
    QaFlowState.INTENT_ROUTE.value: "意图路由",
    QaFlowState.CHITCHAT.value: "闲聊应答",
    QaFlowState.RETRIEVE.value: "检索召回",
    QaFlowState.RERANK.value: "重排",
    QaFlowState.THRESHOLD.value: "阈值过滤",
    QaFlowState.ZERO_RECALL.value: "零召回拒答",
    QaFlowState.GENERATE.value: "RAG 生成",
    QaFlowState.RAG_QA.value: "检索作答",
}

#: 公共前缀：无论走哪条路，都是「收到 → 查缓存」开头。
_FRONT = [QaFlowState.RECEIVED.value, QaFlowState.CACHE_CHECK.value]

#: 公共检索段：非缓存、非闲聊的路径都要走「并行前段 → 路由 → 检索 → 重排 → 阈值」。
_RETRIEVAL = [
    QaFlowState.PREPARE.value,
    QaFlowState.INTENT_ROUTE.value,
    QaFlowState.RETRIEVE.value,
    QaFlowState.RERANK.value,
    QaFlowState.THRESHOLD.value,
]

_PATHS: dict[str, list[str]] = {
    QaFlowState.CACHE_HIT.value: _FRONT + [QaFlowState.CACHE_HIT.value],
    QaFlowState.CHITCHAT.value: _FRONT + _RETRIEVAL[:2] + [QaFlowState.CHITCHAT.value],
    QaFlowState.ZERO_RECALL.value: _FRONT + _RETRIEVAL + [QaFlowState.ZERO_RECALL.value],
    QaFlowState.RAG_QA.value: (
        _FRONT + _RETRIEVAL
        + [QaFlowState.GENERATE.value, QaFlowState.RAG_QA.value]
    ),
}


def resolve_decision(*, prepared_present: bool, docs_count: int) -> str:
    """
    三个**非缓存**终端状态的唯一判定处（cache_hit 由缓存查询直接决定，不进这里）。

    与 rag_chain 里原来的三元式逐字等价；抽出来是为了让 module27 能
    穷举断言，而不是靠端到端测试间接覆盖。

    :param prepared_present: `_prepare_parallel` 是否返回了检索准备（None = 闲聊）
    :param docs_count: 阈值过滤后的文档数（prepared 不存在时传 0）
    """
    if not prepared_present:
        return QaFlowState.CHITCHAT.value
    if docs_count <= 0:
        return QaFlowState.ZERO_RECALL.value
    return QaFlowState.RAG_QA.value


def flow_path(decision: str) -> list[str]:
    """
    给定终端状态，返回**按序经过**的状态名列表（含终端自身）。

    溯源页拿它高亮实走路径；未知终端状态抛 ValueError —— 决策点的
    拼错应该在写入侧就炸，而不是在页面上静默画一条空路径。
    """
    path = _PATHS.get(decision)
    if path is None:
        raise ValueError(
            f"未知的问答终端状态「{decision}」；合法值：{sorted(TERMINAL_STATES)}"
        )
    return list(path)
