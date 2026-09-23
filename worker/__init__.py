"""
解析任务 Worker 包。

只包含两件事：
    worker/app.py    Celery 应用与配置（队列、超时、投递语义）
    worker/tasks.py  任务定义（薄壳，真正的逻辑在 core/parsing.py）

启动：`make worker`（= `celery -A worker.app worker ...`），见 Makefile。
"""
