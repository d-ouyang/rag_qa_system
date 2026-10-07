"""
问答接口模块 —— 把 RAGChain 的能力封装为标准化 RESTful 接口。

--------------------------------------------------------------------------
接口一览（统一前缀 /api/v1/qa）
--------------------------------------------------------------------------
    POST   /ask                       同步问答（一次拿完整答案）
    POST   /ask/stream                流式问答（NDJSON，逐 token 返回）
    GET    /sessions                  列出**当前用户自己**的会话（P2-12c）
    GET    /sessions/{session_id}     查看自己的某个会话的对话历史
    PATCH  /sessions/{session_id}     重命名 / 置顶
    POST   /sessions/{id}/truncate    截断历史（编辑重发）
    DELETE /sessions/{session_id}     删除会话
    GET    /health                    链路健康检查（LLM/检索器/记忆/意图）

--------------------------------------------------------------------------
分层原则（为什么接口层这么「薄」）
--------------------------------------------------------------------------
本模块只做四件事：参数校验（pydantic）、协议转换（HTTP ↔ dict）、
错误码映射（业务异常 → HTTP 状态码）、序列化。
所有业务逻辑（检索、生成、记忆、意图）都在 core/rag_chain.py ——
接口层不写业务，将来换 gRPC / WebSocket 时 core 一行都不用动。

--------------------------------------------------------------------------
P2-12c：所有会话路由都挂了身份，每个动作都带 owner_id
--------------------------------------------------------------------------
12c 之前这些路由**都没有挂 `current_actor`**，所以「这条会话是谁的」
这件事在接口层根本不存在 —— 任何登录用户（甚至裸跑 8000 时的任何人）
都能读、改、删任何 session_id。现在每个路由都拿 `Actor`，
再把 `actor.id` 当作 `owner_id` 往下传，判据由存储层执行。

为什么归属判据在存储层而不在接口层：
    「会话归谁」是存储层的数据（MySQL 的 user_id 列）。
    若在接口层写 `WHERE user_id = actor.id`，每新增一个后端就得在接口层
    改一遍，而漏改就是泄漏。现在接口层只负责「把身份传下去」。

⚠️ **break-glass 身份（`actor.id is None`，`.env` 里那个不在库里的超管）
在这一层拿到 `owner_id=None`，语义是「只认无主会话」**：
    他看得到自己（无主）建的会话，看不到任何已归属员工的会话。
    这是刻意的：给超管开「能看所有人会话」的后门，
    等于让整个隔离模型有一个必须靠「大家记得别用」来守的例外。
    break-glass 的定位是「账号系统挂了还能进来改配置」，不是「查看员工对话」。
    真正的跨账号查看应该是 P2-14 写权限里一个**显式**的、按角色授的功能，
    而不是一个隐式的绕过通道。
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.identity import Actor, current_actor
from core.memory_manager import get_memory_manager
from core.rag_chain import get_rag_chain
from core.session_store import SessionOwnershipError

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


def _ownership_error_to_http(e: SessionOwnershipError) -> HTTPException:
    """越权 → 403（用户拍板：不是 404）。理由见 SessionStore.is_foreign_to。"""
    return HTTPException(
        status_code=403,
        detail=f"无权访问该会话（{e.session_id}）",
    )


def _session_missing_or_forbidden(session_id: str, owner_id: int | None) -> HTTPException:
    """
    「打不开这条会话」时区分 404 与 403（用户拍板：越权报 403）。

    存储层把「不存在」和「不属于我」压成同一个空结果，理由是**对逻辑层**
    两者没区别。但对用户有区别：他带着一个自己列表里没有的 id 来访问，
    404 会让他以为「我记错了 / 刷新一下就好」，403 直接告诉他「这是别人的」。
    所以这里多问存储层一次「这条到底存不存在、是不是别人的」。

    ⚠️ 这条判断**只在已经失败时**才走（不存在 → 404），不是每次读都查两遍 ——
    正常路径只有一次 load。越权信息也只回「无权访问」，
    不回「它的主人是 wu.jing」，那等于把员工名册变成一个可枚举的目录。
    """
    if get_memory_manager().is_foreign(session_id, owner_id=owner_id):
        return HTTPException(status_code=403, detail="无权访问该会话")
    return HTTPException(status_code=404, detail="会话不存在或已过期")


# --------------------------------------------------------------------------- #
# 接口实现
# --------------------------------------------------------------------------- #
@router.post(
    "/ask",
    response_model=AskResponse,
    summary="同步问答",
    description="一次请求拿到完整答案。意图识别为闲聊时不做检索直接回答。",
)
def ask(request: AskRequest, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """同步问答接口。"""
    session_id = _resolve_session_id(request.session_id)
    # ⚠️ 越权预检必须在**调链路之前**（P2-12c）。
    # 不预检的话，越权会一路走到 `rag_chain.query` 的最后一次写记忆才抛
    # `SessionOwnershipError` —— 也就是说模型已经完整答完一遍、检索也跑完了，
    # 才告诉你这轮问的不该问。省下来的不只是响应时间，还有真金白银的
    # token 与一次检索。
    if get_memory_manager().is_foreign(session_id, owner_id=actor.id):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    logger.info("收到问答请求 | session_id=%s 用户=%s query=%.24s",
                session_id, actor.username, request.question)
    try:
        return get_rag_chain().query(request.question, session_id, owner_id=actor.id)
    except SessionOwnershipError as e:
        # 越权写别人的会话。必须排在 ValueError 之前：
        # SessionOwnershipError 是普通 Exception，落到最后的兜底会被报成 502，
        # 而 502 的语义是「下游挂了」—— 用户会一直重试，而重试永远不会成功。
        raise _ownership_error_to_http(e) from e
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
def ask_stream(request: AskRequest, actor: Actor = Depends(current_actor)) -> StreamingResponse:
    """
    流式问答接口（NDJSON）。

    为什么用 NDJSON 而不是 SSE：前端 fetch + ReadableStream 就能逐行解析，
    不需要 EventSource（它只支持 GET，带不了请求体），实现更简单。

    ⚠️ **越权必须在生成器启动前判，不能在生成器里判**（P2-12c）：
    StreamingResponse 一旦返回 200，状态码就永远改不了了，
    之后任何异常只能作为一帧 `{"type":"error"}` 下发 ——
    也就是说客户端会先拿到 200，再在流里读到 403。
    正确做法是在这里（响应还没构造）同步问一次存储层，
    不属于我就直接抛 403，客户端拿到的是一个干净的 HTTP 错误。
    这多了一次查询，但只在「客户端传了一个别人的 session_id」时才发生。
    """
    session_id = _resolve_session_id(request.session_id)
    logger.info("收到流式问答请求 | session_id=%s 用户=%s query=%.24s",
                session_id, actor.username, request.question)

    # 预检：这条会话是不是别人的？只在「确实属于别人」时拒绝。
    # 「不存在」不拒绝 —— 传一个库里还没有的 session_id 是**首问的正常情况**
    #（前端 createSession() 每次都生成新 UUID），拒绝等于把新会话堵死。
    if get_memory_manager().is_foreign(session_id, owner_id=actor.id):
        raise HTTPException(status_code=403, detail="无权访问该会话")

    def event_generator() -> Iterator[str]:
        """把 RAGChain.stream 的 dict 事件序列化为 NDJSON 行。"""
        chain_stream = get_rag_chain().stream(request.question, session_id, owner_id=actor.id)
        try:
            # 先把 session_id 作为首帧发出去：客户端需要它做续聊
            yield json.dumps(
                {"type": "session", "session_id": session_id},
                ensure_ascii=False,
            ) + "\n"
            for event in chain_stream:
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except GeneratorExit:
            # Starlette 在客户端断开时 close() 这个生成器。
            # 必须把内层流也关掉，否则 LLM 还会继续吐 token。
            logger.info("客户端断开连接，停止流式生成 | session_id=%s", session_id)
            closer = getattr(chain_stream, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    logger.warning("关闭问答流失败", exc_info=True)
            raise
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
    summary="列出我的会话",
    description="只返回**当前登录用户自己**的会话。这是P2-12c 之后语义的变化，见迭代文档。",
)
def list_sessions(actor: Actor = Depends(current_actor)) -> list[dict[str, Any]]:
    """
    列出当前用户自己的会话（P2-12c）。

    12c 之前这里是「全部会话」—— 也就是说，任何登录用户都能看到
    同事的会话列表（标题、消息条数、token 用量、最后活跃时间）。
    现在按 `actor.id` 过滤。
    """
    return get_memory_manager().list_sessions(owner_id=actor.id)


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
def get_session_history(
    session_id: str, actor: Actor = Depends(current_actor)
) -> dict[str, Any]:
    """
    查看某个会话的对话历史（只能是自己的会话）。

    每轮问答的展示元数据（引用资料 / 意图 / token 用量 / 耗时 / 提问时间）
    按「轮」存放在 MemoryManager，这里回填到该轮的 assistant 消息上——
    前端刷新后恢复的就是这些字段，引用不再丢失。

    ⚠️ 空历史有两种情况，12c 之前分不开，现在也仍然分不开
    （「会话存在但一条消息都没有」vs「会话不存在」）：
        前者是真的空会话（12c 之前允许建了就不说话），
        后者拿别人的 id 来访问 —— 但那种情况会被 `is_foreign` 拦下报 403。
    剩下的「我自己的空会话」返回 200 + 空列表，行为与12c 之前一致。
    """
    memory = get_memory_manager()
    messages = memory.get_messages(session_id, owner_id=actor.id)
    if not messages and memory.is_foreign(session_id, owner_id=actor.id):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    metas = memory.get_exchange_meta(session_id, owner_id=actor.id)
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
def update_session(
    session_id: str, request: SessionUpdateRequest, actor: Actor = Depends(current_actor)
) -> dict[str, Any]:
    """重命名或置顶会话（只能改自己的）。两个字段都不传时返回当前元数据。"""
    memory = get_memory_manager()
    try:
        meta = memory.update_session_meta(
            session_id, title=request.title, pinned=request.pinned, owner_id=actor.id,
        )
    except SessionOwnershipError as e:
        raise _ownership_error_to_http(e) from e
    if meta is None:
        raise _session_missing_or_forbidden(session_id, actor.id)
    return {"session_id": session_id, **meta}


@router.post(
    "/sessions/{session_id}/truncate",
    summary="截断会话历史（编辑重发）",
)
def truncate_session(
    session_id: str, request: SessionTruncateRequest, actor: Actor = Depends(current_actor)
) -> dict[str, Any]:
    """
    把历史截断到前 keep_messages 条（奇数向下取偶，保证轮边界完整）。

    前端「编辑历史提问并重新发送」的流程：
    先调本接口截掉该提问及其后的消息 → 前端改写文本 → 走正常 ask 重问。
    """
    memory = get_memory_manager()
    try:
        ok = memory.truncate_session(
            session_id, request.keep_messages, owner_id=actor.id,
        )
    except SessionOwnershipError as e:
        raise _ownership_error_to_http(e) from e
    if not ok:
        raise _session_missing_or_forbidden(session_id, actor.id)
    return {
        "session_id": session_id,
        "message_count": len(memory.get_messages(session_id, owner_id=actor.id)),
    }


@router.delete(
    "/sessions/{session_id}",
    summary="清空会话记忆",
)
def delete_session(session_id: str, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """清空某个会话的全部记忆（只能删自己的）。"""
    cleared = get_memory_manager().clear_session(session_id, owner_id=actor.id)
    if not cleared:
        # 幂等：重复点「清空」应该得到同样的成功结果。
        # 但「属于别人」不是幂等的一部分 —— 必须报 403，
        # 否则「删不掉」和「删了别人的（其实没删）」返回同一个体，
        # 用户会以为删成功而实际没删，下次再看记录还在，只会以为系统有 bug。
        if get_memory_manager().is_foreign(session_id, owner_id=actor.id):
            raise HTTPException(status_code=403, detail="无权访问该会话")
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
