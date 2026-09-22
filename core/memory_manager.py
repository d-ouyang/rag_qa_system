"""
会话记忆模块 —— 按 session_id 维护多轮对话历史，解决「上下文丢失」问题。

--------------------------------------------------------------------------
为什么需要它（面试常问）
--------------------------------------------------------------------------
LLM 本身是无状态的：每次 invoke 看到的只有这一次传入的消息。
用户说「它多少钱？」，如果没有前几轮对话，模型根本不知道「它」指什么。

所以应用层必须自己保存对话历史，每次提问时把「历史消息 + 当前问题」
一起塞进 prompt。这个「历史存哪、怎么存、存多久」就是本模块的职责。

--------------------------------------------------------------------------
三个关键设计决策
--------------------------------------------------------------------------
1. 为什么用 dict[session_id, ChatMessageHistory] 而不是全局一份历史
   多用户/多标签页共用一个服务时，全局历史会把 A 的对话串给 B —— 既泄露隐私
   又污染上下文。按 session_id 隔离，是最小可用、也最常见的方案。
   （生产环境量级大了会把存储换成 Redis，本模块的接口不变，只换内部实现。）

2. 为什么要限制窗口（MEMORY_MAX_TURNS），而不是全量塞历史
   · token 成本：历史越长，每次请求的 prompt 越大，越慢越贵；
   · 上下文窗口：超出模型 max context 会直接报错或被静默截断；
   · 相关性：很久之前的对话多数对当前问题没帮助，反而稀释注意力。
   所以只保留最近 N 轮（默认 10 轮 = 20 条消息），更早的从头部裁掉。

3. 为什么每个会话一把锁，而不是全局一把大锁
   全局锁会让「用户 A 写历史」阻塞「用户 B 读历史」，并发度直接归零。
   按 session_id 粒度加锁：同一会话内串行（保证消息顺序不乱），
   不同会话之间完全并行。

4. 为什么要 TTL 清理
   内存字典只增不减迟早 OOM。超过 MEMORY_SESSION_TTL_SECONDS 没活动的
   会话被惰性回收（下次访问时发现过期就删掉），不依赖后台线程，
   简单可靠、没有线程泄漏风险。
"""

import logging
import threading
import time
from typing import Any

from langchain_core.chat_history import BaseChatMessageHistory, InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from config.settings import settings

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    多轮对话记忆管理器（内存版）。

    对外语义：
        · get_messages(session_id)      取某个会话的历史消息（LangChain Message 对象列表）
        · add_exchange(session_id, q, a) 一轮问答结束后写入（user + ai 两条）
        · clear_session(session_id)     清空某个会话
        · session_count() / list_sessions()  运维/接口层用

    线程模型：不同 session 并行、同一 session 串行（每会话一把锁）。
    """

    def __init__(
        self,
        max_turns: int | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """
        :param max_turns: 每会话最多保留的对话轮数，缺省读 settings.MEMORY_MAX_TURNS
        :param ttl_seconds: 会话闲置多少秒后过期，缺省读 settings.MEMORY_SESSION_TTL_SECONDS
        """
        # 注意不能用 `or` 兜底：0 是合法值（测试里 ttl_seconds=0 表示「立刻过期」），
        # `0 or 默认值` 会把 0 静默吞掉换成默认值
        self.max_turns: int = int(max_turns if max_turns is not None else settings.MEMORY_MAX_TURNS)
        self.ttl_seconds: int = int(
            ttl_seconds if ttl_seconds is not None else settings.MEMORY_SESSION_TTL_SECONDS
        )

        # 会话存储：session_id -> (消息历史, 最后活跃时间戳)
        # 最后活跃时间用于惰性 TTL 回收
        self._sessions: dict[str, InMemoryChatMessageHistory] = {}
        self._last_active: dict[str, float] = {}

        # 每会话一把锁 + 一把保护「会话字典本身」的元锁
        # 元锁只在「创建/删除会话」这种结构性操作时短暂持有，
        # 消息读写走会话级锁，互不阻塞
        self._session_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

        logger.info(
            "MemoryManager 初始化完成 | 最大轮数=%d 会话TTL=%ds",
            self.max_turns,
            self.ttl_seconds,
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _get_or_create_session(self, session_id: str) -> InMemoryChatMessageHistory:
        """
        取会话历史对象；不存在则创建；存在但已过期则清掉重来。

        过期会话「清掉重来」而不是报错：对调用方来说，
        过期恢复对话等价于「开一个新会话」，语义最简单。
        """
        with self._meta_lock:
            history = self._sessions.get(session_id)
            if history is not None and self._is_expired(session_id):
                logger.info("会话已过期，重置记忆 | session_id=%s", session_id)
                history = None
            if history is None:
                history = InMemoryChatMessageHistory()
                self._sessions[session_id] = history
                self._session_locks.setdefault(session_id, threading.Lock())
                logger.debug("创建新会话 | session_id=%s 当前会话数=%d", session_id, len(self._sessions))
            self._last_active[session_id] = time.time()
            return history

    def _is_expired(self, session_id: str) -> bool:
        """会话是否已超过 TTL 未活动。"""
        last = self._last_active.get(session_id)
        return last is not None and (time.time() - last) > self.ttl_seconds

    def _get_lock(self, session_id: str) -> threading.Lock:
        """取会话级锁（先确保会话存在，锁与会话同生命周期）。"""
        with self._meta_lock:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _trim_history(self, history: InMemoryChatMessageHistory) -> None:
        """
        把历史裁剪到最近 max_turns 轮（即 2*max_turns 条消息）。

        从头部删（最早的消息先走）：越旧的消息对当前问题价值越低。
        InMemoryChatMessageHistory.messages 返回的是 list 副本，
        直接改它无效，必须用 clear() + add_messages() 重写。
        """
        max_messages = self.max_turns * 2
        if len(history.messages) <= max_messages:
            return
        kept = history.messages[-max_messages:]
        history.clear()
        history.add_messages(kept)
        logger.debug("历史已裁剪到最近 %d 轮", self.max_turns)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def get_history(self, session_id: str) -> BaseChatMessageHistory:
        """
        返回会话的 ChatMessageHistory 对象。

        暴露 LangChain 标准接口对象，是为了将来想换 RunnableWithMessageHistory
        这类官方组件时能直接对接（它要求的正是 BaseChatMessageHistory）。
        """
        return self._get_or_create_session(session_id)

    def get_messages(self, session_id: str) -> list[BaseMessage]:
        """取某个会话的历史消息副本（副本：外部改动不会写回存储）。"""
        history = self._get_or_create_session(session_id)
        with self._get_lock(session_id):
            return list(history.messages)

    def add_exchange(self, session_id: str, question: str, answer: str) -> None:
        """
        一轮问答结束后写入记忆（用户问 + AI 答，两条一起，原子语义）。

        为什么是「一轮一起写」而不是分开写：
        如果先写 user 消息、AI 回答时挂了，历史里就留下一条没有回答的问题，
        下一轮模型会误以为「这个问题还没回答」而重复作答。
        要么两条都写成功，要么都不写（异常时由调用方保证不调用本方法）。
        """
        history = self._get_or_create_session(session_id)
        with self._get_lock(session_id):
            history.add_messages([
                HumanMessage(content=question),
                AIMessage(content=answer),
            ])
            self._trim_history(history)
        logger.info(
            "记忆已更新 | session_id=%s 历史条数=%d",
            session_id,
            len(history.messages),
        )

    def clear_session(self, session_id: str) -> bool:
        """
        清空某个会话的全部记忆。

        :return: 会话存在并清除成功返回 True；会话本就不存在返回 False
        """
        with self._meta_lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            self._last_active.pop(session_id, None)
            self._session_locks.pop(session_id, None)
        logger.info("会话已清空 | session_id=%s", session_id)
        return True

    def session_count(self) -> int:
        """当前存活会话数（接口层/健康检查用）。"""
        with self._meta_lock:
            return len(self._sessions)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出全部会话及其基本信息（运维/调试用，不含消息正文）。"""
        with self._meta_lock:
            return [
                {
                    "session_id": sid,
                    "message_count": len(self._sessions[sid].messages),
                    "last_active": self._last_active.get(sid),
                }
                for sid in self._sessions
            ]

    def cleanup_expired(self) -> int:
        """
        主动清理所有过期会话（惰性清理之外的兜底，可由定时任务调用）。

        :return: 被清理的会话数
        """
        removed: list[str] = []
        with self._meta_lock:
            for sid in list(self._sessions):
                if self._is_expired(sid):
                    del self._sessions[sid]
                    self._last_active.pop(sid, None)
                    self._session_locks.pop(sid, None)
                    removed.append(sid)
        if removed:
            logger.info("主动清理过期会话 | 数量=%d", len(removed))
        return len(removed)


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_memory_manager: MemoryManager | None = None
_memory_manager_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    """获取全局唯一的记忆管理器（双检锁，与项目内其他单例同风格）。"""
    global _memory_manager
    if _memory_manager is None:
        with _memory_manager_lock:
            if _memory_manager is None:
                _memory_manager = MemoryManager()
                logger.debug("MemoryManager 单例已创建")
    return _memory_manager


def reset_memory_manager() -> None:
    """重置单例（测试切换配置时用）。"""
    global _memory_manager
    with _memory_manager_lock:
        _memory_manager = None
        logger.info("MemoryManager 单例已重置")
