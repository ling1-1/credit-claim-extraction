"""
京东资产拍卖采集器 - 核心工具包

功能模块:
- config: 集中配置管理
- exceptions: 自定义异常体系
- logger: 结构化日志模块
- field_standardizer: 字段标准化引擎
- conflict_detector: 多来源冲突检测器
- ai_extractor: AI 辅助字段提取引擎
"""
__version__ = "2.0.0"

from .config import Config, get_config, set_config
from .exceptions import (
    CrawlError,
    DatabaseError,
    ExtractionError,
    JDAPIError,
    JDScraperError,
)
from .logger import StructuredLogger, get_logger, set_logger

__all__ = [
    # Config
    "Config",
    "get_config",
    "set_config",
    # Exceptions
    "JDScraperError",
    "JDAPIError",
    "CrawlError",
    "ExtractionError",
    "DatabaseError",
    # Logger
    "StructuredLogger",
    "get_logger",
    "set_logger",
]
