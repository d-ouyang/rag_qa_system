"""
MySQL 会话存储实现 —— 会话 / 消息 / 用量的**真相源**。

--------------------------------------------------------------------------
为什么是 MySQL 而不是 Redis（这是本模块存在的全部理由）
--------------------------------------------------------------------------
第 1 版（`v2.0.0-p0.1`，2026-09-23）把会话落到了 Redis，方案已作废。
错在两处「定位错配」：

    · **可靠性错配**：会话是多轮对话沉淀下来的**永久业务资产**，
      而 Redis 是内存库 —— 重启、`maxmemory` 驱逐、容量保护都会让它消失。
      把「不可丢的数据」放进「随时可丢的介质」，是架构级的错。
    · **职责错配**：Redis 的位置是「队列 broker + 短期缓存」。
      让它同时当真相源，就没法回答「这份数据丢了要不要报警」——
      队列丢了可以重放，真相丢了就是事故，两者的运维口径完全不同。

现在的分层是：**MySQL 存真相，Redis 只存「丢了能重来」的东西**。
完整的四层归属见 `docs/PLAN-v2.0.0.md` §4；变更过程见 p0.1 迭代文档 §7.1。

--------------------------------------------------------------------------
「业务侧零改动」是怎么做到的
--------------------------------------------------------------------------
`MemoryManager`、`rag_chain`、`qa.py` **一行未改**。靠的是 `SessionStore` 抽象：
本模块只实现 load/save/delete/exists/list_ids/purge_expired/stats/health/
session_lock/write_allowed 这一组方法，把「快照 ↔ 数据行」的翻译关在内部。

能做到这一点，是因为抽象层当初选对了粒度 —— 接口是**整条会话快照**，
不是「单条消息的 CRUD」。于是存储层有空间把「一次覆盖写」自由地翻译成
行级 INSERT / UPDATE / DELETE，而逻辑层完全不必知道。

--------------------------------------------------------------------------
两条容易写错的语义（都对齐 MemorySessionStore，两边跑同一套断言）
--------------------------------------------------------------------------
1. **`save` 会改写 `last_active`**：不是「存调用方传进来的值」，而是「记为当前时刻」。
   只要发生一次真实写入，就说明这个会话是活跃的。两个后端必须一致，
   否则「换后端」就等于换了个 bug。
2. **`load(touch=True)` 续期，`touch=False` 不续期**。会话列表这种「扫一眼」的读
   必须传 False —— 否则有人反复刷新列表就等于给所有会话无限续命，
   「闲置过期」这条设计直接失效。

--------------------------------------------------------------------------
刻意的取舍
--------------------------------------------------------------------------
· **TTL 是软过期**：`load` 读到超期返回 None，但**不删行**。
  永久业务资产不该被 TTL 抹掉；`purge_expired()` 也只打 `is_archived=1`，
  物理删除留到 P2-10 定保留策略。
· **「现在」一律取应用侧时钟**（`time.time()`），不用 MySQL 的 `NOW()`。
  两套时钟混用会出现「写进去的时间比读的那一刻还晚」这类幽灵问题。
· **时间戳用无时区 `DATETIME(6)` 而非 float**：为了能在 SQL 客户端里直接看懂、
  直接写 `WHERE last_active_at > ...`。代价是按**本机本地时区**解释，
  容器已固定 `--default-time-zone=+08:00`（见 docker-compose.yml）。
  跨时区部署时要改成统一存 UTC 或换 `TIMESTAMP` 类型。
· **不用外键级联**：删除链路由应用层显式负责（磁盘 / Chroma / MySQL 三件事），
  加外键会把「顺手删了什么」藏进隐式行为里。
· **不做容量保护**：`write_allowed()` 恒为 True。容量预警是 Redis 后端为
  「写爆共享内存、连累同机其他服务」引入的（见 `core/redis_monitor.py`）；
  MySQL 的瓶颈是磁盘，而磁盘写满时 INSERT 自己就会报错，
  不需要应用层提前猜 —— 猜错了反而会误拒正常写入。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.orm import Session

from config.settings import settings
from core import db as db_module
from core.schema import chat_message_table as MSG
from core.schema import session_table as SESS
from core.session_store import SessionSnapshot, SessionStore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 时间戳 ↔ DATETIME 转换
# --------------------------------------------------------------------------- #
def _to_db_time(ts: float) -> datetime:
    """
    Unix 时间戳（float）→ 无时区 datetime。

    `datetime.fromtimestamp` 按**本机本地时区**换算，写进 DATETIME 列的就是本地墙上时间；
    读回来用 `.timestamp()` 同样按本地时区解释，一来一回自洽。
    跨时区部署时这条链会错位 —— 那时应改成统一存 UTC（见模块文档）。
    """
    return datetime.fromtimestamp(ts)


def _to_ts(dt: datetime) -> float:
    """无时区 datetime → Unix 时间戳（float）。"""
    return dt.timestamp()


class MySQLSessionStore(SessionStore):
    """
    会话存储的 MySQL 实现（生产真相源）。

    读写模型：**整条快照进，整条快照出**；内部把它 diff 成行级写入。
    并发模型：同一 session 串行（`SELECT ... FOR UPDATE`），不同 session 完全并行。
    """

    name = "mysql"

    def __init__(self, ttl_seconds: int | None = None, session_factory: Any | None = None) -> None:
        """
        :param ttl_seconds: 会话闲置过期秒数；缺省读 settings.MEMORY_SESSION_TTL_SECONDS
        :param session_factory: 注入 SQLAlchemy sessionmaker（测试用）；缺省用 core/db.py 的全局工厂
        """
        # 不能用 `or` 兜底：0 是合法值（测试里 ttl_seconds=0 表示「立刻过期」）
        self.ttl_seconds: int = int(
            ttl_seconds if ttl_seconds is not None else settings.MEMORY_SESSION_TTL_SECONDS
        )
        self._factory_override = session_factory
        # 线程局部：记录「本线程当前是否已持有某会话的锁事务」。
        # 为什么必须放线程局部：FastAPI 的同步路由跑在线程池里，
        # 一个连接/事务只能属于一个线程；放实例属性会让并发线程互相踩。
        self._local = threading.local()
        logger.info("MySQLSessionStore 初始化完成 | 会话TTL=%ds", self.ttl_seconds)

    # ------------------------------------------------------------------ #
    # 事务与锁
    # ------------------------------------------------------------------ #
    def _factory(self) -> Any:
        return self._factory_override if self._factory_override is not None else db_module.get_session_factory()

    def _held(self, key: str | None) -> Session | None:
        """本线程是否已持有 `key` 的锁事务；持有则返回那个 Session。"""
        held = getattr(self._local, "held", None)
        if held is not None and held[0] == key:
            return held[1]
        return None

    def _require_held(self, session_id: str) -> Session:
        """
        取「当前必然处于锁内」的那个 Session。

        用显式 raise 而不是 `assert`：assert 在 `python -O` 下会被整条去掉，
        届时这里会静默变成 None，后面报的是
        「'NoneType' object has no attribute 'execute'」这种指不到根因的错。
        """
        session = self._held(session_id)
        if session is None:
            raise RuntimeError(
                f"session_lock({session_id!r}) 未生效：只能在锁内调用本方法"
            )
        return session

    @contextmanager
    def _tx(self, key: str | None = None) -> Iterator[Session]:
        """
        取一个可用的 Session。

        **关键**：如果本线程已持有同一个 key 的锁事务，就把那个 Session 借出去，
        而不是新开一个 —— 否则 `load` / `save` 会各跑各的事务，
        `SELECT ... FOR UPDATE` 加的锁根本保护不到它们（锁在 A 事务，
        读写发生在 B 事务，等于没锁）。这是本模块最容易写错的地方。
        """
        held = self._held(key)
        if held is not None:
            yield held  # 复用外层事务：绝不自己 commit / rollback / close
            return
        session = self._factory()()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def session_lock(self, session_id: str) -> Iterator[None]:
        """
        会话级锁：保证同一 session 的「读 → 改 → 写」三步串行。

        实现是 `SELECT ... FOR UPDATE`（行锁），比 Redis 的分布式锁更简单也更可靠 ——
        锁与被保护的数据落在**同一个事务边界**内，不存在「锁在内存、数据在库」的错位。

        会话尚不存在时，InnoDB 在 REPEATABLE READ 下会对主键区间加**间隙锁**，
        因此并发创建同一个 session_id 同样会被串行化（不会出现两份会话）。
        """
        if self._held(session_id) is not None:
            # 可重入：同一线程已持有，直接复用（save 内部会再调一次 session_lock）
            yield
            return

        session = self._factory()()
        try:
            session.execute(select(SESS.c.id).where(SESS.c.id == session_id).with_for_update())
            self._local.held = (session_id, session)
            try:
                yield
            finally:
                self._local.held = None
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # 编解码：快照 ↔ 数据行
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decode_session_meta(row: Mapping[str, Any]) -> dict[str, Any]:
        """
        会话管理元数据。

        `pinned` 恒存在（对齐 SessionSnapshot 的默认值 `{"pinned": False}`）；
        `title` 为空串时不写出该键 —— 否则 `{"pinned": False}` 这种「没设过标题」
        会被读成 `{"title": ""}`，键集与写进去的不一致。
        """
        meta: dict[str, Any] = {"pinned": bool(row["is_pinned"])}
        title = row["title"]
        if title:
            meta["title"] = title
        return meta

    @staticmethod
    def _decode_usage(row: Mapping[str, Any]) -> dict[str, int]:
        """
        token 用量。

        恒返回完整 4 个键（哪怕全 0）。Memory 版可能返回 `{}`，
        两者在消费口径上等价：`get_usage()` 与 `list_sessions()` 都会 `or _USAGE_ZERO` 补零。
        返回定长字典的好处是调用方不必再判「这个键在不在」。
        """
        return {
            "input_tokens": int(row["usage_input_token"] or 0),
            "output_tokens": int(row["usage_output_token"] or 0),
            "cache_read_tokens": int(row["usage_cache_read_token"] or 0),
            "requests": int(row["usage_requests"] or 0),
        }

    @staticmethod
    def _extract_ref_ids(meta: Mapping[str, Any]) -> list[Any]:
        """
        从轮元数据里抽出引用切片 id 列表（PLAN §11 D3：只存 chunk_id，不存切片正文）。

        ⚠️ 当前 `_extract_sources()` 产出的 source 只有
        `index / source(磁盘路径) / snippet(200 字) / 两个分数`，**还没有 `chunk_id`**。
        所以本列现在恒为 `[]`，等 P0-4 把 `chunk_id` 写进 Chroma metadata 后自动生效。
        先建列是刻意的：P0-4 只改写入侧，不必再动 DDL。
        """
        sources = meta.get("sources") or []
        refs: list[Any] = []
        for item in sources:
            if isinstance(item, Mapping):
                cid = item.get("chunk_id")
                if cid is not None:
                    refs.append(cid)
        return refs

    def _build_rows(self, session_id: str, snapshot: SessionSnapshot) -> list[dict[str, Any]]:
        """
        快照 → 消息行。

        轮元数据挂在**该轮的 assistant 行**上（`seq = 2i+1`）：
        `exchange_meta[i]` 描述的是第 i 轮（用户问 + AI 答）这一整轮，
        但引用资料、意图、耗时都是「AI 这一答」的属性，挂 assistant 行语义最准。

        `turn_meta is None`（SQL NULL）精确表示「本轮没有 meta」——
        这样 `load` 能把 `exchange_meta` 的长度也原样还原，而不是靠猜。
        """
        turns = len(snapshot.messages) // 2
        metas = snapshot.exchange_meta or []
        if len(metas) > turns:
            # 不该发生：上层 _trim() 会保证 len(exchange_meta) <= 轮数。
            # 真发生了说明调用方绕过 MemoryManager 直接写了快照 —— 截断 + 告警，
            # 而不是静默丢掉（静默丢会导致「引用资料莫名少一条」这种极难查的现象）。
            logger.warning(
                "exchange_meta 比轮数还长，超出部分无法落库 | session_id=%s 元数据=%d 轮数=%d",
                session_id, len(metas), turns,
            )

        rows: list[dict[str, Any]] = []
        for seq, message in enumerate(snapshot.messages):
            row: dict[str, Any] = {
                "session_id": session_id,
                "seq": seq,
                "role": str(message.get("role", "")),
                "content": str(message.get("content", "")),
                "turn_meta": None,
                "ref_ids": None,
                "usage_input_token": 0,
                "usage_output_token": 0,
                "is_cache_hit": False,
            }
            if seq % 2 == 1:
                turn = seq // 2
                if turn < len(metas):
                    meta = metas[turn] if isinstance(metas[turn], Mapping) else {}
                    row["turn_meta"] = dict(meta)
                    row["ref_ids"] = self._extract_ref_ids(meta)
                    usage = meta.get("usage") or {}
                    row["usage_input_token"] = int(usage.get("input_tokens") or 0)
                    row["usage_output_token"] = int(usage.get("output_tokens") or 0)
                    # 目前没有任何链路会设置 cache_hit（问答字符串缓存是 P3 的事），
                    # 这里先读这个键，P3 落地时不必再改存储层。
                    row["is_cache_hit"] = bool(meta.get("cache_hit", False))
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ #
    # SessionStore 接口实现
    # ------------------------------------------------------------------ #
    def load(self, session_id: str, touch: bool = True) -> SessionSnapshot | None:
        now = time.time()
        with self._tx(session_id) as session:
            row = session.execute(
                select(SESS).where(SESS.c.id == session_id)
            ).mappings().first()
            if row is None:
                return None
            if bool(row["is_archived"]) or (now - _to_ts(row["last_active_at"])) > self.ttl_seconds:
                # 软过期：对外表现等同「开新会话」，但**不动数据**。
                # 归档标记交给 purge_expired() 统一处理，读路径保持零副作用。
                return None

            if touch:
                session.execute(
                    update(SESS).where(SESS.c.id == session_id).values(last_active_at=_to_db_time(now))
                )

            message_rows = session.execute(
                select(MSG.c.seq, MSG.c.role, MSG.c.content, MSG.c.turn_meta)
                .where(MSG.c.session_id == session_id)
                .order_by(MSG.c.seq)
            ).all()

            messages = [{"role": str(r[1]), "content": str(r[2])} for r in message_rows]
            # turn_meta 非 NULL 才算「本轮有 meta」——这样连 exchange_meta 的**长度**
            # 都能精确还原，而不是靠「轮数」反推（轮数反推在
            # 「messages 有 3 条、meta 只有 1 条」这种非规范快照上会多补一条空 dict）。
            exchange_meta = [dict(r[3]) for r in message_rows if r[3] is not None]

            return SessionSnapshot(
                messages=messages,
                exchange_meta=exchange_meta,
                session_meta=self._decode_session_meta(row),
                usage=self._decode_usage(row),
                # touch=True 时报「刚刷新的时间」，与 MemorySessionStore 就地改写
                # snapshot.last_active 的行为对齐；touch=False 报库里的真实值。
                last_active=now if touch else _to_ts(row["last_active_at"]),
            )

    def save(self, session_id: str, snapshot: SessionSnapshot) -> None:
        """
        整条覆盖写入（存在即覆盖，不存在即创建），并刷新 last_active。

        行级 diff 规则（按 `seq` 对齐，而不是按自增 id）：
            目标有 / 库里没有 → INSERT
            两边都有         → UPDATE（**无条件**，见下）
            库里有 / 目标没有 → DELETE（截断与编辑重发就靠这条）
        """
        with self.session_lock(session_id):
            session = self._require_held(session_id)
            now = time.time()
            session_meta = snapshot.session_meta or {}
            usage = snapshot.usage or {}

            session_values: dict[str, Any] = {
                "title": str(session_meta.get("title") or ""),
                "is_pinned": bool(session_meta.get("pinned", False)),
                "usage_input_token": int(usage.get("input_tokens") or 0),
                "usage_output_token": int(usage.get("output_tokens") or 0),
                "usage_cache_read_token": int(usage.get("cache_read_tokens") or 0),
                "usage_requests": int(usage.get("requests") or 0),
                "last_active_at": _to_db_time(now),
                # 一次成功写入即视为「重新活跃」：会话可能刚从归档态被写回来
                "is_archived": False,
            }

            exists = session.execute(
                select(SESS.c.id).where(SESS.c.id == session_id)
            ).scalar() is not None
            if exists:
                session.execute(update(SESS).where(SESS.c.id == session_id).values(**session_values))
            else:
                session.execute(insert(SESS).values(id=session_id, **session_values))
                logger.debug("新建会话 | session_id=%s", session_id)

            target_rows = self._build_rows(session_id, snapshot)
            target_seqs = {row["seq"] for row in target_rows}
            existing_seqs = set(
                session.execute(
                    select(MSG.c.seq).where(MSG.c.session_id == session_id)
                ).scalars()
            )

            obsolete = existing_seqs - target_seqs
            if obsolete:
                # 先删后插：删掉尾部多余的行，再补新增的行。
                # 同一个 seq 不会既在 obsolete 又在 target（差集定义），所以不会撞唯一键。
                session.execute(
                    delete(MSG).where(MSG.c.session_id == session_id, MSG.c.seq.in_(obsolete))
                )

            fresh = [row for row in target_rows if row["seq"] not in existing_seqs]
            if fresh:
                session.execute(insert(MSG), fresh)

            # ⚠️ 这里**刻意不做逐列比较**来跳过「没变的行」。
            # 原因：turn_meta 里有浮点（ts / elapsed_ms），MySQL 的 JSON 列按 double 存，
            # 读回的值可能与写入值差 1 个 ULP —— 拿它判「没变」会漏掉真实的 meta 变更
            # （比如同一条回答的 intent/sources 变了），属于静默丢数据。
            # 代价是每轮重写最多 20 行、每行几百字节，换掉一整类难查的问题，值。
            for row in target_rows:
                if row["seq"] in existing_seqs:
                    session.execute(
                        update(MSG)
                        .where(MSG.c.session_id == session_id, MSG.c.seq == row["seq"])
                        .values(**{k: v for k, v in row.items() if k not in ("session_id", "seq")})
                    )

    def delete(self, session_id: str) -> bool:
        """删除会话及其全部消息；返回「原本是否存在」。幂等。"""
        with self.session_lock(session_id):
            session = self._require_held(session_id)
            # 没有 FK 级联，所以消息必须显式删 —— 否则留下的是永远查不到、也永远删不掉的孤儿行
            removed = session.execute(
                delete(MSG).where(MSG.c.session_id == session_id)
            ).rowcount or 0
            existed = (session.execute(
                delete(SESS).where(SESS.c.id == session_id)
            ).rowcount or 0) > 0
        if existed:
            logger.info("会话已删除 | session_id=%s 消息=%d 行", session_id, removed)
        return existed

    def exists(self, session_id: str) -> bool:
        cutoff = _to_db_time(time.time() - self.ttl_seconds)
        with self._tx(session_id) as session:
            found = session.execute(
                select(SESS.c.id).where(
                    SESS.c.id == session_id,
                    SESS.c.is_archived.is_(False),
                    SESS.c.last_active_at > cutoff,
                )
            ).first()
        return found is not None

    def list_ids(self) -> list[str]:
        cutoff = _to_db_time(time.time() - self.ttl_seconds)
        with self._tx() as session:
            rows = session.execute(
                select(SESS.c.id)
                .where(SESS.c.is_archived.is_(False), SESS.c.last_active_at > cutoff)
                .order_by(SESS.c.last_active_at.desc())
            ).scalars().all()
        return [str(r) for r in rows]

    def purge_expired(self) -> int:
        """
        把过期会话标记为归档。

        **只打标记、不删行** —— 会话是永久业务资产，TTL 只负责「不再出现在列表里」，
        不负责销毁。物理清理的保留策略是 P2-10 的活。
        """
        cutoff = _to_db_time(time.time() - self.ttl_seconds)
        with self._tx() as session:
            affected = session.execute(
                update(SESS)
                .where(SESS.c.is_archived.is_(False), SESS.c.last_active_at <= cutoff)
                .values(is_archived=True)
            ).rowcount or 0
        if affected:
            logger.info("过期会话已归档 | 数量=%d", affected)
        return int(affected)

    def stats(self) -> dict[str, Any]:
        """
        存储层统计。

        ⚠️ 与 Memory / Redis 版**有意不同**：不提供 `avg_session_bytes` / `bytes_estimate`。
        那两个字段是为回答「Redis 会不会被写爆」而生的容量估算；
        MySQL 的容量瓶颈是磁盘，暴露一个基于采样的字节数只会误导人。
        取而代之给真实值：存活会话数、消息行数、以及表的实际占用（information_schema，代价 O(1)）。
        """
        cutoff = _to_db_time(time.time() - self.ttl_seconds)
        alive = select(SESS.c.id).where(
            SESS.c.is_archived.is_(False), SESS.c.last_active_at > cutoff
        )
        with self._tx() as session:
            session_count = session.execute(
                select(func.count()).select_from(SESS).where(
                    SESS.c.is_archived.is_(False), SESS.c.last_active_at > cutoff
                )
            ).scalar() or 0
            message_count = session.execute(
                select(func.count()).select_from(MSG).where(MSG.c.session_id.in_(alive))
            ).scalar() or 0
            db_size = session.execute(
                text(
                    "SELECT COALESCE(SUM(data_length + index_length), 0) "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME IN ('session', 'chat_message')"
                ),
                {"db": settings.MYSQL_DATABASE},
            ).scalar() or 0
        return {
            "backend": self.name,
            "session_count": int(session_count),
            "message_count": int(message_count),
            "ttl_seconds": self.ttl_seconds,
            "db_size_bytes": int(db_size),
        }

    def health(self) -> dict[str, Any]:
        """连通性健康检查（不抛异常，失败信息放返回值里）。"""
        return db_module.check_connection()

    def write_allowed(self) -> tuple[bool, str]:
        """MySQL 版恒允许写入（容量保护是 Redis 特有的问题，理由见模块文档）。"""
        return True, ""

    def close(self) -> None:
        db_module.dispose_engine()
