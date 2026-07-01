"""
配置管理模块
集中管理所有配置项，支持命令行参数覆盖
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class APIConfig:
    """API 相关配置"""
    jd_api_url: str = "https://api.m.jd.com/api"
    default_appid: str = "paimai"


@dataclass
class CrawlConfig:
    """爬取相关配置"""
    default_throttle: float = 0.35
    default_timeout: int = 25
    max_retries: int = 3
    default_per_category_limit: int = 2


@dataclass
class LogConfig:
    """日志相关配置"""
    log_level: str = "INFO"
    log_file: Path | None = None
    console_output: bool = True


@dataclass
class AIConfig:
    """AI 提取相关配置（阿里云百炼）"""
    model: str = "qwen"                              # ← 改为 "qwen"，触发 QwenClient
    vision_model: str = "qwen-vl-plus"               # 图片表格/OCR 兜底模型
    api_key: str = field(default_factory=lambda: os.getenv("JD_AI_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("JD_AI_BASE_URL", "https://dashscope.aliyuncs.com"))
    timeout: int = 30
    max_retries: int = 1
    qps: int = 10



@dataclass
class DatabaseConfig:
    """数据库相关配置"""
    default_db_name: str = "jd_auction.sqlite"


@dataclass
class Config:
    """全局配置"""
    api: APIConfig = field(default_factory=APIConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    log: LogConfig = field(default_factory=LogConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)

    def update_from_dict(self, config_dict: dict[str, Any]) -> None:
        """从字典更新配置（用于命令行参数覆盖）"""
        for section_name, section_values in config_dict.items():
            if hasattr(self, section_name):
                section = getattr(self, section_name)
                for key, value in section_values.items():
                    if hasattr(section, key):
                        setattr(section, key, value)


# 全局单例配置
_global_config: Config | None = None


def get_config() -> Config:
    """获取全局配置单例"""
    global _global_config
    if _global_config is None:
        _global_config = Config()
    return _global_config


def set_config(config: Config) -> None:
    """设置全局配置"""
    global _global_config
    _global_config = config
