"""
Celery 应用 —— 解析任务的队列与投递语义配置。

--------------------------------------------------------------------------
定位：只做「投递与消费」，不做状态真相源
--------------------------------------------------------------------------
本应用**不配 result backend**（`task_ignore_result = True` + 不设 `result_backend`）。
理由是「同一份事实只存一处」：

    任务跑完没有？   → 看 MySQL `document.status`
    文档切了几片？   → 看 MySQL `document.chunk_count`
    失败原因是什么？ → 看 MySQL `document.fail_reason`

若再往 Redis 里存一份 Celery result，就有两份事实了。两份事实的代价不是
「多占点内存」，而是**它们迟早会不一致**，而且不一致时你无法判断该信谁：
任务结果说成功、MySQL 说 parsing —— 到底是真的成功只是状态没写回，
还是写回了但被覆盖了？没有判决依据，只能人工翻日志。

代价要认：拿不到「返回值」。所以任务的返回值只在**日志**里有意义，
对外可观测性一律走 MySQL + `/api/v1/system/queue`。

--------------------------------------------------------------------------
broker 用 db1，与缓存分库
--------------------------------------------------------------------------
`REDIS_QUEUE_URL = redis://.../1`。队列和缓存分库的好处是运维动作能分开做：
`FLUSHDB` 清缓存不会顺手把待解析任务清掉，`LLEN` 看积压也不会被缓存 key 干扰。

--------------------------------------------------------------------------
投递语义：acks_late + prefetch=1
--------------------------------------------------------------------------
    task_acks_late = True
        任务**执行完**才 ack。worker 在解析途中被 kill，消息不会被 ack，
        会被重新投递（Redis 传输下由 visibility_timeout 兜底）。
        若为 False（默认），消息一投出去就 ack —— worker 一崩任务就永久消失，
        而 MySQL 里那条记录会永远停在 parsing。这就是「消息丢了但没人知道」。

    worker_prefetch_multiplier = 1
        一次只预取 1 条。默认值 4 会让一个 worker 先抓 4 条到本地再慢慢跑，
        队列里看着「没有积压」其实任务全堆在 worker 内存里 ——
        这时候 kill 掉 worker，那几条未执行的任务虽然有 acks_late 保护，
        但队列深度观测会严重失真。

    task_reject_on_worker_lost = True
        子进程异常死亡时把任务退回队列，而不是标记为失败。

    broker_transport_options.visibility_timeout
        Redis 传输层无法感知消费者死亡，只能靠「超过这个时间还没 ack 就
        视为投递失败」把它还给队列。取值与 MySQL 侧的孤儿判定
        （TASK_STALE_PARSING_SECONDS）对齐：两边同时到点，不会出现
        「消息回来了但库里不让抢」或者「库里能抢了但消息还没回来」。
        ⚠️ 必须显著大于单次解析的硬超时，否则长任务会被重复投递。
"""

from __future__ import annotations

import logging
import sys

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging

from config.settings import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 进程池的选择：macOS 必须 solo，Linux 用默认 prefork
# --------------------------------------------------------------------------- #
# 这不是口味问题，是被上游决定的两件事夹出来的唯一解：
#
# ① billiard（Celery 的进程池实现）自 5.x 起在 darwin 上**默认 spawn**，
#    并在源码里写明理由（bpo-33725）：macOS 10.14 之后，fork 之后继续跑
#    任意代码不再可靠（原生库的线程/锁状态会被子进程继承坏）。所以
#    「强制改回 fork」是错的方向 —— 那是绕开一个真实的平台缺陷。
#
# ② 但 Celery 的 prefork 池依赖**父进程里准备好的模块级状态**
#    （`celery.app.trace._localized`），而 spawn 出来的是全新解释器，拿不到它。
#    上游本该在 spawn 路径上补 `setup_worker_optimizations()`，可那段补丁
#    只在环境变量 `FORKED_BY_MULTIPROCESSING` 存在时才走
#    （celery/concurrency/prefork.py 的 on_after_fork），darwin 下走不到。
#    结果是任务一收到就炸：`ValueError: not enough values to unpack
#    (expected 3, got 0)`，栈落在 celery/app/trace.py 的 fast_trace_task。
#
# 结论：macOS 上用 solo（不起子进程，天然绕开 ①②）。
# 代价要认：solo 不支持 `worker_max_tasks_per_child`，也就是**失去了
# 「定期回收进程防内存泄漏」这道保险**。本地开发可以接受（跑完就关）；
# 生产在 Linux 上跑 prefork，那道保险照常在。
#
# 顺带的好处：solo 下嵌入模型只加载一次、常驻同一个进程，
# 不像 prefork 每重建一个子进程就重载一遍模型。
_WORKER_POOL = "solo" if sys.platform == "darwin" else "prefork"

celery_app = Celery(
    "rag_qa_worker",
    # broker 只连队列库（db1）。刻意不传 backend= —— 本项目不用 result backend。
    broker=settings.REDIS_QUEUE_URL,
    # 任务模块显式列出来：worker 是独立进程，不会像 Web 那样「被 import 触发注册」。
    # 漏了这行，worker 能起来但所有任务都报 NotRegistered。
    include=["worker.tasks"],
)

_conf: dict[str, object] = {
    # ---- 队列 ----
    "task_default_queue": settings.TASK_QUEUE_NAME,
    # 与 delay()/apply_async() 的默认行为一致：只认 JSON，不认 pickle。
    # pickle 反序列化等于在 worker 里执行任意代码，broker 一旦被人碰到就是 RCE。
    "task_serializer": "json",
    "result_serializer": "json",
    "accept_content": ["json"],
    # ---- 不使用 result backend ----
    "task_ignore_result": True,
    # ---- 投递语义 ----
    "task_acks_late": True,
    "task_reject_on_worker_lost": True,
    "worker_prefetch_multiplier": 1,
    # ---- 超时 ----
    # 软超时先到：让任务有机会把 document.status 写成 fail，而不是留下 parsing 僵尸
    "task_soft_time_limit": settings.TASK_SOFT_TIME_LIMIT_SECONDS,
    # 硬超时后手：到点 SIGKILL，兜住「软超时抛不出来」的情况
    # （任务卡在 C 扩展里时，SIGALRM 驱动的软超时可能迟迟不触发）
    "task_time_limit": settings.TASK_TIME_LIMIT_SECONDS,
    # ---- 进程池 ----
    "worker_pool": _WORKER_POOL,
    "worker_concurrency": settings.WORKER_CONCURRENCY,
    # ---- 时间 ----
    # 与 MySQL 容器（--default-time-zone=+08:00）和 core/db.py 的 now_db() 对齐，
    # 避免日志里出现两种时区的时间戳
    "timezone": "Asia/Shanghai",
    "enable_utc": False,
    # ---- 连接 ----
    # 启动时 Redis 还没起来就退出，是启动顺序问题而不是配置问题；
    # 开这个让 worker 重试等一会儿，容器编排下更稳
    "broker_connection_retry_on_startup": True,
    "broker_transport_options": {"visibility_timeout": settings.TASK_STALE_PARSING_SECONDS},
}

if _WORKER_POOL != "solo":
    # solo 池不接受这个参数（它会告警并忽略），所以只在 prefork 下设置
    _conf["worker_max_tasks_per_child"] = settings.WORKER_MAX_TASKS_PER_CHILD

celery_app.conf.update(**_conf)


# 让 worker 进程的日志格式与 Web 端一致（同一套 logging 配置，排查时不用换脑子）。
# 用 Celery 的 signal 而不是在模块顶层直接调 setup_logging()：
# worker.tasks 会被 Web 进程 import（为了拿到任务名），顶层调用会在 Web 进程里
# 二次初始化日志。
@celery_setup_logging.connect
def _configure_logging(**_: object) -> None:
    from config.logging_config import setup_logging

    setup_logging()
