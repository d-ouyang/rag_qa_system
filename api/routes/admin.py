"""
管理端接口 —— `/api/v1/admin/*`（P2-13）。

--------------------------------------------------------------------------
为什么不新建一个后端进程
--------------------------------------------------------------------------
4C4G 的内存预算是硬约束（PLAN §11.1）：多一个常驻 Node/Python 进程，
就要从别的地方挤内存出来。而管理端是**低频操作台** —— 一天用几次，
为它单独养一个进程，等于用常驻开销换偶发的 CRUD。

所以它是 FastAPI 里的一组路由：跟问答链路共用同一个进程与连接池，
前端是另一份静态产物（见 `admin-console/`）。设计规格 §6 的 D7 也是这个结论。

--------------------------------------------------------------------------
这里的每个写操作都会在 P2-13d 补审计
--------------------------------------------------------------------------
本轮先不建 `audit_log` 表（那是 13d 整格的事），但写操作的**函数形状**
已经按「将来要留审计」来设计：路由不做业务逻辑，一律转给 `core.admin_service`
的一个具名动作。等 13d 落审计时，在一个地方插进去就够了，
不需要回到每个接口里去找「这个动作叫什么」。

--------------------------------------------------------------------------
错误一律是中文人话
--------------------------------------------------------------------------
`HTTPException(detail="...")` 会被前端直接显示给用户。
HR 看不懂 "Duplicate entry 'wu.jing' for key 'uk_user_username'" 这种数据库原文，
所以唯一键冲突必须在服务层翻成「登录名 wu.jing 已被使用」再往上抛。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from config.settings import settings
from core import user_repo as repo
from core.identity import Actor, require_staff

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["管理端"])


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
