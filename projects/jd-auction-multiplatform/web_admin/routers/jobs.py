"""Scheduled crawl job management API."""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import WebConfig
from ..database import execute, query_all, query_one
from ..services import scheduler_service
from ..services.task_trigger import trigger_crawl

router = APIRouter(prefix="/api/jobs", tags=["任务管理"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg
    try:
        scheduler_service.ensure_runtime_columns(cfg)
    except Exception:
        pass


def _validate_cron_expr(cron_expr: Optional[str]) -> None:
    cron = (cron_expr or "").strip()
    if cron and not scheduler_service.get_next_run_time(cron):
        raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {cron}")


def _validate_crawl_mode(mode: Optional[str]) -> str:
    mode = (mode or "incremental").strip()
    if mode not in ("sample", "full", "incremental"):
        raise HTTPException(status_code=400, detail=f"无效的采集模式: {mode}")
    return mode


def _sync_scheduler_if_running() -> None:
    try:
        if scheduler_service.get_status().get("running"):
            scheduler_service.sync_jobs()
    except Exception:
        pass


class JobCreate(BaseModel):
    job_name: str
    source_platform: str
    cron_expr: Optional[str] = ""
    category_scope: Optional[str] = ""
    crawl_mode: str = "incremental"
    page_limit: int = 5
    per_category_limit: int = 10
    throttle_seconds: float = 0.35
    item_concurrency: int = 3
    ai_enabled: bool = True
    attachment_parse_enabled: bool = False
    enabled: bool = True


class JobUpdate(BaseModel):
    job_name: Optional[str] = None
    source_platform: Optional[str] = None
    cron_expr: Optional[str] = None
    category_scope: Optional[str] = None
    crawl_mode: Optional[str] = None
    page_limit: Optional[int] = None
    per_category_limit: Optional[int] = None
    throttle_seconds: Optional[float] = None
    item_concurrency: Optional[int] = None
    ai_enabled: Optional[bool] = None
    attachment_parse_enabled: Optional[bool] = None
    enabled: Optional[bool] = None


@router.get("")
def list_jobs(page: int = 1, size: int = 20, platform: str = "", status: str = ""):
    where: list[str] = []
    params: list[Any] = []
    if platform:
        where.append("j.source_platform = %s")
        params.append(platform)
    if status == "enabled":
        where.append("j.enabled = 1")
    elif status == "disabled":
        where.append("j.enabled = 0")
    where_clause = "WHERE " + " AND ".join(where) if where else ""

    count = query_one(_config, f"SELECT COUNT(*) AS cnt FROM crawl_jobs j {where_clause}", params) or {"cnt": 0}
    offset = (page - 1) * size
    rows = query_all(
        _config,
        f"""
        SELECT j.*,
               (SELECT COUNT(*) FROM crawl_job_runs WHERE job_id = j.job_id) AS run_count,
               (SELECT MAX(started_at) FROM crawl_job_runs WHERE job_id = j.job_id) AS last_run_at
        FROM crawl_jobs j
        {where_clause}
        ORDER BY j.job_id DESC
        LIMIT %s OFFSET %s
        """,
        params + [size, offset],
    )

    for row in rows:
        cron = row.get("cron_expr") or ""
        row["next_run_time"] = scheduler_service.get_next_run_time(cron) if cron and row.get("enabled") else None

    return {"total": count["cnt"], "page": page, "size": size, "items": rows}


@router.get("/{job_id}")
def get_job(job_id: int):
    job = query_one(_config, "SELECT * FROM crawl_jobs WHERE job_id = %s", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    job["runs"] = query_all(
        _config,
        "SELECT * FROM crawl_job_runs WHERE job_id = %s ORDER BY started_at DESC LIMIT 20",
        (job_id,),
    )
    return job


@router.post("", status_code=201)
def create_job(job: JobCreate):
    _validate_cron_expr(job.cron_expr)
    crawl_mode = _validate_crawl_mode(job.crawl_mode)
    execute(
        _config,
        """
        INSERT INTO crawl_jobs
            (job_name, source_platform, cron_expr, category_scope, crawl_mode,
             page_limit, per_category_limit, throttle_seconds, item_concurrency,
             ai_enabled, attachment_parse_enabled, enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            job.job_name,
            job.source_platform,
            job.cron_expr or None,
            job.category_scope.strip() or None if job.category_scope else None,
            crawl_mode,
            job.page_limit,
            job.per_category_limit,
            job.throttle_seconds,
            job.item_concurrency,
            int(job.ai_enabled),
            int(job.attachment_parse_enabled),
            int(job.enabled),
        ),
    )
    _sync_scheduler_if_running()
    return {"message": "创建成功"}


@router.put("/{job_id}")
def update_job(job_id: int, job: JobUpdate):
    existing = query_one(_config, "SELECT * FROM crawl_jobs WHERE job_id = %s", (job_id,))
    if not existing:
        raise HTTPException(404, "任务不存在")

    updates: dict[str, Any] = {}
    for field in (
        "job_name",
        "source_platform",
        "cron_expr",
        "category_scope",
        "crawl_mode",
        "page_limit",
        "per_category_limit",
        "throttle_seconds",
        "item_concurrency",
        "enabled",
        "ai_enabled",
        "attachment_parse_enabled",
    ):
        val = getattr(job, field, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if field in ("ai_enabled", "attachment_parse_enabled", "enabled"):
            updates[field] = int(val)
        elif field == "crawl_mode":
            updates[field] = _validate_crawl_mode(val)
        else:
            updates[field] = val

    if not updates:
        raise HTTPException(400, "没有需要更新的字段")
    if "cron_expr" in updates:
        _validate_cron_expr(updates["cron_expr"])

    set_clause = ", ".join(f"{key} = %s" for key in updates)
    execute(_config, f"UPDATE crawl_jobs SET {set_clause} WHERE job_id = %s", list(updates.values()) + [job_id])
    _sync_scheduler_if_running()
    return {"message": "更新成功"}


@router.delete("/{job_id}")
def delete_job(job_id: int):
    execute(_config, "DELETE FROM crawl_jobs WHERE job_id = %s", (job_id,))
    _sync_scheduler_if_running()
    return {"message": "删除成功"}


@router.post("/{job_id}/toggle")
def toggle_job(job_id: int):
    job = query_one(_config, "SELECT enabled, cron_expr FROM crawl_jobs WHERE job_id = %s", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    new_status = 0 if job["enabled"] else 1
    if new_status:
        _validate_cron_expr(job.get("cron_expr"))
    execute(_config, "UPDATE crawl_jobs SET enabled = %s WHERE job_id = %s", (new_status, job_id))
    _sync_scheduler_if_running()
    return {"enabled": bool(new_status)}


@router.post("/{job_id}/run-now")
def run_job_now(job_id: int):
    job = query_one(_config, "SELECT * FROM crawl_jobs WHERE job_id = %s", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")

    platform = job.get("source_platform") or "jd"
    limit_val = job.get("per_category_limit")
    limit = int(limit_val) if limit_val is not None and int(limit_val) > 0 else 10
    execute(
        _config,
        "INSERT INTO crawl_job_runs (job_id, source_platform, status, started_at) "
        "VALUES (%s, %s, 'running', NOW())",
        (job_id, platform),
    )
    run_row = query_one(_config, "SELECT LAST_INSERT_ID() AS rid")
    run_id = int(run_row["rid"]) if run_row and run_row.get("rid") is not None else None
    task_id = trigger_crawl(
        _config,
        platform=platform,
        limit=limit,
        category=job.get("category_scope") or "",
        attachment_parse_enabled=bool(job.get("attachment_parse_enabled")),
        mode=job.get("crawl_mode") or "incremental",
        page_limit=job.get("page_limit"),
        item_concurrency=int(job.get("item_concurrency") or 3),
        ai_mode="async" if job.get("ai_enabled") else "off",
        run_id=run_id,
    )
    if run_id:
        execute(_config, "UPDATE crawl_job_runs SET task_ref = %s WHERE run_id = %s", (task_id, run_id))
    return {
        "message": f"任务已触发: {platform} limit={limit}",
        "task_id": task_id,
        "run_id": run_id,
    }


@router.get("/{job_id}/runs")
def get_job_runs(job_id: int, page: int = 1, size: int = 20):
    count = query_one(
        _config,
        "SELECT COUNT(*) AS cnt FROM crawl_job_runs WHERE job_id = %s",
        (job_id,),
    ) or {"cnt": 0}
    offset = (page - 1) * size
    rows = query_all(
        _config,
        """
        SELECT *
        FROM crawl_job_runs
        WHERE job_id = %s
        ORDER BY started_at DESC
        LIMIT %s OFFSET %s
        """,
        (job_id, size, offset),
    )
    return {"total": count["cnt"], "page": page, "size": size, "items": rows}
