"""Queue monitoring API for the formal MySQL schema."""

import json
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from ..config import WebConfig
from ..database import execute, query_all, query_one
from ..services import ai_queue_auto
from ..services.task_trigger import get_running_tasks, trigger_ai_enrich

router = APIRouter(prefix="/api/queues", tags=["队列管理"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


def _empty_page(page: int, size: int, error: str = "") -> dict[str, Any]:
    return {"total": 0, "page": page, "size": size, "items": [], "error": error}


def _stats(keys: list[str], error: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {key: 0 for key in keys}
    if error:
        payload["error"] = error
    return payload


def _resolve_ai_profile_policy(ai_profile: str, concurrency: int) -> tuple[str, int]:
    """Validate an optional AI profile and clamp concurrency by profile policy."""
    profile_name = (ai_profile or "").strip()
    effective_concurrency = max(1, int(concurrency or 1))
    if not profile_name:
        return "", effective_concurrency
    row = query_one(
        _config,
        """
        SELECT profile_name, enabled, max_concurrency
        FROM ai_model_profiles
        WHERE profile_name=%s
        """,
        (profile_name,),
    )
    if not row:
        raise HTTPException(400, f"AI模型配置不存在: {profile_name}")
    if not int(row.get("enabled") or 0):
        raise HTTPException(400, f"AI模型配置未启用: {profile_name}")
    max_concurrency = row.get("max_concurrency")
    if max_concurrency is not None and int(max_concurrency or 0) > 0:
        effective_concurrency = min(effective_concurrency, int(max_concurrency))
    return profile_name, effective_concurrency


_ALLOWED_AI_TASK_TYPES = {
    "text",
    "long_text",
    "debt",
    "vision",
    "attachment",
    "field_enrichment",
    "ip_image_details",
    "attachment_parse",
}


def _parse_ai_task_types(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raw_items: list[Any] = []
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                raw_items = parsed
            else:
                raw_items = [part for part in re.split(r"[,/;，、\s]+", text) if part]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]

    result: list[str] = []
    for item in raw_items:
        task_type = str(item or "").strip()
        if not task_type or task_type not in _ALLOWED_AI_TASK_TYPES or task_type in result:
            continue
        result.append(task_type)
    return result


def _safe_worker_suffix(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-") or "default"


def _split_limit(total: int, count: int) -> list[int]:
    total = max(1, int(total or 1))
    count = max(1, min(int(count or 1), total))
    base = total // count
    remainder = total % count
    return [max(1, base + (1 if idx < remainder else 0)) for idx in range(count)]


def _profile_policy_from_row(row: dict[str, Any], concurrency: int) -> dict[str, Any]:
    profile_name = str(row.get("profile_name") or "").strip()
    effective_concurrency = max(1, int(concurrency or 1))
    max_concurrency = row.get("max_concurrency")
    if max_concurrency is not None and int(max_concurrency or 0) > 0:
        effective_concurrency = min(effective_concurrency, int(max_concurrency))
    return {
        "profile_name": profile_name,
        "concurrency": effective_concurrency,
        "task_types": _parse_ai_task_types(row.get("task_types")),
        "priority": int(row.get("priority") or 0),
    }


def _resolve_ai_profile_detail(ai_profile: str, concurrency: int) -> dict[str, Any]:
    profile_name = (ai_profile or "").strip()
    effective_concurrency = max(1, int(concurrency or 1))
    if not profile_name:
        return {"profile_name": "", "concurrency": effective_concurrency, "task_types": [], "priority": 0}
    row = query_one(
        _config,
        """
        SELECT profile_name, enabled, max_concurrency, task_types, priority
        FROM ai_model_profiles
        WHERE profile_name=%s
        """,
        (profile_name,),
    )
    if not row:
        raise HTTPException(400, f"AI model profile not found: {profile_name}")
    if not int(row.get("enabled") or 0):
        raise HTTPException(400, f"AI model profile is disabled: {profile_name}")
    return _profile_policy_from_row(row, effective_concurrency)


def _enabled_ai_profile_policies(concurrency: int) -> list[dict[str, Any]]:
    rows = query_all(
        _config,
        """
        SELECT profile_name, enabled, max_concurrency, task_types, priority, is_default
        FROM ai_model_profiles
        WHERE enabled=1
        ORDER BY priority DESC, is_default DESC, profile_name ASC
        """,
    )
    return [
        _profile_policy_from_row(row, concurrency)
        for row in rows
        if str(row.get("profile_name") or "").strip()
    ]


@router.get("/ai")
def ai_queue_stats() -> dict[str, Any]:
    try:
        row = query_one(
            _config,
            """
            SELECT
              COALESCE(SUM(queue_status='pending'), 0) AS pending,
              COALESCE(SUM(queue_status='running'), 0) AS running,
              COALESCE(SUM(queue_status='parsing'), 0) AS parsing,
              COALESCE(SUM(queue_status='paused'), 0) AS paused,
              COALESCE(SUM(queue_status='success'), 0) AS success,
              COALESCE(SUM(queue_status='failed'), 0) AS failed,
              COUNT(*) AS total
            FROM ai_enrichment_queue
            """,
        ) or {}
        row["running_tasks"] = [
            task for task in get_running_tasks() if task.get("type") == "ai_enrich"
        ]
        row["auto"] = ai_queue_auto.get_config()
        return row
    except Exception as exc:
        return {
            **_stats(["pending", "running", "parsing", "paused", "success", "failed", "total"], str(exc)),
            "running_tasks": [],
            "auto": ai_queue_auto.get_config(),
        }


@router.get("/ai/tasks")
def ai_queue_tasks(status: str = "", platform: str = "", time_from: str = "", time_to: str = "", page: int = 1, size: int = 20) -> dict[str, Any]:
    try:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("q.queue_status = %s")
            params.append(status)
        if platform:
            where.append("q.source_platform = %s")
            params.append(platform)
        if time_from:
            where.append("q.created_at >= %s")
            params.append(time_from)
        if time_to:
            where.append("q.created_at <= %s")
            params.append(time_to)
        where_clause = "WHERE " + " AND ".join(where) if where else ""
        count = query_one(
            _config,
            f"SELECT COUNT(*) AS cnt FROM ai_enrichment_queue q {where_clause}",
            params,
        ) or {"cnt": 0}
        offset = (page - 1) * size
        rows = query_all(
            _config,
            f"""
            SELECT q.*, i.source_platform, i.source_item_id, i.project_name, i.asset_group
            FROM ai_enrichment_queue q
            LEFT JOIN auction_items i ON i.item_id = q.item_id
            {where_clause}
            ORDER BY q.ai_task_id DESC
            LIMIT %s OFFSET %s
            """,
            params + [size, offset],
        )
        return {"total": count["cnt"], "page": page, "size": size, "items": rows}
    except Exception as exc:
        return _empty_page(page, size, str(exc))


@router.post("/ai/process")
def process_ai_queue(limit: int = 20, concurrency: int = 3, ai_profile: str = "") -> dict[str, Any]:
    requested_profile = (ai_profile or "").strip()
    if requested_profile:
        policy = _resolve_ai_profile_detail(requested_profile, concurrency)
        task_id = trigger_ai_enrich(
            _config,
            limit=max(1, int(limit or 1)),
            concurrency=policy["concurrency"],
            ai_profile=policy["profile_name"],
            worker_id=f"web-worker-{_safe_worker_suffix(policy['profile_name'])}",
            task_types=policy["task_types"],
        )
        return {
            "mode": "single",
            "task_id": task_id,
            "task_ids": [task_id],
            "message": "AI 队列处理已触发",
            "profiles": [policy],
            "ai_profile": policy["profile_name"],
            "concurrency": policy["concurrency"],
        }

    policies = _enabled_ai_profile_policies(concurrency)
    if not policies:
        effective_concurrency = max(1, int(concurrency or 1))
        task_id = trigger_ai_enrich(
            _config,
            limit=max(1, int(limit or 1)),
            concurrency=effective_concurrency,
            ai_profile="",
            worker_id="web-worker-auto",
            task_types=[],
        )
        return {
            "mode": "auto",
            "task_id": task_id,
            "task_ids": [task_id],
            "message": "AI 队列处理已触发",
            "profiles": [{"profile_name": "", "concurrency": effective_concurrency, "task_types": []}],
            "ai_profile": "",
            "concurrency": effective_concurrency,
        }

    task_ids: list[str] = []
    for policy, profile_limit in zip(policies, _split_limit(limit, len(policies))):
        task_ids.append(
            trigger_ai_enrich(
                _config,
                limit=profile_limit,
                concurrency=policy["concurrency"],
                ai_profile=policy["profile_name"],
                worker_id=f"web-worker-{_safe_worker_suffix(policy['profile_name'])}",
                task_types=policy["task_types"],
            )
        )
    return {
        "mode": "auto",
        "task_id": task_ids[0] if task_ids else "",
        "task_ids": task_ids,
        "message": f"AI 队列处理已触发：{len(task_ids)} 个模型 worker",
        "profiles": policies,
        "ai_profile": "",
        "concurrency": max((policy["concurrency"] for policy in policies), default=max(1, int(concurrency or 1))),
    }


@router.get("/ai/auto")
def get_ai_auto_config() -> dict[str, Any]:
    return ai_queue_auto.get_config()


@router.post("/ai/auto")
def update_ai_auto_config(payload: dict[str, Any]) -> dict[str, Any]:
    profile_name, effective_concurrency = _resolve_ai_profile_policy(
        str(payload.get("ai_profile") or ""),
        int(payload.get("concurrency") or 3),
    )
    return ai_queue_auto.configure(
        enabled=payload.get("enabled"),
        concurrency=effective_concurrency,
        interval=payload.get("interval"),
        limit=payload.get("limit"),
        ai_profile=profile_name,
    )


@router.post("/ai/retry")
def retry_failed_ai_tasks(item_id: str = "", status: str = "failed", platform: str = "", time_from: str = "", time_to: str = "") -> dict[str, Any]:
    try:
        where: list[str] = []
        params: list[Any] = []
        if item_id:
            where.append("ai_task_id = %s")
            params.append(item_id)
        else:
            where.append("queue_status = %s")
            params.append(status)
            if platform:
                where.append("source_platform = %s")
                params.append(platform)
            if time_from:
                where.append("created_at >= %s")
                params.append(time_from)
            if time_to:
                where.append("created_at <= %s")
                params.append(time_to)
        where_clause = " AND ".join(where)
        affected = execute(
            _config,
            f"""
            UPDATE ai_enrichment_queue
            SET queue_status='pending',
                retry_count=0,
                locked_by=NULL,
                locked_at=NULL,
                running_profile_name=NULL,
                running_provider=NULL,
                running_model_name=NULL,
                last_error=NULL,
                updated_at=NOW()
            WHERE {where_clause}
            """,
            params,
        )
        return {"updated": affected}
    except Exception as exc:
        return {"updated": 0, "error": str(exc)}


@router.post("/ai/unlock-stale")
def unlock_stale_ai_tasks(minutes: int = 5) -> dict[str, Any]:
    try:
        affected = execute(
            _config,
            """
            UPDATE ai_enrichment_queue
            SET queue_status='pending',
                locked_by=NULL,
                locked_at=NULL,
                running_profile_name=NULL,
                running_provider=NULL,
                running_model_name=NULL,
                updated_at=NOW()
            WHERE queue_status IN ('running', 'parsing')
              AND locked_at IS NOT NULL
              AND locked_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)
            """,
            (minutes,),
        )
        return {"updated": affected}
    except Exception as exc:
        return {"updated": 0, "error": str(exc)}


@router.post("/ai/pause")
def pause_ai_tasks(status: str = "", platform: str = "", time_from: str = "", time_to: str = "") -> dict[str, Any]:
    try:
        where: list[str] = []
        params: list[Any] = []
        if status:
            if status not in {"pending", "running"}:
                raise HTTPException(400, "Only pending/running AI tasks can be paused")
            where.append("queue_status = %s")
            params.append(status)
        else:
            where.append("queue_status IN ('pending', 'running')")
        if platform:
            where.append("source_platform = %s")
            params.append(platform)
        if time_from:
            where.append("created_at >= %s")
            params.append(time_from)
        if time_to:
            where.append("created_at <= %s")
            params.append(time_to)
        affected = execute(
            _config,
            f"""
            UPDATE ai_enrichment_queue
            SET queue_status='paused',
                locked_by=NULL,
                locked_at=NULL,
                running_profile_name=NULL,
                running_provider=NULL,
                running_model_name=NULL,
                updated_at=NOW()
            WHERE {" AND ".join(where)}
            """,
            params,
        )
        return {"updated": affected}
    except HTTPException:
        raise
    except Exception as exc:
        return {"updated": 0, "error": str(exc)}


@router.post("/ai/resume")
def resume_ai_tasks(platform: str = "", time_from: str = "", time_to: str = "") -> dict[str, Any]:
    try:
        where: list[str] = ["queue_status = 'paused'"]
        params: list[Any] = []
        if platform:
            where.append("source_platform = %s")
            params.append(platform)
        if time_from:
            where.append("created_at >= %s")
            params.append(time_from)
        if time_to:
            where.append("created_at <= %s")
            params.append(time_to)
        affected = execute(
            _config,
            f"""
            UPDATE ai_enrichment_queue
            SET queue_status='pending',
                locked_by=NULL,
                locked_at=NULL,
                running_profile_name=NULL,
                running_provider=NULL,
                running_model_name=NULL,
                updated_at=NOW()
            WHERE {" AND ".join(where)}
            """,
            params,
        )
        return {"updated": affected}
    except Exception as exc:
        return {"updated": 0, "error": str(exc)}


@router.get("/review")
def review_queue(page: int = 1, size: int = 20, status: str = "pending") -> dict[str, Any]:
    try:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("r.review_status = %s")
            params.append(status)
        where_clause = "WHERE " + " AND ".join(where) if where else ""
        count = query_one(
            _config,
            f"SELECT COUNT(*) AS cnt FROM review_queue r {where_clause}",
            params,
        ) or {"cnt": 0}
        offset = (page - 1) * size
        rows = query_all(
            _config,
            f"""
            SELECT r.*, i.source_platform, i.source_item_id, i.project_name, i.asset_group
            FROM review_queue r
            LEFT JOIN auction_items i ON i.item_id = r.item_id
            {where_clause}
            ORDER BY r.review_id DESC
            LIMIT %s OFFSET %s
            """,
            params + [size, offset],
        )
        return {"total": count["cnt"], "page": page, "size": size, "items": rows}
    except Exception as exc:
        return _empty_page(page, size, str(exc))


@router.get("/crawl")
def crawl_queue_stats(batch_id: str = "") -> dict[str, Any]:
    try:
        keys = ["pending", "pending_ai", "running", "success", "failed", "skipped", "updated", "unchanged", "total"]
        where = "WHERE batch_id = %s" if batch_id else ""
        params = (batch_id,) if batch_id else ()
        row = query_one(
            _config,
            f"""
            SELECT
              COALESCE(SUM(queue_status='pending'), 0) AS pending,
              COALESCE(SUM(queue_status='pending_ai'), 0) AS pending_ai,
              COALESCE(SUM(queue_status='running'), 0) AS running,
              COALESCE(SUM(queue_status='success'), 0) AS success,
              COALESCE(SUM(queue_status='failed'), 0) AS failed,
              COALESCE(SUM(queue_status='skipped'), 0) AS skipped,
              COALESCE(SUM(queue_status='updated'), 0) AS updated,
              COALESCE(SUM(queue_status='unchanged'), 0) AS unchanged,
              COUNT(*) AS total
            FROM crawl_queue
            {where}
            """,
            params,
        )
        payload = _stats(keys)
        if row:
            for key in keys:
                payload[key] = int(row.get(key) or 0)
        return payload
    except Exception as exc:
        return _stats(keys, str(exc))


@router.get("/crawl/error-stats")
def crawl_error_stats(batch_id: str = "") -> dict[str, Any]:
    """获取错误分类统计"""
    try:
        where = "WHERE q.queue_status = 'failed'"
        params: list[Any] = []
        if batch_id:
            where += " AND q.batch_id = %s"
            params.append(batch_id)
        rows = query_all(
            _config,
            f"""
            SELECT q.last_error, COUNT(*) AS cnt
            FROM crawl_queue q
            {where}
            GROUP BY q.last_error
            ORDER BY cnt DESC
            LIMIT 20
            """,
            params,
        )
        return {"items": rows or []}
    except Exception as exc:
        return {"items": [], "error": str(exc)}


@router.post("/crawl/retry")
def retry_crawl_tasks(batch_id: str = "", source_platform: str = "", status: str = "failed", item_id: str = "") -> dict[str, Any]:
    """重试采集队列中的失败任务。"""
    try:
        where: list[str] = []
        params: list[Any] = []
        if item_id:
            where.append("queue_id = %s")
            params.append(item_id)
        else:
            where.append("queue_status = %s")
            params.append(status)
            if batch_id:
                where.append("batch_id = %s")
                params.append(batch_id)
            if source_platform:
                where.append("source_platform = %s")
                params.append(source_platform)
        where_clause = " AND ".join(where)
        affected = execute(
            _config,
            f"""
            UPDATE crawl_queue
            SET queue_status = 'pending',
                retry_count = 0,
                last_error = NULL,
                locked_by = NULL,
                locked_at = NULL,
                updated_at = NOW()
            WHERE {where_clause}
            """,
            params,
        )
        return {"updated": affected}
    except Exception as exc:
        return {"updated": 0, "error": str(exc)}


@router.get("/crawl/tasks")
def crawl_queue_tasks(batch_id: str = "", status: str = "", page: int = 1, size: int = 20) -> dict[str, Any]:
    try:
        page = max(1, int(page or 1))
        size = max(1, min(200, int(size or 20)))
        offset = (page - 1) * size
        where: list[str] = []
        params: list[Any] = []
        if batch_id:
            where.append("q.batch_id = %s")
            params.append(batch_id)
        if status:
            where.append("q.queue_status = %s")
            params.append(status)
        where_clause = "WHERE " + " AND ".join(where) if where else ""
        count = query_one(_config, f"SELECT COUNT(*) AS cnt FROM crawl_queue q {where_clause}", params) or {"cnt": 0}
        total = int(count.get("cnt") or 0)
        if total == 0:
            return _empty_page(page, size)
        rows = query_all(
            _config,
            f"""
            SELECT
              q.queue_id,
              q.batch_id,
              q.source_platform,
              q.source_item_id,
              CAST(q.item_id AS CHAR) AS item_id,
              i.project_name,
              i.asset_group,
              i.asset_group_label,
              q.queue_status AS status,
              q.last_error AS error_message,
              NULL AS prev_batch_id,
              NULL AS changed_fields_json,
              q.discovered_at AS created_at,
              q.updated_at,
              q.queue_status,
              q.priority,
              q.retry_count,
              q.max_retries,
              q.locked_by,
              q.locked_at,
              q.source_url,
              b.source_platform AS batch_platform,
              b.started_at AS batch_started_at,
              'crawl_queue' AS queue_table
            FROM crawl_queue q
            LEFT JOIN auction_items i ON i.item_id = q.item_id
            LEFT JOIN crawl_batches b ON b.batch_id = q.batch_id
            {where_clause}
            ORDER BY q.discovered_at DESC, q.queue_id DESC
            LIMIT %s OFFSET %s
            """,
            params + [size, offset],
        )
        return {"total": total, "page": page, "size": size, "items": rows or []}
    except Exception as exc:
        return _empty_page(page, size, str(exc))


@router.get("/crawl/checkpoints")
def crawl_checkpoints() -> dict[str, Any]:
    """获取所有平台的断点状态"""
    try:
        if not _table_exists("crawl_checkpoints"):
            return {"items": []}
        rows = query_all(
            _config,
            """
            SELECT source_platform, category_key, current_page, total_items_seen,
                   last_item_id, started_at, updated_at
            FROM crawl_checkpoints
            ORDER BY updated_at DESC
            """,
        )
        return {"items": rows or []}
    except Exception as exc:
        return {"items": [], "error": str(exc)}
