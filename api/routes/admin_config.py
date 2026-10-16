"""
管理端运行时配置接口（v4.0.0 P4-2，PLAN-v4.0.0.md）。

--------------------------------------------------------------------------
路由
--------------------------------------------------------------------------
    GET   /api/v1/admin/config            注册表全量 + 当前生效值（superadmin）
    PATCH /api/v1/admin/config/{key}      改一项（superadmin；写审计，即时生效）

--------------------------------------------------------------------------
门禁
--------------------------------------------------------------------------
require_superadmin（``kb_role=superadmin``，判据出处 core/identity.py，
取值清单与 kb_acl 同源）。与 PLAN-v4.0.0 §1 Q5 的裁决一致：
v1 **全部**配置变更超管专属；role=admin 但 kb_role≠superadmin 的人
只能看只读设置页。

GET 也挂同一个门禁而不是「登录即可看」：注册表里是运营参数的真实值
（阈值/开关/轮数），收敛在超管视野内没有坏处，还省掉一层
「GET 谁能看、PATCH 谁能改」的差异化门禁逻辑——前端拿 403 当
「只读模式」的信号即可。

--------------------------------------------------------------------------
「能改什么」是白名单，不是敞口
--------------------------------------------------------------------------
未在 core/config_flags.FLAG_REGISTRY 登记的 key 一律 404。
密钥/连接串这类敏感项**结构性**进不来（根本不在注册表里），
不靠「写代码时记得别返回」。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from core import audit_repo
from core.config_flags import FLAG_REGISTRY, describe_flags, validate_flag_value
from core.config_service import ConfigError, config_service
from core.identity import Actor, require_superadmin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/config", tags=["管理端-配置"])


class FlagValueBody(BaseModel):
    """PATCH 请求体。值必须是标量；类型/边界校验在 config_flags.validate_flag_value。"""

    value: Any

    @field_validator("value")
    @classmethod
    def _reject_container(cls, v: Any) -> Any:
        # None / list / dict 在 config_service._type_of 里也会被拒；这里提前拦
        # 只是让 422 的报错信息更早、更直白（省得用户以为「再传一次就行」）。
        if v is None or isinstance(v, (list, dict)):
            raise ValueError("配置值只能是 bool / int / float 标量")
        return v


@router.get("", summary="运行时配置清单（注册表 + 当前生效值）")
def get_runtime_config(actor: Actor = Depends(require_superadmin)) -> dict[str, Any]:
    """设置页「运行时开关」卡片的数据源。只含注册表标量项，不含任何密钥。"""
    return {"can_edit": True, "flags": describe_flags()}


@router.patch("/{key}", summary="修改一个运行时配置项（即时生效，写审计）")
def patch_runtime_config(
    key: str,
    body: FlagValueBody,
    actor: Actor = Depends(require_superadmin),
) -> dict[str, Any]:
    """
    改一项配置：类型/边界校验 → ConfigService 落库（进程内缓存即时更新）→ 审计。

    即时生效的语义：**下一个请求**就用新值；进行中的请求用旧值跑完，
    什么都不用重启、不用清缓存。
    """
    spec = FLAG_REGISTRY.get(key)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"未登记的配置项：{key}"
        )
    try:
        value = validate_flag_value(key, body.value)
    except ConfigError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    old_value = config_service.get_flag(key, spec.default())
    try:
        config_service.set_flag(key, value, actor=actor.username)
    except ConfigError as e:
        # 类型冲突（库里已有别的类型）→ 422。
        # DB 故障**不在这里兜**：config_service 写路径 fail-closed，异常原样上抛成 500
        # —— 管理端必须看见「没保存成功」，而不是以为改成了。
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    audit_repo.record(
        actor_user_id=actor.id,
        actor_username=actor.username,
        actor_role=actor.role,
        action="config.update",
        target_type="system",
        target_label=key,
        detail={"key": key, "old": old_value, "new": value},
    )
    logger.info(
        "运行时配置已变更 | key=%s %r -> %r | actor=%s",
        key, old_value, value, actor.username,
    )
    return {"key": key, "value": value, "value_type": spec.value_type}
