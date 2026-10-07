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

v2.0.0 拆开之后（分层见 docs/PLAN-v2.0.0.md §4）：
    · core/session_store.py  MemorySessionStore  —— 进程内，开发 / 测试用
    · core/mysql_store.py    MySQLSessionStore   —— **生产真相源**，重启不丢
    · core/redis_store.py    RedisSessionStore   —— ⚠️ **已退役**，仅 module6/7 的
                                                     Redis 语义验证还在用它，MySQL 版
                                                     稳定后删除。生产路径不再构建它
                                                     （`MEMORY_BACKEND` 取值只有 memory|mysql）
    · core/memory_manager.py MemoryManager       —— 会话语义，与存储后端无关
换后端只改一行配置（MEMORY_BACKEND=mysql），本模块与所有调用方零改动。

> 2026-09-23 修订：原来 `MEMORY_BACKEND` 的第二个取值是 `redis`，已作废。
> 原因是「会话是永久业务资产，不该存在随时会被驱逐的内存库里」，详见 mysql_store.py 头部。

--------------------------------------------------------------------------
为什么接口是「整条会话快照」而不是「单条消息 CRUD」
--------------------------------------------------------------------------
会话的读写模式是「一轮问答 = 读一次全部历史 + 写一次全部历史」：
    · 逻辑上天然以 session 为单位，没有「单独改第 3 条消息」的场景；
    · 存整条快照 → 一次写完，天然原子，不会出现「消息写进去了、
      元数据没写进去」的半截状态；
    · 反而避免了「每条消息一条记录」带来的一堆小 key（Redis 里小 key 多了
      内存碎片和 RTT 都很难看：10 轮对话 = 21 次往返 vs 1 次）。

> 这段理由最初是为 Redis 写的，换成 MySQL 后依然成立 ——
> 只是「一次写完」的载体从 `HSET` 变成了**一个事务**：
> 存储层在事务里把快照 diff 成行级 INSERT/UPDATE/DELETE，
> 要么全成，要么全滚（见 core/mysql_store.py 的 save()）。

代价是每次写要序列化整条会话。控制手段是 MEMORY_MAX_TURNS（限制轮数）
和 MEMORY_MAX_SESSION_BYTES（限制单会话字节数），见 config/settings.py。

--------------------------------------------------------------------------
快照的数据结构（也是存储层序列化用的结构）
--------------------------------------------------------------------------
    messages      [{"role": "user"|"assistant", "content": "..."}]  按时间正序
    exchange_meta [{"sources": [...], "intent": "...", ...}]        与 messages 按轮对齐
    session_meta  {"pinned": bool, "title": str}                    会话管理元数据
    usage         {"input_tokens": 0, ...}                          会话累计 token 用量
    last_active   float                                             最后活跃 Unix 时间戳

⚠️ **归属（owner_id / user_id）刻意不在这个结构里**（P2-12c）：
它是「这条会话归谁」的控制面信息，由存储层自己持有（内存版 = 并行字典，
MySQL 版 = `session.user_id` 列），不参与逻辑层的数据交换。
理由：快照一旦带上 user_id，任何一个调用方都能改写它，归属就成了
「谁最后写谁说了算」；而归属必须是「只有存储层能定」。
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


class SessionOwnershipError(Exception):
    """
    「这条会话属于别人」——P2-12c 新增。

    为什么用异常而不是返回 False / None：
      · 返回 None 与「会话不存在」无法区分，接口层只能报404，
        而用户看到的是「我明明在列表里看到它」→ 极难排查；
      · 返回 False 同理，且更容易被写成 `if not ok: return` 而**吞掉**；
    异常让「越权」这件事**必须**在某处被显式处理，漏了就变成 500，
        而 500 是测试会红的那种失败。

    携带 `owner_id` 与 `session_id` 是为了接口层能说清「谁的哪条」——
    只说「无权限」的话，用户根本不知道是哪一条会话出的问题。
    """

    def __init__(self, session_id: str, owner_id: int | None) -> None:
        self.session_id = session_id
        self.owner_id = owner_id
        super().__init__(
            f"会话 {session_id!r} 不属于当前用户（owner_id={owner_id}），拒绝访问"
        )


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
    1. load 返回 None 表示「不存在**或**已过期**或不属于该owner**」——调用方无需区分，
       语义都是「开新会话」；
    2. load 命中即视为「一次活跃」，需要刷新 TTL（与 v1.0.0 内存版语义一致）；
    3. save 是整条覆盖写，且刷新 TTL；不存在的会话会被创建；
    4. delete 幂等：删不存在的会话不报错，返回值表示「是否真的删掉了」；
    5. list_ids 按最后活跃时间倒序（前端会话列表直接用这个顺序）。

    ---------------------------------------------------------------------------
    会话归属：`owner_id` 为什么是**必填**而不是可选（P2-12c）
    ---------------------------------------------------------------------------
    `session.user_id` 这一列从 P0-1b 就建好了，但一直没人写 —— 12c 之前
    「这条会话属于谁」这个问题在代码里**没有答案**，于是任何人都能读任何人的会话。

    现在归属判据下沉到存储层：读、写、删、列表四个操作**都要带 `owner_id`**。
    做成**必填**而不是「可选、默认 None = 不过滤」，理由是 fail-closed：

        def load(self, sid, *, owner_id=None)   ← 可选 = 危险
        def load(self, sid, *, owner_id: int)   ← 必填 = 安全

    可选版本下，某个调用点忘了传 owner_id → 静默返回**全部**会话 →
    「A 看到了 B 的问答记录」而没有任何报错。必填版本下同样的疏漏
    在第一次调用时就 `TypeError`，开发期立刻暴露。

    ⚠️ **`owner_id=None` 的语义是「只认无主会话」，不是「不过滤」**（用户已拍板）：
        · 已存在的会话，其 `user_id IS NULL`（12c 之前建的）→ 任何人都能认领它
        · 已存在的会话，其 `user_id = 440` → 只有 440 能读/写它
        · 新建的会话 → 归属写死为传入的 owner_id
    这样「存量会话归谁」有了确定答案：**第一个打开它的人**。
    反过来，**不存在「不过滤」这个取值** —— 这是刻意的，
    因为「不过滤」意味着「泄漏给所有人」。
    """

    #: 后端名字，用于日志和健康检查展示（memory / mysql / redis，redis 已退役）
    name: str = "unknown"

    @abstractmethod
    def load(self, session_id: str, *, owner_id: int | None,
             touch: bool = True) -> SessionSnapshot | None:
        """
        读取会话快照；不存在 / 已过期 / **不属于 owner_id** 都返回 None。

        :param owner_id: 会话归属。`None` 表示「只认无主会话」（详见类文档）。
        :param touch: 是否把本次读取记为「活跃」（刷新 TTL / last_active）。
                      会话列表、统计这类「扫一眼」的读必须传 False，
                      否则频繁刷新列表就等于给所有会话无限续命。
        """

    @abstractmethod
    def save(self, session_id: str, snapshot: SessionSnapshot, *,
             owner_id: int | None) -> None:
        """
        整条覆盖写入并刷新 TTL。

        归属规则（详见类文档）：
          · 新建 → `user_id = owner_id`
          · 命中无主会话 → **认领**它（`user_id = owner_id`）
          · 命中他人会话 → **不写**并抛 `SessionOwnershipError`
        最后一条是安全边界：认领只对「无主」生效，
        否则 A 传一个属于 B 的 session_id 就能把 B 的会话改写掉。
        """

    @abstractmethod
    def delete(self, session_id: str, *, owner_id: int | None) -> bool:
        """
        删除会话及其数据；返回是否真实删除（不存在或不属于 owner 返回 False）。
        幂等。
        """

    @abstractmethod
    def exists(self, session_id: str, *, owner_id: int | None = None) -> bool:
        """会话是否存在、未过期、且属于 `owner_id`。

        ⚠️ `owner_id` 默认 `None`（=只认无主）**只给运维/测试用**：
        业务调用点必须显式传，否则「查存在性」会漏掉别人的会话。
        唯一把它当默认值也安全的地方是 module8 的契约测试（那里测的就是无主语义）。
        """

    @abstractmethod
    def list_ids(self, *, owner_id: int | None = None) -> list[str]:
        """列出属于 `owner_id` 的会话 id，按最后活跃时间倒序。

        ⚠️ 与 `exists` 同理：默认值 `None` = 只列无主会话，**不是「列出全部」**。
        """

    @abstractmethod
    def is_foreign_to(self, session_id: str, *, owner_id: int | None) -> bool:
        """
        这条会话**存在，但不属于** `owner_id` → True。

        为什么必须有这个方法（而不是让接口层看 `load` 的返回值）：
            load / exists / list_ids 都把「不存在」和「不属于我」压成同一个 False ——
            这是**故意**的，因为对调用方而言两者都是「开不了这条会话」。
            但用户拍板的是**403 越权**而不是 404，理由是：
            他拿着一个自己列表里没有、但确实存在的 session_id，
            404 会让他反复以为自己记错了 id，而 403 直接告诉他「这条是别人的」。
            差别只有这一个错误码，却需要知道「到底存不存在」——
            而这个信息在`load` 的返回值里已经丢了，只能另外问一次。

        为什么不用「先查归属再判」拼在接口层：
            归属是存储层的知识（列 / 并行字典 / hash field），
            泄漏到接口层就等于每个后端都要在接口层写一遍自己的查法。

        ⚠️ 无主会话（`user_id IS NULL`）**不算foreign**：它对所有人可认领。
        """

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
    · 本地不装 MySQL 也能跑测试（CI 友好）；
    · 是 MySQL 版的「行为基准」——两边跑同一套断言，能第一时间发现 MySQL 版
      语义漂移。

    ⚠️ 归属存**并行的字典**而不是塞进 SessionSnapshot：
    快照是「业务交换格式」，把 `user_id` 塞进去会让每个调用方
    （add_exchange / update_session_meta / add_usage…）都有机会改写它 ——
    而归属**只应该**由存储层在 save 时决定。放在外面就没有这个面。
    """

    name = "memory"

    def __init__(self, ttl_seconds: int | None = None) -> None:
        self.ttl_seconds: int = int(
            ttl_seconds if ttl_seconds is not None else settings.MEMORY_SESSION_TTL_SECONDS
        )
        self._data: dict[str, SessionSnapshot] = {}
        # session_id → 归属 uid。None 表示「无主」（12c 之前建的会话）。
        # ⚠️ 用**单独**字典而不是 snapshot 里的字段，理由见类文档。
        self._owners: dict[str, int | None] = {}
        # 会话级锁 + 保护字典结构的元锁（与原实现同构：元锁只做结构变更，短持有）
        self._session_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.RLock()
        logger.info("MemorySessionStore 初始化完成 | 会话TTL=%ds", self.ttl_seconds)

    # -------- 内部 -------- #
    def _is_expired(self, snapshot: SessionSnapshot) -> bool:
        """last_active 超过 TTL。只给统计用，不再据此删会话或把它从列表拿掉。"""
        return (time.time() - snapshot.last_active) > self.ttl_seconds

    def _owns(self, session_id: str, owner_id: int | None) -> bool:
        """
        这条会话是不是该 owner 的。

        ⚠️ 注意「无主会话对**所有人**可见」这条：它是「认领」的前提。
        若读侧看不见，用户第一次打开 12c 之前留下的会话时load 返回 None，
        逻辑层会当成「会话不存在」而从零重建，随后 save 把原有内容覆盖掉——
        存量内容静默丢失，而且丢得没有任何日志。
        所以三个实现的这条语义必须**逐条一致**，共享契约测试也断言它。

        ⚠️ 本方法**不判断会话是否存在**：不在 `_owners` 里的 sid，
        `_owners.get` 返回 None，会被判成「无主 → 可见」。
        所以每个调用方都必须**先确认存在**再问归属
        （load / exists / delete 都先查 `_data` / `_owners` 成员）。
        不这么做的后果：查一个根本不存在的会话时，`delete` 会当成
        「无主会话可删」而返回 True，接口层据此回200，用户以为删掉了。
        """
        actual = self._owners.get(session_id)
        return actual is None or actual == owner_id

    # -------- 接口实现 -------- #
    def load(self, session_id: str, *, owner_id: int | None,
             touch: bool = True) -> SessionSnapshot | None:
        with self._meta_lock:
            # 先取快照再判归属：不存在就是不存在，与归属无关。
            snapshot = self._data.get(session_id)
            if snapshot is None:
                return None
            if not self._owns(session_id, owner_id):
                return None
            if touch:
                # 打开会话或接着问，记一次活跃。刷新左侧列表走 touch=False，不算打开。
                snapshot.last_active = time.time()
            return snapshot

    def save(self, session_id: str, snapshot: SessionSnapshot, *,
             owner_id: int | None) -> None:
        with self._meta_lock:
            if session_id in self._owners and not self._owns(session_id, owner_id):
                # 与 MySQL 版同一条边界：认领只对「无主」生效。
                raise SessionOwnershipError(session_id, owner_id)
            snapshot.last_active = time.time()
            self._data[session_id] = snapshot
            self._owners[session_id] = owner_id
            self._session_locks.setdefault(session_id, threading.Lock())

    def delete(self, session_id: str, *, owner_id: int | None) -> bool:
        with self._meta_lock:
            # 顺序要紧：先判存在，再判归属。反过来的话，不存在的会话会被
            # 当成「无主 → 可删」，返回 True（见 `_owns` 的注释）。
            if session_id not in self._data or not self._owns(session_id, owner_id):
                return False
            existed = self._data.pop(session_id, None) is not None
            self._owners.pop(session_id, None)
            self._session_locks.pop(session_id, None)
        if existed:
            logger.info("会话已删除 | session_id=%s", session_id)
        return existed

    def exists(self, session_id: str, *, owner_id: int | None = None) -> bool:
        with self._meta_lock:
            return session_id in self._data and self._owns(session_id, owner_id)

    def list_ids(self, *, owner_id: int | None = None) -> list[str]:
        with self._meta_lock:
            alive = [
                (sid, snap) for sid, snap in self._data.items()
                if self._owns(sid, owner_id)
            ]
        alive.sort(key=lambda item: item[1].last_active, reverse=True)
        return [sid for sid, _ in alive]

    def is_foreign_to(self, session_id: str, *, owner_id: int | None) -> bool:
        with self._meta_lock:
            if session_id not in self._data:
                return False
            actual = self._owners.get(session_id)
            # 无主不是 foreign —— 认领的前提就是它对所有人可见
            return actual is not None and actual != owner_id

    def purge_expired(self) -> int:
        """不再按闲置时间删除会话。保留方法是因为监控回调还在调它。"""
        return 0

    def stats(self) -> dict[str, Any]:
        """
        存储层统计。

        体积用采样估算（最近 20 个会话求均值后外推）：全量序列化所有会话是
        O(总字节) 的操作，而 stats 会被健康检查、内存预警接口反复调用，
        不能让一个「看看情况」的接口把 CPU 吃掉。抽样在量级判断上完全够用。

        ⚠️ `session_count` 是**全部**会话数（不带 owner 过滤），因为它是
        「内存/连接池用了多少」这类运维口径，与「谁看得见什么」无关。
        要看某个用户的会话数，用 `list_ids(owner_id=...)`。
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
    MemoryManager 的构造需要能注入不同 Store（测试里传内存版、生产传 MySQL 版），
    单例会让「换后端」变成改全局状态，测试之间互相污染。

    :param ttl_seconds: 会话闲置过期秒数；缺省由各 Store 自行读 settings。
                        显式传入是为了让 `MemoryManager(ttl_seconds=0)` 这类
                        测试用法真的生效（否则 Manager 的 TTL 与 Store 的 TTL
                        会各说各话，过期行为对不上）。

    取值非法时**抛 ValueError，不回退**（理由见函数体里的注释）。

    ⚠️ 工厂**只负责选实现**，不管归属：各 Store 怎么存user_id、怎么判越权，
    是各自的责任（实现细节差异很大：内存版是并行字典，MySQL 版是一列 + 索引）。
    唯一的共同约定是 `SessionStore` 类文档里那三条 —— 工厂换实现时它不成立，
    就说明实现没遵守契约。
    """
    backend = (settings.MEMORY_BACKEND or "memory").strip().lower()
    if backend == "mysql":
        # 延迟 import 不是洁癖：本地跑测试 / CI 不需要 MySQL，
        # 若在 import 期就依赖 pymysql，等于把「不装数据库也能跑单测」这个能力删掉了。
        from core.mysql_store import MySQLSessionStore

        return MySQLSessionStore(ttl_seconds=ttl_seconds)
    if backend == "memory":
        return MemorySessionStore(ttl_seconds=ttl_seconds)

    # ⚠️ 走到这里**故意抛错，不静默回退**。
    # 「未知取值就回退成内存版」看着友好，实际后果是：生产上把持久化悄悄关掉，
    # 重启即丢全部会话，而日志里只有一行 WARNING。静默的数据丢失比启动失败坏得多 ——
    # 启动失败五分钟内就会被发现；静默丢数据往往要等到用户投诉，那时已经找不回来了。
    if backend == "redis":
        raise ValueError(
            "MEMORY_BACKEND=redis 已废弃：会话真相源已改为 MySQL。"
            "RedisSessionStore 仅保留给 tests/test_module6、test_module7 的语义验证，"
            "不再参与生产装配。请改为 MEMORY_BACKEND=mysql；"
            "本地开发 / 测试用 MEMORY_BACKEND=memory。"
        )
    raise ValueError(f"未知的 MEMORY_BACKEND={backend!r}，只支持 memory | mysql")
