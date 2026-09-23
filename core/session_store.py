"""
会话存储抽象层 —— 把「会话数据存哪」从「会话逻辑怎么算」里拆出来。

--------------------------------------------------------------------------
为什么要做这层抽象
--------------------------------------------------------------------------
v1.0.0 的 MemoryManager 把两件事揉在一个类里：
    ① 会话逻辑：轮数裁剪、元数据与消息对齐、编辑重发的截断语义；
    ② 存储实现：进程内 dict + threading.Lock。
这导致「换 Redis」必须动逻辑代码，而逻辑代码是经过测试、语义微妙的部分
（比如截断点必须向下取偶），改动风险高。

拆开之后：
    · 逻辑层（memory_manager.MemoryManager）只管算，不懂 Redis；
    · 存储层（本模块 / redis_store.py）只管存取，不懂对话语义。
换后端 = 换一个 Store 实现 + 一行配置，逻辑层与调用方零改动。

--------------------------------------------------------------------------
为什么接口是「整条会话快照」而不是「单条消息 CRUD」
--------------------------------------------------------------------------
会话的读写模式是「一轮问答 = 读一次全部历史 + 写一次全部历史」：
    · 逻辑上天然以 session 为单位，没有「单独改第 3 条消息」的场景；
    · 存整条快照 → 一次 HSET 写完，天然原子，不会出现「消息写进去了、
      元数据没写进去」的半截状态；
    · 反而避免了「每条消息一个 key」带来的一堆小 key（Redis 里小 key 多了
      内存碎片和 RTT 都很难看：10 轮对话 = 21 次往返 vs 1 次）。

代价是每次写要序列化整条会话。控制手段是 MEMORY_MAX_TURNS（限制轮数）
和 MEMORY_MAX_SESSION_BYTES（限制单会话字节数），见 config/settings.py。

--------------------------------------------------------------------------
快照的数据结构（也是落 Redis 的 JSON 结构）
--------------------------------------------------------------------------
    messages      [{"role": "user"|"assistant", "content": "..."}]  按时间正序
    exchange_meta [{"sources": [...], "intent": "...", ...}]        与 messages 按轮对齐
    session_meta  {"pinned": bool, "title": str}                    会话管理元数据
    usage         {"input_tokens": 0, ...}                          会话累计 token 用量
    last_active   float                                             最后活跃 Unix 时间戳
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from config.settings import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 快照数据结构
# --------------------------------------------------------------------------- #
@dataclass
class SessionSnapshot:
    """
    一个会话的完整状态（存储层与逻辑层之间的唯一交换格式）。

    为什么用 dataclass 而不是直接传 dict：
    字段名写错在 dict 里是「静默返回 None」，在 dataclass 里是 AttributeError，
    后者在开发期就能暴露，前者要等到线上发现「历史里所有意图都空了」才发现。
    """

    messages: list[dict[str, str]] = field(default_factory=list)
    exchange_meta: list[dict[str, Any]] = field(default_factory=list)
    session_meta: dict[str, Any] = field(default_factory=lambda: {"pinned": False})
    usage: dict[str, int] = field(default_factory=dict)
    last_active: float = field(default_factory=time.time)

    # -------- 序列化 -------- #
    def to_json_map(self) -> dict[str, str]:
        """
        转成 Redis hash 的 field → value 映射（每个 field 一段 JSON 字符串）。

        为什么按 field 拆开而不是整条 JSON 塞一个 field：
        · Redis hash 支持单 field 读取，将来要做「只取用量」「只取置顶标记」的
          轻量查询时不用反序列化整条会话；
        · 单 field 更新不需要读改写整条。
        """
        return {
            "messages": json.dumps(self.messages, ensure_ascii=False),
            "exchange_meta": json.dumps(self.exchange_meta, ensure_ascii=False),
            "session_meta": json.dumps(self.session_meta, ensure_ascii=False),
            "usage": json.dumps(self.usage, ensure_ascii=False),
            "last_active": repr(float(self.last_active)),
        }

    @classmethod
    def from_json_map(cls, raw: dict[Any, Any]) -> "SessionSnapshot":
        """
        从 Redis hash 还原快照。

        容错原则：**单个 field 坏掉不能让整条会话消失**。
        用户宁可丢「置顶标记」也不该丢「全部历史」，所以每个 field 单独 try，
        解析失败就用默认值 + WARN 日志。redis-py 返回的 key/value 是 bytes，
        这里统一 decode（也兼容传入 str 的情况，fakeredis 会返回 str）。
        """

        def _decode(v: Any) -> str:
            return v.decode("utf-8") if isinstance(v, bytes) else str(v)

        def _load(name: str, default: Any) -> Any:
            raw_value = raw.get(name) or raw.get(name.encode("utf-8"))
            if raw_value is None:
                return default
            try:
                return json.loads(_decode(raw_value))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning("会话字段解析失败，使用默认值 | field=%s err=%s", name, e)
                return default

        last_active_raw = raw.get("last_active") or raw.get(b"last_active")
        try:
            last_active = float(_decode(last_active_raw)) if last_active_raw is not None else time.time()
        except (TypeError, ValueError):
            last_active = time.time()

        return cls(
            messages=_load("messages", []),
            exchange_meta=_load("exchange_meta", []),
            session_meta=_load("session_meta", {"pinned": False}),
            usage=_load("usage", {}),
            last_active=last_active,
        )

    def size_bytes(self) -> int:
        """估算序列化后的体积（写入前的自检用，不追求精确到字节）。"""
        return sum(len(v.encode("utf-8")) for v in self.to_json_map().values())


# --------------------------------------------------------------------------- #
# 抽象接口
# --------------------------------------------------------------------------- #
class SessionStore(ABC):
    """
    会话存储接口。

    实现约定（两个实现都必须满足，测试里也是按这套断言）：
    1. load 返回 None 表示「不存在**或**已过期」——调用方无需区分，语义都是「开新会话」；
    2. load 命中即视为「一次活跃」，需要刷新 TTL（与 v1.0.0 内存版语义一致）；
    3. save 是整条覆盖写，且刷新 TTL；不存在的会话会被创建；
    4. delete 幂等：删不存在的会话不报错，返回值表示「是否真的删掉了」；
    5. list_ids 按最后活跃时间倒序（前端会话列表直接用这个顺序）。
    """

    #: 后端名字，用于日志和健康检查展示（memory / redis）
    name: str = "unknown"

    @abstractmethod
    def load(self, session_id: str, touch: bool = True) -> SessionSnapshot | None:
        """
        读取会话快照；不存在或已过期返回 None。

        :param touch: 是否把本次读取记为「活跃」（刷新 TTL / last_active）。
                      会话列表、统计这类「扫一眼」的读必须传 False，
                      否则频繁刷新列表就等于给所有会话无限续命。
        """

    @abstractmethod
    def save(self, session_id: str, snapshot: SessionSnapshot) -> None:
        """整条覆盖写入并刷新 TTL。"""

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """删除会话；返回是否真实删除（不存在返回 False）。"""

    @abstractmethod
    def exists(self, session_id: str) -> bool:
        """会话是否存在且未过期。"""

    @abstractmethod
    def list_ids(self) -> list[str]:
        """列出全部会话 id，按最后活跃时间倒序。"""

    @abstractmethod
    def purge_expired(self) -> int:
        """主动清理过期会话（内存版需要；Redis 版清索引残留），返回清理数量。"""

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        """存储层统计（供健康检查 / 内存预警接口展示）。"""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """连通性健康检查（不抛异常，失败信息放返回值里）。"""

    def close(self) -> None:  # pragma: no cover - 默认无资源可释放
        """释放底层资源（连接池等）。内存版是空实现。"""

    def write_allowed(self) -> tuple[bool, str]:
        """
        当前是否允许写入。返回 (是否允许, 不允许的原因)。

        为什么把「能不能写」做成存储层能力而不是全局开关：
        容量保护是存储层的知识（Redis 才知道自己还剩多少内存、
        内存版根本不关心这个）。逻辑层只问「现在能写吗」，不关心为什么。
        默认实现永远允许 —— 内存版不存在「存储满了还硬写」的问题。
        """
        return True, ""

    @abstractmethod
    @contextmanager
    def session_lock(self, session_id: str) -> Iterator[None]:
        """
        会话级锁：保证「读改写」三步对同一会话串行。

        为什么必须锁：整个写入是 load → 改 → save，不加锁时两个并发请求
        可能都读到旧快照，后写的那个把先写的覆盖掉（丢一轮对话）。
        """
        yield  # pragma: no cover


# --------------------------------------------------------------------------- #
# 内存实现（v1.0.0 行为，本地开发与测试用）
# --------------------------------------------------------------------------- #
class MemorySessionStore(SessionStore):
    """
    进程内 dict 存储 —— 保留 v1.0.0 的行为，用于本地开发与单元测试。

    特点：零依赖、零网络、重启即丢。之所以保留而不是删掉：
    · 本地不装 Redis 也能跑测试（CI 友好）；
    · 是 Redis 版的「行为基准」——两边跑同一套断言，能第一时间发现 Redis 版
      语义漂移。
    """

    name = "memory"

    def __init__(self, ttl_seconds: int | None = None) -> None:
        self.ttl_seconds: int = int(
            ttl_seconds if ttl_seconds is not None else settings.MEMORY_SESSION_TTL_SECONDS
        )
        self._data: dict[str, SessionSnapshot] = {}
        # 会话级锁 + 保护字典结构的元锁（与原实现同构：元锁只做结构变更，短持有）
        self._session_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.RLock()
        logger.info("MemorySessionStore 初始化完成 | 会话TTL=%ds", self.ttl_seconds)

    # -------- 内部 -------- #
    def _is_expired(self, snapshot: SessionSnapshot) -> bool:
        return (time.time() - snapshot.last_active) > self.ttl_seconds

    # -------- 接口实现 -------- #
    def load(self, session_id: str, touch: bool = True) -> SessionSnapshot | None:
        with self._meta_lock:
            snapshot = self._data.get(session_id)
            if snapshot is None:
                return None
            if self._is_expired(snapshot):
                # 惰性回收：不依赖后台线程，读到过期就顺手删掉
                del self._data[session_id]
                self._session_locks.pop(session_id, None)
                logger.info("会话已过期，惰性回收 | session_id=%s", session_id)
                return None
            if touch:
                # 读也算活跃（与 v1.0.0 语义一致：用户打开旧会话查看历史即续命）
                snapshot.last_active = time.time()
            return snapshot

    def save(self, session_id: str, snapshot: SessionSnapshot) -> None:
        with self._meta_lock:
            snapshot.last_active = time.time()
            self._data[session_id] = snapshot
            self._session_locks.setdefault(session_id, threading.Lock())

    def delete(self, session_id: str) -> bool:
        with self._meta_lock:
            existed = self._data.pop(session_id, None) is not None
            self._session_locks.pop(session_id, None)
        if existed:
            logger.info("会话已删除 | session_id=%s", session_id)
        return existed

    def exists(self, session_id: str) -> bool:
        with self._meta_lock:
            snapshot = self._data.get(session_id)
        return snapshot is not None and not self._is_expired(snapshot)

    def list_ids(self) -> list[str]:
        with self._meta_lock:
            alive = [(sid, s) for sid, s in self._data.items() if not self._is_expired(s)]
        alive.sort(key=lambda item: item[1].last_active, reverse=True)
        return [sid for sid, _ in alive]

    def purge_expired(self) -> int:
        with self._meta_lock:
            expired = [sid for sid, s in self._data.items() if self._is_expired(s)]
            for sid in expired:
                del self._data[sid]
                self._session_locks.pop(sid, None)
        if expired:
            logger.info("主动清理过期会话 | 数量=%d", len(expired))
        return len(expired)

    def stats(self) -> dict[str, Any]:
        """
        存储层统计。

        体积用采样估算（最近 20 个会话求均值后外推）：全量序列化所有会话是
        O(总字节) 的操作，而 stats 会被健康检查、内存预警接口反复调用，
        不能让一个「看看情况」的接口把 CPU 吃掉。抽样在量级判断上完全够用。
        """
        with self._meta_lock:
            count = len(self._data)
            sample = sorted(self._data.values(), key=lambda s: s.last_active, reverse=True)[:20]
            usages = [s.size_bytes() for s in sample]
        info: dict[str, Any] = {
            "backend": self.name,
            "session_count": count,
            "ttl_seconds": self.ttl_seconds,
        }
        if usages:
            avg = sum(usages) / len(usages)
            info["avg_session_bytes"] = int(avg)
            info["bytes_estimate"] = int(avg * count)
            info["sample_size"] = len(usages)
        return info

    def health(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": True, "detail": "进程内存储，无外部依赖"}

    @contextmanager
    def session_lock(self, session_id: str) -> Iterator[None]:
        with self._meta_lock:
            lock = self._session_locks.setdefault(session_id, threading.Lock())
        with lock:
            yield


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def build_session_store(ttl_seconds: int | None = None) -> SessionStore:
    """
    按 settings.MEMORY_BACKEND 构建存储实例。

    为什么需要工厂而不是模块级单例：
    MemoryManager 的构造需要能注入不同 Store（测试里传内存版、生产传 Redis 版），
    单例会让「换后端」变成改全局状态，测试之间互相污染。

    :param ttl_seconds: 会话闲置过期秒数；缺省由各 Store 自行读 settings。
                        显式传入是为了让 `MemoryManager(ttl_seconds=0)` 这类
                        测试用法真的生效（否则 Manager 的 TTL 与 Store 的 TTL
                        会各说各话，过期行为对不上）。
    """
    backend = (settings.MEMORY_BACKEND or "memory").strip().lower()
    if backend == "redis":
        # 延迟 import：没装 redis 包或没配 Redis 时，memory 模式不应受任何影响
        from core.redis_store import RedisSessionStore

        return RedisSessionStore(ttl_seconds=ttl_seconds)
    if backend != "memory":
        logger.warning("未知的 MEMORY_BACKEND=%s，回退为 memory", backend)
    return MemorySessionStore(ttl_seconds=ttl_seconds)
