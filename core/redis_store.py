"""
Redis 会话存储实现 —— 让会话在服务重启后依然存在。

--------------------------------------------------------------------------
为什么生产必须落 Redis（而不是继续用进程内 dict）
--------------------------------------------------------------------------
进程内 dict 有三个致命问题，上线后全部会变成真实事故：
    1. 重启即丢：改配置 / 发版 / 容器漂移，所有用户的历史对话凭空消失；
    2. 无法水平扩展：起两个 worker，用户第二次请求路由到另一个进程，
       历史是空的（表现为「聊着聊着模型失忆了」，且偶发、极难排查）；
    3. 内存不可控：会话只增不减，最终 OOM 的是整个 Web 进程，
       而不是「只丢掉一些历史」。
Redis 一次解决这三条：数据在进程外、多 worker 共享、TTL 由 Redis 自己兜底。

--------------------------------------------------------------------------
Redis 数据结构与 key 设计
--------------------------------------------------------------------------
    rag:session:{session_id}    HASH   一个会话的全部字段（TTL = 会话闲置时长）
        messages        JSON 数组  [{role, content}]        对话历史
        exchange_meta   JSON 数组  每轮展示元数据（引用/意图/用量/耗时）
        session_meta    JSON 对象  {pinned, title}
        usage           JSON 对象  累计 token 用量
        last_active     float      最后活跃时间戳
    rag:sessions                ZSET   member=session_id, score=last_active
                                       （会话索引，供「列出全部会话」按活跃排序）
    rag:lock:{session_id}       STRING 会话级写锁（NX + PX + token）

为什么用 HASH 而不是把整条会话当一个 STRING：
    · 单 field 可独立读写（将来做「只刷新置顶标记」时不必反序列化全部历史）；
    · 内存效率更好（ziplist/listpack 编码下小 hash 比同体积 string 省）。
为什么 TTL 不在应用层判断而在 Redis 上：
    应用层判断需要「每读一次都要写一次时间戳」，且清理依赖进程活着；
    Redis 的 TTL 是服务端行为，进程全挂了 key 照样准时消失，更可靠。

索引 ZSET 的残留问题：key 过期后 ZSET 里的 member 不会自动消失
（Redis 的 keyspace notification 要额外订阅，不值得）。所以索引**只在读的时候
惰性校验**（见 list_ids），并给 purge_expired 做批量清理。

--------------------------------------------------------------------------
大访问量下的几个关键处理（用户明确关注的「数据量大/访问量大」）
--------------------------------------------------------------------------
1. 连接池复用（REDIS_MAX_CONNECTIONS）：绝不为每个请求新建连接 ——
   建连成本（TCP + AUTH）比一次命令本身贵两个数量级。
2. 单次往返：一次会话读写各走一条 pipeline（MULTI/EXEC），
   而不是 5 次独立命令（5 次 = 5 个 RTT，局域网看着不多，
   高并发下就是连接池被占满的直接原因）。
3. 命令超时（REDIS_SOCKET_TIMEOUT）：Redis 慢查询或网络抖动时
   请求线程必须能被释放，否则线程池会被拖死。
4. 不使用 KEYS：KEYS 是 O(N) 且阻塞 Redis 单线程，线上等于自杀；
   需要扫 key 的地方一律 SCAN 游标分批。
5. 单会话体积上限（MEMORY_MAX_SESSION_BYTES）：防「用户粘贴 200KB 文本」
   把单会话撑成大 key —— 大 key 的危害不只是占内存，
   还在于每次读写都要搬运整块（网络 + 序列化），是延迟毛刺的常见来源。
6. 写入失败降级（MEMORY_DEGRADE_ON_ERROR）：Redis 挂了/写满时，
   宁可丢「多轮上下文」也不能让问答整体 502 —— 可用性优先于记忆完整性。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from config.settings import settings
from core.session_store import SessionSnapshot, SessionStore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 释放锁的 Lua 脚本
# --------------------------------------------------------------------------- #
# 为什么必须用 Lua 而不是 DEL：
#   拿到锁的进程如果执行超时（比如 GC 停顿），锁会被 TTL 自动释放并被别人拿走；
#   此时它再执行 DEL，删掉的就是**别人的锁**，于是出现两个进程同时持锁。
#   脚本里比对 value（自己的 token）再删，保证「只删自己的锁」，且比对+删除原子。
_LUA_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# 判定「写失败是不是因为 Redis 内存满了」的关键字。
# 命中即说明触到了 maxmemory，属于容量问题而非代码问题，必须走高优先级告警。
_OOM_HINTS = ("oom command not allowed", "maxmemory", "used memory")


# --------------------------------------------------------------------------- #
# 运行态（进程级，供监控与存储共享）
# --------------------------------------------------------------------------- #
class RedisRuntimeState:
    """
    Redis 运行态的共享容器（进程内单例）。

    为什么要这个类：三处都需要同一份状态，但不该互相直接引用 ——
        · 监控线程写：最近一次内存水位、是否只读降级；
        · 存储层读：写入前判断是否该拒绝写；
        · 接口层读：健康检查 / 内存预警接口展示。
    用一个带锁的小容器当交汇点，依赖方向保持单向（都指向它）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_memory_status: dict[str, Any] | None = None
        # 只读降级原因：空串表示可正常写入；非空表示写入被保护性拒绝
        self.write_blocked_reason: str = ""
        # 写入失败计数（OOM / 连接失败），供健康检查展示「最近是否在丢记忆」
        self.write_failures: int = 0
        self.last_write_error: str = ""

    def set_memory_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            self.last_memory_status = status

    def get_memory_status(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self.last_memory_status) if self.last_memory_status else None

    def block_writes(self, reason: str) -> None:
        with self._lock:
            if self.write_blocked_reason != reason:
                logger.critical("Redis 写入已降级为只读保护 | 原因：%s", reason)
            self.write_blocked_reason = reason

    def unblock_writes(self) -> None:
        with self._lock:
            if self.write_blocked_reason:
                logger.warning("Redis 写入保护已解除 | 原原因：%s", self.write_blocked_reason)
            self.write_blocked_reason = ""

    def is_write_blocked(self) -> tuple[bool, str]:
        with self._lock:
            return bool(self.write_blocked_reason), self.write_blocked_reason

    def record_write_failure(self, err: str) -> None:
        with self._lock:
            self.write_failures += 1
            self.last_write_error = err

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "write_blocked": bool(self.write_blocked_reason),
                "write_blocked_reason": self.write_blocked_reason,
                "write_failures": self.write_failures,
                "last_write_error": self.last_write_error,
            }


_runtime_state = RedisRuntimeState()


def get_runtime_state() -> RedisRuntimeState:
    """取进程级 Redis 运行态（监控 / 存储 / 接口层共用）。"""
    return _runtime_state


# --------------------------------------------------------------------------- #
# Redis 客户端（连接池单例）
# --------------------------------------------------------------------------- #
_client_lock = threading.Lock()
_client: Any = None


def get_redis_client() -> Any:
    """
    获取全局 Redis 客户端（连接池复用，双检锁单例）。

    为什么要单例 + 连接池：
    redis-py 的 Redis 对象本身是线程安全的，内部挂着 ConnectionPool；
    每次请求 new 一个客户端 = 每次新建 TCP 连接，高并发下会迅速耗尽
    文件描述符并让 Redis 端 maxclients 打满。
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import redis  # 延迟 import：memory 模式下不装 redis 包也能跑

                _client = redis.Redis.from_url(
                    settings.REDIS_URL,
                    max_connections=int(settings.REDIS_MAX_CONNECTIONS),
                    socket_timeout=float(settings.REDIS_SOCKET_TIMEOUT),
                    socket_connect_timeout=float(settings.REDIS_CONNECT_TIMEOUT),
                    # 健康检查：连接闲置后被对端（或中间代理）静默断开时，
                    # 取出的坏连接会直接报错。开启后取连接时自动 PING 一次。
                    health_check_interval=30,
                    decode_responses=False,  # 保持 bytes，由 SessionSnapshot 统一 decode
                )
                logger.info(
                    "Redis 客户端已创建 | url=%s 连接池上限=%d 命令超时=%.1fs",
                    _redact_url(settings.REDIS_URL),
                    settings.REDIS_MAX_CONNECTIONS,
                    settings.REDIS_SOCKET_TIMEOUT,
                )
    return _client


def reset_redis_client() -> None:
    """关闭并重置客户端（测试切换实例时用）。"""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception as e:  # pragma: no cover - 关闭失败不影响测试继续
                logger.debug("关闭 Redis 客户端时出错（忽略）：%s", e)
            _client = None


def _redact_url(url: str) -> str:
    """日志里抹掉 URL 中的密码，避免凭据进日志文件。"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


# --------------------------------------------------------------------------- #
# Redis 存储实现
# --------------------------------------------------------------------------- #
class RedisSessionStore(SessionStore):
    """会话存储的 Redis 实现。对外语义与 MemorySessionStore 完全一致。"""

    name = "redis"

    def __init__(self, client: Any | None = None, ttl_seconds: int | None = None) -> None:
        """
        :param client: 注入的 Redis 客户端（测试用 fakeredis 注入）；
                       缺省走全局连接池单例
        :param ttl_seconds: 会话闲置过期秒数，缺省读 settings
        """
        self.ttl_seconds: int = int(
            ttl_seconds if ttl_seconds is not None else settings.MEMORY_SESSION_TTL_SECONDS
        )
        self._client = client if client is not None else get_redis_client()
        self._prefix = (settings.REDIS_KEY_PREFIX or "rag").rstrip(":")
        self._release_lock_script: Any = None
        logger.info(
            "RedisSessionStore 初始化完成 | 前缀=%s 会话TTL=%ds 分布式锁=%s",
            self._prefix,
            self.ttl_seconds,
            "开" if settings.REDIS_LOCK_ENABLED else "关",
        )

    # ------------------------------------------------------------------ #
    # key 工具
    # ------------------------------------------------------------------ #
    def _session_key(self, session_id: str) -> str:
        return f"{self._prefix}:session:{session_id}"

    @property
    def _index_key(self) -> str:
        return f"{self._prefix}:sessions"

    def _lock_key(self, session_id: str) -> str:
        return f"{self._prefix}:lock:{session_id}"

    # ------------------------------------------------------------------ #
    # 错误处理
    # ------------------------------------------------------------------ #
    def _is_oom(self, e: Exception) -> bool:
        """判断异常是不是 Redis 内存满导致的写入拒绝。"""
        msg = str(e).lower()
        return any(hint in msg for hint in _OOM_HINTS)

    def _on_write_error(self, e: Exception, session_id: str) -> None:
        """
        写入失败的统一处理。

        策略（可用性优先）：
        · OOM → 立刻进入只读保护，不再反复冲击已经写满的 Redis，同时抛出高优先级日志；
        · 其他错误（连接失败/超时）→ 记数并告警，但不降级（大概率是瞬时抖动）。
        是否向上抛由 MEMORY_DEGRADE_ON_ERROR 决定：默认 True = 吞掉异常，问答照常返回。
        """
        state = get_runtime_state()
        state.record_write_failure(f"{type(e).__name__}: {e}")
        if self._is_oom(e):
            state.block_writes("Redis 内存已满（maxmemory 触顶），写入被拒绝")
            logger.critical("Redis 写入被内存上限拒绝，已进入只读保护 | session_id=%s", session_id)
        else:
            logger.error("Redis 写入失败 | session_id=%s err=%s: %s", session_id, type(e).__name__, e)
        if not settings.MEMORY_DEGRADE_ON_ERROR:
            raise e

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def load(self, session_id: str, touch: bool = True) -> SessionSnapshot | None:
        """
        读会话快照。touch=True 时命中即刷新 TTL（与内存版「读也算活跃」语义对齐）。

        为什么命中后要单独一次 touch 而不是合成一条 pipeline：
        HGETALL 的结果决定「要不要 touch」，两者有先后依赖，无法合并；
        代价是 1 次额外 RTT，换来的是「用户翻看旧会话不会被 TTL 清掉」。
        touch 用 pipeline 把 EXPIRE + ZADD 合成一次往返。

        touch=False 的场景（会话列表 / 统计）必须走这条路：否则有人反复刷新
        会话列表就等于给全部会话续命，「闲置过期」会彻底失效。
        """
        key = self._session_key(session_id)
        try:
            raw = self._client.hgetall(key)
        except Exception as e:
            # 读失败一律降级为「没有历史」：问答可以继续，只是丢上下文。
            # 注意这里**不**设置只读保护 —— 读失败往往只是瞬时网络抖动。
            logger.error("Redis 读取会话失败，本次按无历史处理 | session_id=%s err=%s", session_id, e)
            return None

        if not raw:
            return None

        snapshot = SessionSnapshot.from_json_map(raw)
        if not touch:
            return snapshot

        # touch：只有 key 确实存在时才续期（否则会写出一个空 key + 脏索引项）
        now = time.time()
        snapshot.last_active = now
        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.expire(key, self.ttl_seconds)
            pipe.zadd(self._index_key, {session_id: now})
            pipe.execute()
        except Exception as e:  # pragma: no cover - touch 失败不影响本次读取结果
            logger.warning("会话 TTL 续期失败（不影响本次读取） | session_id=%s err=%s", session_id, e)
        return snapshot

    def save(self, session_id: str, snapshot: SessionSnapshot) -> None:
        """
        整条覆盖写 + 刷新 TTL + 更新索引，一次 pipeline 搞定。

        用 transaction=True（MULTI/EXEC）：三个命令要么都生效要么都不生效，
        避免「数据写了但 TTL 没设」→ key 永不过期 → 内存缓慢泄漏。
        """
        snapshot.last_active = time.time()
        key = self._session_key(session_id)
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.hset(key, mapping=snapshot.to_json_map())
            pipe.expire(key, self.ttl_seconds)
            pipe.zadd(self._index_key, {session_id: snapshot.last_active})
            pipe.execute()
        except Exception as e:
            self._on_write_error(e, session_id)

    def delete(self, session_id: str) -> bool:
        key = self._session_key(session_id)
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.delete(key)
            # 同步删索引项，否则会话列表会残留一个点进去是空白的幽灵条目
            pipe.zrem(self._index_key, session_id)
            results = pipe.execute()
        except Exception as e:
            self._on_write_error(e, session_id)
            return False
        deleted = bool(results and results[0])
        if deleted:
            logger.info("会话已删除 | session_id=%s", session_id)
        return deleted

    def exists(self, session_id: str) -> bool:
        try:
            return bool(self._client.exists(self._session_key(session_id)))
        except Exception as e:
            logger.error("Redis exists 失败 | session_id=%s err=%s", session_id, e)
            return False

    def list_ids(self) -> list[str]:
        """
        列出全部会话（按活跃度倒序）。

        索引 ZSET 里可能残留已过期的 member（Redis 过期 key 不会自动从 ZSET 摘除），
        所以拿到的候选要逐个 EXISTS 校验，顺手把死成员 ZREM 掉。
        校验走一条 pipeline，N 个会话也只有 1 个 RTT。
        """
        try:
            ids = self._client.zrevrange(self._index_key, 0, -1)
        except Exception as e:
            logger.error("Redis 读取会话索引失败：%s", e)
            return []
        ids = [i.decode("utf-8") if isinstance(i, bytes) else str(i) for i in ids]
        if not ids:
            return []

        alive: list[str] = []
        dead: list[str] = []
        try:
            pipe = self._client.pipeline(transaction=False)
            for sid in ids:
                pipe.exists(self._session_key(sid))
            flags = pipe.execute()
            for sid, flag in zip(ids, flags):
                (alive if flag else dead).append(sid)
            if dead:
                # 清残留（顺手做，不用等 purge_expired）
                self._client.zrem(self._index_key, *dead)
                logger.info("清理会话索引残留 | 数量=%d", len(dead))
        except Exception as e:  # pragma: no cover - 校验失败就退回索引原始列表
            logger.warning("会话索引校验失败，返回原始索引：%s", e)
            return ids
        return alive

    def purge_expired(self) -> int:
        """
        清理索引残留。

        注意语义差异：内存版是「真的删掉过期会话」，Redis 版过期会话
        由 Redis 自己删（应用不插手），这里只是把索引里的死成员摘掉。
        所以返回值是「摘掉的索引项数」，不是「释放的会话数」。
        """
        try:
            # score = 最后活跃时间，早于 now - ttl 的必然已经过期
            cutoff = time.time() - self.ttl_seconds
            stale = self._client.zrangebyscore(self._index_key, "-inf", cutoff)
            stale = [i.decode("utf-8") if isinstance(i, bytes) else str(i) for i in stale]
            if not stale:
                return 0
            self._client.zrem(self._index_key, *stale)
            logger.info("主动清理会话索引残留 | 数量=%d", len(stale))
            return len(stale)
        except Exception as e:
            logger.error("清理会话索引失败：%s", e)
            return 0

    # ------------------------------------------------------------------ #
    # 观察
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        """
        存储层统计。

        体积估算用抽样（MEMORY USAGE 最多查 20 个 key 取均值 × 会话数）：
        全量查是 O(N) 次命令，接口层不能让一个健康检查把 Redis 拖慢；
        抽样在「量级判断」这个用途上完全够用（误差 ±20% 不影响决策）。
        """
        info: dict[str, Any] = {
            "backend": self.name,
            "ttl_seconds": self.ttl_seconds,
            "key_prefix": self._prefix,
        }
        try:
            session_count = int(self._client.zcard(self._index_key))
            info["session_count"] = session_count
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
            info.update(get_runtime_state().snapshot())
            return info

        # 体积采样单独兜底：MEMORY USAGE 在部分托管 Redis / 代理上是禁用命令，
        # 拿不到体积不该影响「会话数」这种关键信息的上报。
        if session_count:
            try:
                sample = self._client.zrevrange(self._index_key, 0, 19)
                sample = [i.decode("utf-8") if isinstance(i, bytes) else str(i) for i in sample]
                usages: list[int] = []
                pipe = self._client.pipeline(transaction=False)
                for sid in sample:
                    pipe.memory_usage(self._session_key(sid))
                for val in pipe.execute():
                    if isinstance(val, int) and val > 0:
                        usages.append(val)
                if usages:
                    avg = sum(usages) / len(usages)
                    info["avg_session_bytes"] = int(avg)
                    info["bytes_estimate"] = int(avg * session_count)
                    info["sample_size"] = len(usages)
            except Exception as e:
                info["bytes_estimate_error"] = f"{type(e).__name__}: {e}"

        # 融合运行态：接口层一次调用就能拿到「量 + 水位 + 是否只读降级」
        info.update(get_runtime_state().snapshot())
        return info

    def health(self) -> dict[str, Any]:
        """连通性检查（不抛异常，失败信息放返回值）。"""
        try:
            started = time.perf_counter()
            pong = self._client.ping()
            latency_ms = (time.perf_counter() - started) * 1000
            server_version = "unknown"
            try:
                raw = self._client.info("server")
                server_version = str(raw.get("redis_version", "unknown"))
            except Exception:  # pragma: no cover - 权限受限时拿不到版本，不影响健康判定
                pass
            return {
                "backend": self.name,
                "ok": bool(pong),
                "latency_ms": round(latency_ms, 2),
                "server_version": server_version,
            }
        except Exception as e:
            return {
                "backend": self.name,
                "ok": False,
                "detail": f"{type(e).__name__}: {e}",
            }

    def write_allowed(self) -> tuple[bool, str]:
        """
        当前是否允许写入（保留接口，语义见 SessionStore.write_allowed）。

        Redis 版会看运行态：内存水位到 fatal 且开启了只读保护时返回 False。
        """
        blocked, reason = get_runtime_state().is_write_blocked()
        return (not blocked, reason)

    def close(self) -> None:
        """关闭连接池（应用退出/测试收尾时调用）。"""
        reset_redis_client()

    # ------------------------------------------------------------------ #
    # 会话级锁（跨进程）
    # ------------------------------------------------------------------ #
    @contextmanager
    def session_lock(self, session_id: str) -> Iterator[None]:
        """
        会话级分布式锁。

        为什么用 SET NX PX 而不是 Redlock / 或干脆不加锁：
        · 我们的临界区只有「读快照 → 改 → 写回」，耗时毫秒级，单 Redis 实例
          的 SET NX 足够（多 master 的场景才需要 Redlock 那套共识）；
        · 拿不到锁时**降级为无锁执行**并把冲突窗口交给 WARN 日志，
          而不是直接报错 —— 丢一轮记忆比让用户看到 500 可接受得多。
          这个取舍在文档里写清楚，不藏着。
        """
        if not settings.REDIS_LOCK_ENABLED:
            yield
            return

        key = self._lock_key(session_id)
        token = uuid.uuid4().hex
        ttl_ms = int(settings.REDIS_LOCK_TTL_MS)
        deadline = time.monotonic() + int(settings.REDIS_LOCK_WAIT_MS) / 1000.0
        acquired = False
        try:
            while time.monotonic() < deadline:
                try:
                    if self._client.set(key, token, nx=True, px=ttl_ms):
                        acquired = True
                        break
                except Exception as e:
                    # Redis 不可用时不要让业务卡在锁上：直接放行（无锁执行）
                    logger.warning("获取会话锁异常，降级为无锁执行 | session_id=%s err=%s", session_id, e)
                    break
                time.sleep(0.02)  # 20ms 轮询：临界区毫秒级，这个粒度足够且不压 Redis
            if not acquired:
                logger.warning(
                    "获取会话锁超时，降级为无锁写入（存在极小概率丢一轮记忆） | session_id=%s",
                    session_id,
                )
            yield
        finally:
            if acquired:
                try:
                    if self._release_lock_script is None:
                        self._release_lock_script = self._client.register_script(_LUA_RELEASE_LOCK)
                    self._release_lock_script(keys=[key], args=[token])
                except Exception as e:  # pragma: no cover - 释放失败靠 TTL 兜底
                    logger.warning("释放会话锁失败（将由 TTL 自动过期） | err=%s", e)
