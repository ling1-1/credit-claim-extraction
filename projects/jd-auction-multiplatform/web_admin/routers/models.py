"""AI 模型配置管理 API"""

import json
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Optional

from ..config import WebConfig
from ..database import query_all, query_one, execute

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["模型管理"])
_config: Optional[WebConfig] = None

PROVIDERS = ["deepseek", "openai", "qwen", "bailian"]
TASK_TYPES = {"text", "long_text", "debt", "vision", "attachment"}


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


# ── Pydantic schemas ──

class ProfileCreate(BaseModel):
    profile_name: str
    provider: str
    model_name: Optional[str] = None
    vision_model_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env_var: Optional[str] = None
    api_key_value: Optional[str] = None
    timeout_seconds: Optional[int] = None
    max_retries: Optional[int] = None
    qps: Optional[int] = None
    max_concurrency: Optional[int] = None
    task_types: Optional[list[str]] = None
    priority: int = 100
    enabled: int = 1
    is_default: int = 0
    note: Optional[str] = None


class ProfileUpdate(BaseModel):
    provider: Optional[str] = None
    model_name: Optional[str] = None
    vision_model_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env_var: Optional[str] = None
    api_key_value: Optional[str] = None
    timeout_seconds: Optional[int] = None
    max_retries: Optional[int] = None
    qps: Optional[int] = None
    max_concurrency: Optional[int] = None
    task_types: Optional[list[str]] = None
    priority: Optional[int] = None
    enabled: Optional[int] = None
    is_default: Optional[int] = None
    note: Optional[str] = None


class TestConnectionRequest(BaseModel):
    provider: str
    model_name: str
    api_key: str = ""
    base_url: str = ""
    profile_name: str = ""


def _parse_task_types(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return []


def _dump_task_types(value: Optional[list[str]]) -> Optional[str]:
    if not value:
        return None
    cleaned = []
    for item in value:
        task_type = str(item or "").strip()
        if not task_type:
            continue
        if task_type not in TASK_TYPES:
            raise HTTPException(400, f"不支持的任务类型: {task_type}")
        cleaned.append(task_type)
    return json.dumps(sorted(set(cleaned)), ensure_ascii=False) if cleaned else None


def _normalize_profile_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["task_types"] = _parse_task_types(row.get("task_types"))
    return row


# ── API 端点 ──

@router.get("/profiles")
def list_profiles() -> list[dict[str, Any]]:
    """获取所有 AI 模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    rows = query_all(
        _config,
        """SELECT profile_name, provider, model_name, vision_model_name,
                  base_url, api_key_env_var,
                  CASE WHEN api_key_value IS NOT NULL AND api_key_value != '' THEN 1 ELSE 0 END AS has_api_key,
                  timeout_seconds, max_retries, qps, max_concurrency, task_types, priority, enabled, is_default,
                  note, created_at, updated_at
           FROM ai_model_profiles
           ORDER BY is_default DESC, enabled DESC, priority ASC, profile_name ASC""",
    )
    return [_normalize_profile_row(row) for row in rows]


@router.get("/profiles/{profile_name}")
def get_profile(profile_name: str) -> dict[str, Any]:
    """获取单个模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    row = query_one(
        _config,
        "SELECT * FROM ai_model_profiles WHERE profile_name = %s",
        (profile_name,),
    )
    if not row:
        raise HTTPException(404, f"配置 '{profile_name}' 不存在")
    # 不暴露完整 api_key_value，只标记是否有
    row["has_api_key"] = bool(row.get("api_key_value") and row["api_key_value"].strip())
    if "api_key_value" in row:
        row["api_key_value"] = "***" if row["has_api_key"] else ""
    return _normalize_profile_row(row)


@router.post("/profiles")
def create_profile(body: ProfileCreate) -> dict[str, Any]:
    """创建新的 AI 模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"不支持的 provider: {body.provider}，支持: {', '.join(PROVIDERS)}")
    if not body.profile_name or not body.profile_name.strip():
        raise HTTPException(400, "profile_name 不能为空")

    existing = query_one(_config, "SELECT 1 FROM ai_model_profiles WHERE profile_name = %s", (body.profile_name.strip(),))
    if existing:
        raise HTTPException(400, f"配置 '{body.profile_name}' 已存在")

    # 如果设为默认，先取消其他默认
    if body.is_default == 1:
        execute(_config, "UPDATE ai_model_profiles SET is_default = 0 WHERE is_default = 1")

    execute(
        _config,
        """INSERT INTO ai_model_profiles
           (profile_name, provider, model_name, vision_model_name, base_url,
            api_key_env_var, api_key_value, timeout_seconds, max_retries, qps,
            max_concurrency, task_types, priority, enabled, is_default, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            body.profile_name.strip(),
            body.provider.strip(),
            (body.model_name or "").strip() or None,
            (body.vision_model_name or "").strip() or None,
            (body.base_url or "").strip() or None,
            (body.api_key_env_var or "").strip() or None,
            (body.api_key_value or "").strip() or None,
            body.timeout_seconds,
            body.max_retries,
            body.qps,
            body.max_concurrency,
            _dump_task_types(body.task_types),
            int(body.priority or 100),
            body.enabled,
            body.is_default,
            (body.note or "").strip() or None,
        ),
    )
    logger.info(f"创建 AI 配置: {body.profile_name} (provider={body.provider})")
    return {"ok": True, "profile_name": body.profile_name}


@router.put("/profiles/{profile_name}")
def update_profile(profile_name: str, body: ProfileUpdate) -> dict[str, Any]:
    """更新 AI 模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    existing = query_one(_config, "SELECT * FROM ai_model_profiles WHERE profile_name = %s", (profile_name,))
    if not existing:
        raise HTTPException(404, f"配置 '{profile_name}' 不存在")
    if body.provider and body.provider not in PROVIDERS:
        raise HTTPException(400, f"不支持的 provider: {body.provider}，支持: {', '.join(PROVIDERS)}")

    # 如果设为默认，先取消其他默认
    if body.is_default == 1:
        execute(_config, "UPDATE ai_model_profiles SET is_default = 0 WHERE is_default = 1 AND profile_name != %s", (profile_name,))

    fields: list[str] = []
    values: list[Any] = []
    for key in ["provider", "model_name", "vision_model_name", "base_url",
                "api_key_env_var", "api_key_value", "timeout_seconds",
                "max_retries", "qps", "max_concurrency", "task_types",
                "priority", "enabled", "is_default", "note"]:
        if key in body.model_dump(exclude_unset=True):
            val = getattr(body, key)
            if isinstance(val, str):
                val = val.strip() or None
            if key == "task_types":
                val = _dump_task_types(val)
            fields.append(f"{key} = %s")
            values.append(val)

    if not fields:
        raise HTTPException(400, "没有需要更新的字段")

    values.append(profile_name)
    execute(_config, f"UPDATE ai_model_profiles SET {', '.join(fields)} WHERE profile_name = %s", tuple(values))
    logger.info(f"更新 AI 配置: {profile_name}")
    return {"ok": True, "profile_name": profile_name}


@router.delete("/profiles/{profile_name}")
def delete_profile(profile_name: str) -> dict[str, Any]:
    """删除 AI 模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    existing = query_one(_config, "SELECT * FROM ai_model_profiles WHERE profile_name = %s", (profile_name,))
    if not existing:
        raise HTTPException(404, f"配置 '{profile_name}' 不存在")
    execute(_config, "DELETE FROM ai_model_profiles WHERE profile_name = %s", (profile_name,))
    logger.info(f"删除 AI 配置: {profile_name}")
    return {"ok": True}


@router.post("/profiles/{profile_name}/activate")
def activate_profile(profile_name: str) -> dict[str, Any]:
    """激活 / 设为默认 AI 模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    existing = query_one(_config, "SELECT * FROM ai_model_profiles WHERE profile_name = %s", (profile_name,))
    if not existing:
        raise HTTPException(404, f"配置 '{profile_name}' 不存在")
    if not existing.get("enabled"):
        raise HTTPException(400, f"配置 '{profile_name}' 未启用，请先启用")

    execute(_config, "UPDATE ai_model_profiles SET is_default = 0")
    execute(_config, "UPDATE ai_model_profiles SET is_default = 1 WHERE profile_name = %s", (profile_name,))
    logger.info(f"激活 AI 配置: {profile_name}")
    return {"ok": True, "profile_name": profile_name, "provider": existing["provider"], "model_name": existing.get("model_name", "")}


@router.get("/active")
def get_active_profile() -> dict[str, Any]:
    """获取当前激活的 AI 模型配置"""
    if not _config:
        raise HTTPException(500, "配置未初始化")
    # 优先取 MySQL 中的 is_default
    row = query_one(
        _config,
        """SELECT profile_name, provider, model_name, vision_model_name,
                  base_url, timeout_seconds, max_retries, qps,
                  max_concurrency, task_types, priority, note
           FROM ai_model_profiles
           WHERE enabled = 1 AND is_default = 1
           ORDER BY priority ASC, updated_at DESC
           LIMIT 1""",
    )
    if row:
        row["source"] = "mysql"
        return _normalize_profile_row(row)

    # 回退到 .env / 内置默认
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from jd.ai_config import resolve_ai_config
        cfg = resolve_ai_config()
        return {
            "profile_name": cfg.profile_name or "builtin",
            "provider": cfg.provider,
            "model_name": cfg.model_name,
            "vision_model_name": cfg.vision_model,
            "base_url": cfg.base_url,
            "source": cfg.source,
        }
    except Exception:
        raise HTTPException(500, "无法获取 AI 配置")


@router.post("/test-connection")
def test_connection(body: TestConnectionRequest) -> dict[str, Any]:
    """测试 AI 模型连接"""
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from jd.ai_extractor import AIFieldExtractor

        # 编辑模式下前端不回显 api_key，为空时从数据库查询已存储的密钥
        api_key = (body.api_key or "").strip()
        base_url = (body.base_url or "").strip()

        if not api_key:
            if body.profile_name:
                stored = query_one(
                    _config,
                    "SELECT api_key_env_var, api_key_value, base_url FROM ai_model_profiles WHERE profile_name = %s",
                    (body.profile_name,),
                )
                if stored:
                    api_key = (stored.get("api_key_value") or "") or ""
                    # 优先用传入的 base_url，否则用 DB 中的
                    if not base_url:
                        base_url = (stored.get("base_url") or "") or ""

        if not api_key:
            return {"ok": False, "message": "API Key 不能为空（编辑模式下需重新输入密钥，或保存后再测试）"}

        ext = AIFieldExtractor(
            provider=body.provider,
            api_key=api_key,
            base_url=base_url,
            model_name=body.model_name,
        )
        messages = [
            {"role": "user", "content": "请用 JSON 格式回复：{\"status\":\"ok\"}"}
        ]
        resp = ext.client.chat_completion(messages, model=body.model_name)
        content = ext.client.extract_json_from_response(resp)
        logger.info(f"模型连接测试成功: {body.provider}/{body.model_name}")
        return {
            "ok": True,
            "message": f"连接成功！模型 {body.model_name} 响应正常",
            "sample": str(content)[:200],
        }
    except Exception as e:
        detail = str(e)
        if hasattr(e, "response") and hasattr(e.response, "text"):
            detail = e.response.text[:500]
        logger.warning(f"模型连接测试失败: {body.provider}/{body.model_name}: {detail}")
        return {
            "ok": False,
            "message": f"连接失败: {detail[:300]}",
        }


@router.get("/providers")
def list_providers() -> list[dict[str, str]]:
    """获取支持的 AI 供应商列表"""
    return [
        {"key": "bailian", "label": "百炼 (bailian) - 阿里云 DashScope OpenAI 兼容"},
        {"key": "qwen", "label": "千问 (qwen) - 阿里云 DashScope"},
        {"key": "deepseek", "label": "DeepSeek - api.deepseek.com"},
        {"key": "openai", "label": "OpenAI - api.openai.com"},
    ]
