"""
Alembic 运行时环境。

--------------------------------------------------------------------------
与默认模板的三处关键差异（都改在「为什么」上）
--------------------------------------------------------------------------
1. **连接串来自 `config/settings.py`，不来自 `alembic.ini`**
   默认模板要求你把 URL 写进 alembic.ini。那意味着数据库密码在仓库里
   出现**第二份**（第一份在 .env），而 alembic.ini 是要提交的 ——
   两份必然漂移，且漂移的表现是「应用连 A 库、迁移改 B 库」，破坏性极强。
   所以这里在运行时把 settings.MYSQL_URL 注入 config，ini 里留空。

2. **target_metadata = None（不接 autogenerate）**
   本项目迁移**手写**。原因是数据分层里 MySQL 是真相源，DDL 一旦写错
   （漏索引、漏字符集）事后修起来的代价远高于手写一遍。
   另外 autogenerate 也识别不了 MySQL 的字符集/排序规则这类表级选项。
   将来要开 autogenerate 时，把它换成 core/schema.py 的 metadata 即可。

3. **fileConfig 只在我们自己的日志配置未生效时才跑**
   alembic.ini 里有 [loggers] 段，import 时会用 basicConfig 覆盖 root logger，
   把应用侧已经配好的格式冲掉。这里显式保留应用配置（详见下方注释）。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证 `python -m alembic` / 直接调 alembic 两种姿势下都能 import 到项目包
# （alembic.ini 里也有 prepend_sys_path=. ，这里是双保险）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings  # noqa: E402

config = context.config

# ---- 注入连接串（见文件头第 1 点）----
config.set_main_option("sqlalchemy.url", settings.MYSQL_URL)

# ---- 日志 ----
# Alembic 默认会 fileConfig(alembic.ini) 从而重置 root logger。
# 设 ALEMBIC_SKIP_LOGGING=1 可跳过（跑迁移时想看到应用侧的格式时用）。
if config.config_file_name is not None and not os.getenv("ALEMBIC_SKIP_LOGGING"):
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    """
    「离线模式」：只生成 SQL 文本，不连数据库（`alembic upgrade head --sql`）。

    用途是把 DDL 交给 DBA 走审批 —— 生产库的账号通常不给 DDL 权限，
    只有这种方式能既保留迁移的可审计性又不动手连生产。
    """
    context.configure(
        url=settings.MYSQL_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """「在线模式」：直连数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool：迁移是一次性的短命进程，用完即退。
        # 用默认连接池会在进程退出时留下「未归位连接」的告警噪音，且毫无收益。
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
