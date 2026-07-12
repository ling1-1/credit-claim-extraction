"""采集批次 API."""

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from ..config import WebConfig
from ..database import execute, query_all, query_one
from ..services.task_trigger import get_running_tasks, stop_tasks_by_platform, trigger_crawl

router = APIRouter(prefix="/api/batches", tags=["批次记录"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _load_params(raw_params: Any) -> dict[str, Any]:
    if isinstance(raw_params, dict):
        return raw_params
    if not raw_params:
        return {}
    try:
        parsed = json.loads(raw_params)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _crawl_mode(params: dict[str, Any]) -> str:
    mode = (params.get("crawl_mode") or params.get("mode") or "incremental").strip()
    return mode if mode in {"sample", "full", "incremental"} else "incremental"


def _ai_mode(params: dict[str, Any]) -> str:
    explicit = params.get("ai_mode")
    if explicit in {"sync", "async", "off"}:
        return explicit
    return "async" if _as_bool(params.get("ai_enabled"), True) else "off"


def _reconcile_stale_running_batches() -> None:
    running_platforms = {
        str(task.get("platform") or "")
        for task in get_running_tasks()
        if task.get("status") in {"running", "pending"} and task.get("platform")
    }
    message = "服务重启或任务超时后未找到对应后台采集进程，自动标记为停止"
    if running_platforms:
        placeholders = ", ".join(["%s"] * len(running_platforms))
        execute(
            _config,
            f"""
            UPDATE crawl_batches
            SET status='stopped',
                finished_at=NOW(),
                message=CONCAT(IFNULL(message, ''), %s)
            WHERE status='running'
              AND TIMESTAMPDIFF(SECOND, started_at, NOW()) > 300
              AND source_platform NOT IN ({placeholders})
            """,
            [f" | {message}", *sorted(running_platforms)],
        )
        return
    execute(
        _config,
        """
        UPDATE crawl_batches
        SET status='stopped',
            finished_at=NOW(),
            message=CONCAT(IFNULL(message, ''), %s)
        WHERE status='running'
          AND TIMESTAMPDIFF(SECOND, started_at, NOW()) > 300
        """,
        (f" | {message}",),
    )


@router.get("")
def list_batches(
    page: int = 1,
    size: int = 20,
    platform: str = "",
    status: str = "",
    from_date: str = "",
    to_date: str = "",
):
    """列出采集批次."""
    _reconcile_stale_running_batches()
    where: list[str] = []
    params: list[Any] = []
    if platform:
        where.append("b.source_platform = %s")
        params.append(platform)
    if status:
        where.append("b.status = %s")
        params.append(status)
    if from_date:
        where.append("b.started_at >= %s")
        params.append(from_date)
    if to_date:
        where.append("b.started_at < DATE_ADD(%s, INTERVAL 1 DAY)")
        params.append(to_date)

    where_clause = "WHERE " + " AND ".join(where) if where else ""
    count = query_one(
        _config,
        f"SELECT COUNT(*) AS cnt FROM crawl_batches b {where_clause}",
        params,
    ) or {"cnt": 0}

    page = max(1, page)
    size = min(max(1, size), 200)
    offset = (page - 1) * size
    rows = query_all(
        _config,
        f"""
        SELECT b.*,
               TIMESTAMPDIFF(SECOND, b.started_at, COALESCE(b.finished_at, NOW())) AS duration_seconds,
               (SELECT COUNT(*) FROM auction_items WHERE batch_id = b.batch_id) AS item_count
        FROM crawl_batches b
        {where_clause}
        ORDER BY b.started_at DESC
        LIMIT %s OFFSET %s
        """,
        params + [size, offset],
    )
    return {"total": count["cnt"], "page": page, "size": size, "items": rows}


@router.get("/{batch_id}")
def get_batch(batch_id: str):
    """获取批次详情."""
    batch = query_one(_config, "SELECT * FROM crawl_batches WHERE batch_id = %s", (batch_id,))
    if not batch:
        raise HTTPException(404, "批次不存在")
    items = query_all(
        _config,
        """
        SELECT item_id, source_platform, source_item_id, project_name,
               asset_group, asset_group_label, project_status,
               start_price_display, final_price_display, last_crawled_at
        FROM auction_items
        WHERE batch_id = %s
        ORDER BY item_id
        """,
        (batch_id,),
    )
    failed_items = [it for it in items if it.get("project_status") == "failed"]
    return {**batch, "items": items, "failed_items_count": len(failed_items)}


@router.get("/{batch_id}/errors")
def get_batch_errors(batch_id: str):
    """获取批次错误信息."""
    batch = query_one(_config, "SELECT * FROM crawl_batches WHERE batch_id = %s", (batch_id,))
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


@router.post("/{batch_id}/retry")
def retry_batch(batch_id: str):
    """使用原批次参数重新触发采集."""
    batch = query_one(_config, "SELECT * FROM crawl_batches WHERE batch_id = %s", (batch_id,))
    if not batch:
        raise HTTPException(404, "批次不存在")

    platform = batch.get("source_platform") or "jd"
    params = _load_params(batch.get("parameters_json"))
    limit = _as_int(params.get("limit") or params.get("per_category_limit"), 10)
    category = params.get("category_scope") or params.get("category") or ""
    attachment = _as_bool(params.get("attachment_parse_enabled"), False)
    page_limit = _as_int(params.get("page_limit"), 0) or None
    platform_concurrency = _as_int(params.get("platform_concurrency"), 1)
    item_concurrency = _as_int(params.get("item_concurrency"), 1)
    ai_profile = str(params.get("ai_profile") or "")

    running = get_running_tasks()
    platform_running = [
        t
        for t in running
        if t.get("platform") == platform and t.get("status") in ("running", "pending")
    ]
    if platform_running:
        task_ids = ", ".join(str(t.get("task_id")) for t in platform_running)
        raise HTTPException(
            409,
            f"平台 {platform} 已有 {len(platform_running)} 个任务正在运行，请先停止后再重试。运行中任务: {task_ids}",
        )

    task_id = trigger_crawl(
        _config,
        platform=platform,
        limit=limit,
        category=category,
        attachment_parse_enabled=attachment,
        mode=_crawl_mode(params),
        page_limit=page_limit,
        ai_mode=_ai_mode(params),
        platform_concurrency=platform_concurrency,
        item_concurrency=item_concurrency,
        ai_profile=ai_profile,
    )
    execute(
        _config,
        "UPDATE crawl_batches SET message = CONCAT(IFNULL(message,''), %s) WHERE batch_id = %s",
        (f" | 重试触发: {task_id}", batch_id),
    )
    return {
        "message": f"重试任务已触发: {platform} (limit={limit})",
        "task_id": task_id,
        "retried_from": batch_id,
    }


@router.post("/{batch_id}/stop")
def stop_batch(batch_id: str):
    """停止正在运行的批次."""
    batch = query_one(_config, "SELECT * FROM crawl_batches WHERE batch_id = %s", (batch_id,))
    if not batch:
        raise HTTPException(404, "批次不存在")
    if batch.get("status") != "running":
        raise HTTPException(400, f"批次状态为 {batch.get('status')!r}，无法停止")

    platform = batch.get("source_platform") or "jd"
    stopped = stop_tasks_by_platform(platform)
    msg = (
        f"用户手动停止，终止了 {stopped} 个子进程"
        if stopped > 0
        else "用户手动停止，未找到运行中的子进程，已标记批次为停止"
    )
    execute(
        _config,
        "UPDATE crawl_batches SET status='stopped', finished_at=NOW(), message=%s WHERE batch_id = %s",
        (msg, batch_id),
    )
    return {"message": msg}


@router.delete("/{batch_id}")
def delete_batch(batch_id: str):
    """删除批次记录."""
    execute(_config, "DELETE FROM crawl_batches WHERE batch_id = %s", (batch_id,))
    return {"message": "删除成功"}
