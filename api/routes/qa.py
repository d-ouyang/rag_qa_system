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
    # chunk_id 是 P0-4a 加的**引用键**：前端拿它调 GET /api/v1/chunks/{chunk_id} 反查切片全文。
    # ⚠️ 这个字段必须写在这里，不能指望 response_model 透传 ——
    #    pydantic 默认 extra='ignore'，`_extract_sources()` 多给的键会被**静默丢掉**，
    #    现象是「后端日志里有 chunk_id、接口返回里没有」，且不会报任何错。
    #    由 tests/test_module10_chunk_refs.py 的「溯源字段契约」用例守着。
    chunk_id: str | None = Field(
        default=None,
        description="切片引用键（格式 `<doc_id>:<chunk_index>`）；P0-3 之前的遗留切片为 null，"
                    "前端应渲染成不可点击的纯文本",
    )
    source: str = Field(description="来源文件路径（落盘路径；悬停展示）")
    file_name: str | None = Field(
        default=None,
        description="原始文件名（展示用）。老会话里没有这个字段时，由历史接口按 doc_id 补上",
    )
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
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="本次问答 token 用量：input_tokens / output_tokens / cache_read_tokens",
    )
    cache_hit: bool = Field(default=False, description="本次回答来自相同问题缓存，未调用大模型")


class MessageItem(BaseModel):
    """一条对话历史消息（assistant 消息按轮回填本轮详情）。"""

    role: str = Field(description="user / assistant")
    content: str = Field(description="消息内容")
    ts: float | None = Field(default=None, description="提问时间（Unix 时间戳，同轮两条消息相同）")
    sources: list[SourceItem] | None = Field(
        default=None, description="本轮引用资料（仅 assistant 消息携带）",
    )
    intent: str | None = Field(default=None, description="本轮意图（仅 assistant 消息携带）")
    usage: dict[str, int] | None = Field(
        default=None, description="本轮 token 用量（仅 assistant 消息携带）",
    )
    elapsed_ms: float | None = Field(
        default=None, description="本轮总耗时毫秒（仅 assistant 消息携带）",
    )


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
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="会话累计 token 用量：input_tokens / output_tokens / cache_read_tokens / requests",
    )
    pinned: bool = Field(default=False, description="是否置顶")
    title: str | None = Field(default=None, description="用户自定义标题（覆盖自动标题）")


class SessionUpdateRequest(BaseModel):
    """会话元数据更新请求（重命名 / 置顶）。"""

    title: str | None = Field(default=None, max_length=60, description="自定义标题（不传不改）")
    pinned: bool | None = Field(default=None, description="置顶标记（不传不改）")


class SessionTruncateRequest(BaseModel):
    """会话截断请求（编辑重发用）。"""

    keep_messages: int = Field(
        ge=0, description="保留前 N 条消息；奇数自动向下取偶（轮边界对齐）",
    )


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
    """列出当前全部会话（运维/调试用；存储后端见 MEMORY_BACKEND 配置）。"""
    return get_memory_manager().list_sessions()


def _fill_source_file_names(sources: list[Any]) -> list[Any]:
    """
    老会话的 sources 只存了落盘路径（uuid 文件名）。
    按 chunk_id 里的 doc_id 回表补原始文件名，让刷新后的历史也能显示文件名。
    """
    if not sources:
        return []
    from core.document_repo import get as get_document

    names: dict[int, str] = {}
    filled: list[Any] = []
    for item in sources:
        if not isinstance(item, dict):
            filled.append(item)
            continue
        row = dict(item)
        if not row.get("file_name"):
            doc_part = str(row.get("chunk_id") or "").split(":", 1)[0]
            if doc_part.isdigit():
                doc_id = int(doc_part)
                if doc_id not in names:
                    record = get_document(doc_id)
                    names[doc_id] = record.file_name if record else ""
                if names[doc_id]:
                    row["file_name"] = names[doc_id]
        filled.append(row)
    return filled


@router.get(
    "/sessions/{session_id}",
    response_model=SessionHistoryResponse,
    summary="查看会话历史",
)
def get_session_history(session_id: str) -> dict[str, Any]:
    """
    查看某个会话的对话历史。

    每轮问答的展示元数据（引用资料 / 意图 / token 用量 / 耗时 / 提问时间）
    按「轮」存放在 MemoryManager，这里回填到该轮的 assistant 消息上——
    前端刷新后恢复的就是这些字段，引用不再丢失。
    """
    memory = get_memory_manager()
    messages = memory.get_messages(session_id)
    metas = memory.get_exchange_meta(session_id)
    items: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        turn = i // 2                       # 第几轮（0 起）
        meta = metas[turn] if turn < len(metas) else {}
        item: dict[str, Any] = {
            "role": "user" if m.type == "human" else "assistant",
            "content": str(m.content),
            "ts": meta.get("ts"),
        }
        if item["role"] == "assistant":
            item.update({
                "sources": _fill_source_file_names(meta.get("sources") or []),
                "intent": meta.get("intent"),
                "usage": meta.get("usage"),
                "elapsed_ms": meta.get("elapsed_ms"),
            })
        items.append(item)
    return {
        "session_id": session_id,
        "message_count": len(messages),
        "messages": items,
    }


@router.patch(
    "/sessions/{session_id}",
    summary="更新会话元数据（重命名 / 置顶）",
)
def update_session(session_id: str, request: SessionUpdateRequest) -> dict[str, Any]:
    """重命名或置顶会话。两个字段都不传时返回当前元数据。"""
    meta = get_memory_manager().update_session_meta(
        session_id, title=request.title, pinned=request.pinned,
    )
    if meta is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return {"session_id": session_id, **meta}


@router.post(
    "/sessions/{session_id}/truncate",
    summary="截断会话历史（编辑重发）",
)
def truncate_session(session_id: str, request: SessionTruncateRequest) -> dict[str, Any]:
    """
    把历史截断到前 keep_messages 条（奇数向下取偶，保证轮边界完整）。

    前端「编辑历史提问并重新发送」的流程：
    先调本接口截掉该提问及其后的消息 → 前端改写文本 → 走正常 ask 重问。
    """
    ok = get_memory_manager().truncate_session(session_id, request.keep_messages)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    memory = get_memory_manager()
    return {
        "session_id": session_id,
        "message_count": len(memory.get_messages(session_id)),
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
    """
    健康检查：只读状态，不触发真实 LLM 调用。

    记忆段额外补上存储后端名：排查「重启后会话还在不在」这类问题时，
    第一眼要确认的就是现在到底挂的是 memory 还是 redis。
    """
    info = get_rag_chain().get_chain_info()
    memory = info.setdefault("memory", {})
    if isinstance(memory, dict):
        memory["backend"] = get_memory_manager().store.name
    return {"status": "ok", **info}
