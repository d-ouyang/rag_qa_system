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
`MemoryManager`、`rag_chain`、`qa.py` **在 P2-12c 之前一行未改**。靠的是
`SessionStore` 抽象：本模块只实现 load/save/delete/exists/list_ids/purge_expired/
stats/health/session_lock/write_allowed 这一组方法，
把「快照 ↔ 数据行」的翻译关在内部。

> P2-12c 修订：抽象层为了加 `owner_id`，五个读写方法的签名变了，
> 于是 `MemoryManager` 与 `qa.py` 也跟着改了 —— **零改动的前提是接口不变**，
> 接口一改，抽象层就要在调用侧「补齐」这个新维度。
> 但「翻译」仍然关在存储层内：调用方传的是 `owner_id: int | None`，
> 完全不知道底下是 `user_id` 列还是并行字典，也不需要知道。

能做到这一点，是因为抽象层当初选对了粒度 —— 接口是**整条会话快照**，
不是「单条消息的 CRUD」。于是存储层有空间把「一次覆盖写」自由地翻译成
行级 INSERT / UPDATE / DELETE，而逻辑层完全不必知道。

--------------------------------------------------------------------------
三条容易写错的语义（都对齐 MemorySessionStore，两边跑同一套断言）
--------------------------------------------------------------------------
1. **`save` 会改写 `last_active`**：不是「存调用方传进来的值」，而是「记为当前时刻」。
   只要发生一次真实写入，就说明这个会话是活跃的。两个后端必须一致，
   否则「换后端」就等于换了个 bug。
2. **`load(touch=True)` 续期，`touch=False` 不续期**。会话列表这种「扫一眼」的读
   必须传 False —— 刷新列表不算「打开了这条会话」，否则「多久没打开」
   会被刷列表这个动作冲掉。
3. **`session.user_id` 是归属的唯一判据，且由本层独家写入**（P2-12c）。
   列和索引 `idx_session_user_active` 从 P0-1b 就建好了，但直到12c 才有人写它 ——
   在此之前这一列恒为 NULL，而任何地方都没在查询里用它，于是
   「任何登录用户都能读任何人的会话」。
   本版起：五个读写方法全部带 `owner_id`，谓词是统一的 `_owner_match()`
   （`user_id = :owner OR (user_id IS NULL AND :owner IS NOT NULL)`）；
   `save()` 负责三态（新建 / 认领无主 / 拒绝他人，详见其注释）。

   代价与遗留：
     · **无主会话对所有登录用户可见**（直到第一个人写入并认领它）。
       这是为「存量会话归谁」选的确定答案 —— 归第一个打开它的人，
       而不是永远躺在列表里没人认领。当前库里 `session` 表 0 行，
       所以这个窗口在真实数据上不存在；将来若有历史数据要处理，
       应写一次性脚本按业务键回填，而不是靠「谁先点谁认领」。
     · **认领后 `last_active_at` 会被刷新**：认领本身就是一次写入，
       所以行为与Memory 版一致（那边也是 save 时无条件刷）。
     · 谓词里的 `OR user_id IS NULL` 用不上 `idx_session_user_active`
       的最左前缀优化，实测会话量级（个人部署几千条内）不需要拆。
       真要优化，正确做法是给 `list_ids` 分两支查再在内存里合，
       而不是去掉 OR —— 去掉 OR 就没有「认领」了。

--------------------------------------------------------------------------
刻意的取舍
--------------------------------------------------------------------------
· **闲置不再隐藏会话**：`load` / `list_ids` 无视 TTL 和 `is_archived`。
  `last_active_at` 仍在写入和打开时更新，用来回答「多久没再问」。
  `purge_expired()` 不再打归档标记。用户点删除才删行。
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

from sqlalchemy import and_ as sa_and  # noqa: F401  (保留给后续分支条件用)
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy import or_ as sa_or
from sqlalchemy.orm import Session

from config.settings import settings
from core import db as db_module
from core.schema import chat_message_table as MSG
from core.schema import session_table as SESS
from core.session_store import SessionOwnershipError, SessionSnapshot, SessionStore

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


# --------------------------------------------------------------------------- #
# 归属谓词（P2-12c）
# --------------------------------------------------------------------------- #
def _owner_match(owner_id: int | None) -> Any:
    """
    「这条会话属于 owner_id」的 SQL 条件，返回可直接进 `where()` 的表达式。

    ⚠️ **不是** `user_id = :owner`，因为那条对无主会话全是 NULL 判定
    （`user_id = NULL` → NULL → WHERE 不成立 → 一条无主会话都查不出来）。

    两条分支：
        owner_id=None    → `user_id IS NULL`（只认无主；运维/测试口径）
        owner_id=440     → `user_id = 440 OR user_id IS NULL`（我的 + 无主的）

    ⚠️ 第二种形态里那个 `OR user_id IS NULL` 是**功能性的**，不是偷懒：
    「认领」要求无主会话对所有人**可见**。若读侧看不见，用户第一次打开
    12c 之前留下的会话时 load 返回 None，逻辑层会当成「会话不存在」而
    从零重建，随后 save 覆盖掉原有内容 —— 存量内容静默丢失。
    「认领」最终只在 `save()` 里落定（那里显式处理三态），读侧只负责让路。

    取舍：这条 OR 用不上 `idx_session_user_active` 的最左前缀，
    实测会话量级（个人部署几千条内）不需要拆。真要优化，
    正确做法是分两支查再在内存里合，而不是去掉 OR ——
    去掉 OR 就没有「认领」了。
    """
    if owner_id is None:
        return SESS.c.user_id.is_(None)
    return sa_or(SESS.c.user_id == owner_id, SESS.c.user_id.is_(None))


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

        这个键在 P0-1b 建表时就把列备好了（`chat_message.ref_ids`），
        但直到 **P0-4a** 才真正有值 —— 在此之前 `_extract_sources()` 产出的
        source 只有 `index / source(磁盘路径) / snippet / 两个分数`，没有 chunk_id，
        于是本列恒为 `[]`。

        P0-4a 之后：`core/parsing.py` 在写入切片时把 `chunk_id`
        （`"<doc_id>:<chunk_index>"`）写进 Chroma metadata，`_extract_sources()`
        把它带进 sources，这里就能读到了。前端据此调
        `GET /api/v1/chunks/{chunk_id}` 反查那段引用的完整正文。

        宽容度是有意的：老切片（P0-3 之前入库、连 doc_id 都没有）经
        `_resolve_chunk_id()` 会得到 None，这里跳过而不写 null ——
        存一个 null 进数组会让「引用了几条」这个计数虚高。
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
    def load(self, session_id: str, *, owner_id: int | None,
             touch: bool = True) -> SessionSnapshot | None:
        """
        读会话快照；不存在 / **不属于 owner_id** 都返回 None。

        ⚠️ 归属条件与 `SELECT ... FOR UPDATE` 的锁在**同一条 where**里：
        先按 id 锁行、再按归属过滤的话，锁的是别人的行、判的是自己的条件，
        中间一旦有并发 save 就会读到不该读的东西。合成一条最简单。
        """
        now = time.time()
        with self._tx(session_id) as session:
            row = session.execute(
                select(SESS).where(SESS.c.id == session_id, _owner_match(owner_id))
            ).mappings().first()
            if row is None:
                return None

            if touch:
                # 按 id 即可：上面已经确认这条会话属于 owner_id 了。
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

    def save(self, session_id: str, snapshot: SessionSnapshot, *,
             owner_id: int | None) -> None:
        """
        整条覆盖写入（存在即覆盖，不存在即创建），并刷新 last_active。

        归属三态（P2-12c 的安全边界都在这里）：
            库里没有                → 新建，`user_id = owner_id`
            库里有且 user_id IS NULL → **认领**（`user_id` 写成 owner_id）
            库里有且属于别人        → **抛 SessionOwnershipError**，一个字都不写
        第三条为什么必须在写之前判：一旦先写了 messages 再发现越权，
        对方的会话已经被污染（而且是在同一个事务里，不 rollback 就留在库里）。
        反过来，先判归属再写，就不存在「写到一半才拒绝」。

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

            # 先查归属（拿 user_id 本体，不用 _owner_match —— 这里要区分
            # 「不存在 / 无主 / 属于别人」三种，而谓词把它们压成了两个）
            current_owner = session.execute(
                select(SESS.c.user_id).where(SESS.c.id == session_id)
            ).scalar_one_or_none()

            if current_owner is not None and current_owner != owner_id:
                # 属于别人。抛异常而不是静默忽略 ——
                # 静默忽略的话，调用方（rag_chain）会以为「写成功了」，
                # 而这轮问答根本没存下来，用户刷新页面才发现自己的话没了。
                raise SessionOwnershipError(session_id, owner_id)

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
                # 归属落库：新建或认领都写 owner_id；owner_id=None 时写 NULL
                #（这正是「owner_id=None 只认无主、写入仍保持无主」的循环关系）
                "user_id": owner_id,
            }

            exists = session.execute(
                select(SESS.c.id).where(SESS.c.id == session_id)
            ).scalar() is not None
            if exists:
                session.execute(update(SESS).where(SESS.c.id == session_id).values(**session_values))
                if current_owner is None and owner_id is not None:
                    logger.info("认领无主会话 | session_id=%s新主人=%s", session_id, owner_id)
            else:
                session.execute(insert(SESS).values(id=session_id, **session_values))
                logger.debug("新建会话 | session_id=%s owner=%s", session_id, owner_id)

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

    def delete(self, session_id: str, *, owner_id: int | None) -> bool:
        """
        删除会话及其全部消息；返回是否真实删除。幂等。

        ⚠️ 两条 delete 都**必须带归属条件**，不只是 SESS 那条：
        消息表是按 session_id 筛的，若先删消息、后发现会话不属于自己，
        对方的聊天记录已经没了（而且 delete 的 rowcount 不会帮你撤销）。
        所以先用带归属的 SESS delete 拿到 rowcount，>0 才动消息；
        顺序反过来也安全（消息删了但 SESS 没删干净 → 返回 False 且残留会话）。
        这里选「先 SESS 后MSG」，因为 SESS 的 rowcount 是「是否真的属于我且删掉了」
        这个唯一可信的判据。
        """
        with self.session_lock(session_id):
            session = self._require_held(session_id)
            existed = (session.execute(
                delete(SESS).where(SESS.c.id == session_id, _owner_match(owner_id))
            ).rowcount or 0) > 0
            removed = 0
            if existed:
                # 没有 FK 级联，所以消息必须显式删 —— 否则留下的是永远查不到、
                # 也永远删不掉的孤儿行
                removed = session.execute(
                    delete(MSG).where(MSG.c.session_id == session_id)
                ).rowcount or 0
        if existed:
            logger.info("会话已删除 | session_id=%s owner=%s 消息=%d 行",
                        session_id, owner_id, removed)
        return existed

    def exists(self, session_id: str, *, owner_id: int | None = None) -> bool:
        with self._tx(session_id) as session:
            found = session.execute(
                select(SESS.c.id).where(SESS.c.id == session_id, _owner_match(owner_id))
            ).first()
        return found is not None

    def list_ids(self, *, owner_id: int | None = None) -> list[str]:
        """列出会话 id（**只有** owner_id 本人的 + 无主的），按最后活跃倒序。"""
        with self._tx() as session:
            rows = session.execute(
                select(SESS.c.id).where(_owner_match(owner_id)).order_by(SESS.c.last_active_at.desc())
            ).scalars().all()
        return [str(r) for r in rows]

    def is_foreign_to(self, session_id: str, *, owner_id: int | None) -> bool:
        """只查 user_id 这一列（不 SELECT *），存在且非空且不是我 → True。"""
        with self._tx(session_id) as session:
            actual = session.execute(
                select(SESS.c.user_id).where(SESS.c.id == session_id)
            ).scalar_one_or_none()
        if actual is None:
            # 「不存在」与「无主」在这里被压成同一个 None。
            # 对这个方法而言**恰好是对的**：两者都不是 foreign ——
            # 无主会话对所有人可认领，不该被判成「别人的」。
            # 代价是「不存在」也要查一次，但调用方本来就已经查过了，
            # 这条只是把「403 还是 200+空」的判断收敛到一个方法里。
            return False
        # owner_id=None 时任何已归属会话都不是自己的（它是别人的，只是我不知道是谁）
        return True if owner_id is None else int(actual) != int(owner_id)

    def purge_expired(self) -> int:
        """不再按闲置时间归档。会话留在列表里，直到用户删除。"""
        return 0

    def stats(self) -> dict[str, Any]:
        """
        存储层统计。

        ⚠️ 与 Memory / Redis 版**有意不同**：不提供 `avg_session_bytes` / `bytes_estimate`。
        那两个字段是为回答「Redis 会不会被写爆」而生的容量估算；
        MySQL 的容量瓶颈是磁盘，暴露一个基于采样的字节数只会误导人。
        取而代之给真实值：会话数、消息行数、以及表的实际占用（information_schema，代价 O(1)）。
        闲置不再把会话排除在计数之外。
        """
        with self._tx() as session:
            session_count = session.execute(
                select(func.count()).select_from(SESS)
            ).scalar() or 0
            message_count = session.execute(
                select(func.count()).select_from(MSG)
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
