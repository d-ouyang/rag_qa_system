"""
MySQL 连接层 —— 只负责「怎么连」，不负责「表长什么样」。

--------------------------------------------------------------------------
职责边界（为什么要把这张表划清楚）
--------------------------------------------------------------------------
    core/db.py      连接 / 连接池 / 事务边界            ← 本模块
    core/schema.py  五张表的 Python 侧定义（查询用）
    alembic/versions/*.py  DDL（建表 / 改表的历史快照）

三者的关系：`alembic` 负责把库「变成」某个形状，`schema.py` 是应用侧
「照着这个形状读写」的类型化视图。**两边刻意不共享同一份定义** —— 理由见
`alembic/versions/0001_*.py` 的文件头（迁移必须是冻结的历史快照，
不能随模型演进一起变，否则老迁移在新代码下会执行失败）。

--------------------------------------------------------------------------
为什么是同步 SQLAlchemy 而不是 async
--------------------------------------------------------------------------
`MemoryManager` 整条链路是同步的（FastAPI 的同步 def 路由跑在线程池里）。
为了用上 `AsyncSession` 而把 MemoryManager / rag_chain / qa.py 全改成 async，
是「技术选型倒挂业务复杂度」的典型 —— 收益只是省几个线程，代价是把一条
已经测过、语义微妙（截断必须向下取偶、meta 与轮数对齐）的链路全部重写。

--------------------------------------------------------------------------
为什么 Engine 是进程内单例
--------------------------------------------------------------------------
`create_engine` 每次调用都会新建一个连接池。若在每次请求里调一次，
连接池就形同虚设（每请求新池 → 每请求新建 TCP 连接 + MySQL 握手 + 认证），
而且旧池不会被回收，几分钟就能把 MySQL 的 max_connections 打满。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings

logger = logging.getLogger(__name__)

# 进程内单例。之所以用模块级变量而不是 functools.lru_cache：
# dispose_engine() 需要把它置回 None（测试里换库、进程优雅退出时都要用）。
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """返回进程内共享的 Engine（懒创建）。"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.MYSQL_URL,
            # ---- 连接池 ----
            pool_size=settings.MYSQL_POOL_SIZE,
            max_overflow=settings.MYSQL_MAX_OVERFLOW,
            # ---- 连接活性 ----
            # pre_ping：借出前先探活。不开的话，MySQL 8 的 wait_timeout 把空闲连接
            # 单方面掐掉后，池子里仍留着「僵尸连接」，下一个请求直接 Lost connection 2006。
            # 这类故障只在「低谷后突发流量」时出现，没有它基本定位不到。
            pool_pre_ping=settings.MYSQL_POOL_PRE_PING,
            # recycle：主动回收早于服务端动手，把「服务端先掐」变成「客户端先换」。
            pool_recycle=settings.MYSQL_POOL_RECYCLE_SECONDS,
            # ---- 超时 ----
            # 不设 connect_timeout 时，MySQL 不可达会让请求线程卡在 TCP 握手上很久，
            # 表现为「接口没报错但也一直不返回」，比直接失败更难排查。
            connect_args={"connect_timeout": settings.MYSQL_CONNECT_TIMEOUT},
            echo=settings.MYSQL_ECHO_SQL,
            future=True,
        )
        logger.info(
            "MySQL Engine 已创建 | %s:%s/%s pool=%d+%d pre_ping=%s",
            settings.MYSQL_HOST,
            settings.MYSQL_PORT,
            settings.MYSQL_DATABASE,
            settings.MYSQL_POOL_SIZE,
            settings.MYSQL_MAX_OVERFLOW,
            settings.MYSQL_POOL_PRE_PING,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回 Session 工厂（懒创建，与 Engine 共享生命周期）。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            # autoflush=False：本项目的手写 diff 逻辑里，flush 时机必须由我们显式控制。
            # 留着 autoflush 会在任意一次 query 前把未完成的 INSERT 提前刷出去，
            # 让「先查后写」的顺序变得不可预测。
            autoflush=False,
            # expire_on_commit=False：commit 后仍可读对象属性。
            # 默认 True 会在 commit 后把对象标记为过期，下次访问属性就再发一条 SELECT ——
            # 在「commit 后立刻把结果返回给调用方」的模式下纯属多余往返。
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    事务上下文：正常提交 / 异常回滚 / 最终关闭。

    统一走这个上下文，是为了让「忘记 rollback」和「忘记 close」这两类
    连接泄漏在代码层面不可能发生 —— 池子被漏几次就再也借不出连接了。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def now_db() -> datetime:
    """
    项目统一的「当前时间」口径：**naive 本地时区**。

    为什么要有这个函数而不是各处直接 datetime.now()：
    「用哪个时区写库」必须是一个能被搜索到的单点决策。散落的 datetime.now()
    会让下一个人无从判断「这个值到底是本地时间还是 UTC」，最后只能靠逐个试。

    当前口径与 MySQL 容器 `--default-time-zone=+08:00` 对齐（见 docker-compose.yml）：
    写入的是本地挂钟时间。这不是最优解（最优是全程 UTC + 展示层转换），
    但它与 P0-1 的 `MySQLSessionStore` 保持一致 —— 两处口径不同会让
    「会话时间对得上、文档时间对不上」这种问题变得极难定位。
    时区问题已在 p0.1 迭代文档「已知边界」中登记，统一改造留到 P2-8 配置治理。
    """
    return datetime.now()


def check_connection() -> dict[str, Any]:
    """
    连通性健康检查。**不抛异常**，失败信息放在返回值里。

    为什么不抛：健康检查是所有接口里最不能挂的一个 —— 它自己挂掉，
    监控看到的就是「接口 500」而不是「数据库不可达」，排查方向直接跑偏。
    """
    target = f"{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DATABASE}"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"backend": "mysql", "ok": True, "detail": target}
    except Exception as e:  # noqa: BLE001 - 健康检查要吞掉一切异常
        return {"backend": "mysql", "ok": False, "detail": f"{target} → {type(e).__name__}: {e}"}


def dispose_engine() -> None:
    """释放连接池。测试换库、进程退出时调用。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
        logger.info("MySQL Engine 已释放")
    _engine = None
    _session_factory = None
