"""Guard thread for optional AI enrichment queue auto-processing."""

import json
import logging
import re
import threading
import time
from typing import Any, Optional

from ..config import WebConfig
from .task_trigger import get_running_tasks, trigger_ai_enrich

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {
    "enabled": False,
    "concurrency": 3,
    "interval": 20,
    "limit": 50,
    "ai_profile": "",
}

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_config: Optional[WebConfig] = None

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
    if _config is None:
        return {"profile_name": "", "concurrency": max(1, int(concurrency or 1)), "task_types": [], "priority": 0}
    profile_name = (ai_profile or "").strip()
    if not profile_name:
        return {"profile_name": "", "concurrency": max(1, int(concurrency or 1)), "task_types": [], "priority": 0}
    from ..database import query_one

    row = query_one(
        _config,
        """
        SELECT profile_name, enabled, max_concurrency, task_types, priority
        FROM ai_model_profiles
        WHERE profile_name=%s
        """,
        (profile_name,),
    )
    if not row or not int(row.get("enabled") or 0):
        return {"profile_name": "", "concurrency": max(1, int(concurrency or 1)), "task_types": [], "priority": 0}
    return _profile_policy_from_row(row, concurrency)


def _enabled_ai_profile_policies(concurrency: int) -> list[dict[str, Any]]:
    if _config is None:
        return []
    from ..database import query_all

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


def configure(
    enabled: Optional[bool] = None,
    concurrency: Optional[int] = None,
    interval: Optional[int] = None,
    limit: Optional[int] = None,
    ai_profile: Optional[str] = None,
) -> dict[str, Any]:
    """Update auto-processing settings.

    The default mode is manual. Auto-processing only runs when ``enabled`` is
    explicitly set to true by configuration or the admin UI.
    """
    with _lock:
        if enabled is not None:
            _state["enabled"] = bool(enabled)
        if concurrency is not None:
            _state["concurrency"] = max(1, int(concurrency))
        if interval is not None:
            _state["interval"] = max(5, int(interval))
        if limit is not None:
            _state["limit"] = max(1, int(limit))
        if ai_profile is not None:
            _state["ai_profile"] = str(ai_profile or "").strip()
    return get_config()


def configure_from_config(config: WebConfig) -> dict[str, Any]:
    """Apply startup defaults from WebConfig."""
    return configure(
        enabled=bool(getattr(config, "ai_queue_auto_enabled", False)),
        concurrency=int(getattr(config, "ai_queue_auto_concurrency", 3) or 3),
        interval=int(getattr(config, "ai_queue_auto_interval", 20) or 20),
        limit=int(getattr(config, "ai_queue_auto_limit", 50) or 50),
        ai_profile=str(getattr(config, "ai_queue_auto_profile", "") or ""),
    )


def get_config() -> dict[str, Any]:
    with _lock:
        state = dict(_state)
        state["thread_alive"] = bool(_thread and _thread.is_alive())
        return state


def start(config: WebConfig) -> bool:
    """Start the guard thread idempotently.

    Starting the guard does not mean tasks will run. Task consumption is gated
    by ``enabled`` and defaults to false.
    """
    global _thread, _config
    _config = config
    configure_from_config(config)
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
    thread = threading.Thread(target=_loop, daemon=True, name="ai-queue-auto")
    thread.start()
    _thread = thread
    logger.info("AI queue guard started; auto-processing enabled=%s", _state["enabled"])
    return True


def stop() -> None:
    """Disable auto-processing. The daemon thread remains idle."""
    with _lock:
        _state["enabled"] = False


def _loop() -> None:
    while True:
        with _lock:
            interval = int(_state["interval"])
        time.sleep(interval)
        try:
            _maybe_trigger()
        except Exception as exc:
            logger.warning("AI queue guard error: %s", exc)


def _maybe_trigger() -> None:
    with _lock:
        enabled = bool(_state["enabled"])
        concurrency = int(_state["concurrency"])
        limit = int(_state["limit"])
        ai_profile = str(_state.get("ai_profile") or "")
    if not enabled or _config is None:
        return

    try:
        from ..database import execute

        unlocked = execute(
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
              AND locked_at < DATE_SUB(NOW(), INTERVAL 5 MINUTE)
            """,
        )
        if unlocked:
            logger.info("Unlocked stale AI tasks: %s", unlocked)
    except Exception as exc:
        logger.warning("Failed to unlock stale AI tasks: %s", exc)

    if any(task.get("type") == "ai_enrich" for task in get_running_tasks()):
        return

    try:
        from ..database import query_one

        stats = query_one(
            _config,
            "SELECT SUM(CASE WHEN queue_status='pending' THEN 1 ELSE 0 END) AS pending "
            "FROM ai_enrichment_queue",
        )
    except Exception as exc:
        logger.warning("Failed to query AI pending queue: %s", exc)
        return

    pending = int((stats or {}).get("pending") or 0)
    if pending <= 0:
        return

    try:
        requested_profile = ai_profile.strip()
        if requested_profile:
            policy = _resolve_ai_profile_detail(requested_profile, concurrency)
            trigger_ai_enrich(
                _config,
                limit=limit,
                concurrency=policy["concurrency"],
                ai_profile=policy["profile_name"],
                worker_id=f"auto-worker-{_safe_worker_suffix(policy['profile_name'])}",
                task_types=policy["task_types"],
            )
            logger.info(
                "Auto-triggered AI enrichment: pending=%s, limit=%s, concurrency=%s, ai_profile=%s, task_types=%s",
                pending,
                limit,
                policy["concurrency"],
                policy["profile_name"] or "default",
                policy["task_types"],
            )
            return

        policies = _enabled_ai_profile_policies(concurrency)
        if not policies:
            trigger_ai_enrich(
                _config,
                limit=limit,
                concurrency=concurrency,
                ai_profile="",
                worker_id="auto-worker-auto",
                task_types=[],
            )
            logger.info(
                "Auto-triggered AI enrichment: pending=%s, limit=%s, concurrency=%s, ai_profile=default",
                pending,
                limit,
                concurrency,
            )
            return

        task_ids: list[str] = []
        for policy, profile_limit in zip(policies, _split_limit(limit, len(policies))):
            task_ids.append(
                trigger_ai_enrich(
                    _config,
                    limit=profile_limit,
                    concurrency=policy["concurrency"],
                    ai_profile=policy["profile_name"],
                    worker_id=f"auto-worker-{_safe_worker_suffix(policy['profile_name'])}",
                    task_types=policy["task_types"],
                )
            )
        logger.info(
            "Auto-triggered AI enrichment fan-out: pending=%s, workers=%s, limit=%s",
            pending,
            len(task_ids),
            limit,
        )
    except Exception as exc:
        logger.warning("Failed to auto-trigger AI enrichment: %s", exc)
