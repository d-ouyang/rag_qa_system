"""
Redis 内存预警 —— 回答「数据量/访问量涨上来时，怎么提前发现、怎么自动止损」。

--------------------------------------------------------------------------
为什么必须有这一层
--------------------------------------------------------------------------
Redis 是**共享资源**。同一台机器上通常还跑着别的服务，而 Redis 的
maxmemory 是全局的：本应用把会话写爆，受害的是所有依赖这个 Redis 的业务
（症状是它们开始报 OOM command not allowed，排查起来还以为是自己的 bug）。

而 Redis 满了之后的表现是**静默的**：会话写不进去，用户那边只是「模型失忆了」，
除非埋了监控，否则要等用户投诉才发现。所以必须在写满**之前**就分级预警。

--------------------------------------------------------------------------
分级与对应动作（阈值见 config/settings.py）
--------------------------------------------------------------------------
    ok        < 70%   正常，无动作
    warn      ≥ 70%   打 WARNING 日志 + 接口暴露水位（给运维留出扩容窗口）
    critical  ≥ 85%   ERROR 日志 + 主动清理过期会话索引（回收可观空间）
                      + 标记 need_action，提示「该扩容量或缩短 TTL 了」
    fatal     ≥ 95%   CRITICAL 日志 + 写入降级为只读保护：
                      新会话不再写记忆，但**问答本身照常返回**
                      （可用性优先：宁可丢多轮上下文，不让服务整体 503）

关键取舍：**fatal 不做「拒绝服务」而做「只读降级」**。
理由是会话记忆属于增强项而不是核心链路 —— RAG 问答没有历史上下文依然能回答，
但如果因为「存不下历史」而给用户 500，那才是真正的故障。

水位恢复（< critical）时自动解除只读保护，不需要人工重启。
水位判定还有个诚实之处：如果 Redis 没设 maxmemory（=0，等于无上限），
就用 REDIS_MEMORY_ASSUMED_MAX_MB 折算比例，并在返回值里标注
ratio_basis="assumed"，避免把「估算」当成「事实」看。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from config.settings import settings

logger = logging.getLogger(__name__)

# 分级常量（接口层的 level 字段就是这几个值）
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"
LEVEL_FATAL = "fatal"
LEVEL_UNKNOWN = "unknown"


def human_bytes(num: int | float | None) -> str:
    """把字节数转成人类可读字符串（日志与接口展示用）。"""
    if num is None:
        return "未知"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.2f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.2f}TB"  # pragma: no cover - 循环已在 TB 收敛


class RedisMemoryMonitor:
    """
    Redis 内存水位监控。

    设计上刻意保持「只读 + 告警 + 有限止损」三个动作，不做自动扩缩容 ——
    自动扩容需要基础设施配合（云 Redis 的弹性规格），在代码里假装能做
    只会掩盖真实问题。
    """

    def __init__(
        self,
        client: Any | None = None,
        key_prefix: str | None = None,
        cleanup_hook: Callable[[], int] | None = None,
    ) -> None:
        self._client = client
        self._prefix = (key_prefix or settings.REDIS_KEY_PREFIX or "rag").rstrip(":")
        # critical 水位时调用的清理动作（一般传 SessionStore.purge_expired）
        self._cleanup_hook = cleanup_hook
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # 上一次的分级：只在级别变化时打日志，否则每 60s 一条会把日志刷爆
        self._last_level: str | None = None

    # ------------------------------------------------------------------ #
    # 依赖注入
    # ------------------------------------------------------------------ #
    def _get_client(self) -> Any:
        if self._client is None:
            from core.redis_store import get_redis_client

            self._client = get_redis_client()
        return self._client

    def set_cleanup_hook(self, hook: Callable[[], int]) -> None:
        """注册 critical 水位下的清理回调（通常是 store.purge_expired）。"""
        self._cleanup_hook = hook

    # ------------------------------------------------------------------ #
    # 采样
    # ------------------------------------------------------------------ #
    def _read_info_memory(self) -> dict[str, Any]:
        """读 INFO memory（一次往返拿全部内存指标）。"""
        raw = self._get_client().info("memory")
        if isinstance(raw, dict):
            return raw
        # 某些客户端/代理会把 INFO 返回成原始文本，这里降级自行解析
        parsed: dict[str, Any] = {}
        for line in str(raw).splitlines():
            if ":" in line and not line.startswith("#"):
                k, _, v = line.partition(":")
                parsed[k.strip()] = v.strip()
        return parsed

    @staticmethod
    def _as_int(info: dict[str, Any], key: str, default: int = 0) -> int:
        try:
            return int(info.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(info: dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            return float(info.get(key, default))
        except (TypeError, ValueError):
            return default

    def inspect(self) -> dict[str, Any]:
        """
        采集一次内存水位快照并分级（**不产生副作用**，可被接口层随意调用）。

        :return: 结构化状态 dict，字段见文件头说明；Redis 不可达时 level=unknown
        """
        try:
            info = self._read_info_memory()
        except Exception as e:
            logger.warning("读取 Redis 内存信息失败（不影响问答链路）：%s", e)
            # ⚠️ 不可达时也**照常给 thresholds 与 advice**。
            #
            # 一开始这里只返回 {level, reason}，后来改掉了，原因有二：
            # 1. 接口 `GET /api/v1/system/memory` 的字段形状不该随连通性变化 ——
            #    消费方（前端仪表盘、外部监控）需要「这几个 key 一定在」，
            #    否则每次都得写两套解析。所以约定：level / reason / thresholds /
            #    advice / checked_at **恒在**；比值与内存量等「读不到就是读不到」的
            #    字段才允许缺失。
            # 2. Redis 挂掉正是最需要可执行建议的时刻，而原来这一支一句建议都没有。
            status: dict[str, Any] = {
                "level": LEVEL_UNKNOWN,
                "reason": f"无法读取 Redis 内存信息：{type(e).__name__}: {e}",
                "thresholds": self._thresholds(),
                "checked_at": time.time(),
            }
            status["advice"] = self._advice(status)
            return status

        used = self._as_int(info, "used_memory")
        maxmemory = self._as_int(info, "maxmemory")
        policy = str(info.get("maxmemory_policy", "unknown"))

        # 水位基准：优先用 Redis 自己的 maxmemory；没设上限时用假定容量折算，
        # 并如实标注 basis，避免把估算值当成硬事实。
        if maxmemory > 0:
            ratio_basis = "maxmemory"
            denominator = maxmemory
        else:
            ratio_basis = "assumed"
            denominator = int(settings.REDIS_MEMORY_ASSUMED_MAX_MB) * 1024 * 1024
        ratio = used / denominator if denominator else 0.0

        # 应用自身的会话量（顺带把小 key 统计出来，否则「Redis 快满了」看不出是谁的锅）
        app_session_count = None
        try:
            app_session_count = int(self._get_client().zcard(f"{self._prefix}:sessions"))
        except Exception:  # pragma: no cover - 索引不存在时也无所谓
            pass

        status: dict[str, Any] = {
            "level": self._classify(ratio),
            "used_ratio": round(ratio, 4),
            "ratio_basis": ratio_basis,
            "used_memory": used,
            "used_memory_human": human_bytes(used),
            "used_memory_peak_human": human_bytes(self._as_int(info, "used_memory_peak")),
            "used_memory_rss_human": human_bytes(self._as_int(info, "used_memory_rss")),
            "maxmemory": maxmemory,
            "maxmemory_human": human_bytes(maxmemory) if maxmemory > 0 else "未设置（无上限）",
            "maxmemory_policy": policy,
            "mem_fragmentation_ratio": round(self._as_float(info, "mem_fragmentation_ratio", 1.0), 3),
            "app_session_count": app_session_count,
            "thresholds": self._thresholds(),
            "checked_at": time.time(),
        }
        status["advice"] = self._advice(status)
        return status

    @staticmethod
    def _thresholds() -> dict[str, float]:
        """三级水位阈值。**纯配置**，不依赖 Redis 是否可达，所以任何分支都能给。"""
        return {
            "warn": settings.REDIS_MEMORY_WARN_RATIO,
            "critical": settings.REDIS_MEMORY_CRITICAL_RATIO,
            "fatal": settings.REDIS_MEMORY_FATAL_RATIO,
        }

    def _classify(self, ratio: float) -> str:
        if ratio >= float(settings.REDIS_MEMORY_FATAL_RATIO):
            return LEVEL_FATAL
        if ratio >= float(settings.REDIS_MEMORY_CRITICAL_RATIO):
            return LEVEL_CRITICAL
        if ratio >= float(settings.REDIS_MEMORY_WARN_RATIO):
            return LEVEL_WARN
        return LEVEL_OK

    def _advice(self, status: dict[str, Any]) -> list[str]:
        """按水位给出可直接执行的处置建议（写给半夜被叫起来的人看的）。"""
        level = status["level"]
        if level == LEVEL_UNKNOWN:
            # 注意：不能走到下面那条「水位正常，无需处理」——
            # unknown 是「没读到」，不是「读到了且正常」，这两件事搞混会掩盖故障。
            return [
                "Redis 不可达：先确认服务/容器在跑，再查 REDIS_URL 与网络连通性。",
                "本次只告警、不阻断写入：问答链路已按「无历史」降级继续，不会因此报错。",
                "但要清楚：unknown 期间内存预警这层保护实际是失效的，不宜长期停留在此状态。",
            ]
        tips: list[str] = []
        if level in (LEVEL_WARN, LEVEL_CRITICAL, LEVEL_FATAL):
            tips.append(
                "确认 maxmemory-policy：会话数据应配合 noeviction（宁可写入失败也不要"
                "随机驱逐，否则用户看到的是「历史莫名其妙少了几轮」这种最坏情况）；"
                "如需保命中率可评估 volatile-lru。"
            )
        if level in (LEVEL_CRITICAL, LEVEL_FATAL):
            tips.append("缩短 MEMORY_SESSION_TTL_SECONDS（如 6h → 2h），让冷会话更快自然过期。")
            tips.append("下调 MEMORY_MAX_TURNS，直接降低单会话体积上限。")
        if level == LEVEL_FATAL:
            tips.append("紧急：扩容 Redis 规格或清理其他业务的大 key；当前已对新会话启用只读保护。")
        if not tips:
            tips.append("水位正常，无需处理。")
        return tips

    # ------------------------------------------------------------------ #
    # 告警 + 止损
    # ------------------------------------------------------------------ #
    def check_and_act(self) -> dict[str, Any]:
        """
        巡检一次并按级别执行动作（后台线程与健康检查走这条）。

        动作清单：
        · 级别变化时才打日志（防刷屏）；
        · critical/fatal 触发一次清理（回收索引残留，并向运行态同步）；
        · fatal 且打开 REDIS_MEMORY_FATAL_READONLY → 写入降级为只读保护；
        · 水位回落到 critical 以下 → 自动解除保护。
        """
        from core.redis_store import get_runtime_state  # 延迟导入避免模块级循环依赖

        state = get_runtime_state()
        status = self.inspect()
        level = status["level"]

        if level == LEVEL_UNKNOWN:
            logger.warning("Redis 内存巡检跳过 | %s", status.get("reason"))
            return status

        # 只在级别变化时打日志：60s 一次巡检，同一级别刷一整天会把日志冲垮
        if level != self._last_level:
            detail = (
                f"水位={status['used_ratio']:.1%}（基准={status['ratio_basis']}） "
                f"已用={status['used_memory_human']} 上限={status['maxmemory_human']} "
                f"本应用会话数={status.get('app_session_count')}"
            )
            if level == LEVEL_FATAL:
                logger.critical("Redis 内存进入 FATAL 水位 | %s", detail)
            elif level == LEVEL_CRITICAL:
                logger.error("Redis 内存进入 CRITICAL 水位 | %s", detail)
            elif level == LEVEL_WARN:
                logger.warning("Redis 内存进入 WARN 水位 | %s", detail)
            else:
                logger.info("Redis 内存水位恢复正常 | %s", detail)
            self._last_level = level

        if level in (LEVEL_CRITICAL, LEVEL_FATAL) and self._cleanup_hook is not None:
            try:
                purged = self._cleanup_hook()
                if purged:
                    status["purged_index_entries"] = purged
            except Exception as e:  # pragma: no cover - 清理失败不该影响巡检
                logger.warning("内存高压下的清理动作失败：%s", e)

        # 只读保护的开/关
        if level == LEVEL_FATAL and settings.REDIS_MEMORY_FATAL_READONLY:
            state.block_writes(
                f"Redis 内存水位 {status['used_ratio']:.1%} 已达 FATAL 阈值"
                f"（{status['used_memory_human']}/{status['maxmemory_human']}）"
            )
        elif level in (LEVEL_OK, LEVEL_WARN) and state.is_write_blocked()[0]:
            # 水位回落才恢复写入；critical 区间保持保护，避免在阈值附近反复横跳
            state.unblock_writes()

        state.set_memory_status(status)
        return status

    # ------------------------------------------------------------------ #
    # 后台巡检线程
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """
        启动后台巡检线程（幂等：重复调用不会起第二个线程）。

        为什么用守护线程 + 独立线程而不是塞进 FastAPI 的定时任务：
        巡检必须**脱离请求生命周期**运行（没人访问时也要在看水位），
        守护线程在进程退出时自动结束，不需要额外的优雅停机编排。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="redis-memory-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Redis 内存预警已启动 | 间隔=%ds 阈值 warn=%.0f%% critical=%.0f%% fatal=%.0f%%",
            settings.REDIS_MEMORY_CHECK_INTERVAL_SECONDS,
            settings.REDIS_MEMORY_WARN_RATIO * 100,
            settings.REDIS_MEMORY_CRITICAL_RATIO * 100,
            settings.REDIS_MEMORY_FATAL_RATIO * 100,
        )

    def stop(self) -> None:
        """停止巡检线程（测试收尾用）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        interval = max(5, int(settings.REDIS_MEMORY_CHECK_INTERVAL_SECONDS))
        # 立即巡检一次，不必等第一个间隔（否则重启后前 60s 处于盲区）
        while not self._stop_event.is_set():
            try:
                self.check_and_act()
            except Exception as e:  # pragma: no cover - 巡检自身绝不能把线程搞死
                logger.error("Redis 内存巡检异常（已捕获，不影响服务）：%s", e)
            # 用 Event.wait 而不是 sleep：stop() 能立刻唤醒，测试不必等一个完整周期
            self._stop_event.wait(interval)


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_monitor: RedisMemoryMonitor | None = None
_monitor_lock = threading.Lock()


def get_redis_monitor() -> RedisMemoryMonitor:
    """获取全局内存监控单例（双检锁，与项目其他单例同风格）。"""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = RedisMemoryMonitor()
    return _monitor


def start_redis_memory_monitor(cleanup_hook: Callable[[], int] | None = None) -> RedisMemoryMonitor:
    """
    启动内存预警（仅在 MEMORY_BACKEND=redis 且开启监控时真正启动）。

    调用点：MemoryManager 切换 Redis 后端时。这样「谁引入的容量风险谁负责建监控」，
    不需要应用启动流程额外记得调一次。
    """
    monitor = get_redis_monitor()
    if cleanup_hook is not None:
        monitor.set_cleanup_hook(cleanup_hook)
    if settings.REDIS_MEMORY_MONITOR_ENABLED:
        monitor.start()
    else:
        logger.info("Redis 内存预警已按配置关闭（REDIS_MEMORY_MONITOR_ENABLED=false）")
    return monitor


def reset_redis_monitor() -> None:
    """重置监控单例（测试用）。"""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
        _monitor = None
