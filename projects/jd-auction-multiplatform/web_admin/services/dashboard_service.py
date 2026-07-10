"""仪表盘数据聚合服务"""

from typing import Any

from ..config import WebConfig
from ..database import cached_query, get_active_platforms, query_all, query_one


def get_stats(config: WebConfig) -> dict[str, Any]:
    """获取核心统计指标"""
    total_items = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM auction_items",
    ) or {"cnt": 0}

    total_platforms = query_one(
        config,
        "SELECT COUNT(DISTINCT source_platform) AS cnt FROM auction_items",
    ) or {"cnt": 0}

    today_items = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM auction_items WHERE DATE(last_crawled_at) = CURDATE()",
    ) or {"cnt": 0}

    failed_batches = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM crawl_batches WHERE status = 'failed' AND DATE(started_at) = CURDATE()",
    ) or {"cnt": 0}

    # AI 队列统计
    ai_pending = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM ai_enrichment_queue WHERE queue_status = 'pending'",
    ) or {"cnt": 0}

    ai_running = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM ai_enrichment_queue WHERE queue_status = 'running'",
    ) or {"cnt": 0}

    ai_parsing = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM ai_enrichment_queue WHERE queue_status = 'parsing'",
    ) or {"cnt": 0}

    ai_paused = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM ai_enrichment_queue WHERE queue_status = 'paused'",
    ) or {"cnt": 0}

    ai_done = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM ai_enrichment_queue WHERE queue_status = 'success'",
    ) or {"cnt": 0}

    ai_failed = query_one(
        config,
        "SELECT COUNT(*) AS cnt FROM ai_enrichment_queue WHERE queue_status = 'failed'",
    ) or {"cnt": 0}

    return {
        "total_items": total_items["cnt"],
        "total_platforms": total_platforms["cnt"],
        "today_items": today_items["cnt"],
        "today_failed_batches": failed_batches["cnt"],
        "ai_queue": {
            "pending": ai_pending["cnt"],
            "running": ai_running["cnt"],
            "parsing": ai_parsing["cnt"],
            "paused": ai_paused["cnt"],
            "success": ai_done["cnt"],
            "failed": ai_failed["cnt"],
        },
    }


def get_trend(config: WebConfig, days: int = 7) -> list[dict[str, Any]]:
    """获取 N 天采集趋势"""
    rows = query_all(
        config,
        """
        SELECT
            DATE(started_at) AS date,
            COUNT(*) AS total_batches,
            SUM(CASE WHEN status IN ('success', 'partial_success') THEN 1 ELSE 0 END) AS success_batches,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_batches
        FROM crawl_batches
        WHERE started_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        GROUP BY DATE(started_at)
        ORDER BY date
        """,
        (days,),
    )
    # 补充每天的采集标的数量
    for row in rows:
        date = row["date"]
        item_count = query_one(
            config,
            "SELECT COUNT(*) AS cnt FROM auction_items WHERE DATE(last_crawled_at) = %s",
            (date,),
        )
        row["items_collected"] = item_count["cnt"] if item_count else 0
    return rows


def get_platform_distribution(config: WebConfig) -> list[dict[str, Any]]:
    """获取平台占比"""
    return query_all(
        config,
        """
        SELECT source_platform, COUNT(*) AS count
        FROM auction_items
        GROUP BY source_platform
        ORDER BY count DESC
        """,
    )


def get_asset_distribution(config: WebConfig) -> list[dict[str, Any]]:
    """获取资产类型分布"""
    return query_all(
        config,
        """
        SELECT asset_group, asset_group_label, COUNT(*) AS count
        FROM auction_items
        GROUP BY asset_group, asset_group_label
        ORDER BY count DESC
        """,
    )


def get_recent_activity(config: WebConfig, limit: int = 10) -> list[dict[str, Any]]:
    """获取最近采集活动"""
    return query_all(
        config,
        """
        SELECT b.batch_id, b.source_platform,
               DATE_FORMAT(b.started_at, '%%Y-%%m-%%d %%H:%%i') AS started_at_display,
               b.status, b.message,
               b.summary_json
        FROM crawl_batches b
        ORDER BY b.started_at DESC
        LIMIT %s
        """,
        (limit,),
    )
