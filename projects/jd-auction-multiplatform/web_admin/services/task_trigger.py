"""Background task trigger service for crawler and AI enrichment CLI commands."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..config import WebConfig
from ..database import execute

_running_tasks: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _public_task(task_id: str, info: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": task_id, **{k: v for k, v in info.items() if k != "process"}}


def get_running_tasks() -> list[dict[str, Any]]:
    """Return active background tasks and refresh process state."""
    with _lock:
        now = time.time()
        expired: list[str] = []
        for task_id, info in _running_tasks.items():
            status = info.get("status")
            if status in ("completed", "failed", "timeout", "stopped"):
                if now - float(info.get("updated_at") or 0) > 60:
                    expired.append(task_id)
                continue

            if status == "running":
                proc = info.get("process")
                if proc and proc.poll() is not None:
                    rc = proc.poll()
                    info["status"] = "completed" if rc == 0 else "failed"
                    info["return_code"] = rc
                    info["updated_at"] = now
                elif now - float(info.get("started_at") or 0) > int(info.get("timeout") or 600):
                    if proc:
                        proc.kill()
                    info["status"] = "timeout"
                    info["updated_at"] = now

        for task_id in expired:
            _running_tasks.pop(task_id, None)

        return [
            _public_task(task_id, info)
            for task_id, info in _running_tasks.items()
            if info.get("status") in ("running", "pending")
        ]


def list_tasks(include_finished: bool = True) -> list[dict[str, Any]]:
    """Return tracked background tasks without exposing process handles."""
    get_running_tasks()
    with _lock:
        rows = [
            _public_task(task_id, info)
            for task_id, info in _running_tasks.items()
            if include_finished or info.get("status") in ("running", "pending")
        ]
    rows.sort(key=lambda r: r.get("started_at") or r.get("updated_at") or 0, reverse=True)
    return rows


def _tail(value: Optional[str], limit: int = 5000) -> str:
    return (value or "")[-limit:]


def _run_status_to_db_status(status: str) -> str:
    if status == "completed":
        return "success"
    if status == "stopped":
        return "cancelled"
    return "failed"


def _update_job_run(
    config: Optional[WebConfig],
    run_id: Optional[int],
    status: str,
    return_code: Optional[int] = None,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Persist subprocess result to crawl_job_runs when the task was run from a job."""
    if not config or not run_id:
        return

    stdout_tail = _tail(stdout, 5000)
    stderr_tail = _tail(stderr, 5000)
    summary_json = json.dumps(
        {
            "task_status": status,
            "return_code": return_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        },
        ensure_ascii=False,
    )
    message = (stderr_tail or stdout_tail or status)[:5000]
    try:
        execute(
            config,
            "UPDATE crawl_job_runs "
            "SET status = %s, finished_at = NOW(), message = %s, summary_json = %s "
            "WHERE run_id = %s",
            (_run_status_to_db_status(status), message, summary_json, run_id),
        )
    except Exception:
        # Keep the in-memory task result even if DB status persistence fails.
        pass


def _mark_latest_running_crawl_batch(
    config: Optional[WebConfig],
    platform: str,
    status: str,
    message: str,
) -> None:
    if not config or not platform:
        return
    try:
        execute(
            config,
            """
            UPDATE crawl_batches
            SET status=%s,
                finished_at=NOW(),
                message=CONCAT(IFNULL(message, ''), %s)
            WHERE source_platform=%s
              AND status='running'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (status, f" | {message}", platform),
        )
    except Exception:
        pass


def _run_async(
    task_id: str,
    cmd: list[str],
    cwd: str,
    timeout: int,
    config: Optional[WebConfig] = None,
    run_id: Optional[int] = None,
) -> None:
    """Run a command in a background thread and update task/run status."""
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    rc: Optional[int] = None
    status = "failed"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        with _lock:
            if task_id in _running_tasks:
                _running_tasks[task_id]["process"] = proc
                _running_tasks[task_id]["status"] = "running"
                _running_tasks[task_id]["started_at"] = time.time()

        if timeout and timeout > 0:
            stdout, stderr = proc.communicate(timeout=timeout)
        else:
            stdout, stderr = proc.communicate()
        rc = proc.returncode
        status = "completed" if rc == 0 else "failed"
        with _lock:
            if task_id in _running_tasks:
                _running_tasks[task_id]["status"] = status
                _running_tasks[task_id]["return_code"] = rc
                _running_tasks[task_id]["output"] = _tail(stdout, 5000)
                _running_tasks[task_id]["error"] = _tail(stderr, 2000)
                _running_tasks[task_id]["updated_at"] = time.time()
    except subprocess.TimeoutExpired:
        status = "timeout"
        if proc:
            proc.kill()
        with _lock:
            if task_id in _running_tasks:
                _running_tasks[task_id]["status"] = status
                _running_tasks[task_id]["error"] = "timeout"
                _running_tasks[task_id]["updated_at"] = time.time()
    except Exception as exc:
        status = "failed"
        stderr = str(exc)
        with _lock:
            if task_id in _running_tasks:
                _running_tasks[task_id]["status"] = status
                _running_tasks[task_id]["error"] = stderr
                _running_tasks[task_id]["updated_at"] = time.time()
    finally:
        _update_job_run(config, run_id, status, rc, stdout, stderr)
        with _lock:
            task_info = dict(_running_tasks.get(task_id) or {})
        if task_info.get("type") == "crawl" and status in {"failed", "timeout", "stopped"}:
            platform = str(task_info.get("platform") or "")
            _mark_latest_running_crawl_batch(
                config,
                platform,
                "failed" if status == "failed" else "stopped",
                f"后台采集任务{status}，已同步终止遗留 running 批次",
            )


def trigger_crawl(
    config: WebConfig,
    platform: str = "jd",
    limit: int = 10,
    category: str = "",
    attachment_parse_enabled: bool = False,
    mode: str = "incremental",
    page_limit: Optional[int] = None,
    ai_mode: str = "async",
    platform_concurrency: int = 1,
    item_concurrency: int = 1,
    ai_profile: str = "",
    run_id: Optional[int] = None,
    request_timeout: int | float | None = None,
    browser_timeout_ms: int | None = None,
    cquae_page_size: int | None = None,
    cquae_max_pages: int | None = None,
    cquae_browser_settle_ms: int | None = None,
    cquae_profile_path: str = "",
) -> str:
    """Trigger a crawler subprocess and return task_id."""
    platform = platform or "jd"
    mode = mode if mode in ("sample", "full", "incremental") else "incremental"
    ai_mode = ai_mode if ai_mode in ("sync", "async", "off") else "async"
    effective_limit = 0 if mode == "full" else int(limit or 0)
    task_id = f"crawl_{platform}_{int(time.time() * 1000)}"
    cmd = [
        sys.executable,
        "multi_platform_runner.py",
        "crawl",
        "--platform",
        platform,
        "--limit",
        str(effective_limit),
        "--mode",
        mode,
        "--platform-concurrency",
        str(max(1, int(platform_concurrency or 1))),
        "--item-concurrency",
        str(max(1, int(item_concurrency or 1))),
        "--ai-mode",
        ai_mode,
    ]
    if category and platform == "jd":
        cmd.extend(["--jd-categories", category])
    if attachment_parse_enabled:
        cmd.append("--parse-attachments")
    if ai_profile:
        cmd.extend(["--ai-profile", ai_profile])
    if platform == "cquae":
        profile_path = cquae_profile_path or str(Path(config.project_root) / ".browser" / "cquae")
        cmd.extend([
            "--request-timeout",
            str(0 if request_timeout is None else request_timeout),
            "--browser-timeout-ms",
            str(0 if browser_timeout_ms is None else browser_timeout_ms),
            "--cquae-page-size",
            str(60 if cquae_page_size is None else cquae_page_size),
            "--cquae-max-pages",
            str(0 if cquae_max_pages is None else cquae_max_pages),
            "--cquae-browser-settle-ms",
            str(800 if cquae_browser_settle_ms is None else cquae_browser_settle_ms),
            "--cquae-profile-path",
            profile_path,
        ])

    effective_timeout = 0 if platform == "cquae" and mode == "full" else config.task_timeout

    with _lock:
        _running_tasks[task_id] = {
            "type": "crawl",
            "platform": platform,
            "limit": effective_limit,
            "category": category,
            "mode": mode,
            "page_limit": page_limit,
            "ai_mode": ai_mode,
            "run_id": run_id,
            "cmd": cmd,
            "status": "pending",
            "started_at": 0,
            "timeout": effective_timeout,
        }

    thread = threading.Thread(
        target=_run_async,
        args=(task_id, cmd, config.project_root, effective_timeout, config, run_id),
        daemon=True,
    )
    thread.start()
    return task_id


def trigger_ai_enrich(
    config: WebConfig,
    limit: int = 20,
    worker_id: str = "web-worker",
    concurrency: int = 1,
    ai_profile: str = "",
    task_types: Optional[list[str]] = None,
) -> str:
    """Trigger AI enrichment worker."""
    task_id = f"ai_enrich_{time.time_ns()}"
    ai_profile = (ai_profile or "").strip()
    normalized_task_types: list[str] = []
    for item in task_types or []:
        task_type = str(item or "").strip()
        if task_type and task_type not in normalized_task_types:
            normalized_task_types.append(task_type)
    cmd = [
        sys.executable,
        "multi_platform_runner.py",
        "ai-enrich",
        "--limit",
        str(limit),
        "--worker-id",
        worker_id,
        "--concurrency",
        str(max(1, int(concurrency or 1))),
    ]
    if ai_profile:
        cmd.extend(["--ai-profile", ai_profile])
    if normalized_task_types:
        cmd.extend(["--task-types", ",".join(normalized_task_types)])

    with _lock:
        _running_tasks[task_id] = {
            "type": "ai_enrich",
            "limit": limit,
            "worker_id": worker_id,
            "concurrency": max(1, int(concurrency or 1)),
            "ai_profile": ai_profile,
            "task_types": normalized_task_types,
            "status": "pending",
            "started_at": 0,
            "timeout": config.task_timeout,
            "cmd": cmd,
        }

    thread = threading.Thread(
        target=_run_async,
        args=(task_id, cmd, config.project_root, config.task_timeout, config, None),
        daemon=True,
    )
    thread.start()
    return task_id


def get_task_status(task_id: str) -> dict[str, Any] | None:
    """Return task status by id."""
    get_running_tasks()
    with _lock:
        info = _running_tasks.get(task_id)
        if not info:
            return None
        return _public_task(task_id, info)


def stop_task(task_id: str) -> bool:
    """Stop one task by id."""
    get_running_tasks()
    with _lock:
        info = _running_tasks.get(task_id)
        if not info:
            return False
        proc = info.get("process")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                return False
        info["status"] = "stopped"
        info["updated_at"] = time.time()
        return True


def stop_tasks_by_platform(platform: str) -> int:
    """Stop all active tasks for a platform."""
    get_running_tasks()
    stopped = 0
    with _lock:
        for _task_id, info in list(_running_tasks.items()):
            if info.get("platform") != platform or info.get("status") not in ("running", "pending"):
                continue
            proc = info.get("process")
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    continue
            info["status"] = "stopped"
            info["updated_at"] = time.time()
            stopped += 1
    return stopped
