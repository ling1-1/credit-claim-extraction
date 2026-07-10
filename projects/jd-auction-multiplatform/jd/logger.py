"""
结构化日志模块
输出 JSON Lines 格式，同时保持控制台输出可读性
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TextIO

from .config import LogConfig


class StructuredLogger:
    """结构化日志记录器"""

    def __init__(self, config: Optional[LogConfig] = None) -> None:
        self.config = config or LogConfig()
        self._file_handle: Optional[TextIO] = None
        self._level_map = {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50,
        }
        self._level = self._level_map.get(self.config.log_level.upper(), 20)

        if self.config.log_file:
            self.config.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(self.config.log_file, "a", encoding="utf-8")

    def _should_log(self, level: str) -> bool:
        return self._level_map.get(level.upper(), 20) >= self._level

    def _log(
        self,
        level: str,
        event: str,
        message: str = "",
        paimai_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """记录结构化日志"""
        if not self._should_log(level):
            return

        log_entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "level": level.upper(),
            "event": event,
        }
        if message:
            log_entry["message"] = message
        if paimai_id:
            log_entry["paimai_id"] = paimai_id
        if duration_ms is not None:
            log_entry["duration_ms"] = round(duration_ms, 2)

        # 添加额外字段
        for key, value in kwargs.items():
            if value is not None:
                log_entry[key] = value

        # 写入文件（JSON 格式）
        if self._file_handle:
            self._file_handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            self._file_handle.flush()

        # 控制台输出（简化格式）
        if self.config.console_output:
            self._print_console(level, log_entry)

    def _print_console(self, level: str, log_entry: dict[str, Any]) -> None:
        """控制台简化输出"""
        ts = log_entry.get("timestamp", "")[:19]
        event = log_entry.get("event", "")
        msg = log_entry.get("message", "")
        paimai_id = log_entry.get("paimai_id", "")
        duration = log_entry.get("duration_ms", "")

        level_colors = {
            "DEBUG": "\033[36m",
            "INFO": "\033[32m",
            "WARNING": "\033[33m",
            "ERROR": "\033[31m",
            "CRITICAL": "\033[35m",
        }
        color = level_colors.get(level, "")
        reset = "\033[0m"

        parts = [
            f"\033[90m{ts}\033[0m",
            f"{color}{level:8s}{reset}",
            f"\033[94m{event}\033[0m",
        ]
        if paimai_id:
            parts.append(f"\033[33m[{paimai_id}]\033[0m")
        if msg:
            parts.append(msg)
        if duration:
            parts.append(f"\033[90m({duration}ms)\033[0m")

        try:
            print(" ".join(parts), file=sys.stderr)
        except OSError:
            # 控制台/管道被外部工具关闭时，不能让日志输出中断采集流程。
            pass

    def debug(self, event: str, message: str = "", **kwargs: Any) -> None:
        self._log("DEBUG", event, message, **kwargs)

    def info(self, event: str, message: str = "", **kwargs: Any) -> None:
        self._log("INFO", event, message, **kwargs)

    def warning(self, event: str, message: str = "", **kwargs: Any) -> None:
        self._log("WARNING", event, message, **kwargs)

    def error(self, event: str, message: str = "", **kwargs: Any) -> None:
        self._log("ERROR", event, message, **kwargs)

    def critical(self, event: str, message: str = "", **kwargs: Any) -> None:
        self._log("CRITICAL", event, message, **kwargs)

    def log_crawl_start(self, batch_id: str, parameters: dict[str, Any]) -> None:
        self.info("crawl_start", f"开始采集批次: {batch_id}", parameters=parameters)

    def log_crawl_end(self, batch_id: str, items_count: int, errors_count: int) -> None:
        self.info(
            "crawl_end",
            f"采集批次完成: {batch_id}",
            batch_id=batch_id,
            items_count=items_count,
            errors_count=errors_count,
        )

    def log_api_call(self, function_id: str, status: str = "success", duration_ms: float = 0, **kwargs: Any) -> None:
        level = "INFO" if status == "success" else "WARNING"
        self._log(level, "api_call", f"API {function_id}: {status}", function_id=function_id, duration_ms=duration_ms, **kwargs)

    def log_api_retry(self, function_id: str, attempt: int, max_retries: int, error: str) -> None:
        self.warning(
            "api_retry",
            f"API 重试 {function_id}: 第 {attempt}/{max_retries} 次",
            function_id=function_id,
            attempt=attempt,
            max_retries=max_retries,
            error=error,
        )

    def log_extraction(self, field_key: str, status: str, source: str, paimai_id: Optional[str] = None) -> None:
        self.debug(
            "extraction",
            f"字段提取: {field_key} {status}",
            field_key=field_key,
            status=status,
            source=source,
            paimai_id=paimai_id,
        )

    def log_db_upsert(self, table: str, paimai_id: Optional[str] = None, duration_ms: float = 0) -> None:
        self.debug(
            "db_upsert",
            f"数据库写入: {table}",
            table=table,
            paimai_id=paimai_id,
            duration_ms=duration_ms,
        )

    def log_error(self, error_type: str, message: str, paimai_id: Optional[str] = None, **kwargs: Any) -> None:
        self.error(
            "error",
            message,
            error_type=error_type,
            paimai_id=paimai_id,
            **kwargs,
        )

    def close(self) -> None:
        """关闭日志文件"""
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None


# 全局日志单例
_global_logger: Optional[StructuredLogger] = None


def get_logger(config: Optional[LogConfig] = None) -> StructuredLogger:
    """获取全局日志单例"""
    global _global_logger
    if config is not None:
        if _global_logger:
            _global_logger.close()
        _global_logger = StructuredLogger(config)
    elif _global_logger is None:
        _global_logger = StructuredLogger(config)
    return _global_logger


def set_logger(logger: StructuredLogger) -> None:
    """设置全局日志单例"""
    global _global_logger
    if _global_logger:
        _global_logger.close()
    _global_logger = logger


# 计时装饰器
def timed(logger: StructuredLogger, event: str, **static_kwargs: Any):
    """计时装饰器"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                duration = (time.time() - start) * 1000
                logger.info(event, duration_ms=duration, **static_kwargs, **kwargs)
                return result
            except Exception as e:
                duration = (time.time() - start) * 1000
                logger.error(event, message=str(e), duration_ms=duration, **static_kwargs, **kwargs)
                raise
        return wrapper
    return decorator
