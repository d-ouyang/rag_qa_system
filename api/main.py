"""
FastAPI 应用入口 —— 组装应用、中间件与路由。

启动方式：
    make api                          （等价于下面这条）
    .venv/bin/uvicorn api.main:app --reload --port 8000

启动后：
    接口文档（Swagger UI）  http://localhost:8000/docs
    接口文档（ReDoc）       http://localhost:8000/redoc
"""

import logging
import sys
from pathlib import Path

# 保证「从任意目录启动 uvicorn」都能 import 到 config/core 包：
# uvicorn api.main:app 的 cwd 不一定是项目根，先把项目根塞进 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.logging_config import setup_logging
from config.settings import settings
# main.py 与 routes/ 同属 api 包，这里用包内相对导入，避免依赖「项目根是否在 sys.path」
from .routes import admin, chunks, documents, internal, qa, system

# 初始化日志（类体在 import 时已执行，这里调用是项目既有约定，无实际副作用）
setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.PROJECT_VERSION,
    description=(
        "锅圈RAG 智能问答系统：向量检索 + CrossEncoder 重排 + "
        "多轮对话记忆 + 意图识别，对外提供 RESTful 问答接口。"
    ),
)

# CORS：白名单从 settings 读（.env 里 JSON 数组格式配置）。
# 不配置的话浏览器跨域请求（Vue 5173 → API 8000）会被直接拦截。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由：问答能力统一挂在 /api/v1/qa 前缀下
app.include_router(qa.router)
# 知识库文档管理（上传/列表/删除/统计），挂在 /api/v1/documents
app.include_router(documents.router)
# 切片引用反查（点回答里的引用 → 取完整正文与来源），挂在 /api/v1/chunks
app.include_router(chunks.router)
# 系统配置（设置页读取关键配置项），挂在 /api/v1/system
app.include_router(system.router)
# 管理端（P2-13）：人事 CRUD 与密码管理。挂在 /api/v1/admin
# 门槛在 core/identity.require_staff 里 —— 网关的路径级授权是粗筛，
# 真正的判定必须在后端（设计规格 §5.2 第 13 行）
app.include_router(admin.router)
# 内部接口（P2-11c）：网关把登录请求转到这里，由后端用 `password_policy.verify()` 判定。
# ⚠️ 三个接口都必须带 `X-Internal-Token`，且**不出现在 `/docs` 里**
#    （include_in_schema=False）—— 它能验证密码，不该是一份公开说明书。
#    排在 `/api/v1/admin` 之后：它是**最靠内**的一层（只有网关能调）。
app.include_router(internal.router)


@app.get("/", summary="服务状态", include_in_schema=False)
def root() -> dict[str, str]:
    """根路径探活：确认服务进程活着（详细状态见 /api/v1/qa/health）。"""
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.PROJECT_VERSION,
        "docs": "/docs",
    }


@app.on_event("startup")
def on_startup() -> None:
    """启动日志：打印关键配置，方便确认「起的是不是想要的那个配置」。

    ⚠️ P2-15c 起这里还做**超额行为档位**的启动校验（fail-closed）：
       `TOKEN_QUOTA_OVER_ACTION` 配成任何不是 "notify" 的值都直接拒启 ——
       理由见 `core/quota_policy.OVER_ACTIONS`（唯一合法档位是「仅提醒」，
       用户 2026-10-07 拍板）。宁可起不来，也不要
       「以为配了阻断、其实什么都没发生」。
    """
    from core import quota_policy as quota_policy  # 局部导入避免环

    # 校验在 quota_policy（判定唯一处）；非法值抛 ValueError → 进程拒启。
    quota_policy.validate_over_action(settings.TOKEN_QUOTA_OVER_ACTION)
    logger.info(
        "超额行为档位 = %s（仅提醒：超阈值只显示横幅与看板标红，不阻断）",
        settings.TOKEN_QUOTA_OVER_ACTION,
    )
    logger.info(
        "FastAPI 启动完成 | %s v%s | LLM_PROVIDER=%s 向量库=%s 重排=%s",
        settings.PROJECT_NAME,
        settings.PROJECT_VERSION,
        settings.LLM_PROVIDER,
        settings.VECTOR_STORE_TYPE,
        settings.USE_RERANKER,
    )
