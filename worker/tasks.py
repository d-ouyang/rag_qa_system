"""
解析任务定义 —— 一层**薄壳**，逻辑全在 `core/parsing.py`。

--------------------------------------------------------------------------
为什么这里只写三行
--------------------------------------------------------------------------
Celery task 是「进程边界上的包装」，它天生不具备可测性：
要跑起来得有 broker、有 worker 进程、有 Redis。若把状态机逻辑写在这里，
「抢不到任务怎么办」「解析炸了怎么写状态」「文件被删了怎么办」这些
真正容易写错的路径就只能靠起一整套基础设施来验证 —— 实践中等于不验证。

所以 `run_parse_task()` 放在 core 层（同步、无队列依赖、可直接调用），
task 只负责把 doc_id 递进去。测试覆盖 core 层就等价于覆盖了任务逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from core.parsing import run_parse_task
from worker.app import celery_app

logger = logging.getLogger(__name__)

# 任务名显式写成常量：生产端（core/queue.py 的 enqueue_parse）与
# 消费端（本模块的装饰器）必须用同一个字符串。写成两处字面量的话，
# 改一处忘一处的结果是**任务静默进队列但永远没人消费** ——
# API 返回 200、文档停在 pending，没有任何报错。常量被两边共同 import，
# 拼错就是 ImportError。
PARSE_TASK_NAME = "rag.parse_document"


@celery_app.task(name=PARSE_TASK_NAME)
def parse_document_task(doc_id: int) -> dict[str, Any]:
    """
    解析一个文档。入参只有 doc_id —— 其余信息（路径、原文件名、项目）
    全部从 MySQL 现读。

    为什么不把文件路径当参数传进来：那等于把「消息里的事实」和
    「库里的事实」变成两份。文件被改名或记录被删之后，消息里那份就成了
    幽灵；重启后从队列里捞出来重放，会去解析一个不该再解析的路径。
    只传 ID，事实永远只有库那一份。

    异常：`run_parse_task` 自身不会抛（失败落 status=fail）。
    只有数据库不可用时会穿透出来 —— 那时状态没写成功，记录停留在 parsing，
    靠孤儿超时被重新抢（见 alembic/versions/0002）。让它抛出是对的：
    至少 Celery 日志里会留下痕迹，比静默吞掉强。
    """
    logger.info("收到解析任务 | doc_id=%s", doc_id)
    result = run_parse_task(int(doc_id))
    if result.get("ok"):
        logger.info("任务完成 | doc_id=%s | 切片=%s", doc_id, result.get("chunk_count"))
        from core.qa_cache import bump_kb_version
        bump_kb_version()
    else:
        # 不打 ERROR：解析失败是**业务结果**（文件坏），不是系统故障。
        # 用 ERROR 会让真正的系统故障淹没在噪音里。
        logger.warning("任务未完成 | doc_id=%s | 原因=%s", doc_id, result.get("reason"))
    return result
