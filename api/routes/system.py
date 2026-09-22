"""
系统配置接口 —— 支撑前端「系统设置」界面，读取并展示当前问答系统的关键配置项。

--------------------------------------------------------------------------
接口一览（统一前缀 /api/v1/system）
--------------------------------------------------------------------------
    GET /settings     关键配置项（分组返回，密钥脱敏只显示是否已配置）
    GET /health       与 /api/v1/qa/health 等价的轻量状态（服务进程/版本）

设计原则：
1. 只读。配置来源是 .env + 环境变量（config/settings.py），修改配置应改
   .env 后重启服务，而不是通过 HTTP 改运行态（会与 .env 不一致）。
2. 密钥绝不外泄：只返回 *_set 布尔值，告诉前端「是否已配置」。
3. 嵌入模型信息从向量库管理器取（它是单例，与问答链路共用同一份模型），
   保证设置页展示的与真实检索用的是同一套配置。
"""

import logging
from typing import Any

from fastapi import APIRouter

from config.settings import settings
from core.vector_store import get_vector_store_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["系统配置"])


@router.get(
    "/settings",
    summary="读取系统关键配置",
    description="分组返回问答系统关键配置项（LLM/检索/切分/记忆/向量库/意图识别），密钥脱敏。",
)
def get_system_settings() -> dict[str, Any]:
    # 按 provider 取当前生效的 LLM 信息（三个 provider 的字段名不同，统一归一化）
    llm_model = {
        "openai": settings.OPENAI_MODEL_NAME,
        "siliconflow": settings.SILICONFLOW_MODEL_NAME,
        "ollama": settings.OLLAMA_MODEL_NAME,
    }.get(settings.LLM_PROVIDER, "")
    llm_base_url = {
        "openai": settings.OPENAI_BASE_URL,
        "siliconflow": settings.SILICONFLOW_BASE_URL,
        "ollama": settings.OLLAMA_BASE_URL,
    }.get(settings.LLM_PROVIDER, "")
    api_key_set = {
        "openai": bool(settings.OPENAI_API_KEY),
        "siliconflow": bool(settings.SILICONFLOW_API_KEY),
        "ollama": True,  # ollama 本地服务无需密钥
    }.get(settings.LLM_PROVIDER, False)

    # 嵌入模型的真实运行信息（从单例取，与检索链路一致；包含模型名/设备/维度）
    try:
        embedding_info: dict[str, Any] = get_vector_store_manager().get_stats()
        embedding = {
            "model": embedding_info.get("embedding_model"),
            "device": embedding_info.get("embedding_device"),
            "dimension": embedding_info.get("embedding_dimension"),
            "loaded_from": embedding_info.get("embedding_loaded_from"),
        }
        vector_store = {
            "type": embedding_info.get("vector_store_type"),
            "persist_directory": embedding_info.get("persist_directory"),
            "collection_name": embedding_info.get("collection_name"),
            "total_vectors": embedding_info.get("total_vectors"),
            "source_count": embedding_info.get("source_count"),
        }
    except Exception as e:
        # 向量库初始化失败不应让设置页整个 502：降级展示 settings 静态值
        logger.warning("读取向量库运行信息失败，设置页降级为静态配置：%s", e)
        embedding = {"model": settings.EMBEDDING_MODEL_NAME, "device": settings.EMBEDDING_DEVICE}
        vector_store = {
            "type": settings.VECTOR_STORE_TYPE,
            "persist_directory": str(settings.VECTOR_DB_DIR),
        }

    return {
        "project": {
            "name": settings.PROJECT_NAME,
            "version": settings.PROJECT_VERSION,
            "api_host": settings.API_HOST,
            "api_port": settings.API_PORT,
            "log_level": settings.LOG_LEVEL,
        },
        "llm": {
            "provider": settings.LLM_PROVIDER,
            "model": llm_model,
            "base_url": llm_base_url,
            "api_key_set": api_key_set,
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "timeout_seconds": settings.LLM_TIMEOUT,
            "max_retries": settings.LLM_MAX_RETRIES,
            "reasoning_enabled": settings.OLLAMA_REASONING if settings.LLM_PROVIDER == "ollama" else None,
            "thinking_enabled": (
                settings.SILICONFLOW_ENABLE_THINKING if settings.LLM_PROVIDER == "siliconflow" else None
            ),
        },
        "retrieval": {
            "search_top_k": settings.SEARCH_TOP_K,
            "use_reranker": settings.USE_RERANKER,
            "reranker_model": settings.RERANKER_MODEL_NAME if settings.USE_RERANKER else None,
            "rerank_candidate_multiplier": settings.RERANK_CANDIDATE_MULTIPLIER,
            "rerank_score_threshold": settings.RERANK_SCORE_THRESHOLD,
        },
        "chunking": {
            "chunk_size": settings.CHUNK_SIZE,
            "chunk_overlap": settings.CHUNK_OVERLAP,
        },
        "memory": {
            "max_turns": settings.MEMORY_MAX_TURNS,
            "session_ttl_seconds": settings.MEMORY_SESSION_TTL_SECONDS,
        },
        "intent": {
            "provider": settings.INTENT_LLM_PROVIDER,
            "model": settings.INTENT_LLM_MODEL_NAME or settings.OLLAMA_MODEL_NAME,
            "timeout_seconds": settings.INTENT_LLM_TIMEOUT,
        },
        "embedding": embedding,
        "vector_store": vector_store,
    }


@router.get(
    "/health",
    summary="服务进程状态",
    description="轻量探活：只确认服务活着与版本号（链路级检查见 /api/v1/qa/health）。",
)
def system_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "name": settings.PROJECT_NAME,
        "version": settings.PROJECT_VERSION,
    }
