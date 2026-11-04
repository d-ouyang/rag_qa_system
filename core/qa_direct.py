"""
QA 问答集直击（PLAN-v4.0.0 优化 2，v4.0.0-p4.14）。

--------------------------------------------------------------------------
它解决什么
--------------------------------------------------------------------------
制度类知识库的提问高度收敛（考勤/请假/报销/薪酬占大头），其中很大一部分
可以离线策划成标准问答。用户问题**直接命中** QA 集条目时，返回策划好的
标准答案——跳过改写、检索、生成全程，**一次大模型调用都没有**，延迟与
幻觉风险同时归零。

与 qa_cache 的区别（防止混为一谈）：
    · qa_cache 缓存「线上真实发生过的问答」——运行时产物，TTL 失效，省 token；
    · QA 集是「离线策划的标准问答」——运营资产，人工审核、随版本走，
      既省 token 又保证 Top 问题的答案质量恒定。

--------------------------------------------------------------------------
匹配分级（宁缺毋滥）
--------------------------------------------------------------------------
    ① 规范化精确匹配：复用 qa_cache.normalize_question 口径，扫
       question + alt_questions；零成本、零误击；
    ② embedding 相似度：①未中才算，query 与全部条目问句算余弦相似度，
       ≥ SEMANTIC_THRESHOLD（0.92，由 golden 评测校准）才算直击。
       只做这两级，不做模糊检索排序——直击的价值在「确定对」，
       相似度不够高就该走正常检索链。

--------------------------------------------------------------------------
fail-open 边界（直击坏了的症状是「多走一次检索」，不是「问不了」）
--------------------------------------------------------------------------
文件缺失/字段损坏、embedding 调用失败、开关关闭——一律返回 None 落回
正常链路，只留 WARNING 日志。直击是优化路径，绝不能变成新的故障点。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import settings
from core.config_flags import flag_value
from core.qa_cache import is_standalone_question, normalize_question

logger = logging.getLogger(__name__)

#: 语义直击阈值：bge 系同义改写对通常 >0.95，无关句 <0.5；0.92 由 golden 评测校准
SEMANTIC_THRESHOLD = 0.92


@dataclass(frozen=True)
class QaEntry:
    """QA 集条目。字段约定见 docs/PLAN-v4.0.0.md 优化日程一节。"""

    id: str
    question: str
    answer: str
    alt_questions: tuple[str, ...] = ()
    source_doc: str = ""
    source_section: str = ""
    tags: tuple[str, ...] = field(default=())


def load_qa_entries(path: str | Path) -> list[QaEntry]:
    """
    从 JSONL 文件加载 QA 集。文件不存在或整批损坏抛异常（由调用方 fail-open）。

    单行损坏跳过并 WARNING——一条坏数据不该废掉整个直击层；
    但一条行合法却缺 question/answer 视为文件级问题，抛 ValueError。
    """
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"QA 集文件不存在：{file}")
    entries: list[QaEntry] = []
    for line_no, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("QA 集第 %d 行 JSON 损坏，跳过：%s", line_no, e)
            continue
        if not obj.get("question") or not obj.get("answer"):
            raise ValueError(f"QA 集第 {line_no} 行缺 question/answer：{raw[:80]}")
        entries.append(
            QaEntry(
                id=str(obj.get("id") or f"qa{line_no:03d}"),
                question=str(obj["question"]),
                answer=str(obj["answer"]),
                alt_questions=tuple(str(a) for a in obj.get("alt_questions") or ()),
                source_doc=str(obj.get("source_doc") or ""),
                source_section=str(obj.get("source_section") or ""),
                tags=tuple(str(t) for t in obj.get("tags") or ()),
            )
        )
    if not entries:
        raise ValueError(f"QA 集为空：{file}")
    return entries


def _cosine(a: list[float], b: list[float]) -> float:
    """纯 python 余弦（48 条 × 维度千级，一次点积微秒级，不值得引 numpy 依赖）。"""
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


class QaDirectIndex:
    """
    QA 直击索引：精确层（规范化 dict）+ 语义层（惰性向量表）。

    线程安全：精确层构建后不可变；语义向量惰性计算一次（双检锁）。
    embeddings 可注入（测试打桩用，LangChain Embeddings 接口：embed_documents /
    embed_query），缺省用全局嵌入模型 get_embedding_model()。
    """

    def __init__(
        self,
        entries: list[QaEntry],
        embeddings=None,
    ) -> None:
        self.entries = entries
        self._embeddings = embeddings
        # 精确层：规范化问句 → (条目, 命中的原问句)。标准问法与同义问法同权重。
        self._exact: dict[str, tuple[QaEntry, str]] = {}
        for e in entries:
            for text in (e.question, *e.alt_questions):
                norm = normalize_question(text)
                if norm and norm not in self._exact:
                    self._exact[norm] = (e, text)
        # 语义层：惰性计算 (条目, 问句) 列表的向量
        self._semantic_items: list[tuple[QaEntry, str]] = [
            (e, text) for e in entries for text in (e.question, *e.alt_questions)
        ]
        self._semantic_vectors: list[list[float]] | None = None
        self._semantic_lock = threading.Lock()

    def _get_embeddings(self):
        if self._embeddings is None:
            from core.embedding import get_embedding_model

            self._embeddings = get_embedding_model()
        return self._embeddings

    # ------------------------------------------------------------------ #
    # 精确层
    # ------------------------------------------------------------------ #
    def lookup_exact(self, question: str) -> tuple[QaEntry, str] | None:
        """规范化精确命中。零误击——命中即返回（条目, 命中的问句原文）。"""
        return self._exact.get(normalize_question(question))

    # ------------------------------------------------------------------ #
    # 语义层
    # ------------------------------------------------------------------ #
    def _ensure_semantic_vectors(self) -> bool:
        """惰性计算全部问句向量。成功返回 True；嵌入模型不可用返回 False（只试一次不反复重试）。"""
        if self._semantic_vectors is not None:
            return True
        with self._semantic_lock:
            if self._semantic_vectors is not None:
                return True
            try:
                texts = [text for _, text in self._semantic_items]
                started = time.perf_counter()
                vectors = self._get_embeddings().embed_documents(texts)
                if len(vectors) != len(texts):
                    raise RuntimeError(
                        f"嵌入返回数量不符：期望 {len(texts)} 实得 {len(vectors)}"
                    )
                self._semantic_vectors = [list(map(float, v)) for v in vectors]
                logger.info(
                    "QA 直击语义层就绪 | 条目问句=%d 耗时=%.0fms",
                    len(texts),
                    (time.perf_counter() - started) * 1000,
                )
                return True
            except Exception as e:
                logger.warning("QA 直击语义向量计算失败，语义层停用（精确层不受影响）：%s", e)
                self._semantic_vectors = []
                return False

    def lookup_semantic(
        self, question: str, threshold: float = SEMANTIC_THRESHOLD
    ) -> tuple[QaEntry, float] | None:
        """语义直击：query 向量与全部条目问句算余弦，取最高分；≥ 阈值才命中。失败返回 None。"""
        if not self._ensure_semantic_vectors():
            return None
        vectors = self._semantic_vectors
        if not vectors:
            return None
        try:
            qvec = list(map(float, self._get_embeddings().embed_query(question)))
        except Exception as e:
            logger.warning("QA 直击 query 嵌入失败，本次走正常检索：%s", e)
            return None
        best_idx, best_score = -1, 0.0
        for idx, vec in enumerate(vectors):
            score = _cosine(qvec, vec)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx < 0 or best_score < threshold:
            return None
        entry, _text = self._semantic_items[best_idx]
        return entry, best_score


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_qa_index: QaDirectIndex | None = None
_qa_index_lock = threading.Lock()
_qa_index_broken: bool = False


def get_qa_index() -> QaDirectIndex | None:
    """全局唯一 QA 索引（双检锁）。文件缺失/损坏只报一次，之后直接 None（fail-open）。"""
    global _qa_index, _qa_index_broken
    if _qa_index_broken:
        return None
    if _qa_index is None:
        with _qa_index_lock:
            if _qa_index is None:
                try:
                    entries = load_qa_entries(settings.QA_SET_PATH)
                    _qa_index = QaDirectIndex(entries)
                    logger.info("QA 直击索引就绪 | 条目=%d 文件=%s", len(entries), settings.QA_SET_PATH)
                except Exception as e:
                    logger.warning("QA 直击索引构建失败，直击停用（问答链路不受影响）：%s", e)
                    _qa_index_broken = True
    return _qa_index


def reset_qa_index() -> None:
    """重置单例（测试与 QA 集热更后调用）。"""
    global _qa_index, _qa_index_broken
    with _qa_index_lock:
        _qa_index = None
        _qa_index_broken = False


# --------------------------------------------------------------------------- #
# 对外入口：rag_chain 在缓存未命中后调用这一个函数即可
# --------------------------------------------------------------------------- #
def try_qa_direct_hit(question: str) -> dict[str, Any] | None:
    """
    尝试 QA 直击。命中返回 {entry, via, score}；未命中/开关关/依赖问句/任何失败返回 None。

    门控顺序（先便宜后贵、先安全后激进）：
        ① 热开关 qa_direct_hit（缺省关，fail-closed）；
        ② 精确层 —— 逐字命中人工策划的完整问句，置信最高，先放行
           （不受 qa_cache 的「<6 字」公共缓存启发式误伤，「婚假多少天」可直击）；
        ③ 独立问句门控 is_standalone_question —— 只约束语义层，
           避免「那婚假呢」这类短指代句被向量相似度误命中；
        ④ 语义层。
    """
    if not flag_value("qa_direct_hit"):
        return None
    index = get_qa_index()
    if index is None:
        return None
    hit = index.lookup_exact(question)
    if hit is not None:
        entry, matched = hit
        logger.info("QA 直击命中 | 层=exact 条目=%s 问句=%.24s", entry.id, matched)
        return {"entry": entry, "via": "exact", "score": 1.0}
    if not is_standalone_question(question):
        return None
    hit = index.lookup_semantic(question)
    if hit is not None:
        entry, score = hit
        logger.info("QA 直击命中 | 层=semantic 条目=%s 分数=%.3f", entry.id, score)
        return {"entry": entry, "via": "semantic", "score": round(score, 4)}
    return None
