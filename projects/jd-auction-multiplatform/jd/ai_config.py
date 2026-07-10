
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Union


DEFAULT_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com",
    "bailian": "https://dashscope.aliyuncs.com",   # 百炼 = 千问的 OpenAI 兼容模式
}

DEFAULT_PROVIDER_MODELS = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-v4-flash",
    "qwen": "qwen-plus",
    "bailian": "deepseek-v4-flash",     # 百炼默认使用 deepseek 模型
}

DEFAULT_PROVIDER_VISION_MODELS = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-v4-flash",
    "qwen": "qwen-vl-plus",
    "bailian": "deepseek-v4-flash",     # 百炼视觉模型
}


@dataclass(frozen=True)
class ResolvedAIConfig:
    profile_name: str = ""
    provider: str = "qwen"
    model_name: str = "qwen-plus"
    vision_model: str = "qwen-vl-plus"
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com"
    timeout: int = 0
    max_retries: int = 0
    qps: int = 10
    source: str = "defaults"

    def to_extractor_config(
        self,
        *,
        enable_single_field_fallback: bool = False,
        circuit_breaker_failures: int = 0,
        circuit_breaker_cooldown_seconds: int = 0,
    ) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "vision_model": self.vision_model,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "qps": self.qps,
            "enable_single_field_fallback": enable_single_field_fallback,
            "circuit_breaker_failures": circuit_breaker_failures,
            "circuit_breaker_cooldown_seconds": circuit_breaker_cooldown_seconds,
        }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _truthy(value: Any) -> bool:
    text = _clean(value).lower()
    return text not in {"", "0", "false", "no", "off", "disabled"}


def _first(*values: Any) -> str:
    for value in values:
        text = _clean(value)
        if text:
            return text
    return ""


def _int_or(default: int, *values: Any) -> int:
    for value in values:
        text = _clean(value)
        if not text:
            continue
        try:
            return int(float(text))
        except ValueError:
            continue
    return default


def project_dotenv_candidates() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    candidates = [Path.cwd() / ".env", root / ".env"]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            unique.append(candidate)
            seen.add(resolved)
    return unique


def load_dotenv_file(
    path: Union[str, Path],
    *,
    env: MutableMapping[str, str] | None = None,
    override: bool = False,
) -> dict[str, str]:
    target = env if env is not None else os.environ
    path = Path(path)
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        if override or key not in target:
            target[key] = value
        loaded[key] = value
    return loaded


def load_default_dotenv(*, env: MutableMapping[str, str] | None = None) -> None:
    target = env if env is not None else os.environ
    for path in project_dotenv_candidates():
        load_dotenv_file(path, env=target, override=False)


def _prepared_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None,
    dotenv_path: Union[str, Path, None],
) -> Mapping[str, str]:
    if env is None:
        load_default_dotenv()
        return dict(os.environ)
    mutable: dict[str, str] = dict(env)
    if dotenv_path:
        load_dotenv_file(dotenv_path, env=mutable, override=False)
    return mutable


def _profile_env_value(env: Mapping[str, str], profile: str, key: str) -> str:
    if not profile:
        return ""
    return _clean(env.get(f"AI_{profile.upper()}_{key}"))


def _provider_from_profile(profile: str) -> str:
    provider = profile.lower()
    if provider in DEFAULT_PROVIDER_BASE_URLS:
        return provider
    return ""


def _enabled_mysql_profile(mysql_profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not mysql_profile:
        return {}
    if not _truthy(mysql_profile.get("enabled", 1)):
        return {}
    return mysql_profile


def resolve_ai_config(
    *,
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
    dotenv_path: Union[str, Path, None] = None,
    mysql_profile: Mapping[str, Any] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> ResolvedAIConfig:
    env_map = _prepared_env(env, dotenv_path)
    mysql_row = _enabled_mysql_profile(mysql_profile)
    cli_values = cli or {}

    cli_profile = _first(cli_values.get("profile_name"), cli_values.get("profile"))
    env_profile = _first(env_map.get("AI_ACTIVE_PROFILE"), env_map.get("AI_PROFILE"))
    mysql_profile_name = _clean(mysql_row.get("profile_name"))
    profile_name = _first(cli_profile, mysql_profile_name, env_profile)

    provider = _first(
        cli_values.get("provider"),
        cli_values.get("ai_provider"),
        cli_values.get("model"),
        mysql_row.get("provider"),
        _profile_env_value(env_map, profile_name, "PROVIDER"),
        env_map.get("AI_PROVIDER"),
        _provider_from_profile(profile_name),
        "qwen",
    ).lower()
    if provider not in DEFAULT_PROVIDER_BASE_URLS:
        provider = "qwen"

    model_name = _first(
        cli_values.get("model_name"),
        cli_values.get("ai_model_name"),
        mysql_row.get("model_name"),
        mysql_row.get("model"),
        _profile_env_value(env_map, profile_name, "MODEL_NAME"),
        _profile_env_value(env_map, provider, "MODEL_NAME"),
        env_map.get("AI_MODEL_NAME"),
        DEFAULT_PROVIDER_MODELS.get(provider),
    )

    vision_model = _first(
        cli_values.get("vision_model"),
        cli_values.get("vision_model_name"),
        mysql_row.get("vision_model_name"),
        mysql_row.get("vision_model"),
        _profile_env_value(env_map, profile_name, "VISION_MODEL"),
        _profile_env_value(env_map, provider, "VISION_MODEL"),
        env_map.get("AI_VISION_MODEL"),
        DEFAULT_PROVIDER_VISION_MODELS.get(provider),
    )

    api_key_from_mysql_env = ""
    api_key_env_var = _clean(mysql_row.get("api_key_env_var"))
    if api_key_env_var:
        api_key_from_mysql_env = _clean(env_map.get(api_key_env_var))

    api_key = _first(
        cli_values.get("api_key"),
        cli_values.get("ai_api_key"),
        api_key_from_mysql_env,
        mysql_row.get("api_key_value"),
        _profile_env_value(env_map, profile_name, "API_KEY"),
        _profile_env_value(env_map, provider, "API_KEY"),
        env_map.get("AI_API_KEY"),
    )

    base_url = _first(
        cli_values.get("base_url"),
        cli_values.get("ai_base_url"),
        mysql_row.get("base_url"),
        _profile_env_value(env_map, profile_name, "BASE_URL"),
        _profile_env_value(env_map, provider, "BASE_URL"),
        env_map.get("AI_BASE_URL"),
        DEFAULT_PROVIDER_BASE_URLS.get(provider),
    )

    timeout = _int_or(
        0,
        cli_values.get("timeout"),
        cli_values.get("ai_timeout"),
        mysql_row.get("timeout_seconds"),
        _profile_env_value(env_map, profile_name, "TIMEOUT"),
        env_map.get("AI_TIMEOUT"),
    )
    max_retries = _int_or(
        0,
        cli_values.get("max_retries"),
        cli_values.get("ai_max_retries"),
        mysql_row.get("max_retries"),
        _profile_env_value(env_map, profile_name, "MAX_RETRIES"),
        env_map.get("AI_MAX_RETRIES"),
    )
    qps = _int_or(
        10,
        cli_values.get("qps"),
        cli_values.get("ai_qps"),
        mysql_row.get("qps"),
        _profile_env_value(env_map, profile_name, "QPS"),
        env_map.get("AI_QPS"),
    )

    source = "defaults"
    if env_profile or env_map.get("AI_API_KEY") or _profile_env_value(env_map, provider, "API_KEY"):
        source = ".env"
    if mysql_row:
        source = "mysql"
    if any(_clean(v) for v in cli_values.values()):
        source = "cli"

    return ResolvedAIConfig(
        profile_name=profile_name,
        provider=provider,
        model_name=model_name,
        vision_model=vision_model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
        qps=max(qps, 1),
        source=source,
    )


def load_mysql_ai_profile(mysql_config: Any, profile_name: str = "") -> dict[str, Any] | None:
    if mysql_config is None:
        return None
    try:
        from jd_mysql_store import mysql_connection
    except Exception:
        return None

    try:
        with mysql_connection(mysql_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE %s", ("ai_model_profiles",))
                if not cur.fetchone():
                    return None
                if profile_name:
                    cur.execute(
                        """
                        SELECT *
                        FROM ai_model_profiles
                        WHERE profile_name = %s AND enabled = 1
                        LIMIT 1
                        """,
                        (profile_name,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT *
                        FROM ai_model_profiles
                        WHERE enabled = 1
                        ORDER BY is_default DESC, updated_at DESC, profile_name ASC
                        LIMIT 1
                        """
                    )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception:
        return None
