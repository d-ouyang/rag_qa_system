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
from core import audit_repo
from core import kb_acl
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


class KbRoleBody(BaseModel):
    """
    下发知识库写权限的请求体。

    ⚠️ 类型是 `str` 而不是枚举，**故意的** —— 合法性由
    `kb_acl.check_kb_role` 在服务层判，它抛 `ValueError` 而 FastAPI 不会
    把它翻成 422。这样「档位名拼错」的报错文案与服务层其它地方一致
    （`kb_role 只能是 [...]，收到 'boss'`），而不是变成一条
    Pydantic 的英文校验消息。

    **代价**：OpenAPI 里看不出合法值 —— 所以 `/options` 额外下发
    `kb_roles` 字典，前端下拉的数据源来自那里（不接受自由输入）。
    """
    kb_role: str


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
        # 🔴 P2-14f：**五档知识库写权限的字典来自后端**，前端不自己维护一份。
        # 理由同 13d踩过的坑（后端写 `{from,to}`、前端读 `detail.status`，
        # 两边各自自洽，页面上整列是 `—`，而接口全绿、不报错）。
        # 顺带把每档的「能干什么」也下发（从 kb_acl.capabilities 派生）——
        # 界面上要能给管理员看「这一档到底能做什么」，而不只是一句标签。
        "kb_roles": [
            {"value": v, "label": kb_acl.KB_ROLE_LABELS[v],
             "capabilities": kb_acl.capabilities(v)}
            for v in sorted(kb_acl.KB_ROLES)
        ],
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
    """
    单个员工的详情，**手机号给原值**（列表接口才脱敏）。

    为什么这一处不脱敏：编辑弹窗要拿它当前值来显示。若这里给 `138****0001`，
    管理员不改手机号直接点保存，就会把**脱敏串当新值写回库**——
    数据被悄悄污染，而且当场没有任何报错（它是个格式合法的字符串）。
    这不是理论风险：浏览器实测第一次就撞上了（详见迭代文档 §4.1）。

    能看到原值的只有 admin / hr（`require_staff`），而他们本来就能改这个字段 ——
    脱敏在这里要防的不是「改」，只是「一眼扫过去时不该完整露出所有人手机号」。
    """
    try:
        record = svc._load_user(user_id)
    except svc.AdminError as e:
        raise _fail(e) from e
    return record.to_dict(raw_phone=True)


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


@router.patch(
    "/users/{user_id}/kb-role",
    summary="下发 / 收回知识库写权限（仅管理员）",
    description=(
        "知识库是**全公司共用**的（不做读隔离），所以这一档管的是"
        "「谁能往里加东西、谁能删东西、谁能重建整库索引」。\n\n"
        "**改完会 `token_version + 1`** → 被改的人必须重新登录才生效。"
    ),
)
def patch_kb_role(
    user_id: int, body: KbRoleBody, actor: Actor = Depends(require_admin)
) -> dict[str, Any]:
    """
    与 `PATCH /role` **完全并列**的另一个维度（D14）：`role` 管「能不能进管理端」，
    `kb_role` 管「能不能改知识库」。15 种组合全部合法。

    ⚠️ **不接受自由输入**：合法值由 `kb_acl.check_kb_role` 判，
    拼错会400 而不是静默失效 —— 静默失效是权限系统最坏的失败方式
    （管理员以为授权成功了，那个人却还是传不上去，且没有任何报错）。
    """
    try:
        record = svc.set_kb_role(actor, user_id, body.kb_role)
    except svc.AdminError as e:
        raise _fail(e) from e
    except ValueError as e:
        # check_kb_role 的非法值 —— 400（请求有问题），不是 500。
        # 路由层显式接住它，是因为「档位名拼错」是**调用方的错**，
        # 而服务层抛 ValueError 是为了不给路由层漏判的机会。
        raise HTTPException(status_code=400, detail=str(e)) from e
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
# 审计日志（13d）—— 只读
# --------------------------------------------------------------------------- #
@router.get("/audit-logs", summary="审计日志（只读、不可改不可删）")
def list_audit_logs(
    actor: str | None = Query(default=None, description="按操作人登录名模糊筛"),
    action: str | None = Query(default=None, description="按动作精确筛，如 user.password.reset"),
    target_type: str | None = Query(default=None, description="user / department / position / auth"),
    target_id: int | None = Query(default=None, description="按被操作对象 id 筛"),
    keyword: str | None = Query(default=None, description="在对象标签与 detail 里模糊搜"),
    since: datetime | None = Query(default=None, description="起始时间（含）"),
    until: datetime | None = Query(default=None, description="结束时间（含）"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    actor_in: Actor = Depends(require_staff),
) -> dict[str, Any]:
    """
    查审计。**只有 GET** —— 这个资源不存在 PUT / DELETE，因为底层表就没有那两种能力
    （`audit_repo` 里没有 update / delete，表里没有 `update_time` / `deleted_at`）。

    ⚠️ 谁能看审计 = 谁能看**所有管理员**的每一次操作，包括重置密码这件事发生过。
    这比「能改员工资料」的门槛更高一档：hr 能改资料但看不到密码相关日志。
    """
    conditions: dict[str, Any] = {
        "actor": actor, "action": action, "target_type": target_type,
        "target_id": target_id, "keyword": keyword, "since": since, "until": until,
    }
    # 逐个剔掉 None：仓储层按「条件为 None 就不过滤」解释，空串会让 LIKE '%%' 全表匹配
    conditions = {k: v for k, v in conditions.items() if v is not None}
    try:
        rows = audit_repo.list_logs(limit=limit, offset=offset, order=order, **conditions)
        total = audit_repo.count_logs(**conditions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "items": [audit_repo.describe(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        # 前端要显示「动作」下拉框，选项只能来自这一份白名单
        "actions": [{"value": k, "label": v} for k, v in audit_repo.ACTION_LABELS.items()],
        "target_types": list(audit_repo.TARGET_TYPES),
    }


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
