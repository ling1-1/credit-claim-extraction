"""Web admin configuration."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    # MySQL connection. Keep the default for local development only.
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_database: str = "auction_data"
    mysql_use_reader: bool = False
    mysql_reader_user: str = "web_reader"
    mysql_reader_password: str = ""

    # Project root used when launching crawler subprocesses.
    project_root: str = ""

    # Background subprocess timeout in seconds.
    task_timeout: int = 600

    # AI queue auto-processing defaults to manual mode.
    ai_queue_auto_enabled: bool = False
    ai_queue_auto_concurrency: int = 3
    ai_queue_auto_interval: int = 20
    ai_queue_auto_limit: int = 50
    ai_queue_auto_profile: str = ""

    # Optional admin authentication. Keep disabled for current localhost-only use.
    auth_enabled: bool = False
    admin_username: str = "admin"
    admin_password: str = ""
    auth_session_ttl_seconds: int = 86400
    auth_cookie_secure: bool = False

    def __post_init__(self) -> None:
        if not self.project_root:
            self.project_root = str(Path(__file__).resolve().parent.parent)
        env_file_values = _load_env_file(Path(self.project_root) / ".env")

        int_keys = {
            "port",
            "mysql_port",
            "task_timeout",
            "ai_queue_auto_concurrency",
            "ai_queue_auto_interval",
            "ai_queue_auto_limit",
            "auth_session_ttl_seconds",
        }
        bool_keys = {"debug", "mysql_use_reader", "ai_queue_auto_enabled", "auth_enabled", "auth_cookie_secure"}
        env_keys = (
            "host",
            "port",
            "debug",
            "mysql_host",
            "mysql_port",
            "mysql_user",
            "mysql_password",
            "mysql_database",
            "mysql_use_reader",
            "mysql_reader_user",
            "mysql_reader_password",
            "task_timeout",
            "ai_queue_auto_enabled",
            "ai_queue_auto_concurrency",
            "ai_queue_auto_interval",
            "ai_queue_auto_limit",
            "ai_queue_auto_profile",
            "auth_enabled",
            "admin_username",
            "admin_password",
            "auth_session_ttl_seconds",
            "auth_cookie_secure",
        )
        for key in env_keys:
            env_name = f"WEB_ADMIN_{key.upper()}"
            env_val = os.environ.get(env_name)
            if env_val is None:
                env_val = env_file_values.get(env_name)
            if env_val is None or env_val == "":
                continue
            if key in int_keys:
                setattr(self, key, int(env_val))
            elif key in bool_keys:
                setattr(self, key, _env_bool(env_val))
            else:
                setattr(self, key, env_val)

    @property
    def mysql_config_dict(self) -> dict[str, Any]:
        if self.mysql_use_reader and self.mysql_reader_password:
            return {
                "host": self.mysql_host,
                "port": self.mysql_port,
                "user": self.mysql_reader_user,
                "password": self.mysql_reader_password,
                "database": self.mysql_database,
            }
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password,
            "database": self.mysql_database,
        }
