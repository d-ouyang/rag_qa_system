from logging.handlers import RotatingFileHandler
from logging import Logger, StreamHandler
from typing import Any, TextIO
import sys
import logging
from config.settings import settings

class setup_logging():
    log_format: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    
    # 根日之器
    root_logger: Logger = logging.getLogger()
    root_logger.setLevel(level=settings.LOG_LEVEL)
    
    # 控制台处理器
    console_handler: StreamHandler[TextIO | Any] = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level=settings.LOG_LEVEL)
    console_handler.setFormatter(fmt=logging.Formatter(log_format, date_format))
    root_logger.addHandler(hdlr=console_handler)
    
    # 文件处理器
    # 设置日志文件的大小，当达到大小的时候，会自动轮转
    file_handler: RotatingFileHandler = RotatingFileHandler(
        filename=settings.LOG_FILE, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,  # 保留5个备份
        encoding="utf-8"
        )
    file_handler.setLevel(level=logging.DEBUG)
    file_handler.setFormatter(fmt=logging.Formatter(log_format, datefmt=date_format))
    root_logger.addHandler(hdlr=file_handler)

    # 减少第三方库的日志噪声
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # urllib3 日志
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx 日志
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)  # huggingface_hub 日志  
    logging.getLogger("transformers").setLevel(logging.WARNING)  # transformers 日志
    logging.getLogger("torch").setLevel(logging.WARNING)  # torch 日志
    logging.getLogger("chromadb").setLevel(logging.WARNING)  # chromadb 日志

    # 测试日志
    root_logger.info(f"日志系统初始化完成, 级别是 {settings.LOG_LEVEL}")