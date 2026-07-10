"""Background task status API."""

from fastapi import APIRouter, HTTPException

from ..services import task_trigger

router = APIRouter(prefix="/api/tasks", tags=["任务状态"])


@router.get("")
@router.get("/")
def list_tasks(include_finished: bool = True):
    """List tracked background tasks started from the web console."""
    return {"items": task_trigger.list_tasks(include_finished=include_finished)}


@router.get("/{task_id}")
def get_task(task_id: str):
    """Get one tracked background task."""
    task = task_trigger.get_task_status(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task
