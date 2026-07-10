"""数据库连接与查询工具"""

import json
import time
from contextlib import contextmanager
from typing import Any, Generator

import pymysql
from pymysql.cursors import DictCursor

from .config import WebConfig


@contextmanager
def get_conn(config: WebConfig) -> Generator[pymysql.Connection, None, None]:
    """获取数据库连接"""
    cfg = config.mysql_config_dict
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(config: WebConfig, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    """查询多条记录"""
    with get_conn(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()


def query_one(config: WebConfig, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    """查询单条记录"""
    with get_conn(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


def execute(config: WebConfig, sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> int:
    """执行 SQL（INSERT/UPDATE/DELETE）"""
    with get_conn(config) as conn:
        with conn.cursor() as cur:
            affected = cur.execute(sql, params or ())
        conn.commit()
        return affected


def get_active_asset_groups(config: WebConfig) -> list[str]:
    """获取数据库中有数据的资产类型列表"""
    rows = query_all(
        config,
        "SELECT DISTINCT asset_group FROM auction_items ORDER BY asset_group",
    )
    return [r["asset_group"] for r in rows if r["asset_group"]]


def get_active_platforms(config: WebConfig) -> list[dict[str, Any]]:
    """获取数据库中有数据的平台列表及统计"""
    rows = query_all(
        config,
        """
        SELECT source_platform, COUNT(*) AS item_count,
               MAX(last_crawled_at) AS last_crawl_time
        FROM auction_items
        GROUP BY source_platform
        ORDER BY source_platform
        """,
    )
    return rows


# 简化的计数缓存（内存 5 秒过期）
_cache: dict[str, tuple[float, Any]] = {}

def cached_query(config: WebConfig, sql: str, params: list[Any] | tuple[Any, ...] | None = None,
                 key: str = "", ttl: float = 5.0) -> Any:
    """带缓存的查询"""
    cache_key = key or sql[:80]
    now = time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < ttl:
        return _cache[cache_key][1]
    result = query_all(config, sql, params)
    _cache[cache_key] = (now, result)
    return result
