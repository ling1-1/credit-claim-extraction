from typing import Optional
"""平台状态 API"""

from fastapi import APIRouter

from ..config import WebConfig
from ..database import get_active_platforms, query_all

router = APIRouter(prefix="/api/platforms", tags=["平台状态"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg

# 平台中文名映射
PLATFORM_NAMES = {
    "jd": "京东拍卖",
    "ali": "阿里拍卖",
    "ejy365": "e交易",
    "cquae": "重庆产权",
    "tpre": "天津产权",
    "sdcqjy": "山东产权",
    "prechina": "预招商",
    "gxcq": "广西产权",
    "cbex": "北京产权交易所",
}


@router.get("")
def list_platforms():
    """平台列表及状态"""
    rows = query_all(
        _config,
        """
        SELECT source_platform,
               COUNT(*) AS total_items,
               COUNT(DISTINCT asset_group) AS asset_type_count,
               MAX(last_crawled_at) AS last_crawl_time,
               MAX(created_at) AS last_discovered
        FROM auction_items
        GROUP BY source_platform
        ORDER BY source_platform
        """,
    )
    result = []
    for row in rows:
        platform = row["source_platform"]
        # 最近批次成功率
        batch_stats = query_all(
            _config,
            """
            SELECT status, COUNT(*) AS cnt
            FROM crawl_batches
            WHERE source_platform = %s
              AND started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
            GROUP BY status
            """,
            (platform,),
        )
        total = sum(b["cnt"] for b in batch_stats) or 1
        success = sum(b["cnt"] for b in batch_stats if b["status"] in ("success", "partial_success"))

        result.append({
            "platform": platform,
            "name": PLATFORM_NAMES.get(platform, platform),
            "total_items": row["total_items"],
            "asset_type_count": row["asset_type_count"],
            "last_crawl_time": row["last_crawl_time"],
            "last_discovered": row["last_discovered"],
            "recent_success_rate": round(success / total * 100, 1),
            "recent_batches": batch_stats,
        })
    return result


@router.get("/{platform}/stats")
def get_platform_stats(platform: str):
    """平台统计详情"""
    rows = query_all(
        _config,
        """
        SELECT asset_group, asset_group_label, COUNT(*) AS cnt
        FROM auction_items
        WHERE source_platform = %s
        GROUP BY asset_group, asset_group_label
        ORDER BY cnt DESC
        """,
        (platform,),
    )
    return {
        "platform": platform,
        "name": PLATFORM_NAMES.get(platform, platform),
        "asset_groups": rows,
    }
