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
from .routes import chunks, documents, qa, system

# 初始化日志（类体在 import 时已执行，这里调用是项目既有约定，无实际副作用）
setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.PROJECT_VERSION,
    description=(
        "企业级 RAG 智能问答系统：向量检索 + CrossEncoder 重排 + "
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
    """启动日志：打印关键配置，方便确认「起的是不是想要的那个配置」。"""
    logger.info(
        "FastAPI 启动完成 | %s v%s | LLM_PROVIDER=%s 向量库=%s 重排=%s",
        settings.PROJECT_NAME,
        settings.PROJECT_VERSION,
        settings.LLM_PROVIDER,
        settings.VECTOR_STORE_TYPE,
        settings.USE_RERANKER,
    )
