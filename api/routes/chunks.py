"""
切片引用反查接口 —— 支撑前端「点击回答里的引用，看这段正文到底出自哪」。

--------------------------------------------------------------------------
这个接口补的是哪条链路
--------------------------------------------------------------------------
一次问答只会把**片段的前 200 字**塞进 `sources.snippet`（见 core/rag_chain.py
的 _extract_sources）—— 因为把每个引用的全文都吐给前端，会让一个回答的
响应体膨胀到几百 KB，而用户通常只关心其中一两条。

于是「想看某条引用的完整上下文」这件事必须由前端按需再要一次：

    前端点引用 → 拿 chunk_id（如 "12:3"）
        → GET /api/v1/chunks/{chunk_id}
        → Chroma 取切片正文
        → metadata.doc_id
        → MySQL 取文件名 / 上传时间 / 归属项目

--------------------------------------------------------------------------
为什么查不到就是查不到（§11 D3 的决定）
--------------------------------------------------------------------------
切片正文**不存快照**，`chat_message.ref_ids` 里只留 chunk_id。
所以文档一旦被删除，历史回答里的引用就再也拿不到正文了 —— 这是刻意的取舍：

    存快照的代价是「删除不彻底」—— 用户删了一份敏感文档，它的正文
    仍然以快照形式留在每个历史回答的 JSON 里，且没有哪张表能把它捞出来清掉。
    不存快照，删除就是删除干净；代价是历史引用变成一条死链。

所以本接口对「已删除」不做任何补偿（不返回缓存、不返回 snippet 兜底），
而是明确回 404 + 一句人话，由前端把引用降级展示成「引用内容已随文档删除」。

--------------------------------------------------------------------------
错误码分工（三者含义不同，前端要能分开处理）
--------------------------------------------------------------------------
    400  chunk_id 格式非法            —— 客户端错误，重试无意义
    404  文档已被删除                 —— 死链，降级展示
    404  文档还在但切片没了            —— 文档被重新解析过，提示刷新

两种 404 的 `detail` 文案不同，前端按文案分发即可（不需要额外错误码字段：
本接口只有一个失败维度，加一层 code 只是把同一个信息写两遍）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import document_repo as repo
from core.vector_store import get_vector_store_manager, parse_chunk_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chunks", tags=["引用反查"])


class ChunkDetail(BaseModel):
    """
    一个切片的完整信息。

    字段分三组，来源不同 —— 这个区分很重要，因为它决定了「缺哪个字段
    代表出事了」：
        · 切片自身（chunk_id / chunk_index / content / page）  ← Chroma
        · 归属文档（doc_id / file_name / upload_time / …）      ← MySQL
        · 一致性（document_exists / document_status）           ← 两者对照
    """

    chunk_id: str = Field(description="切片引用键，形如 12:3（doc_id:chunk_index）")
    doc_id: int = Field(description="归属文档在 MySQL 中的主键")
    chunk_index: int = Field(description="该片段在文档内的序号，从 0 开始")

    content: str = Field(description="切片正文（完整，不截断）")
    char_count: int = Field(description="正文长度")
    page: int | None = Field(default=None, description="PDF/PPT 页码，非分页格式为 null")

    # ---- 以下字段来自 MySQL。文档记录不存在时用 Chroma 元数据兜底或为 null ----
    file_name: str | None = Field(default=None, description="原始文件名（不是 uuid 落盘名）")
    file_type: str | None = Field(default=None, description="无点后缀，如 pdf")
    file_size: int | None = Field(default=None, description="字节数")
    upload_time: str | None = Field(default=None, description="上传时间，YYYY-MM-DD HH:mm:ss")
    project_id: str | None = Field(default=None, description="归属项目")

    # ---- 一致性标记 ----
    document_exists: bool = Field(
        description=(
            "MySQL 里是否还有这条文档记录。切片在、记录不在属于**脏数据**"
            "（删除链路半途失败留下的孤儿向量），此时内容照常返回，"
            "但这一个 false 就是运维该去跑重建脚本的信号。"
        )
    )
    document_status: str | None = Field(
        default=None, description="文档解析状态 pending/parsing/success/fail；记录不存在为 null"
    )


@router.get(
    "/{chunk_id}",
    response_model=ChunkDetail,
    summary="按 chunk_id 反查切片正文与来源",
    description=(
        "回答里的引用只带前 200 字摘要，本接口按需取回**完整正文**与来源文档信息。\n\n"
        "`chunk_id` 形如 `12:3`（`doc_id:chunk_index`）。冒号是 URL 路径里的合法字符，"
        "客户端直接拼即可，不需要转义（`%3A` 同样接受）。\n\n"
        "切片正文**不存快照**：文档被删除后，历史引用会拿到 404，"
        "前端应降级展示成「引用内容已随文档删除」，而不是报错。"
    ),
)
def get_chunk(chunk_id: str) -> ChunkDetail:
    # ---- 1. 语法校验放在最前面 ----
    # 先挡格式再碰数据库：一个乱填的 chunk_id 不该产生两次查询，
    # 也不该因为「查不到」而被报成 404 —— 那会让前端把一个拼错的链接
    # 当成「文档已删除」来展示，方向完全跑偏。
    try:
        doc_id, chunk_index = parse_chunk_id(chunk_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"chunk_id 格式非法：{e}") from e

    # ---- 2. 两个来源各查一次 ----
    # 必须先查 Chroma：切片不在，后面的 MySQL 查询只是为了**给错误分类**，
    # 而分类结果只影响文案。反过来先查 MySQL 也能跑，但那样「切片在不在」
    # 这个决定 200/404 的关键事实就排在了次要位置。
    payload: dict[str, Any] | None = get_vector_store_manager().get_chunk_by_position(
        doc_id, chunk_index
    )
    record = repo.get(doc_id)

    if payload is None:
        # 两种「查不到」，前端处置不同，所以文案必须分开
        if record is None:
            raise HTTPException(status_code=404, detail="引用内容已随文档删除")
        raise HTTPException(
            status_code=404,
            detail="引用片段已失效（文档已重新解析或正在解析），请刷新后重试",
        )

    # ---- 3. 组装 ----
    # 记录存在就用 MySQL 的（权威：文件名可能被改过、时间更准）；
    # 记录不在（孤儿向量）退化用 Chroma 元数据里的那份 —— 内容都能看到，
    # 但 document_exists=false 会把这件事如实说出去。
    return ChunkDetail(
        chunk_id=payload["chunk_id"],
        doc_id=doc_id,
        chunk_index=chunk_index,
        content=payload["content"],
        char_count=payload["char_count"],
        page=payload["page"],
        file_name=record.file_name if record else payload.get("file_name"),
        file_type=payload.get("file_type"),
        file_size=record.file_size if record else None,
        upload_time=(
            record.upload_time.isoformat(sep=" ", timespec="seconds") if record else None
        ),
        project_id=(record.project_id if record else payload.get("project_id")),
        document_exists=record is not None,
        document_status=record.status if record else None,
    )
