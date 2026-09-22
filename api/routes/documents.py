"""
知识库文档管理接口 —— 支撑前端「文件传输 / RAG 知识库管理」界面。

--------------------------------------------------------------------------
接口一览（统一前缀 /api/v1/documents）
--------------------------------------------------------------------------
    POST   /upload                上传单个文档（解析 → 切分 → 向量化入库）
    GET    /                      列出知识库全部文档（按来源分组）
    GET    /stats                 向量库状态统计（后端类型/嵌入模型/总量）
    GET    /chunks?source=<路径>    查看某文档的全部切分片段（全文、字数、页码）
    DELETE /?source=<路径>          按来源删除某文档的全部片段与上传文件

--------------------------------------------------------------------------
与 qa.py 的分层约定一致：本模块只做参数校验、协议转换与错误码映射，
真正的「解析 → 切分 → 嵌入 → 入库」分别复用 core/document_loader.py
与 core/vector_store.py，不在接口层写业务。
"""

import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from config.settings import settings
from core.document_loader import DocumentLoader
from core.vector_store import get_vector_store_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["知识库管理"])

# 单文件上传大小上限（50MB）：DocumentLoader 支持的 Office/PDF 普遍在此范围内
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# 文件名安全化：只保留常见中英文、数字、点、下划线、连字符，防止路径穿越（../ 等）
_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fff]")


def _get_loader() -> DocumentLoader:
    """DocumentLoader 无状态、构造轻量，每次请求新建即可（真正重的向量库是单例）。"""
    return DocumentLoader()


@router.post(
    "/upload",
    summary="上传文档入库",
    description="接收 pdf/word/excel/ppt/csv/html/json/txt/markdown 文件，解析切分后写入向量库。同名文件重复上传会先删除旧片段再入库。",
)
async def upload_document(file: UploadFile = File(..., description="待入库的文档文件")) -> dict[str, Any]:
    # 1. 后缀白名单校验（拒绝在最前面，避免把无意义文件落盘）
    raw_name = file.filename or "unnamed"
    ext = Path(raw_name).suffix.lower()
    loader = _get_loader()
    if ext not in loader.SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 '{ext}'，当前支持：{'/'.join(sorted(e.lstrip('.') for e in loader.SUPPORTED_EXTENSIONS))}",
        )

    # 2. 读文件内容并限制大小（读进内存而不是直接 copy 到磁盘：要先校验大小，避免超大文件占满磁盘）
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"文件超过大小上限（{MAX_UPLOAD_SIZE // 1024 // 1024}MB）")
    if not content:
        raise HTTPException(status_code=400, detail="上传的文件内容为空")

    # 3. 文件名安全化后落盘到 UPLOAD_DIR（重名覆盖写，与「先删旧片段」配套保证幂等）
    safe_name = _SAFE_NAME.sub("_", raw_name)
    target_path = settings.UPLOAD_DIR / safe_name
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)

    store = get_vector_store_manager()
    # 4. 同名重传：先删掉该来源的旧片段，否则同一文件在库里出现两份（新旧内容混检）
    store.delete_by_source(str(target_path))

    # 5. 解析 → 切分 → 入库（load_file 内部失败返回空列表并记日志）
    documents = loader.load_file(target_path)
    if not documents:
        # 解析失败时清掉刚落盘的坏文件，保持 upload 目录干净
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"文件解析失败或内容为空：{raw_name}，请检查文件是否损坏或格式是否正确")

    added = store.add_documents(documents)
    if added == 0:
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"向量库写入失败：{raw_name}")

    logger.info("文档入库成功 | 文件=%s | 片段=%d", safe_name, added)
    return {
        "file_name": safe_name,
        "source": str(target_path),
        "file_type": ext.lstrip("."),
        "chunks_added": added,
        "total_chunks": store.count(),
    }


@router.get(
    "/",
    summary="列出知识库文档",
    description="按来源（文件）分组返回知识库内全部文档及其片段数。",
)
def list_documents() -> dict[str, Any]:
    store = get_vector_store_manager()
    documents = store.list_documents()
    return {
        "total_documents": len(documents),
        "total_chunks": store.count(),
        "documents": documents,
    }


@router.get(
    "/chunks",
    summary="查看文档切分片段",
    description="返回某文档在向量库中的全部片段（全文、字数、页码），供知识库管理页排查「模型实际看到的内容」。",
)
def get_document_chunks(source: str = Query(..., description="文档来源路径（来源见 GET /api/v1/documents）")) -> dict[str, Any]:
    chunks = get_vector_store_manager().get_chunks(source)
    return {"source": source, "chunk_count": len(chunks), "chunks": chunks}


@router.get(
    "/stats",
    summary="向量库状态",
    description="向量库后端类型、持久化目录、嵌入模型、片段总量等统计信息。",
)
def document_stats() -> dict[str, Any]:
    return get_vector_store_manager().get_stats()


@router.delete(
    "/",
    summary="删除知识库文档",
    description="按来源路径删除该文档在向量库中的全部片段，并清理 upload 目录下的对应文件。",
)
def delete_document(source: str = Query(..., description="文档来源路径（来源见 GET /api/v1/documents）")) -> dict[str, Any]:
    store = get_vector_store_manager()
    deleted = store.delete_by_source(source)
    if deleted == 0:
        # 幂等处理：库里没有不报错，但物理文件若存在仍清理
        removed_file = _remove_upload_file(source)
        return {"source": source, "deleted_chunks": 0, "file_removed": removed_file,
                "detail": "该来源在向量库中不存在（可能已删除）"}

    removed_file = _remove_upload_file(source)
    return {"source": source, "deleted_chunks": deleted, "file_removed": removed_file}


def _remove_upload_file(source: str) -> bool:
    """删除 upload 目录下对应的物理文件。只允许删 UPLOAD_DIR 内的文件（防路径穿越误删）。"""
    try:
        path = Path(source).resolve()
        upload_root = settings.UPLOAD_DIR.resolve()
        if upload_root not in path.parents:
            # 不在 upload 目录内（例如通过目录批量入库的文档），不动磁盘文件
            return False
        if path.is_file():
            path.unlink()
            logger.info("已删除上传文件：%s", path)
            return True
    except Exception as e:  # 物理文件清理失败不影响向量库删除结果
        logger.warning("清理上传文件失败（忽略）：%s | %s", source, e)
    return False
