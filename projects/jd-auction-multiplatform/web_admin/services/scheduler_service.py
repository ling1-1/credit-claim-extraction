"""Scheduler service backed by crawl_jobs in MySQL."""

import logging
import threading
from datetime import datetime
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from ..config import WebConfig
from ..database import execute, query_all, query_one
from .task_trigger import trigger_crawl

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
_config: Optional[WebConfig] = None
_lock = threading.Lock()


def _build_job_id(job_id: int) -> str:
    return f"crawl_job_{job_id}"


def _column_exists(config: WebConfig, table: str, column: str) -> bool:
    row = query_one(
        config,
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table, column),
    )
    return bool(row and int(row.get("cnt") or 0) > 0)


def ensure_runtime_columns(config: WebConfig) -> None:
    """Add scheduler columns needed by the web runtime when an old schema is used."""
    additions = [
        (
            "crawl_jobs",
            "crawl_mode",
            "ALTER TABLE crawl_jobs "
            "ADD COLUMN crawl_mode VARCHAR(20) NOT NULL DEFAULT 'incremental' "
            "COMMENT '采集模式: sample/full/incremental' AFTER category_scope",
        ),
        (
            "crawl_job_runs",
            "task_ref",
            "ALTER TABLE crawl_job_runs "
            "ADD COLUMN task_ref VARCHAR(120) NULL COMMENT '后台任务ID' AFTER status",
        ),
        (
            "crawl_jobs",
            "item_concurrency",
            "ALTER TABLE crawl_jobs "
            "ADD COLUMN item_concurrency INT NOT NULL DEFAULT 3 "
            "COMMENT '标的并发数' AFTER throttle_seconds",
        ),
    ]
    for table, column, ddl in additions:
        try:
            if not _column_exists(config, table, column):
                execute(config, ddl)
        except Exception as exc:
            logger.warning("Failed to ensure column %s.%s: %s", table, column, exc)


def _load_jobs_from_db() -> list[dict[str, Any]]:
    if not _config:
        return []
    ensure_runtime_columns(_config)
    rows = query_all(
        _config,
        """
        SELECT job_id, job_name, source_platform, cron_expr,
               per_category_limit, page_limit, category_scope,
               COALESCE(crawl_mode, 'incremental') AS crawl_mode,
               ai_enabled, attachment_parse_enabled, enabled
        FROM crawl_jobs
        WHERE enabled = 1 AND cron_expr IS NOT NULL AND cron_expr != ''
        """,
    )
    return rows or []


def _execute_scheduled_job(
    job_id: int,
    job_name: str,
    platform: str,
    limit: int,
    mode: str = "incremental",
    page_limit: Optional[int] = None,
    category_scope: str = "",
    ai_enabled: bool = False,
    attachment_parse_enabled: bool = False,
) -> None:
    """APScheduler callback. It creates a run record then starts the crawler task."""
    if not _config:
        return

    run_id: Optional[int] = None
    try:
        ensure_runtime_columns(_config)
        execute(
            _config,
            "INSERT INTO crawl_job_runs (job_id, source_platform, status, started_at) "
            "VALUES (%s, %s, 'running', NOW())",
            (job_id, platform or "jd"),
        )
        run_row = query_one(_config, "SELECT LAST_INSERT_ID() AS rid")
        run_id = int(run_row["rid"]) if run_row and run_row.get("rid") is not None else None

        task_id = trigger_crawl(
            _config,
            platform=platform or "all",
            limit=limit,
            category=category_scope or "",
            attachment_parse_enabled=bool(attachment_parse_enabled),
            mode=mode or "incremental",
            page_limit=page_limit,
            ai_mode="async" if ai_enabled else "off",
            run_id=run_id,
        )
        if run_id:
            execute(
                _config,
                "UPDATE crawl_job_runs SET task_ref = %s WHERE run_id = %s",
                (task_id, run_id),
            )
        logger.info("Scheduled crawl job #%s triggered as task %s", job_id, task_id)
    except Exception as exc:
        logger.exception("Scheduled crawl job #%s failed to start", job_id)
        try:
            if run_id:
                execute(
                    _config,
                    "UPDATE crawl_job_runs "
                    "SET status = 'failed', finished_at = NOW(), message = %s "
                    "WHERE run_id = %s",
                    (str(exc)[:5000], run_id),
                )
            else:
                execute(
                    _config,
                    "INSERT INTO crawl_job_runs (job_id, source_platform, status, started_at, finished_at, message) "
                    "VALUES (%s, %s, 'failed', NOW(), NOW(), %s)",
                    (job_id, platform or "jd", str(exc)[:5000]),
                )
        except Exception:
            logger.exception("Failed to persist scheduler start failure")


def sync_jobs() -> dict[str, Any]:
    """Synchronize enabled DB jobs into APScheduler."""
    global _scheduler
    if not _scheduler or not _scheduler.running:
        return {"status": "not_running", "synced": 0, "removed": 0}

    db_jobs = _load_jobs_from_db()
    current_ids: set[int] = set()
    synced = 0
    removed = 0

    for job in db_jobs:
        jid = int(job["job_id"])
        current_ids.add(jid)
        cron_expr = (job.get("cron_expr") or "").strip()
        if not cron_expr or not _is_valid_cron(cron_expr):
            continue
        trigger = _parse_cron_trigger(cron_expr)
        if not trigger:
            continue

        kwargs = {
            "job_id": jid,
            "job_name": job.get("job_name") or f"任务#{jid}",
            "platform": job.get("source_platform") or "jd",
            "limit": int(job.get("per_category_limit") or 10),
            "mode": job.get("crawl_mode") or "incremental",
            "page_limit": job.get("page_limit"),
            "category_scope": job.get("category_scope") or "",
            "ai_enabled": bool(job.get("ai_enabled")),
            "attachment_parse_enabled": bool(job.get("attachment_parse_enabled")),
        }
        _scheduler.add_job(
            _execute_scheduled_job,
            trigger=trigger,
            id=_build_job_id(jid),
            name=kwargs["job_name"],
            kwargs=kwargs,
            replace_existing=True,
        )
        synced += 1

    for scheduled_job in _scheduler.get_jobs():
        if not scheduled_job.id.startswith("crawl_job_"):
            continue
        try:
            jid = int(scheduled_job.id.replace("crawl_job_", ""))
        except ValueError:
            continue
        if jid not in current_ids:
            _scheduler.remove_job(scheduled_job.id)
            removed += 1

    return {"status": "ok", "synced": synced, "removed": removed}


def get_status() -> dict[str, Any]:
    if not _scheduler:
        return {
            "running": False,
            "jobs_count": 0,
            "next_runs": [],
            "scheduled_jobs": [],
            "db_enabled_jobs": len(_load_jobs_from_db()) if _config else 0,
        }

    jobs_info: list[dict[str, Any]] = []
    next_runs: list[dict[str, str]] = []
    for scheduled_job in _scheduler.get_jobs() or []:
        if not scheduled_job.id.startswith("crawl_job_"):
            continue
        next_run_time = scheduled_job.next_run_time
        jobs_info.append(
            {
                "job_id": scheduled_job.id.replace("crawl_job_", ""),
                "name": scheduled_job.name,
                "next_run_time": next_run_time.isoformat() if next_run_time else None,
                "trigger": str(scheduled_job.trigger),
            }
        )
        if next_run_time:
            next_runs.append(
                {
                    "job_name": scheduled_job.name,
                    "next_run": next_run_time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    next_runs.sort(key=lambda x: x["next_run"])
    return {
        "running": _scheduler.running,
        "jobs_count": len(jobs_info),
        "scheduled_jobs": jobs_info,
        "next_runs": next_runs[:10],
        "db_enabled_jobs": len(_load_jobs_from_db()) if _config else 0,
    }


def start(config: WebConfig) -> dict[str, Any]:
    global _scheduler, _config
    with _lock:
        _config = config
        ensure_runtime_columns(config)
        if _scheduler and _scheduler.running:
            return {"status": "already_running", **get_status()}

        _scheduler = BackgroundScheduler(
            timezone="Asia/Shanghai",
            daemon=True,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )
        _scheduler.start()
        result = sync_jobs()
        return {"status": "started", **result, **get_status()}


def stop() -> dict[str, Any]:
    global _scheduler
    with _lock:
        if not _scheduler or not _scheduler.running:
            return {"status": "not_running"}
        before = get_status()
        try:
            _scheduler.shutdown(wait=False)
        finally:
            _scheduler = None
        return {"status": "stopped", "before": before}


def restart() -> dict[str, Any]:
    if _config is None:
        return {"status": "error", "message": "配置未初始化"}
    stop_result = stop()
    start_result = start(_config)
    return {"stop": stop_result, "start": start_result}


def _is_valid_cron(cron_expr: str) -> bool:
    try:
        croniter(cron_expr)
        return True
    except (ValueError, KeyError):
        return False


def _parse_cron_trigger(cron_expr: str) -> Optional[CronTrigger]:
    try:
        parts = cron_expr.strip().split()
        if len(parts) == 5:
            minute, hour, day_of_month, month, day_of_week = parts
            return CronTrigger(
                minute=minute,
                hour=hour,
                day=day_of_month,
                month=month,
                day_of_week=day_of_week,
                timezone="Asia/Shanghai",
            )
        if len(parts) == 6:
            second, minute, hour, day_of_month, month, day_of_week = parts
            return CronTrigger(
                second=second,
                minute=minute,
                hour=hour,
                day=day_of_month,
                month=month,
                day_of_week=day_of_week,
                timezone="Asia/Shanghai",
            )
        if len(parts) == 7:
            second, minute, hour, day_of_month, month, day_of_week, year = parts
            return CronTrigger(
                second=second,
                minute=minute,
                hour=hour,
                day=day_of_month,
                month=month,
                day_of_week=day_of_week,
                year=year,
                timezone="Asia/Shanghai",
            )
        return None
    except Exception as exc:
        logger.error("Cron parse failed for %s: %s", cron_expr, exc)
        return None


def get_next_run_time(cron_expr: str) -> Optional[str]:
    if not cron_expr or not _is_valid_cron(cron_expr):
        return None
    try:
        return croniter(cron_expr, datetime.now()).get_next(datetime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def get_preset_crons() -> list[dict[str, str]]:
    return [
        {"label": "每天 08:00", "value": "0 8 * * *"},
        {"label": "每天 09:00", "value": "0 9 * * *"},
        {"label": "每天 12:00", "value": "0 12 * * *"},
        {"label": "每天 18:00", "value": "0 18 * * *"},
        {"label": "每 2 小时", "value": "0 */2 * * *"},
        {"label": "每 6 小时", "value": "0 */6 * * *"},
        {"label": "每周一 09:00", "value": "0 9 * * 1"},
        {"label": "每月 1 日 09:00", "value": "0 9 1 * *"},
        {"label": "每 30 分钟", "value": "*/30 * * * *"},
    ]
