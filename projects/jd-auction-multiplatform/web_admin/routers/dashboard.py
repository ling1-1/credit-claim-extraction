from typing import Optional
"""仪表盘 API"""

from fastapi import APIRouter

from ..config import WebConfig
from ..database import get_active_platforms
from ..services.dashboard_service import (
    get_asset_distribution,
    get_platform_distribution,
    get_recent_activity,
    get_stats,
    get_trend,
)
from ..services import scheduler_service

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"])

# 全局配置引用（由 main.py 注入）
_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


@router.get("/stats")
def stats():
    """核心统计指标"""
    return get_stats(_config)


@router.get("/trend")
def trend(days: int = 7):
    """采集趋势"""
    return get_trend(_config, days)


@router.get("/platform-distribution")
def platform_distribution():
    """平台占比"""
    return get_platform_distribution(_config)


@router.get("/asset-distribution")
def asset_distribution():
    """资产类型分布"""
    return get_asset_distribution(_config)


@router.get("/recent-activity")
def recent_activity(limit: int = 10):
    """最近活动"""
    return get_recent_activity(_config, limit)


@router.get("/scheduler-status")
def scheduler_status():
    """调度器状态（供仪表盘使用）"""
    return scheduler_service.get_status()
