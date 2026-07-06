from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class APIConfig:
    jd_api_url: str = "https://api.m.jd.com/api"
    default_appid: str = "paimai"


@dataclass
class CrawlConfig:
    default_throttle: float = 0.35
    default_timeout: int = 25
    max_retries: int = 3
    default_per_category_limit: int = 2


@dataclass
class LogConfig:
    log_level: str = "INFO"
    log_file: Path | None = None
    console_output: bool = True


@dataclass
class AIConfig:
    # `model` is kept as a legacy provider selector: qwen/deepseek/openai.
    active_profile: str = ""
    model: str = ""
    model_name: str = ""
    vision_model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout: int = 0
    max_retries: int = 0
    qps: int = 10
    enable_single_field_fallback: bool = False
    enable_vision_ai: bool = False
    max_batches_per_item: int = 1
    circuit_breaker_failures: int = 0
    circuit_breaker_cooldown_seconds: int = 0


@dataclass
class DatabaseConfig:
    default_db_name: str = "jd_auction.sqlite"


@dataclass
class Config:
    api: APIConfig = field(default_factory=APIConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    log: LogConfig = field(default_factory=LogConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)

    def update_from_dict(self, config_dict: dict[str, Any]) -> None:
        for section_name, section_values in config_dict.items():
            if not hasattr(self, section_name):
                continue
            section = getattr(self, section_name)
            for key, value in section_values.items():
                if hasattr(section, key):
                    setattr(section, key, value)


_global_config: Config | None = None


def get_config() -> Config:
    global _global_config
    if _global_config is None:
        _global_config = Config()
    return _global_config


def set_config(config: Config) -> None:
    global _global_config
    _global_config = config
