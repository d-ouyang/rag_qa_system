"""
运行时配置服务（v4.0.0 P4-1，PLAN-v4.0.0.md）—— 业务开关的热更新真相源。

--------------------------------------------------------------------------
它解决什么问题
--------------------------------------------------------------------------
pydantic-settings 在进程启动那一刻把 `.env` 冻结：改任何开关都要
`docker compose up -d --force-recreate` 才生效。对**基础设施配置**
（DB/Redis/Chroma 连接、密钥、端口、CORS）这是应得的代价——重连本就该重启；
但对**业务开关**（QUERY_REWRITE、模型参数、缓存层……）是纯粹的运维摩擦。

ConfigService 把业务开关的真相源挪到 MySQL `system_config` 表：
    · 写：管理端接口（P4-2）→ set_flag → 落库 + 进程内缓存即时更新；
    · 读：请求链路 get_flag(key, default) → 进程内 dict，O(1)，零 DB 开销；
    · 一致：TTL 回源兜底（P1，本模块）；P2 再加 Redis pub/sub 即时失效。

**取值优先级（从高到低）：DB 里的值 > `.env` 的值 > 代码默认值。**
调用方传的 default 就是 `.env`/代码那一档，例如：
    get_flag("query_rewrite", settings.QUERY_REWRITE)

--------------------------------------------------------------------------
与 Redis 的分工（项目铁律不变）
--------------------------------------------------------------------------
MySQL 是真相源；Redis 在 P2 只做「变更广播 + 热缓存」，**不存真相**。
会话/消息/文档/配置——所有真相数据一律不进 Redis。

--------------------------------------------------------------------------
fail-open 还是 fail-closed
--------------------------------------------------------------------------
读路径 **fail-open**：DB 不可用时 get_flag 回落 default。这些是功能开关，
不是安全边界——「因为查不了配置就拒绝服务」比「用默认值多跑一次改写」
严重得多。安全判定（kb_acl / 身份信任边界）永远不走这里，仍在各自模块
fail-closed。
写路径 **fail-closed**：set_flag 落库失败必须抛给调用方（管理端要看见
「没保存成功」，而不是以为改了、其实只改了本进程内存）。

--------------------------------------------------------------------------
与 SessionStore 的同构
--------------------------------------------------------------------------
ConfigStore ABC + Memory/MySQL 两个实现，与 core/session_store.py 的
SessionStore ABC 同一范式：真相源一个实现、测试/开发一个实现。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as mysql_insert

from config.settings import settings
from core import schema
from core.db import now_db, session_scope

logger = logging.getLogger(__name__)

#: 支持的配置值类型。顺序即 _type_of 的判别顺序——**bool 必须在 int 之前**
#: （Python 里 isinstance(True, int) 为真，顺序反了会把布尔开关记成整数）。
SUPPORTED_TYPES = ("bool", "int", "float", "str")


class ConfigError(ValueError):
    """配置值非法：类型不支持，或与既有类型冲突（跨类型改写）。"""


def _type_of(value: Any) -> str:
    """推断值的类型名。拒绝 None / list / dict（配置项是标量开关，不是结构）。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise ConfigError(
        f"不支持的配置值类型 {type(value).__name__}（仅支持 {'/'.join(SUPPORTED_TYPES)}）"
    )


class _Entry(NamedTuple):
    """一条配置的完整形态（缓存与 list_entries 共用）。"""

    value: Any
    value_type: str
    description: str
    updated_at: datetime | None
    updated_by: str


class ConfigStore(ABC):
    """配置存储后端。load_all 允许抛异常（调用方负责兜底回落默认值）。"""

    @abstractmethod
    def load_all(self) -> dict[str, _Entry]:
        """全量加载，返回 {key: _Entry}。"""

    @abstractmethod
    def upsert(self, key: str, entry: _Entry) -> None:
        """写入或更新一条。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除一条。"""


class MemoryConfigStore(ConfigStore):
    """进程内 dict。本地开发 / 测试用（CONFIG_BACKEND=memory）：重启即忘。"""

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}

    def load_all(self) -> dict[str, _Entry]:
        return dict(self._data)

    def upsert(self, key: str, entry: _Entry) -> None:
        self._data[key] = entry

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class MySQLConfigStore(ConfigStore):
    """system_config 表（迁移 0011 / core/schema.py 的 system_config_table）。"""

    def load_all(self) -> dict[str, _Entry]:
        with session_scope() as session:
            rows = session.execute(
                sa.select(
                    schema.system_config_table.c.config_key,
                    schema.system_config_table.c.config_value,
                    schema.system_config_table.c.value_type,
                    schema.system_config_table.c.description,
                    schema.system_config_table.c.updated_at,
                    schema.system_config_table.c.updated_by,
                )
            ).all()
        out: dict[str, _Entry] = {}
        for key, raw, vtype, desc, updated_at, updated_by in rows:
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                # 单行坏数据不该让全站配置读不出来：跳过它（该键回落默认值），留日志。
                logger.warning("system_config 行 %r 的 JSON 解析失败，已跳过（该键回落默认值）", key)
                continue
            out[key] = _Entry(value, vtype, desc, updated_at, updated_by)
        return out

    def upsert(self, key: str, entry: _Entry) -> None:
        stmt = mysql_insert(schema.system_config_table).values(
            config_key=key,
            config_value=json.dumps(entry.value, ensure_ascii=False),
            value_type=entry.value_type,
            description=entry.description,
            updated_at=entry.updated_at,
            updated_by=entry.updated_by,
        )
        stmt = stmt.on_duplicate_key_update(
            config_value=stmt.inserted.config_value,
            value_type=stmt.inserted.value_type,
            description=stmt.inserted.description,
            updated_at=stmt.inserted.updated_at,
            updated_by=stmt.inserted.updated_by,
        )
        with session_scope() as session:
            session.execute(stmt)

    def delete(self, key: str) -> None:
        with session_scope() as session:
            session.execute(
                sa.delete(schema.system_config_table).where(
                    schema.system_config_table.c.config_key == key
                )
            )


class ConfigService:
    """进程内配置门面。线程安全：FastAPI 同步路由跑在线程池，读写都会并发。"""

    def __init__(self, store: ConfigStore, ttl_seconds: int | None = None) -> None:
        self._store = store
        self._ttl = settings.CONFIG_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        self._cache: dict[str, _Entry] = {}
        self._loaded_at: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get_flag(self, key: str, default: Any = None) -> Any:
        """取一个配置值。库里没有 → default；类型不符 → 告警 + default。"""
        self._ensure_fresh()
        entry = self._cache.get(key)
        if entry is None:
            return default
        return self._guard(key, entry.value, default)

    def all_flags(self) -> dict[str, Any]:
        """当前生效的全部配置值（P4-2 设置页 GET 用；只报值，不涉密钥）。"""
        self._ensure_fresh()
        return {k: e.value for k, e in self._cache.items()}

    def list_entries(self) -> list[dict[str, Any]]:
        """全部配置的完整形态（含类型/说明/修改审计），按 key 排序。"""
        self._ensure_fresh()
        return [
            {
                "key": k,
                "value": e.value,
                "value_type": e.value_type,
                "description": e.description,
                "updated_at": e.updated_at,
                "updated_by": e.updated_by,
            }
            for k, e in sorted(self._cache.items())
        ]

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def set_flag(self, key: str, value: Any, actor: str = "system", description: str = "") -> None:
        """设置一个配置值：类型校验 → 落库 → 更新缓存（即时生效，无需重启）。

        :raises ConfigError: 值类型不支持，或与既有类型冲突（跨类型改写需先 unset）。
        """
        vtype = _type_of(value)
        with self._lock:
            self._reload_if_stale_locked()
            old = self._cache.get(key)
            if old is not None and old.value_type != vtype:
                raise ConfigError(
                    f"配置 {key!r} 已有类型 {old.value_type}，不能直接改成 {vtype}；"
                    f"如需变更类型请先 unset_flag 删除旧值"
                )
            desc = description or (old.description if old is not None else "")
            entry = _Entry(value, vtype, desc, now_db(), actor)
            self._store.upsert(key, entry)  # 落库失败直接抛——管理端必须看得见
            self._cache[key] = entry
        logger.info("配置 %s 已更新为 %r（%s，操作人 %s）", key, value, vtype, actor)

    def unset_flag(self, key: str, actor: str = "system") -> None:
        """删除一个配置值：回落 `.env`/代码默认值那一档。"""
        with self._lock:
            self._store.delete(key)
            self._cache.pop(key, None)
        logger.info("配置 %s 已删除（回落 .env/代码默认值），操作人 %s", key, actor)

    def reload(self) -> None:
        """强制回源。启动预热与（P2 的）Redis pub/sub 变更广播都会调它。"""
        with self._lock:
            self._reload_locked()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _ensure_fresh(self) -> None:
        """TTL 过期则回源（双检锁：热路径多数情况不走锁）。"""
        if self._stale():
            with self._lock:
                if self._stale():
                    self._reload_locked()

    def _stale(self) -> bool:
        # ttl<=0 = 每次都回源（仅测试用：外部直改 store 后立即可见）。
        return self._ttl <= 0 or (time.monotonic() - self._loaded_at) >= self._ttl

    def _reload_if_stale_locked(self) -> None:
        if self._stale():
            self._reload_locked()

    def _reload_locked(self) -> None:
        try:
            self._cache = self._store.load_all()
        except Exception as e:  # noqa: BLE001 - 读路径 fail-open：任何异常都回落旧缓存/默认值
            logger.warning(
                "配置回源失败（沿用旧缓存或默认值）: %s: %s", type(e).__name__, e
            )
        finally:
            # 失败也刷新时间戳：否则 DB 挂起时每个请求都会重试连接，放大故障。
            self._loaded_at = time.monotonic()

    @staticmethod
    def _guard(key: str, value: Any, default: Any) -> Any:
        """类型守卫：库里的值与调用方期望的类型不符 → 告警 + 回落默认值。

        最经典的事故是 bool 键被写成字符串 "yes"（Python 里 truthy）——
        不设防就是「开关看起来关了、行为却开着」。bool 与 int 的判别
        顺序不能反（isinstance(True, int) 为真）。
        """
        if default is None:
            return value
        ok = False
        result = value
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):  # bool 是 int 子类，先排除
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if ok:
                result = float(value)
        elif isinstance(default, str):
            ok = isinstance(value, str)
        if not ok:
            logger.warning(
                "配置 %s 的值类型与期望不符（期望 %s，实际 %r），已回落默认值 %r",
                key, type(default).__name__, value, default,
            )
            return default
        return result


def build_config_service() -> ConfigService:
    """按 CONFIG_BACKEND 选存储后端，构造进程内单例。"""
    if settings.CONFIG_BACKEND == "mysql":
        logger.info("运行时配置服务：MySQL system_config 表为真相源（热更新生效）")
        return ConfigService(MySQLConfigStore())
    logger.info("运行时配置服务：进程内存储（CONFIG_BACKEND=memory，重启即忘；生产请配 mysql）")
    return ConfigService(MemoryConfigStore())


#: 进程内单例（与 settings 同构）。rag_chain / api 路由都从这里读。
config_service = build_config_service()
