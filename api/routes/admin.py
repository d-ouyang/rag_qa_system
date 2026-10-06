"""
管理端接口 —— `/api/v1/admin/*`（P2-13）。

--------------------------------------------------------------------------
为什么不新建一个后端进程
--------------------------------------------------------------------------
4C4G 的内存预算是硬约束（PLAN §11.1）：多一个常驻 Node/Python 进程，
就要从别的地方挤内存。而管理端是**低频操作台** —— 一天用几次，
为它单独养一个进程，等于用常驻开销换偶发的 CRUD。

所以它是 FastAPI 里的一组路由：跟问答链路共用同一个进程与连接池，
前端是另一份静态产物（见 `admin-console/`）。设计规格 §6 的 D7 也是这个结论。

--------------------------------------------------------------------------
路由层只做三件事
--------------------------------------------------------------------------
    1. 权限（依赖 `require_staff` / `require_admin`）；
    2. 参数与响应形状（pydantic 模型 / 显式 dict）；
    3. 把 `AdminError` 翻成 HTTP 状态码。

业务逻辑一律在 `core/admin_service.py`。这里出现 `if` 判断业务规则
就是放错地方了 —— 那条规则会只有这一个入口遵守。

--------------------------------------------------------------------------
错误一律是中文人话
--------------------------------------------------------------------------
`AdminError.message` 直接显示给用户。HR 看不懂
"Duplicate entry 'wu.jing' for key 'uk_user_username'" 这种数据库原文，
所以唯一键冲突在服务层就翻成「登录名 wu.jing 已被使用」。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from config.settings import settings
from core import admin_service as svc
from core import user_repo as repo
from core.identity import Actor, require_admin, require_staff

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["管理端"])

_EMPTY_BODY_OK = {status.HTTP_200_OK}


def _fail(error: svc.AdminError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.message)


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class CreateUserBody(BaseModel):
    username: str = Field(..., description="登录名")
    employee_no: str = Field(..., description="工号")
    display_name: str = ""
    email: str | None = None
    phone: str | None = None
    gender: str | None = None
    department_id: int | None = None
    position_id: int | None = None
    role: str = repo.ROLE_USER


class UpdateUserBody(BaseModel):
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    gender: str | None = None
    department_id: int | None = None
    position_id: int | None = None


class StatusBody(BaseModel):
    status: str


class RoleBody(BaseModel):
    role: str


class MustChangeBody(BaseModel):
    must_change: bool


class DepartmentBody(BaseModel):
    code: str
    name: str
    parent_id: int | None = None
    leader_user_id: int | None = None
    sort_order: int = 0


class DepartmentPatchBody(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    leader_user_id: int | None = None
    sort_order: int | None = None


class PositionBody(BaseModel):
    code: str
    name: str
    level: str | None = None
    sequence: str = repo.SEQUENCE_TECH


class PositionPatchBody(BaseModel):
    code: str | None = None
    name: str | None = None
    level: str | None = None
    sequence: str | None = None


def _dropped(model: BaseModel) -> dict[str, Any]:
    """
    取「显式传了」的字段 —— `exclude_unset=True`。

    为什么不用「空值就跳过」：「把邮箱清空」与「不改邮箱」是两件不同的事。
    pydantic 的 unset 正好表达这个区别，而 `None` 就表示真的清空。
    """
    return model.model_dump(exclude_unset=True)


# --------------------------------------------------------------------------- #
# 当前操作者
# --------------------------------------------------------------------------- #
@router.get("/me", summary="当前操作者身份与权限")
def read_me(actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    """
    管理端刷新页面时的第一个请求。

    它一次回答两件事：你是谁（昵称 / 角色 / 身份来源），以及你能做什么
    （`permissions`）。前端据此决定哪些入口看得见 —— 但**权限的真正防线在这里**，
    前端只是别让 HR 看见点了会 403 的按钮。
    """
    return actor.to_dict()


@router.get("/options", summary="下拉字典：部门树 / 职位 / 枚举 / 密码策略")
def read_options(actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    """
    表单需要的字典，一次性取回，省得每个下拉各自发一次请求。

    部门**同时**给扁平列表和树：树用于展示，扁平列表用于「改上级」时
    在前端排除自己的子孙（防环的第一道拦截在前端，后端还会再兜一次 ——
    只放前端，等于相信所有请求都来自自家页面）。

    密码策略参数也在这里下发：前端表单要在提交前就说清「至少 10 位」，
    而不是等后端把同一个数字用一句英文报错扔回来。
    """
    departments = repo.list_departments()
    positions = repo.list_positions()
    return {
        "departments": [d.to_dict() for d in departments],
        "department_tree": repo.build_department_tree(departments),
        "positions": [p.to_dict() for p in positions],
        "roles": sorted(repo.ROLES),
        "statuses": sorted(repo.STATUSES),
        "sequences": sorted(repo.SEQUENCES),
        "password_policy": {
            "min_length": settings.PASSWORD_MIN_LENGTH,
            "expire_days": settings.PASSWORD_EXPIRE_DAYS,
            "warn_days": settings.PASSWORD_EXPIRE_WARN_DAYS,
            "history_keep": settings.PASSWORD_HISTORY_KEEP,
        },
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# 员工（13b）
# --------------------------------------------------------------------------- #
@router.get("/users", summary="员工列表")
def list_users(
    actor: Actor = Depends(require_staff),
    keyword: str | None = Query(default=None, description="姓名 / 登录名 / 工号 / 邮箱"),
    department_id: int | None = None,
    role: str | None = None,
    status: str | None = None,
    include_resigned: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """
    员工列表。默认**不含离职** —— 人事找人时，已离职的人不该混在里面。

    ⚠️ `status` 与 `include_resigned` 的关系：显式传 `status` 时以后者为准
    （要看离职的人就传 `status=resigned`），两个参数同时给是调用方自己的选择。
    """
    rows = repo.list_users(
        department_id=department_id,
        role=role,
        status=status,
        keyword=keyword,
        include_resigned=include_resigned or status == repo.STATUS_RESIGNED,
        limit=limit,
    )
    return {"items": [u.to_dict() for u in rows], "total": len(rows)}


@router.post("/users", summary="新建员工", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserBody, actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    """
    新建员工，**并返回一次性临时密码**。

    ⚠️ 这个明文密码**只出现这一次**：不落日志、不再有第二次查看的接口。
    丢了就只能再重置一次 —— 这是单向哈希的代价，不是设计缺陷。
    """
    try:
        created = svc.create_user(actor, **_dropped(body))
    except svc.AdminError as e:
        raise _fail(e) from e
    return {
        "user": created.record.to_dict(),
        # 只在这里出现一次；前端要把它做成「复制后即关闭」的一次性弹窗
        "temporary_password": created.temporary_password,
    }


@router.get("/users/{user_id}", summary="员工详情")
def read_user(user_id: int, actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    try:
        record = svc._load_user(user_id)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.patch("/users/{user_id}", summary="修改员工资料")
def patch_user(
    user_id: int, body: UpdateUserBody, actor: Actor = Depends(require_staff)
) -> dict[str, Any]:
    """只改资料（姓名 / 邮箱 / 手机 / 性别 / 部门 / 职位）。角色、状态、密码各有自己的入口。"""
    fields = _dropped(body)
    if not fields:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    try:
        record = svc.update_profile(actor, user_id, **fields)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.patch("/users/{user_id}/status", summary="停用 / 复职 / 离职")
def patch_status(
    user_id: int, body: StatusBody, actor: Actor = Depends(require_staff)
) -> dict[str, Any]:
    """
    离职走 `status=resigned`，**不删行** —— 他名下还有会话与文档，
    删了就都变成悬空引用。同时 `token_version + 1`，他手上的 token 立刻失效。
    """
    try:
        record = svc.set_status(actor, user_id, body.status)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.patch("/users/{user_id}/role", summary="改系统角色（仅管理员）")
def patch_role(user_id: int, body: RoleBody, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    try:
        record = svc.set_role(actor, user_id, body.role)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


# --------------------------------------------------------------------------- #
# 密码（13c）
# --------------------------------------------------------------------------- #
@router.post("/users/{user_id}/password/reset", summary="重置为一次性临时密码")
def reset_password(user_id: int, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    """
    重置为**随机**临时密码 + 强制首次登录改密。

    接口里刻意**没有**「设置一个我指定的密码」：管理员不该知道员工的密码。
    一旦存在这个参数，它就会被用来「统一改成同一个口令方便调试」。
    """
    try:
        record, temporary = svc.reset_password(actor, user_id)
    except svc.AdminError as e:
        raise _fail(e) from e
    return {"user": record.to_dict(), "temporary_password": temporary}


@router.patch("/users/{user_id}/password/must-change", summary="开关「下次登录必须改密」")
def patch_must_change(
    user_id: int, body: MustChangeBody, actor: Actor = Depends(require_admin)
) -> dict[str, Any]:
    """ⓘ 这个开关**不会**给密码续期（不动 90 天有效期，见 `admin_service.set_must_change`）。"""
    try:
        record = svc.set_must_change(actor, user_id, body.must_change)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.get("/password/board", summary="密码到期看板")
def password_board(actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    """
    五个数：**待首次改密 / 即将到期 / 已过期 / 长期未登录 / 已锁定**。

    五个数**互不包含**（一个人只落在一个桶里），否则加总对不上在职人数 ——
    这种数字没人敢拿去汇报。
    """
    try:
        board = svc.password_board(actor)
    except svc.AdminError as e:
        raise _fail(e) from e
    return board.to_dict()


# --------------------------------------------------------------------------- #
# 部门（13b）
# --------------------------------------------------------------------------- #
@router.post("/departments", summary="新建部门", status_code=status.HTTP_201_CREATED)
def create_department(body: DepartmentBody, actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    try:
        record = svc.create_department(actor, **_dropped(body))
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.patch("/departments/{dept_id}", summary="改部门（名称 / 上级 / 负责人 / 排序）")
def patch_department(
    dept_id: int, body: DepartmentPatchBody, actor: Actor = Depends(require_staff)
) -> dict[str, Any]:
    fields = _dropped(body)
    if not fields:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    try:
        record = svc.update_department(actor, dept_id, **fields)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.delete("/departments/{dept_id}", summary="删除部门（无子部门且无成员才可删）")
def delete_department(dept_id: int, actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    try:
        svc.delete_department(actor, dept_id)
    except svc.AdminError as e:
        raise _fail(e) from e
    return {"ok": True, "id": dept_id}


# --------------------------------------------------------------------------- #
# 职位（13b）
# --------------------------------------------------------------------------- #
@router.post("/positions", summary="新建职位", status_code=status.HTTP_201_CREATED)
def create_position(body: PositionBody, actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    try:
        record = svc.create_position(actor, **_dropped(body))
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.patch("/positions/{pos_id}", summary="改职位")
def patch_position(
    pos_id: int, body: PositionPatchBody, actor: Actor = Depends(require_staff)
) -> dict[str, Any]:
    fields = _dropped(body)
    if not fields:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    try:
        record = svc.update_position(actor, pos_id, **fields)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict()


@router.delete("/positions/{pos_id}", summary="删除职位（无人挂着才可删）")
def delete_position(pos_id: int, actor: Actor = Depends(require_staff)) -> dict[str, Any]:
    try:
        svc.delete_position(actor, pos_id)
    except svc.AdminError as e:
        raise _fail(e) from e
    return {"ok": True, "id": pos_id}
