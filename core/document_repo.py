"""
document 表的仓储 —— 文档元数据与解析状态的**唯一**读写入口。

--------------------------------------------------------------------------
为什么单独开一层，而不是让路由直接写 SQL
--------------------------------------------------------------------------
`api/routes/documents.py` 现在是「同步解析」的写法，改造后它要面对的是
一个**状态机**（pending → parsing → success / fail），而不是一次写操作。
状态机的每一条边都有并发含义：

    「提交上传」与「Worker 抢任务」是两个进程同时干的事
    「删除文档」与「正在解析」可能撞在一起
    「手动重解析」要能把 fail / 卡住的 parsing 拉回 pending

把这些 UPDATE 语句散在路由里，等于把并发正确性交给「读代码的人是否细心」。
所以收敛到这里，每个函数名就是一条状态迁移，路由只做参数校验与协议转换。

--------------------------------------------------------------------------
原子抢任务：为什么必须在**一条** UPDATE 里完成
--------------------------------------------------------------------------
朴素写法是「先 SELECT 看状态，再 UPDATE」：

    if get(doc_id).status in ('pending', 'fail'):     # ← 竞态窗口
        update(doc_id, status='parsing')

两个 worker 同时读到 pending，两个都去 UPDATE，同一份文件被解析两遍。
两遍的后果不是「多几条切片」——写入前会按 doc_id 删旧数据，所以最后是
「谁后写完谁赢」，数据不一定错；但**嵌入算力白烧一遍**，且日志上看不出异常。

正确写法是把判断塞进 WHERE：

    UPDATE document SET status='parsing', ...
    WHERE id=? AND status IN ('pending','fail')

MySQL 会给这一行加排他锁，两个 UPDATE 串行执行，第二个的 `affected_rows`
是 0。**靠影响行数判断「是否抢到」，不靠事前的 SELECT。**

--------------------------------------------------------------------------
为什么抢任务的 WHERE 里还有一段「stale parsing」
--------------------------------------------------------------------------
见迁移 0002 的文件头（那里讲得最细）。一句话版本：worker 被 `kill -9`
时记录会冻在 `parsing`，而 `status IN ('pending','fail')` 不含 parsing，
这条记录就永远捡不回来了。加一段「parsing 且已开始太久」的兜底分支，
让孤儿任务能被重新抢走，多 worker 下也天然正确（判断依据是单条记录自己
的时间戳，不是全局重置）。

--------------------------------------------------------------------------
为什么 fail_reason 要截断
--------------------------------------------------------------------------
列宽是 `VARCHAR(512)`。越界时 MySQL 在**非严格模式**下会静默截断，
在严格模式下会直接报 1366 错误 —— 而「解析失败」这条路径上再抛一个
数据库异常，会把「这个文件为什么失败」的真实原因彻底盖掉，
排查时看到的是「写库失败」而不是「PDF 加密」。
所以在 Python 侧先截断，保证写失败原因的代码本身不会失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
# ⚠️ 必须别名导入：本模块自己有一个公开的 `delete(doc_id)`（删一条文档记录），
#    若直接 `from sqlalchemy import delete`，模块级 def 会**遮蔽**这个导入，
#    `delete(document_table)` 就变成调用自己 —— 无限递归。
#    症状是 RecursionError（而不是 NameError），栈里看到的是 SQLAlchemy 的
#    Session 构造，方向完全跑偏。别名 + 这行注释是唯一可靠的防复发手段。
from sqlalchemy import delete as sa_delete

from config.settings import settings
from core.db import now_db, session_scope
from core.schema import document_table

logger = logging.getLogger(__name__)

# 解析状态机取值。与 `alembic/versions/0001` 的列注释、以及
# `core/schema.py` 的注释保持三处一致（迁移是冻结快照，所以无法共享常量）。
STATUS_PENDING = "pending"
STATUS_PARSING = "parsing"
STATUS_SUCCESS = "success"
STATUS_FAIL = "fail"
ALL_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_PARSING, STATUS_SUCCESS, STATUS_FAIL)

# 与 document.file_name 的列宽一致（VARCHAR(255)）
_MAX_FILE_NAME_LEN = 255
# 与 document.fail_reason 的列宽一致（VARCHAR(512)）
_MAX_FAIL_REASON_LEN = 512
# 与 document.project_id 的列宽一致（VARCHAR(64)）
_MAX_PROJECT_ID_LEN = 64


@dataclass
class DocumentRecord:
    """一条文档记录。字段与 document 表一一对应，不做任何加工。"""

    doc_id: int
    project_id: str
    file_name: str
    storage_path: str
    file_size: int
    status: str
    chunk_count: int
    fail_reason: str
    attempt_count: int
    upload_time: datetime
    parse_started_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """
        转成 JSON 友好的 dict。

        为什么把时间转成 ISO 字符串而不是交给 FastAPI 自己序列化：
        `upload_time` 是 DATETIME（秒精度）、`parse_started_at` 是 DATETIME(6)
        （微秒精度），两个字段的序列化结果格式会不一致，前端做时间比较时
        容易踩坑。在这里统一成 ISO 格式，出口只有一个形状。
        """
        return {
            "doc_id": self.doc_id,
            "project_id": self.project_id,
            "file_name": self.file_name,
            "storage_path": self.storage_path,
            "file_size": self.file_size,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "fail_reason": self.fail_reason,
            "attempt_count": self.attempt_count,
            "upload_time": self.upload_time.isoformat(sep=" ", timespec="seconds"),
            "parse_started_at": (
                self.parse_started_at.isoformat(sep=" ", timespec="microseconds")
                if self.parse_started_at
                else None
            ),
        }


def _row_to_record(row: Any) -> DocumentRecord:
    """
    把 SQLAlchemy Row 映射成 dataclass。

    用显式字段名而不是 `DocumentRecord(**row._mapping)`：后者在表加列之后
    会因为多出一个未知关键字参数而**在运行时**抛 TypeError，而这里会
    安静地忽略新列 —— 前者更响亮，但失败点在「读文档」而不是「写文档」，
    本模块的取舍是读路径容忍多列（列只增不减），写路径严格按白名单。
    """
    return DocumentRecord(
        doc_id=int(row.id),
        project_id=row.project_id,
        file_name=row.file_name,
        storage_path=row.storage_path,
        file_size=int(row.file_size),
        status=row.status,
        chunk_count=int(row.chunk_count),
        fail_reason=row.fail_reason or "",
        attempt_count=int(row.attempt_count),
        upload_time=row.upload_time,
        parse_started_at=row.parse_started_at,
    )


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def create_pending(
    *,
    file_name: str,
    storage_path: str,
    file_size: int,
    project_id: str = "default",
) -> int:
    """
    插一条 `status=pending` 的记录，返回自增 doc_id。

    注意这里**不做「同项目同名替换」**：那件事要和磁盘、Chroma 的删除一起
    做成三件事，属于应用层的编排（见 `api/routes/documents.py` 的
    `_purge_document`），仓储层只负责「插一行」这一个动作。
    """
    values = {
        "project_id": (project_id or "default")[:_MAX_PROJECT_ID_LEN],
        "file_name": file_name[:_MAX_FILE_NAME_LEN],
        "storage_path": storage_path,
        "file_size": int(file_size),
        "status": STATUS_PENDING,
    }
    with session_scope() as session:
        result = session.execute(document_table.insert().values(**values))
        doc_id = int(result.inserted_primary_key[0])
    logger.info("文档记录已创建 | doc_id=%s | file=%s | size=%s", doc_id, file_name, file_size)
    return doc_id


def try_claim(doc_id: int, *, stale_seconds: int | None = None) -> bool:
    """
    原子抢任务：把文档从「可解析」推进到 `parsing`。**返回是否抢到**。

    可解析 = `pending` / `fail`，或「已经 parsing 但开始时间早于孤儿阈值」。

    为什么 fail 也可被抢：手动重解析接口会把状态重置成 pending，
    但 Redis 重启丢任务时，直接对 `fail` 记录重新入队更省事 ——
    不必先写一次 pending 再抢，少一次状态迁移就少一个中间态。

    :param stale_seconds: 孤儿判定阈值。None 时取 settings.TASK_STALE_PARSING_SECONDS
    """
    stale = settings.TASK_STALE_PARSING_SECONDS if stale_seconds is None else stale_seconds
    started_at = now_db()
    cutoff = started_at - timedelta(seconds=stale)

    claimable = or_(
        document_table.c.status.in_([STATUS_PENDING, STATUS_FAIL]),
        and_(
            document_table.c.status == STATUS_PARSING,
            document_table.c.parse_started_at.is_not(None),
            document_table.c.parse_started_at < cutoff,
        ),
    )
    stmt = (
        update(document_table)
        .where(document_table.c.id == doc_id)
        .where(claimable)
        .values(
            status=STATUS_PARSING,
            attempt_count=document_table.c.attempt_count + 1,
            parse_started_at=started_at,
            fail_reason="",
        )
    )
    with session_scope() as session:
        affected = session.execute(stmt).rowcount

    claimed = affected == 1
    if claimed:
        # 不在这里报 attempt_count：拿到新值要多一次 SELECT，而这个日志只用于确认
        # 「谁抢到了」。需要次数的场景（例如判断是否反复失败）读记录即可。
        logger.info("已抢到解析任务 | doc_id=%s", doc_id)
    else:
        # 影响行数 0 有两种含义，都是「本次不该干活」：别人已抢走 / 状态已不该解析。
        # 刻意区分不出是哪一种 —— 区分它需要额外一次 SELECT，而两种情况的处置完全一样。
        logger.info("无需解析（已被抢走或状态不允许） | doc_id=%s", doc_id)
    return claimed


def mark_success(doc_id: int, chunk_count: int) -> bool:
    """解析成功：写成功状态 + 切片数，并清掉开始时间（不再有任务在跑）。"""
    stmt = (
        update(document_table)
        .where(document_table.c.id == doc_id)
        .values(
            status=STATUS_SUCCESS,
            chunk_count=int(chunk_count),
            fail_reason="",
            parse_started_at=None,
        )
    )
    with session_scope() as session:
        affected = session.execute(stmt).rowcount
    logger.info("解析成功 | doc_id=%s | 片段数=%s", doc_id, chunk_count)
    return affected == 1


def mark_fail(doc_id: int, reason: str) -> bool:
    """解析失败：写失败状态与**可读**原因。切片数归零，避免展示上一次残留的数。"""
    safe_reason = (reason or "").strip()[:_MAX_FAIL_REASON_LEN]
    stmt = (
        update(document_table)
        .where(document_table.c.id == doc_id)
        .values(
            status=STATUS_FAIL,
            chunk_count=0,
            fail_reason=safe_reason,
            parse_started_at=None,
        )
    )
    with session_scope() as session:
        affected = session.execute(stmt).rowcount
    logger.warning("解析失败 | doc_id=%s | 原因=%s", doc_id, safe_reason)
    return affected == 1


def reset_for_reparse(doc_id: int) -> bool:
    """
    手动重解析：把记录拉回 `pending`。

    允许的源状态：`fail` 与「卡住的 parsing」。后者与新任务抢任务时的
    孤儿判定不同 —— 这里是**人工**发起的，所以不看时间戳，只要调用方
    确认要重跑就重跑（否则用户点了「重试」却因为没到孤儿阈值而毫无反应，
    体验上是个说不通的死按钮）。

    不允许 success：想重跑成功的文档应当先删再传，否则「重解析」会变成
    一个语义模糊的操作（你期望它删掉旧切片重建，还是叠加？）。
    """
    stmt = (
        update(document_table)
        .where(document_table.c.id == doc_id)
        .where(document_table.c.status.in_([STATUS_FAIL, STATUS_PARSING, STATUS_PENDING]))
        .values(status=STATUS_PENDING, fail_reason="", parse_started_at=None)
    )
    with session_scope() as session:
        affected = session.execute(stmt).rowcount
    if affected == 1:
        logger.info("已重置为待解析 | doc_id=%s", doc_id)
    return affected == 1


def delete(doc_id: int) -> bool:
    """删除记录。返回是否真的删掉了一行（用于区分「删了」与「本来就没有」）。"""
    with session_scope() as session:
        affected = session.execute(
            sa_delete(document_table).where(document_table.c.id == doc_id)
        ).rowcount
    if affected:
        logger.info("文档记录已删除 | doc_id=%s", doc_id)
    return affected == 1


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get(doc_id: int) -> DocumentRecord | None:
    """按 doc_id 取一条。不存在返回 None（不抛异常 —— 接口层要把它映射成 404）。"""
    with session_scope() as session:
        row = session.execute(
            select(document_table).where(document_table.c.id == doc_id)
        ).first()
    return _row_to_record(row) if row else None


def find_by_name(file_name: str, project_id: str = "default") -> DocumentRecord | None:
    """
    按「项目 + 原始文件名」查最近一条。用于上传时判定「是否同名重传」。

    为什么按 file_name 而不是 storage_path 判重：
    storage_path 是 uuid 名，每次上传都不同，用它判重等于永不重复 ——
    但用户上传两次同一个文件时，知识库里会出现两份内容完全相同的文档，
    检索结果被自己挤占（Top-K 里同内容占满）。同名替换是既有行为，
    本函数是它的依据。

    排序取 upload_time 最大的一条，而不是要求唯一：不做唯一约束是因为
    「同名」本身不是业务主键（同名文件可以合法地分属不同项目），
    而且加唯一约束会在历史脏数据上直接建表失败。
    """
    stmt = (
        select(document_table)
        .where(document_table.c.project_id == (project_id or "default"))
        .where(document_table.c.file_name == file_name[:_MAX_FILE_NAME_LEN])
        .order_by(document_table.c.upload_time.desc(), document_table.c.id.desc())
        .limit(1)
    )
    with session_scope() as session:
        row = session.execute(stmt).first()
    return _row_to_record(row) if row else None


def list_documents(
    *,
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[DocumentRecord]:
    """按状态 / 项目过滤列出文档，最新的排前面。"""
    stmt = select(document_table)
    if status:
        stmt = stmt.where(document_table.c.status == status)
    if project_id:
        stmt = stmt.where(document_table.c.project_id == project_id)
    stmt = (
        stmt.order_by(document_table.c.upload_time.desc(), document_table.c.id.desc())
        .limit(max(1, int(limit)))
        .offset(max(0, int(offset)))
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [_row_to_record(r) for r in rows]


def counts_by_status(project_id: str | None = None) -> dict[str, int]:
    """
    各状态的文档数。四种状态**总是**全量出现（没有的补 0）——
    否则前端拿到 `{"success": 3}` 时无法区分「没有失败的」和「失败字段没实现」。
    """
    stmt = select(document_table.c.status, func.count()).group_by(document_table.c.status)
    if project_id:
        stmt = stmt.where(document_table.c.project_id == project_id)
    with session_scope() as session:
        rows = session.execute(stmt).all()

    counts = {s: 0 for s in ALL_STATUSES}
    for value, number in rows:
        if value in counts:
            counts[value] = int(number)
    counts["total"] = sum(counts[s] for s in ALL_STATUSES)
    return counts


def list_orphan_parsing(stale_seconds: int | None = None) -> list[int]:
    """
    列出「卡住的 parsing」（孤儿任务）的 doc_id。

    用途：worker 启动时或人工排查时把孤儿任务重新入队。
    注意**不要**在 worker 启动时无条件把 parsing 全改回 pending ——
    多 worker 下那会打断正在跑的兄弟进程（理由见迁移 0002 文件头）。
    本函数只负责**列出**，是否处理交给调用方，避免仓储层偷偷改状态。
    """
    stale = settings.TASK_STALE_PARSING_SECONDS if stale_seconds is None else stale_seconds
    cutoff = now_db() - timedelta(seconds=stale)
    stmt = (
        select(document_table.c.id)
        .where(document_table.c.status == STATUS_PARSING)
        .where(or_(
            document_table.c.parse_started_at.is_(None),
            document_table.c.parse_started_at < cutoff,
        ))
        .order_by(document_table.c.id)
    )
    with session_scope() as session:
        return [int(r[0]) for r in session.execute(stmt).all()]
