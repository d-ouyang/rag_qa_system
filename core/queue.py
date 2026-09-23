"""
入队抽象 —— 把「投一条解析任务」这件事与 Celery 的 API 隔开。

--------------------------------------------------------------------------
为什么要有这一层
--------------------------------------------------------------------------
两个理由，一个是设计的，一个是测试的。

**设计上**：入队是「尽力而为」的动作，不是「必须成功」的动作。
真相源是 MySQL 的 `document.status`：投递失败时文档停在 `pending`，
用户点一下「重解析」就能补上，或者等 Redis 恢复后人工补投。
这个语义差异很重要 —— 如果入队失败就直接 500，用户会以为「文件没上传成功」，
于是他重新传一遍，于是磁盘上多一份、MySQL 里多一条。
所以接口层需要的是一个 `bool`，而不是一个会抛的调用。

**测试上**：把这个函数打桩掉，就能在没有 Redis、没有 worker 的情况下
完整验证「上传 → 落库 → 返回」这条链路。若路由里直接写
`celery_app.send_task(...)`，任何测上传的用例都得先起一个 broker。

--------------------------------------------------------------------------
为什么延迟 import worker.app
--------------------------------------------------------------------------
`core/` 是被 Web 进程、worker 进程、运维脚本共同依赖的底座。
若在模块顶层 `from worker.app import celery_app`，就等于让
「core 的 import」依赖「worker 包能 import 成功」——
而 worker 包又 import 了 celery。结果是：没装 celery 的环境里
连 `core.queue` 都 import 不了。放在函数里，这个依赖只在真的要用时成立。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


def enqueue_parse(doc_id: int) -> bool:
    """
    把一条解析任务推进队列。**返回是否投递成功，不抛异常**。

    调用方拿到 False 时应当：保留文档的 `pending` 状态，并在响应里
    如实告知「已入库，但排队失败」—— 不要假装成功，也不要回滚文件
    （文件已经落盘、记录已经建好，回滚反而制造不一致）。

    :param doc_id: document 表主键
    """
    target = int(doc_id)
    try:
        # 延迟导入，理由见模块 docstring
        from worker.app import celery_app
        from worker.tasks import PARSE_TASK_NAME

        celery_app.send_task(
            PARSE_TASK_NAME,
            args=[target],
            queue=settings.TASK_QUEUE_NAME,
        )
        logger.info("解析任务已入队 | doc_id=%s | 队列=%s", target, settings.TASK_QUEUE_NAME)
        return True
    except Exception as e:  # noqa: BLE001 - 投递失败是预期内的情况（Redis 挂了）
        # 用 ERROR 但**不抛**：这是需要人介入的状态（任务没进队列，文档不会自己好），
        # 所以要显眼；而抛出去会把「文件已上传成功」这件事一起抹掉。
        logger.error(
            "解析任务入队失败 | doc_id=%s | 队列=%s | 错误：%s: %s。"
            "文档会停在 pending，Redis 恢复后可用 POST /api/v1/documents/%s/reparse 补投",
            target, settings.TASK_QUEUE_NAME, type(e).__name__, e, target,
        )
        return False


def queue_depth() -> dict[str, object]:
    """
    读队列积压深度（`LLEN`）与 Redis 侧的可达性。**不抛异常**。

    为什么单独用一条裸 redis 连接而不是 Celery 的 API：
    Celery 没有「看一眼队列有多长」的接口（它的 inspect 面向 worker 状态）。
    而积压深度恰恰是运维最需要的那个数 —— 它先于 worker 状态变坏。

    ⚠️ 刻意**不复用** `core/redis_store.get_redis_client()`：那个客户端连的是
    db0（缓存库），而队列在 db1。共用一个客户端读不出队列长度，
    只能靠 `SELECT` 切库 —— 而连接是共享的，切库会污染其他线程的上下文。

    返回结构（键恒定出现，不可用时置 None / False）：
        ok             Redis 是否可达
        queue          队列名
        depth          待消费消息数（LLEN）
        unacked        已投递未被 ack 的消息数（Celery Redis 传输的 unacked 集合）
        detail         不可达时的原因
    """
    info: dict[str, object] = {
        "ok": False,
        "queue": settings.TASK_QUEUE_NAME,
        "depth": None,
        "unacked": None,
        "detail": "",
    }
    try:
        client = _get_queue_client()
        info["depth"] = int(client.llen(settings.TASK_QUEUE_NAME))
        # Celery 的 Redis 传输把「已投递未确认」的消息放在一个 ZSET 里，
        # key 名是「<队列名>_unacked」（早期版本无下划线）。取不到就报 0 ——
        # 这个数只是给运维一个量级感，不值得为版本差异做复杂的兼容探测。
        unacked_key = f"{settings.TASK_QUEUE_NAME}_unacked"
        info["unacked"] = int(client.zcard(unacked_key)) if client.exists(unacked_key) else 0
        info["ok"] = True
    except Exception as e:  # noqa: BLE001 - 队列不可达是预期内情况，不该让监控接口 500
        info["detail"] = f"redis db{_db_index(settings.REDIS_QUEUE_URL)} 不可达：{type(e).__name__}: {e}"
        logger.warning("读取队列深度失败：%s", info["detail"])
    return info


def _db_index(url: str) -> str:
    """从 redis://host:port/N 里取出库号，只用于把错误信息说清楚。"""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else "?"


def worker_alive() -> dict[str, object]:
    """
    问 worker「你还活着吗」。**不抛异常**。

    ⚠️ 这是个**阻塞调用**：Celery 的 `inspect().ping()` 会等满
    `QUEUE_WORKER_PING_TIMEOUT_SECONDS` 才返回 —— 它无从知道有几个 worker
    应该应答，只能把窗口开到超时才收。所以「接口要不要调它」取决于
    调用方能接受多慢。这也是为什么把这个数做成配置项而不是写死：
    写死 5 秒会让监控接口慢到没人接，写死 0.1 秒又会把正常但稍慢的 worker
    误判成死了。

    ⚠️ 不用 `inspect` 的 `active()` / `stats()`：那些会去问每个 worker 的
    运行态，更慢也更容易因为 worker 正在跑长任务而超时。存活探测只该问
    最小的问题。

    返回结构（键恒定出现）：
        ok       是否有 worker 应答
        workers  应答的 worker 名单（如 ["celery@host"]）
        detail   无应答或出错时的原因
    """
    info: dict[str, object] = {"ok": False, "workers": [], "detail": ""}
    timeout = float(settings.QUEUE_WORKER_PING_TIMEOUT_SECONDS)
    try:
        from worker.app import celery_app

        replies = celery_app.control.inspect(timeout=timeout).ping() or {}
        info["workers"] = sorted(str(name) for name in replies)
        info["ok"] = bool(replies)
        if not replies:
            info["detail"] = f"{timeout:g} 秒内没有 worker 应答（worker 未启动或已卡死）"
    except Exception as e:  # noqa: BLE001 - 探测失败就是「探测不到」，不是故障
        info["detail"] = f"{type(e).__name__}: {e}"
        logger.warning("worker 存活探测失败：%s", info["detail"])
    return info


# db1 的客户端单例。与 core/redis_store 的写法一致（全局 + 双检锁），
# 但因为连的是另一个库，必须是独立实例 —— 见 queue_depth() 的注释。
_queue_client: Any = None
_queue_client_lock = threading.Lock()


def _get_queue_client() -> Any:
    """获取队列库（db1）的 Redis 客户端，连接池复用。"""
    global _queue_client
    if _queue_client is None:
        with _queue_client_lock:
            if _queue_client is None:
                import redis  # 延迟 import：不用队列的环境（如纯检索脚本）不必装

                _queue_client = redis.Redis.from_url(
                    settings.REDIS_QUEUE_URL,
                    max_connections=int(settings.REDIS_MAX_CONNECTIONS),
                    socket_timeout=float(settings.REDIS_SOCKET_TIMEOUT),
                    socket_connect_timeout=float(settings.REDIS_CONNECT_TIMEOUT),
                    health_check_interval=30,
                    decode_responses=True,  # 这里只读计数，交给客户端 decode 更省事
                )
                logger.info("队列 Redis 客户端已创建 | url=%s", _redact(settings.REDIS_QUEUE_URL))
    return _queue_client


def reset_queue_client() -> None:
    """关闭并重置队列客户端（测试切换实例时用）。"""
    global _queue_client
    with _queue_client_lock:
        if _queue_client is not None:
            try:
                _queue_client.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("关闭队列 Redis 客户端出错（忽略）：%s", e)
            _queue_client = None


def _redact(url: str) -> str:
    """日志里抹掉 URL 中的密码（redis://user:pass@host 这种形态）。"""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"
