"""
知识库写操作的服务层 —— **写操作的唯一收口处**（P2-14c）。

--------------------------------------------------------------------------
为什么路由不能自己写这些
--------------------------------------------------------------------------
项目里已有一条规矩（13b 立下的）：**路由只收参，写操作全部收口在服务层**。
理由不是洁癖：

  · 审计只需在这一层加一行 —— 散在四个路由里，漏一个路由就少一类日志，
    而审计漏记**不会报错**；
  · 「谁动了这个知识库」这个问题，答案必须落在同一个地方。
    四个路由各写各的，将来「查一下知识库的变更历史」就没法查了。

`api/routes/documents.py` 里的 `_purge_document()` 与 `_accept_one_upload()`
是 P0-3 时代就有的，本轮**没有搬进来** —— 搬动会让这个 diff 从「加权限」
变成「搬代码 + 加权限」，而后者更难审、也更容易在review 里被漏掉。
真正的收口是：**权限判定与审计**这两件事只在这里发生。

--------------------------------------------------------------------------
权限判定的位置（这是本模块唯一需要解释的设计）
--------------------------------------------------------------------------
**不在**这里判权限，而是在**路由的 `Depends`** 里判（`require_kb_*`）。

看起来反直觉 —— 「服务层才是判权限的地方」是常见说法 —— 但这里选相反，
理由是本项目特有的：

  · 14e 的**结构断言**要靠「这条路由的 `Depends` 列表里有没有 `require_kb_*`」
    来判断「守卫是否挂上了」。判据就是这张列表；一旦把判定挪进函数体，
    列表就是空的，断言会失去唯一可靠的机械信号，而这次要防的失效
    恰恰是「漏一条路由且不报错」；
  · 12b 的信任边界（网关证明 + 身份注入）也全在 `Depends` 上，
    权限判定放在同一层，两道防线在同一个位置被检查。

所以本模块接到的 `actor` **已经过了权限判定**。它在这里做的是
「把这次操作写进审计」—— 这才是它存在的理由。
"""
from __future__ import annotations

import logging
from typing import Any

from core import audit_repo
from core.identity import Actor

logger = logging.getLogger(__name__)

# 审计动作名（白名单在 `core/audit_repo.py` 的 ACTION_LABELS）
ACTION_DOCUMENT_UPLOAD = "document.upload"
ACTION_DOCUMENT_DELETE = "document.delete"
ACTION_DOCUMENT_REPARSE = "document.reparse"


def _audit(
    actor: Actor | None,
    action: str,
    *,
    target_id: int | None,
    target_label: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """
    落一条知识库写操作的审计。

    ⚠️ **审计失败不阻断业务**，与 `core/admin_service.py::_audit` 同一取舍：
    审计表与业务表在同一个 MySQL，审计写不进的情形（磁盘满、表锁死）
    几乎同时意味着业务也写不进去；把操作一起拒掉只是让症状换个形状，
    而已经写成功的那部分业务数据并不会回滚（不同事务）。

    `actor=None` 的场合只有一种：内部脚本直接调服务层函数（不经 HTTP）。
    这时 `actor_username` 传 `"(内部脚本)"` 而不是空 —— `audit_repo` 明确
    拒绝空操作人（「没有操作人的日志等于没有日志」），而「脚本」是一个
    如实的答案，不是占位符。
    """
    try:
        audit_repo.record(
            actor_user_id=actor.id if actor else None,
            actor_username=actor.username if actor else "(内部脚本)",
            actor_role=actor.role if actor else None,
            action=action,
            target_type="document",
            target_id=target_id,
            target_label=target_label,
            detail=detail,
            ip=actor.client_ip if actor else None,
        )
    except Exception:  # noqa: BLE001 - 审计不能反过来打断业务，见上面说明
        logger.error(
            "知识库写操作的审计写入失败（业务已完成，但这条动作没有留痕）| "
            "action=%s | target_id=%s | actor=%s",
            action, target_id, actor.username if actor else "(内部脚本)",
            exc_info=True,
        )


def record_upload(actor: Actor | None, *, doc_id: int, file_name: str,
                  file_size: int, project_id: str) -> None:
    """
    记录一次上传。

    `detail` 里只记**元数据**，不记文件内容也不记磁盘路径：
    磁盘路径是 uuid 拼出来的，记它对排查没有额外价值（`document` 表里有），
    而它会随目录结构变化而失效。相比之下 `file_size` 与 `project_id` 是
    「这次上传做了什么」的必要部分。
    """
    _audit(
        actor, ACTION_DOCUMENT_UPLOAD,
        target_id=doc_id,
        target_label=file_name,
        detail={"file_size": int(file_size), "project_id": project_id},
    )


def record_delete(actor: Actor | None, *, doc_id: int, file_name: str,
                  deleted_chunks: int, file_removed: bool, record_removed: bool,
                  reason: str = "主动删除") -> None:
    """
    记录一次删除。**三个清理动作的结果全记**，而不只记「成功」。

    为什么连失败的那几项也要记：删除操作是「三件事缺一即脏数据」
    （切片 / 磁盘文件 / MySQL 记录，见 `_purge_document`）。只记成功的话，
    「文档在列表里不见了但引用点开是空的」这种脏状态没有任何痕迹；
    记下 `deleted_chunks=0` 就是线索。

    `reason` 区分两种删除：用户点删除 vs **上传同名文件时被顶掉**
    （见 `_accept_one_upload`）。后者更值得追—— 它删掉的是一份
    已经入库、可能正被历史回答引用的文档，而且**用户没点删除**。
    两种混在一起看日志时，「我明明没删它怎么没了」就答不上来。
    """
    _audit(
        actor, ACTION_DOCUMENT_DELETE,
        target_id=doc_id,
        target_label=file_name,
        detail={
            "deleted_chunks": int(deleted_chunks),
            "file_removed": bool(file_removed),
            "record_removed": bool(record_removed),
            "reason": reason,
        },
    )


def record_reparse(actor: Actor | None, *, doc_id: int, file_name: str, queued: bool) -> None:
    """记录一次重新解析。`queued=False` 也要记 —— 那意味着「要手动补投」。"""
    _audit(
        actor, ACTION_DOCUMENT_REPARSE,
        target_id=doc_id,
        target_label=file_name,
        detail={"queued": bool(queued)},
    )


__all__ = [
    "ACTION_DOCUMENT_DELETE",
    "ACTION_DOCUMENT_REPARSE",
    "ACTION_DOCUMENT_UPLOAD",
    "record_delete",
    "record_reparse",
    "record_upload",
]