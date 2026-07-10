from typing import Optional
"""定时调度器 API"""

from fastapi import APIRouter

from ..config import WebConfig
from ..services import scheduler_service

router = APIRouter(prefix="/api/scheduler", tags=["调度器管理"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


@router.get("/status")
def get_scheduler_status():
    """获取调度器运行状态和任务列表"""
    return scheduler_service.get_status()


@router.post("/start")
def start_scheduler():
    """启动调度器（从数据库加载启用的定时任务）"""
    if not _config:
        from fastapi import HTTPException
        raise HTTPException(500, "配置未初始化")
    result = scheduler_service.start(_config)
    return result


@router.post("/stop")
def stop_scheduler():
    """停止调度器"""
    return scheduler_service.stop()


@router.post("/restart")
def restart_scheduler():
    """重启调度器（重新加载数据库任务）"""
    if not _config:
        from fastapi import HTTPException
        raise HTTPException(500, "配置未初始化")
    return scheduler_service.restart()


@router.post("/sync")
def sync_jobs():
    """手动同步数据库任务到调度器"""
    result = scheduler_service.sync_jobs()
    return result


@router.get("/presets")
def get_cron_presets():
    """获取常用 Cron 预设"""
    return scheduler_service.get_preset_crons()


@router.get("/preview")
def preview_cron(expr: str):
    """预览某个 Cron 表达式的下次执行时间"""
    next_run = scheduler_service.get_next_run_time(expr)
    if not next_run:
        return {"valid": False, "next_run": None, "error": "无效的 Cron 表达式"}
    return {"valid": True, "next_run": next_run}
