from pathlib import Path
from typing import ClassVar, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # 项目信息
    PROJECT_NAME : str = "rag-qa-system"
    PROJECT_VERSION : str = "1.0.0"
    BASE_DIR : Path = Path(__file__).parent.parent.resolve()

    # 数据目录
    DATA_DIR : Path = BASE_DIR / "data"
    VECTOR_DB_DIR : Path = BASE_DIR / "vector_db"
    MODELS_DIR : Path = BASE_DIR / "models"
    UPLOAD_DIR : Path = BASE_DIR / "upload"

    # 向量数据库的类型
    # 向量库配置
    VECTOR_STORE_TYPE : Literal["faiss", "chroma"] = "chroma"
    EMBEDDING_MODEL_NAME : str = "bge_small_zh"
    EMBEDDING_DEVICE : str = "cpu"      # 可以配置cuda
    CHUNK_SIZE : int = 500
    CHUNK_OVERLAP : int = 50

    # 检索配置
    SEARCH_TOP_K : int = 5
    USE_RERANKER : bool = True
    RERANKER_MODEL_NAME : str = "bge_reranker_base"

     # LLM配置
    LLM_PROVIDER : Literal["openai", "siliconflow", "ollama"] = "siliconflow"

    # ollama配置
    OLLAMA_BASE_URL : str = "http://localhost:11434"
    OLLAMA_MODEL_NAME : str = "qwen3.5:9b"

    # OpenAI配置
    OPENAI_API_KEY : str = ""
    OPENAI_BASE_URL : str = "https://api.openai.com/v1"
    OPENAI_MODEL_NAME : str = "gpt-3.5-turbo"

    # 硅基流动（OpenAI 兼容）
    SILICONFLOW_API_KEY : str = ""
    SILICONFLOW_BASE_URL : str = "https://api.siliconflow.cn/v1"
    SILICONFLOW_MODEL_NAME : str = "Qwen/Qwen3-8B"

    # 服务配置
    API_HOST : str = "localhost"
    API_PORT : int = 8000
    STREAMLIT_PORT : int = 8501

    # 日志配置
    LOG_LEVEL : str = "INFO"
    LOG_FILE : Path = BASE_DIR / "app.log"

    # 安全配置
    ALLOWED_ORIGINS : list[str] = ["http://localhost:5173","http://localhost:8501","http://localhost:3000"]
    SECRET_KEY : str = ""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env", env_file_encoding="utf-8", extra="ignore",
    )

# 实例化全局配置
settings = Settings()   

# 确保必要目录存在
for dir_path in [settings.DATA_DIR, settings.VECTOR_DB_DIR, settings.MODELS_DIR, settings.UPLOAD_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)