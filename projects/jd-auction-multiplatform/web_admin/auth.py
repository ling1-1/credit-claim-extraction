"""Optional Web admin authentication."""

import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import WebConfig

SESSION_COOKIE = "web_admin_session"
_config: Optional[WebConfig] = None
_sessions: dict[str, dict[str, Any]] = {}

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginRequest(BaseModel):
    username: str
    password: str


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


def _auth_enabled() -> bool:
    return bool(_config and _config.auth_enabled)


def _public_api_path(path: str) -> bool:
    return path in {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}


def _clean_expired_sessions(now: float | None = None) -> None:
    current = now if now is not None else time.time()
    expired = [token for token, data in _sessions.items() if float(data.get("expires_at") or 0) <= current]
    for token in expired:
        _sessions.pop(token, None)


def _session_username(token: str) -> str:
    if not token:
        return ""
    _clean_expired_sessions()
    data = _sessions.get(token)
    if not data:
        return ""
    return str(data.get("username") or "")


def _create_session(username: str) -> str:
    if not _config:
        raise HTTPException(500, "认证配置未初始化")
    token = secrets.token_urlsafe(32)
    ttl = max(60, int(_config.auth_session_ttl_seconds or 86400))
    _sessions[token] = {"username": username, "expires_at": time.time() + ttl}
    return token


def _token_from_request(request: Request) -> str:
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    if cookie:
        return cookie
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return ""


async def require_auth_middleware(request: Request, call_next):
    if not _auth_enabled() or not request.url.path.startswith("/api/") or _public_api_path(request.url.path):
        return await call_next(request)

    if _session_username(_token_from_request(request)):
        return await call_next(request)
    return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)


@router.get("/status")
def auth_status(request: Request):
    token = _token_from_request(request)
    username = _session_username(token)
    return {
        "enabled": _auth_enabled(),
        "authenticated": bool(username) if _auth_enabled() else True,
        "username": username,
    }


@router.post("/login")
def login(body: LoginRequest):
    if not _config:
        raise HTTPException(500, "认证配置未初始化")
    if not _config.auth_enabled:
        return {"enabled": False, "authenticated": True}
    if not _config.admin_password:
        raise HTTPException(500, "认证已启用，但未配置管理员密码")

    username_ok = secrets.compare_digest(body.username, _config.admin_username)
    password_ok = secrets.compare_digest(body.password, _config.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(401, "用户名或密码错误")

    token = _create_session(body.username)
    response = JSONResponse({"enabled": True, "authenticated": True, "username": body.username})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=bool(_config.auth_cookie_secure),
        samesite="lax",
        max_age=max(60, int(_config.auth_session_ttl_seconds or 86400)),
    )
    return response


@router.post("/logout")
def logout(request: Request):
    token = _token_from_request(request)
    if token:
        _sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response
