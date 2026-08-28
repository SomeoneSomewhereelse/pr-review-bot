"""Dashboard authentication: a single shared operator credential gates
app/dashboard.py's router. Knows nothing about tickets, reviews, or
providers -- only app.config.settings for the three credential fields. See
docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md.
"""
from __future__ import annotations

import asyncio
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import settings

SESSION_COOKIE_NAME = "dashboard_session"

_JWT_ALGORITHM = "HS256"
_DEFAULT_SESSION_SECONDS = 12 * 60 * 60
_REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60


class SessionRequired(Exception):
    """Raised by require_session when no valid session cookie is present.
    Caught by app/main.py's exception handler (Task 4), which turns it into
    a 401 JSON response (API routes) or a redirect to /login (page routes)."""


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time compare against the configured operator credential.
    Both fields are always compared via hmac.compare_digest, regardless of
    whether the first already failed, so timing never leaks which field (if
    either) was wrong."""
    username_ok = hmac.compare_digest(username, settings.dashboard_username)
    password_ok = hmac.compare_digest(password, settings.dashboard_password)
    return username_ok and password_ok


def create_session_token(remember: bool) -> str:
    """A signed JWT carrying only an expiry claim -- no identity to carry,
    since this is a single shared credential, not a per-user account."""
    seconds = _REMEMBER_SESSION_SECONDS if remember else _DEFAULT_SESSION_SECONDS
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return jwt.encode(
        {"exp": expires_at}, settings.dashboard_session_secret, algorithm=_JWT_ALGORITHM
    )


def set_session_cookie(response: Response, token: str, remember: bool) -> None:
    max_age = _REMEMBER_SESSION_SECONDS if remember else _DEFAULT_SESSION_SECONDS
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="strict"
    )


async def require_session(request: Request) -> None:
    """FastAPI dependency: raises SessionRequired unless the request carries
    a session cookie with a valid signature and an unexpired exp claim."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        raise SessionRequired()
    try:
        jwt.decode(token, settings.dashboard_session_secret, algorithms=[_JWT_ALGORITHM])
    except jwt.exceptions.InvalidTokenError as exc:
        raise SessionRequired() from exc


_LOGIN_FAILURE_DELAY_SECONDS = 1.0
_STATIC_DIR = Path(__file__).parent / "static"
_LOGIN_HTML = (_STATIC_DIR / "login.html").read_text(encoding="utf-8")

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


async def _delay_after_login_failure() -> None:
    """Isolated so tests can patch out the real wait without touching every
    other asyncio.sleep call in the process. Deliberately asyncio.sleep, not
    time.sleep: app/queue/dispatcher.py's serial loop runs in this same
    process's single event loop, and a blocking time.sleep(1) here would
    stall it (and every other in-flight request) for the duration of every
    single failed login attempt."""
    await asyncio.sleep(_LOGIN_FAILURE_DELAY_SECONDS)


@router.get("/login")
async def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML)


@router.post("/api/login")
async def login(payload: LoginRequest) -> JSONResponse:
    if not verify_credentials(payload.username, payload.password):
        await _delay_after_login_failure()
        return JSONResponse({"valid": False, "reason": "invalid_credentials"})
    token = create_session_token(remember=payload.remember)
    response = JSONResponse({"valid": True})
    set_session_cookie(response, token, remember=payload.remember)
    return response


@router.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"valid": True})
    clear_session_cookie(response)
    return response
