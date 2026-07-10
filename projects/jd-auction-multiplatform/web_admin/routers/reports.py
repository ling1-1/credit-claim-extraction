"""质量报告 API"""

from typing import Any, Optional

from fastapi import APIRouter

from ..config import WebConfig
from ..database import execute, query_all, query_one

router = APIRouter(prefix="/api/reports", tags=["质量报告"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


@router.get("")
def list_reports(page: int = 1, size: int = 20):
    """报告列表"""
    count = query_one(
        _config,
        "SELECT COUNT(*) AS cnt FROM data_quality_reports",
    ) or {"cnt": 0}
    offset = (page - 1) * size
    rows = query_all(
        _config,
        "SELECT * FROM data_quality_reports ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (size, offset),
    )
    return {"total": count["cnt"], "page": page, "size": size, "items": rows}


@router.get("/{report_id}")
def get_report(report_id: int):
    """报告详情"""
    report = query_one(
        _config,
        "SELECT * FROM data_quality_reports WHERE report_id = %s",
        (report_id,),
    )
    return report or {"message": "报告不存在"}
