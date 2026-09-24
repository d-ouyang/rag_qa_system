"""
相同问题缓存 —— 规范化后的原问题逐字相同就复用上一次的知识库回答。

不看会话编号。近义句（「如何办理居住证」和「居住证流程是什么」）不在这里命中。

淘汰按最近使用：命中或新写入把这条的分数改成当前时间，池子超过上限时删分数最小的
（最久没被用到）。命中次数只记在值里，不参与淘汰。按次数淘汰会把上个月的热门题
一直留着，这周的新问题进不了池子。

Redis 合适：这份数据丢了，下次提问重新走模型即可。会话正文仍在 MySQL。
知识库一变（解析成功、删除、重灌）就清空池子并抬高版本号，避免答旧文。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

_SPACE_RE = re.compile(r"\s+")
_FULLWIDTH = str.maketrans({
    "？": "?",
    "！": "!",
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
    "（": "(",
    "）": ")",
    "　": " ",
})

# KEYS[1] = LRU 有序集合
# ARGV: prefix, member, score, max, payload
_PUT_LUA = """
local lru = KEYS[1]
local prefix = ARGV[1]
local member = ARGV[2]
local score = tonumber(ARGV[3])
local maxn = tonumber(ARGV[4])
local payload = ARGV[5]
redis.call('SET', prefix .. member, payload)
redis.call('ZADD', lru, score, member)
local n = redis.call('ZCARD', lru)
while n > maxn do
  local popped = redis.call('ZPOPMIN', lru)
  if popped[1] == false or popped[1] == nil then
    break
  end
  local old = popped[1]
  if old == member then
    redis.call('ZADD', lru, score, member)
    break
  end
  redis.call('DEL', prefix .. old)
  n = n - 1
end
return n
"""


_ANAPHORA_RE = re.compile(r"它|这个|那个|上面|刚才|前面|继续|呢$")


def normalize_question(text: str) -> str:
    """去空白、全角转半角、去掉末尾标点。不改动「吗 / 呢」，也不把近义句折成同一句。"""
    folded = text.strip().translate(_FULLWIDTH)
    folded = _SPACE_RE.sub(" ", folded)
    folded = folded.rstrip("?!.,;: ")
    return folded.casefold()


def is_standalone_question(text: str) -> bool:
    """
    原问题本身能不能当公共缓存键。

    「落户如何办理」这种完整问句可以，即使它出现在已有会话里。
    「它呢 / 上面那个 / 继续」依赖上文，写进公共池会把某一次的指代答案发给所有人。
    """
    normalized = normalize_question(text)
    if len(normalized) < 6:
        return False
    return _ANAPHORA_RE.search(normalized) is None


def _prefix() -> str:
    return (settings.REDIS_KEY_PREFIX or "rag").rstrip(":")


def _lru_key() -> str:
    return f"{_prefix()}:qa_cache:lru"


def _item_prefix() -> str:
    return f"{_prefix()}:qa_cache:item:"


def _version_key() -> str:
    return f"{_prefix()}:qa_cache:version"


def _question_id(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _client() -> Any:
    from core.redis_store import get_redis_client

    return get_redis_client()


def _decode(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def current_version(client: Any | None = None) -> int:
    """知识库版本。缺省写成 1；bump 在此基础上递增，旧条目对不上就不再命中。"""
    redis = client or _client()
    key = _version_key()
    raw = redis.get(key)
    if raw is None:
        redis.set(key, 1)
        return 1
    try:
        return int(_decode(raw))
    except ValueError:
        return 1


def lookup_answer(question: str, client: Any | None = None) -> dict[str, Any] | None:
    """
    命中则返回答案载荷，并把这条挪到最近使用的一端。
    版本对不上、Redis 不可用、缓存关闭，都返回 None，调用方走原链路。
    """
    if not settings.QA_CACHE_ENABLED:
        return None
    normalized = normalize_question(question)
    if not normalized:
        return None
    redis = client or _client()
    try:
        member = _question_id(normalized)
        raw = redis.get(_item_prefix() + member)
        if raw is None:
            return None
        payload = json.loads(_decode(raw))
        if int(payload.get("kb_version") or 0) != current_version(redis):
            return None
        payload["hit_count"] = int(payload.get("hit_count") or 0) + 1
        payload["last_hit_at"] = time.time()
        redis.set(_item_prefix() + member, json.dumps(payload, ensure_ascii=False))
        redis.zadd(_lru_key(), {member: time.time_ns()})
        logger.info("问答缓存命中 | hits=%s | q=%.24s", payload["hit_count"], normalized)
        return payload
    except Exception:
        logger.warning("读取问答缓存失败，改为走模型", exc_info=True)
        return None


def store_answer(
    question: str,
    answer: str,
    sources: list[dict[str, Any]],
    intent: str,
    route: str,
    standalone_question: str | None,
    client: Any | None = None,
) -> bool:
    """写入一条。超限时删最久没被用到的。失败只记日志，不影响这次回答。"""
    if not settings.QA_CACHE_ENABLED:
        return False
    normalized = normalize_question(question)
    if not normalized or not answer:
        return False
    redis = client or _client()
    try:
        version = current_version(redis)
        member = _question_id(normalized)
        payload = {
            "kb_version": version,
            "question": normalized,
            "answer": answer,
            "sources": sources,
            "intent": intent,
            "route": route,
            "standalone_question": standalone_question,
            "hit_count": 0,
            "created_at": time.time(),
        }
        redis.eval(
            _PUT_LUA,
            1,
            _lru_key(),
            _item_prefix(),
            member,
            str(time.time_ns()),
            str(int(settings.QA_CACHE_MAX_ENTRIES)),
            json.dumps(payload, ensure_ascii=False),
        )
        logger.info("问答缓存写入 | 上限=%s | q=%.24s", settings.QA_CACHE_MAX_ENTRIES, normalized)
        return True
    except Exception:
        logger.warning("写入问答缓存失败（本次回答仍有效）", exc_info=True)
        return False


def bump_kb_version(client: Any | None = None) -> int | None:
    """
    知识库变了：版本 +1，并清掉池子。
    旧答案即使文本相同也不能再吐出去。
    """
    if not settings.QA_CACHE_ENABLED:
        return None
    redis = client or _client()
    try:
        version = int(redis.incr(_version_key()))
        members = redis.zrange(_lru_key(), 0, -1)
        pipe = redis.pipeline()
        for member in members:
            pipe.delete(_item_prefix() + _decode(member))
        pipe.delete(_lru_key())
        pipe.execute()
        logger.info("知识库版本已更新，问答缓存已清空 | version=%s 条数=%s", version, len(members))
        return version
    except Exception:
        logger.warning("更新知识库版本失败，缓存可能短暂保留旧答案", exc_info=True)
        return None
