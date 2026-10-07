"""
知识库文档管理接口 —— 支撑前端「文件传输 / RAG 知识库管理」界面。

--------------------------------------------------------------------------
接口一览（统一前缀 /api/v1/documents）
--------------------------------------------------------------------------
    POST   /upload                  上传文档（落盘 → 建 pending 记录 → 入队 → **立即返回**）
    POST   /upload/batch            批量上传（p1.5c；逐份独立受理，单份失败不拖垮整批）
    GET    /                        列出知识库文档（**读 MySQL**，支持 ?status= &project_id=）
    GET    /stats                   向量库状态统计（后端类型/嵌入模型/总量）
    GET    /capabilities            **本人**的知识库写权限（14d：前端入口收敛的数据源）
    GET    /download?doc_id=        下载原始文件（不允许直接暴露磁盘路径）
    GET    /{doc_id}/chunks         查看某文档的全部切分片段
    POST   /{doc_id}/reparse        手动重新触发解析
    DELETE /{doc_id}                删除文档（磁盘 + Chroma + MySQL **三件事**）

--------------------------------------------------------------------------
**四条写路由的身份依赖**（P2-14c，2026-10-08 加）
--------------------------------------------------------------------------
14.0 的实测基线：这四条路由当时**一条身份依赖都没有** ——

    普通员工 chen.jie → POST   /api/v1/documents/upload  → 202 + doc_id=587
    普通员工 chen.jie → DELETE /api/v1/documents/587     → {"record_removed": true}

也就是说「谁能改全公司共用的知识库」这件事**当时完全没有答案**。
本轮从零补上守卫：

    POST   /upload           Depends(require_kb_upload)
    POST   /upload/batch     Depends(require_kb_upload)
    POST   /{doc_id}/reparse Depends(require_kb_upload)
    DELETE /{doc_id}         Depends(require_kb_delete)

⚠️ **守卫必须写在 `Depends` 里，不能写在函数体里** —— `tests/test_module12_write_acl.py`
有一条**结构断言**正扫这份源码，核对「每条写路由的 `Depends` 列表里有没有
`require_kb_*`」。判据就是这张列表；一旦把判定挪进函数体，列表就是空的，
而这次要防的失效恰恰是「漏一条路由且**不报错**」。

⚠️ **本文件没有 `PUT` / `PATCH`**。将来新增写操作时，
`tests/test_module12_write_acl.py` 的结构断言会因为「新路由不在已知清单里」而转红 ——
那正是它该做的事。

--------------------------------------------------------------------------
与 P0-3 之前的最大区别：上传不再同步解析
--------------------------------------------------------------------------
旧版把「解析 → 切分 → 嵌入 → 入库」全放在这个 HTTP 请求里跑完再返回。
50MB 的 PDF 会把请求挂住几十秒，浏览器超时、网关超时、用户以为失败重传 ——
而重传会再挂一次。现在这个请求只做三件事（落盘、写待解析记录、投队列），
耗时与文件大小基本无关。

代价是**响应里不再有 `chunks_added`** —— 那个数此刻根本还不存在。
前端必须改成「提交后轮询 `status`」，这是本次改造的必然结果，不是遗漏。

--------------------------------------------------------------------------
分层约定（与 qa.py / system.py 一致）
--------------------------------------------------------------------------
本模块只做参数校验、协议转换与错误码映射。真正的逻辑在三处：
    core/document_repo.py   document 表的状态机（含并发抢任务）
    core/queue.py           投递（失败返回 False 而不抛）
    core/parsing.py         「读盘 → 解析 → 切分 → 写向量库」唯一实现
接口层写业务，就会出现「路由里一套删除逻辑、脚本里另一套」这种经典腐烂。
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status as http_status
from fastapi.responses import FileResponse

from config.settings import settings
from core import document_repo as repo
from core import kb_acl
from core import kb_service
from core.document_loader import DocumentLoader
from core.identity import Actor, current_actor, require_kb_delete, require_kb_upload
from core.parsing import resolve_storage_path
from core.queue import enqueue_parse
from core.vector_store import get_vector_store_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["知识库管理"])

# ⚠️ 路由声明顺序在 FastAPI 里是**有语义的**（按注册顺序匹配）。
#    本文件里所有字面量路径（/stats、/download）都排在参数化路径
#    （/{doc_id}/...）之前。若把 /{doc_id}/chunks 提到 /stats 前面，
#    `/stats` 不会出问题（段数不同），但一旦将来加了 `GET /{doc_id}`，
#    `/download` 就会被它吃掉 —— 提前把顺序定对，比事后调试一个
#    「为什么访问 download 返回 404 文档不存在」便宜得多。


def _get_loader() -> DocumentLoader:
    """DocumentLoader 无状态、构造轻量，每次请求新建即可（真正重的向量库是单例）。"""
    return DocumentLoader()


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
@router.post(
    "/upload",
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="上传文档（异步解析）",
    description=(
        "接收 pdf/word/excel/ppt/csv/html/json/txt/markdown 文件。\n\n"
        "**立即返回 202**，响应里只有 doc_id 与 pending 状态 —— 解析在后台 Worker 里进行，\n"
        "进度请轮询 `GET /api/v1/documents/`（看 status 字段）。\n\n"
        "同项目下同名文件视为**替换**：旧的磁盘文件、向量切片、元数据记录会先被清掉。\n"
        "响应里的 `queued=false` 表示已入库但**排队失败**（Redis 不可用），"
        "文件已保存，可稍后用 `POST /{doc_id}/reparse` 补投。"
    ),
)
async def upload_document(
    file: UploadFile = File(..., description="待入库的文档文件"),
    project_id: str = Query("default", description="归属项目，多租户隔离用"),
    actor: Actor = Depends(require_kb_upload),
) -> dict[str, Any]:
    content = await file.read()
    outcome = _accept_one_upload(file.filename or "unnamed", content, project_id, actor=actor)
    if not outcome["ok"]:
        # 单文件入口保持原有对外语义：失败 = 4xx + detail 文案
        raise HTTPException(status_code=outcome["status_code"], detail=outcome["error"])
    return outcome["result"]


# --------------------------------------------------------------------------- #
# 批量上传（p1.5c）
# --------------------------------------------------------------------------- #
# 单批文件数上限：前端「选文件夹」可能一下选出几百个文件，必须有个闸。
# 串行逐份处理（read → 落盘 → 释放），内存峰值 = 单份大小，不随批量数增长。
BATCH_UPLOAD_MAX_FILES = 50


@router.post(
    "/upload/batch",
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="批量上传文档（异步解析）",
    description=(
        "一次请求提交多份文件（前端多选 / 选文件夹）。\n\n"
        "**逐份独立受理**：单份失败（类型不支持 / 超限 / 空文件）不拖垮整批，\n"
        "每份的结果在 `results` 里单独给出（`ok=false` 时带 `error`）。\n"
        f"单批最多 {BATCH_UPLOAD_MAX_FILES} 份；单份大小上限与单文件接口一致。"
    ),
)
async def upload_documents_batch(
    files: list[UploadFile] = File(..., description="待入库的文档文件列表"),
    project_id: str = Query("default", description="归属项目，多租户隔离用"),
    actor: Actor = Depends(require_kb_upload),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    if len(files) > BATCH_UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"单批最多 {BATCH_UPLOAD_MAX_FILES} 份文件（收到 {len(files)} 份），请分批提交",
        )

    results: list[dict[str, Any]] = []
    for f in files:
        raw_name = f.filename or "unnamed"
        # 串行 read：逐份读进内存、落盘后释放，内存峰值与批量数无关
        content = await f.read()
        outcome = _accept_one_upload(raw_name, content, project_id, actor=actor)
        if outcome["ok"]:
            results.append({"ok": True, **outcome["result"]})
        else:
            results.append({"ok": False, "file_name": raw_name, "error": outcome["error"]})

    accepted = sum(1 for r in results if r["ok"])
    logger.info("批量上传受理 | 总数=%d | 受理=%d | 跳过=%d", len(files), accepted, len(files) - accepted)
    return {
        "total": len(files),
        "accepted": accepted,
        "skipped": len(files) - accepted,
        "results": results,
    }


def _accept_one_upload(
    raw_name: str,
    content: bytes,
    project_id: str,
    *,
    actor: Actor | None = None,
) -> dict[str, Any]:
    """
    单份文件的受理逻辑：后缀校验 → 大小校验 → uuid 落盘 → 同名替换 → 建 pending → 入队。

    单文件与批量两个入口共用这一套。失败**不抛异常**，返回 `{"ok": False, ...}`，
    由调用方决定怎么呈现：单文件接口映射回 4xx（保持原有对外语义），
    批量接口记进该项结果继续处理下一份（单份失败不拖垮整批）。

    `actor` 是**可选**的，这本身是个需要解释的形状：
    权限判定在**路由的 Depends** 里做（`require_kb_upload`），不在这里 ——
    放在这里的话，`Depends` 列表里看不到守卫，14e 的结构断言就判不出来，
    而「这条路由有没有守卫」正是这次最需要被机器检查的事实。

    所以 `actor` 只用于**审计**（谁上传的），而 `actor=None` 时记NULL
    （=「查不到是谁」，比如将来某个内部脚本直接调这个函数）。
    审计失败不阻断上传 —— 理由与 13d 一致：业务与审计不同事务，
    把只读的历史表变成业务单点是更坏的结果。
    """
    # ---- 1. 后缀白名单（拦在最前面，避免把注定失败的文件落盘）----
    ext = Path(raw_name).suffix.lower()
    loader = _get_loader()
    if ext not in loader.SUPPORTED_EXTENSIONS:
        return {
            "ok": False,
            "status_code": 400,
            "error": f"不支持的文件类型 '{ext}'，当前支持：{'/'.join(sorted(e.lstrip('.') for e in loader.SUPPORTED_EXTENSIONS))}",
        }

    # ---- 2. 大小校验（读进内存而不是直接写盘：要先知道大小，避免超大文件把磁盘占满）----
    max_bytes = int(settings.DOC_UPLOAD_MAX_BYTES)
    if len(content) > max_bytes:
        return {"ok": False, "status_code": 413, "error": f"文件超过大小上限（{max_bytes // 1024 // 1024}MB）"}
    if not content:
        return {"ok": False, "status_code": 400, "error": "上传的文件内容为空"}

    # ---- 3. 用 uuid 落盘 ----
    # 磁盘名与原始名解耦的理由：原始名不可信（路径穿越、重复、超长、含 emoji），
    # 而且「同名覆盖」会让一次上传把另一条文档的文件抹掉 —— 那是两个不同 doc_id
    # 共用一个文件，删除其中一个就会把另一个变成幽灵记录（记录在、文件没了）。
    # uuid 之后每个 doc_id 独占一个文件，删除语义干净。
    # 原始名不丢：它存在 MySQL `file_name`，展示与下载都用它。
    disk_name = f"{uuid.uuid4().hex}{ext}"
    target_path = settings.UPLOAD_DIR / disk_name
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)
    # 库里存**相对**路径（理由见 alembic/versions/0001 的列注释）：本地与容器
    # 的项目根不同，绝对路径进库会让两边互相读不到对方的文件。
    storage_path = f"{settings.UPLOAD_DIR.name}/{disk_name}"

    safe_name = _sanitize_display_name(raw_name)

    # ---- 4. 同名替换：先把旧文档彻底清掉，再建新的 ----
    existing = repo.find_by_name(safe_name, project_id)
    if existing is not None:
        purged = _purge_document(existing)
        logger.info(
            "同名文档替换 | 原 doc_id=%s | 新文件=%s | 清理: 切片=%s 磁盘=%s",
            existing.doc_id, safe_name, purged["deleted_chunks"], purged["file_removed"],
        )
        # 被替换掉的那份也是一次「删除」，且它删掉的是**已经入库、有引用指向**的文档
        #（正在被别人的历史回答引用着）。不记的话，「这份文档为什么消失了」
        # 只有 `document.upload` 一条记录，看不出它顶掉过谁。
        kb_service.record_delete(
            actor, doc_id=existing.doc_id, file_name=existing.file_name,
            deleted_chunks=purged["deleted_chunks"],
            file_removed=purged["file_removed"],
            record_removed=purged["record_removed"],
            reason="同名替换",
        )

    # ---- 5. 建待解析记录 ----
    doc_id = repo.create_pending(
        file_name=safe_name,
        storage_path=storage_path,
        file_size=len(content),
        project_id=project_id,
    )

    # ---- 6. 投队列，然后**立刻返回** ----
    queued = enqueue_parse(doc_id)
    result: dict[str, Any] = {
        "doc_id": doc_id,
        "file_name": safe_name,
        "status": repo.STATUS_PENDING,
        "file_size": len(content),
        "file_type": ext.lstrip("."),
        "project_id": project_id,
        "queued": queued,
    }
    if not queued:
        result["detail"] = "文件已保存，但解析任务入队失败（消息队列不可用）。可稍后用 reparse 接口补投。"
    logger.info("文档已受理 | doc_id=%s | 文件=%s | 大小=%s | 排队=%s", doc_id, safe_name, len(content), queued)

    # 审计在**成功受理之后**才落：失败的那些在第 1/2 步就 return 了，
    # 那时没有 doc_id、没有磁盘文件，记一条「上传失败」对排查没有价值
    # （失败原因已经原样返回给调用方了）。
    kb_service.record_upload(
        actor, doc_id=doc_id, file_name=safe_name,
        file_size=len(content), project_id=project_id,
    )
    return {"ok": True, "result": result}


def _sanitize_display_name(raw_name: str) -> str:
    """
    清洗展示用文件名：去掉目录部分与危险字符，并截到列宽以内。

    为什么要剥离目录：部分浏览器（老 IE、某些 SDK）会把客户端的**完整路径**
    塞进 filename 字段，于是 `C:\\Users\\a\\报.docx` 会原样进库；
    更糟的是 `/../../etc/passwd` 这种，一旦被当成路径用过就出事。
    这里只保留最后一段文件名，并且**不允许**它带路径分隔符。

    ⚠️ 清洗后的名字只用于展示与下载时的建议文件名，**绝不用于拼磁盘路径**
    （磁盘名是 uuid，见 upload_document 的第 3 步）。
    """
    base = re.split(r"[\\/]", raw_name or "")[-1].strip() or "unnamed"
    # 控制字符与保留字符换成下划线（保留中文、字母、数字、点、下划线、连字符）
    safe = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", base)
    return safe[:255]


# --------------------------------------------------------------------------- #
# 列表（读 MySQL）
# --------------------------------------------------------------------------- #
@router.get(
    "",
    summary="列出知识库文档",
    description=(
        "**数据源是 MySQL**（不是向量库）—— 因为「正在解析 / 解析失败」的文档在向量库里\n"
        "根本没有切片，只看向量库会让人以为「文件没上传成功」。\n"
        "支持按 status / project_id 过滤。"
    ),
)
@router.get(
    "/",
    include_in_schema=False,
    summary="列出知识库文档（带末尾斜杠，兼容旧调用）",
)
def list_documents(
    status: str | None = Query(None, description="按状态过滤：pending/parsing/success/fail"),
    project_id: str | None = Query(None, description="按项目过滤"),
    limit: int = Query(200, ge=1, le=1000, description="最多返回多少条"),
    offset: int = Query(0, ge=0, description="跳过多少条"),
) -> dict[str, Any]:
    if status and status not in repo.ALL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"未知的 status='{status}'，可选：{'/'.join(repo.ALL_STATUSES)}",
        )

    documents = repo.list_documents(status=status, project_id=project_id, limit=limit, offset=offset)
    return {
        "total_documents": len(documents),
        "total_chunks": get_vector_store_manager().count(),
        "counts": repo.counts_by_status(project_id),
        "documents": [d.to_dict() for d in documents],
    }


@router.get(
    "/stats",
    summary="向量库状态",
    description="向量库后端类型、持久化目录、嵌入模型、片段总量等统计信息。",
)
def document_stats() -> dict[str, Any]:
    return get_vector_store_manager().get_stats()


# --------------------------------------------------------------------------- #
# 当前用户的知识库写权限（14d：前端入口收敛的数据源）
# --------------------------------------------------------------------------- #
@router.get(
    "/capabilities",
    summary="当前用户的知识库写权限",
    description=(
        "返回**调用者本人**的 `kb_role` 与三个动作的判定结果。\n\n"
        "**存在的理由**：前端要按权限显示/隐藏上传区与删除按钮，"
        "而判据的唯一定义在 `core/kb_acl.py`（服务端）。\n"
        "让前端自己抄一份五档表 = 两份清单各自漂 —— 13d 与 14f 都踩过这个形状。\n\n"
        "⚠️ **这不是权限闸门**，只是把既有判定**如实告诉**前端：\n"
        "   真正的拒绝发生在四条写路由的 `Depends(require_kb_*)` 上。\n"
        "   所以即使这个端点被绕过（返回全 true），写操作依然会被后端 403 ——\n"
        "   它错了只是让界面**多显示几个点了会失败的按钮**，不会造成越权。\n\n"
        "⚠️ 用 `current_actor`（任意已登录）而不是 `require_kb_*`：\n"
        "   恰恰是**没有**写权限的人才需要读它（才知道自己该看不能改）。\n"
    ),
)
def my_kb_capabilities(actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    # 判据全部由 kb_acl 派生，这里不做任何 if。
    # 档位值本身也回给前端：界面要显示「你当前是只读」这类说明，
    # 而让前端从 capabilities 反推档位是绕路（且三档能力完全相同，分不出来）。
    return {
        "kb_role": actor.kb_role,
        "capabilities": kb_acl.capabilities(actor.kb_role),
        "label": kb_acl.KB_ROLE_LABELS.get(
            actor.kb_role, kb_acl.KB_ROLE_LABELS[kb_acl.DEFAULT_KB_ROLE]
        ),
    }


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
@router.get(
    "/download",
    summary="下载原始文件",
    description="按 doc_id 返回磁盘上的原始文件（浏览器会以原始文件名保存）。不允许直接暴露磁盘路径。",
)
def download_document(doc_id: int = Query(..., description="文档 ID")) -> FileResponse:
    record = repo.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：doc_id={doc_id}")

    path = _resolve_inside_upload_dir(record.storage_path)
    if path is None:
        # 目录穿越或文件不在 upload/ 内 —— 两种情况都不能把内容吐出去。
        # 用 403 而不是 404：这是「不允许」而不是「没有」，日志里区分得开。
        logger.error("拒绝下载越权路径 | doc_id=%s | storage_path=%s", doc_id, record.storage_path)
        raise HTTPException(status_code=403, detail="该文档的存储路径不在允许的目录内")
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"文档记录存在但磁盘文件已丢失：{record.file_name}（可删除该记录后重新上传）",
        )

    return FileResponse(
        path,
        # filename 用**原始**文件名，浏览器才会存成「差旅报销.pdf」而不是一串 uuid
        filename=record.file_name,
        media_type="application/octet-stream",
    )


def _resolve_inside_upload_dir(storage_path: str) -> Path | None:
    """
    解析 storage_path 并**强制**它落在 UPLOAD_DIR 内，否则返回 None。

    这是本模块唯一的「安全边界」函数，所以写得比看起来需要的更严：
      · `resolve()` 先展开 `..` 与符号链接，再判断 —— 先判断后 resolve 等于没判断
        （`upload/../../etc/passwd` 在字符串层面确实以 upload/ 开头）；
      · 用 `parents` 而不是 `startswith` 判断前缀：`/a/uploadx` 会通过字符前缀
        检查但并不是 `/a/upload` 的子路径。符号链接若指向目录外，resolve 之后
        也会被这里拦下。
    """
    try:
        path = resolve_storage_path(storage_path).resolve()
        upload_root = settings.UPLOAD_DIR.resolve()
    except OSError as e:  # 路径过长、非法字符等
        logger.warning("解析存储路径失败：%s | %s", storage_path, e)
        return None
    return path if upload_root in path.parents else None


# --------------------------------------------------------------------------- #
# 切片
# --------------------------------------------------------------------------- #
@router.get(
    "/{doc_id}/chunks",
    summary="查看文档切分片段",
    description=(
        "返回该文档在向量库中的全部片段（全文、字数、页码、切片序号），"
        "供知识库管理页排查「模型实际看到的内容」。按 chunk_index 升序。"
    ),
)
def get_document_chunks(doc_id: int) -> dict[str, Any]:
    record = repo.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：doc_id={doc_id}")

    chunks = get_vector_store_manager().get_chunks_by_doc_id(doc_id)
    return {
        "doc_id": doc_id,
        "file_name": record.file_name,
        "status": record.status,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


# --------------------------------------------------------------------------- #
# 重新解析
# --------------------------------------------------------------------------- #
@router.post(
    "/{doc_id}/reparse",
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="手动重新解析",
    description=(
        "把文档重新排队解析（用于解析失败后重试、Redis 丢过任务、或换了切分参数想重跑）。\n\n"
        "**已成功的文档不允许重解析**（会返回 409）—— 请先删除再重新上传，"
        "否则「重解析」的语义会变得含糊（是删掉旧切片重建，还是叠加？）。"
    ),
)
def reparse_document(
    doc_id: int,
    actor: Actor = Depends(require_kb_upload),
) -> dict[str, Any]:
    record = repo.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：doc_id={doc_id}")
    if record.status == repo.STATUS_SUCCESS:
        raise HTTPException(
            status_code=409,
            detail="该文档已经解析成功，不支持重解析。若内容有变，请删除后重新上传。",
        )

    # 落到磁盘前先确认文件还在。不确认也能跑（Worker 会写一条「文件不存在」的
    # fail_reason），但那样用户要等一个队列来回才知道结果 —— 而这是本地就能断言的
    # 事实，没必要占用一次任务名额。
    path = _resolve_inside_upload_dir(record.storage_path)
    if path is None or not path.is_file():
        raise HTTPException(
            status_code=409,
            detail=f"磁盘文件已丢失，无法重新解析：{record.file_name}（请删除该记录后重新上传）",
        )

    if not repo.reset_for_reparse(doc_id):
        # reset 的目标条件在两次读之间被别的请求改掉了（例如刚好解析完成）
        raise HTTPException(status_code=409, detail="文档状态刚刚发生变化，请刷新后重试")

    queued = enqueue_parse(doc_id)
    kb_service.record_reparse(actor, doc_id=doc_id, file_name=record.file_name, queued=queued)
    return {
        "doc_id": doc_id,
        "status": repo.STATUS_PENDING,
        "queued": queued,
        **({} if queued else {"detail": "已重置为待解析，但入队失败（消息队列不可用）。"}),
    }


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
@router.delete(
    "/{doc_id}",
    summary="删除知识库文档",
    description=(
        "**三件事缺一即脏数据**：磁盘原文件 → Chroma 全部该 doc_id 的切片 → MySQL 记录。\n\n"
        "顺序是刻意的：先删可再生的（切片、文件），最后删那条「指向它们的索引」。\n"
        "若先删 MySQL 再删切片，中途失败就再也没有东西能告诉我们「该清哪些切片」了 ——\n"
        "库里会永远留着一批查不到、删不掉的孤儿向量。"
    ),
)
def delete_document(
    doc_id: int,
    actor: Actor = Depends(require_kb_delete),
) -> dict[str, Any]:
    record = repo.get(doc_id)
    if record is None:
        # 幂等：删一个不存在的文档不算错误（前端可能连点两次）
        # ⚠️ 这里**不落审计** —— 什么都没删，「谁删了 doc_id=999」这条日志
        # 只有噪音没有信息。真要追「谁在乱点」，靠网关访问日志。
        return {
            "doc_id": doc_id,
            "deleted_chunks": 0,
            "file_removed": False,
            "record_removed": False,
            "detail": "该文档不存在（可能已被删除）",
        }

    purged = _purge_document(record)
    kb_service.record_delete(
        actor, doc_id=doc_id, file_name=record.file_name,
        deleted_chunks=purged["deleted_chunks"],
        file_removed=purged["file_removed"],
        record_removed=purged["record_removed"],
    )
    return {"doc_id": doc_id, **purged}


def _purge_document(record: repo.DocumentRecord) -> dict[str, Any]:
    """
    清掉一条文档的全部痕迹：Chroma 切片 → 磁盘文件 → MySQL 记录。返回清理明细。

    每一步都**独立容错**：切片删失败不该阻止删除磁盘文件（否则用户永远删不掉
    一个「向量库连不上」的文档）。但失败会记 ERROR 日志，并如实反映在返回值里，
    不假装成功。
    """
    store = get_vector_store_manager()
    deleted_chunks = 0
    try:
        deleted_chunks = store.delete_by_doc_id(record.doc_id)
    except Exception as e:  # noqa: BLE001
        logger.error("删除向量切片失败 | doc_id=%s | %s", record.doc_id, e, exc_info=True)

    file_removed = _remove_upload_file(record.storage_path)
    record_removed = repo.delete(record.doc_id)

    logger.info(
        "文档已删除 | doc_id=%s | 文件=%s | 切片=%s | 磁盘=%s | 记录=%s",
        record.doc_id, record.file_name, deleted_chunks, file_removed, record_removed,
    )
    if record_removed or deleted_chunks:
        from core.qa_cache import bump_kb_version
        bump_kb_version()
    return {
        "deleted_chunks": deleted_chunks,
        "file_removed": file_removed,
        "record_removed": record_removed,
        "file_name": record.file_name,
    }


def _remove_upload_file(storage_path: str) -> bool:
    """删除 upload 目录下的物理文件。只允许删 UPLOAD_DIR 内的文件（防路径穿越误删）。"""
    path = _resolve_inside_upload_dir(storage_path)
    if path is None:
        logger.warning("拒绝删除越权路径（忽略）：%s", storage_path)
        return False
    try:
        if path.is_file():
            path.unlink()
            logger.info("已删除上传文件：%s", path)
            return True
    except Exception as e:  # noqa: BLE001 - 物理文件清理失败不影响记录删除
        logger.warning("清理上传文件失败（忽略）：%s | %s", storage_path, e)
    return False
