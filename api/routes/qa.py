"""
问答接口模块 —— 把 RAGChain 的能力封装为标准化 RESTful 接口。

--------------------------------------------------------------------------
接口一览（统一前缀 /api/v1/qa）
--------------------------------------------------------------------------
    POST   /ask                       同步问答（一次拿完整答案）
    POST   /ask/stream                流式问答（NDJSON，逐 token 返回）
    GET    /sessions                  列出全部会话
    GET    /sessions/{session_id}     查看某个会话的对话历史
    DELETE /sessions/{session_id}     清空某个会话的记忆
    GET    /health                    链路健康检查（LLM/检索器/记忆/意图）

--------------------------------------------------------------------------
分层原则（为什么接口层这么「薄」）
--------------------------------------------------------------------------
本模块只做四件事：参数校验（pydantic）、协议转换（HTTP ↔ dict）、
错误码映射（业务异常 → HTTP 状态码）、序列化。
所有业务逻辑（检索、生成、记忆、意图）都在 core/rag_chain.py ——
接口层不写业务，将来换 gRPC / WebSocket 时 core 一行都不用动。
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.memory_manager import get_memory_manager
from core.rag_chain import get_rag_chain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qa", tags=["问答"])


# --------------------------------------------------------------------------- #
# 请求 / 响应模型（pydantic：声明即校验，非法请求 422 自动返回）
# --------------------------------------------------------------------------- #
class AskRequest(BaseModel):
    """问答请求体。"""

    question: str = Field(
        ..., min_length=1, max_length=2000,
        description="用户问题（1~2000 字）",
        examples=["公司的报销流程是什么？"],
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "会话 id，多轮对话的隔离键。"
            "不传则由服务端生成并在响应中返回，客户端保存后下次带上即可续聊"
        ),
        examples=["f47ac10b-58cc-4372-a567-0e02b2c3d479"],
    )


class SourceItem(BaseModel):
    """单条溯源信息（检索到的资料片段）。"""

    index: int = Field(description="资料序号（与 prompt 中【资料N】对应）")
    source: str = Field(description="来源文件路径")
    snippet: str = Field(description="片段摘要（前 200 字）")
    rerank_score: float | None = Field(default=None, description="重排分数 0~1，越大越相关")
    vector_similarity: float | None = Field(default=None, description="向量余弦相似度 -1~1")


class AskResponse(BaseModel):
    """同步问答响应体。"""

    session_id: str = Field(description="会话 id（客户端应保存，续聊时回传）")
    answer: str = Field(description="模型生成的回答")
    intent: str = Field(
        description=(
            "细粒度意图：knowledge_query(知识查询) / operation_guide(操作指导) / "
            "policy_consult(政策咨询) / comparison_analysis(对比分析) / "
            "data_statistics(数据统计) / troubleshooting(故障排查) / chitchat(闲聊)"
        ),
    )
    route: str = Field(description="路由结论：rag_qa（走了检索）/ chitchat（未检索）")
    intent_source: str = Field(description="意图判断来源：llm（小模型）/ rule（规则兜底）")
    standalone_question: str | None = Field(
        default=None,
        description="结合对话历史改写后的独立问题（闲聊时为空）",
    )
    sources: list[SourceItem] = Field(default_factory=list, description="引用资料列表")
    elapsed_ms: float = Field(description="本次问答总耗时（毫秒）")


class MessageItem(BaseModel):
    """一条对话历史消息。"""

    role: str = Field(description="user / assistant")
    content: str = Field(description="消息内容")


class SessionHistoryResponse(BaseModel):
    """会话历史响应体。"""

    session_id: str
    message_count: int = Field(description="历史消息条数（1 轮问答 = 2 条）")
    messages: list[MessageItem]


class SessionInfoItem(BaseModel):
    """会话概要（列表接口用）。"""

    session_id: str
    message_count: int
    last_active: float | None = Field(default=None, description="最后活跃时间（Unix 时间戳）")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _resolve_session_id(session_id: str | None) -> str:
    """
    session_id 缺省时生成 UUID4。

    放在服务端生成而不是强制客户端传：降低接入门槛（首问不用管会话），
    又通过响应把 id 还给客户端，续聊时带回即可。
    """
    return session_id or uuid.uuid4().hex


def _chain_error_to_http(e: Exception) -> HTTPException:
    """
    把链路异常映射成 HTTP 错误。

    502 而不是 500：错误来自下游（LLM 服务/向量库），不是本服务代码 bug，
    客户端可以据此区分「重试可能有用」和「等修 bug」。
    """
    logger.error("问答链路执行失败：%s", e, exc_info=True)
    return HTTPException(
        status_code=502,
        detail=f"问答服务暂时不可用：{type(e).__name__}: {e}",
    )


# --------------------------------------------------------------------------- #
# 接口实现
# --------------------------------------------------------------------------- #
@router.post(
    "/ask",
    response_model=AskResponse,
    summary="同步问答",
    description="一次请求拿到完整答案。意图识别为闲聊时不做检索直接回答。",
)
def ask(request: AskRequest) -> dict[str, Any]:
    """同步问答接口。"""
    session_id = _resolve_session_id(request.session_id)
    logger.info("收到问答请求 | session_id=%s query=%.24s", session_id, request.question)
    try:
        return get_rag_chain().query(request.question, session_id)
    except ValueError as e:
        # 业务层参数问题（空问题等）→ 400
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise _chain_error_to_http(e) from e


@router.post(
    "/ask/stream",
    summary="流式问答（NDJSON）",
    description=(
        "逐 token 返回答案，Content-Type 为 application/x-ndjson，每行一个 JSON 对象：\n"
        '- 第一帧 `{"type":"meta", ...}`：意图、溯源资料、重写后的问题\n'
        '- 中间帧 `{"type":"chunk","content":"..."}`：答案文本增量\n'
        '- 最后一帧 `{"type":"done","elapsed_ms":...}`：完成标记'
    ),
)
def ask_stream(request: AskRequest) -> StreamingResponse:
    """
    流式问答接口（NDJSON）。

    为什么用 NDJSON 而不是 SSE：前端 fetch + ReadableStream 就能逐行解析，
    不需要 EventSource（它只支持 GET，带不了请求体），实现更简单。
    """
    session_id = _resolve_session_id(request.session_id)
    logger.info("收到流式问答请求 | session_id=%s query=%.24s", session_id, request.question)

    def event_generator() -> Iterator[str]:
        """把 RAGChain.stream 的 dict 事件序列化为 NDJSON 行。"""
        try:
            # 先把 session_id 作为首帧发出去：客户端需要它做续聊
            yield json.dumps(
                {"type": "session", "session_id": session_id},
                ensure_ascii=False,
            ) + "\n"
            for event in get_rag_chain().stream(request.question, session_id):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except ValueError as e:
            yield json.dumps(
                {"type": "error", "status": 400, "detail": str(e)},
                ensure_ascii=False,
            ) + "\n"
        except Exception as e:
            # 流式接口一旦开始就不能改状态码，错误只能作为一帧数据下发
            logger.error("流式问答执行失败：%s", e, exc_info=True)
            yield json.dumps(
                {
                    "type": "error",
                    "status": 502,
                    "detail": f"问答服务暂时不可用：{type(e).__name__}: {e}",
                },
                ensure_ascii=False,
            ) + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        # 禁缓存 + 禁缓冲：部分反向代理会攒批响应，X-Accel-Buffering 明确关掉
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/sessions",
    response_model=list[SessionInfoItem],
    summary="列出全部会话",
)
def list_sessions() -> list[dict[str, Any]]:
    """列出当前内存中的全部会话（运维/调试用）。"""
    return get_memory_manager().list_sessions()


@router.get(
    "/sessions/{session_id}",
    response_model=SessionHistoryResponse,
    summary="查看会话历史",
)
def get_session_history(session_id: str) -> dict[str, Any]:
    """查看某个会话的对话历史。"""
    memory = get_memory_manager()
    messages = memory.get_messages(session_id)
    return {
        "session_id": session_id,
        "message_count": len(messages),
        "messages": [
            # LangChain Message 的 type：human/ai → 对外统一为 user/assistant（OpenAI 习惯）
            {"role": "user" if m.type == "human" else "assistant", "content": str(m.content)}
            for m in messages
        ],
    }


@router.delete(
    "/sessions/{session_id}",
    summary="清空会话记忆",
)
def delete_session(session_id: str) -> dict[str, Any]:
    """清空某个会话的全部记忆（用户点「新对话」时调用）。"""
    cleared = get_memory_manager().clear_session(session_id)
    if not cleared:
        # 会话不存在不算错误（幂等）：重复点「清空」应该得到同样的成功结果
        return {"session_id": session_id, "cleared": False, "detail": "会话不存在或已过期"}
    return {"session_id": session_id, "cleared": True}


@router.get(
    "/health",
    summary="链路健康检查",
    description="返回 LLM / 检索器 / 记忆 / 意图分类器的当前状态（不含密钥）。",
)
def health() -> dict[str, Any]:
    """健康检查：只读状态，不触发真实 LLM 调用。"""
    return {"status": "ok", **get_rag_chain().get_chain_info()}
