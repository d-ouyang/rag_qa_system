"""
模块1测试文件：验证配置管理和日志系统是否能正常工作
"""
# 想要导入自定义包或者模块，建议将项目根目录加入系统路径
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


from config.settings import settings
from config.logging_config import setup_logging


# 初始化日志
setup_logging()

# 测试配置
def test_settings():
    print("====测试配置管理====")
    print(f"项目名称: {settings.PROJECT_NAME}")
    print(f"项目版本: {settings.PROJECT_VERSION}")
    print(f"数据目录: {settings.DATA_DIR}")
    print(f"向量库目录: {settings.VECTOR_DB_DIR}")
    print(f"LLM供应商: {settings.LLM_PROVIDER}")
    print("配置成功！！！\n")

# 测试日志的配置
def test_logging():
    print("====测试日志系统====")
    import logging
    logger = logging.getLogger(__name__)
    logger.info("这是一个测试日志信息")
    logger.warning("这是一个测试警告信息")
    logger.error("这是一个测试错误信息")
    print("日志系统测试完成！！！\n")

if __name__ == "__main__":
    test_settings()
    test_logging()
