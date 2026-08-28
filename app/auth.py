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
_LOGIN_FAILURE_DELAY_SECONDS = 1.0
_STATIC_DIR = Path(__file__).parent / "static"
_LOGIN_HTML = (_STATIC_DIR / "login.html").read_text(encoding="utf-8")


class SessionRequired(Exception):
    """Raised by require_session when no valid session cookie is present.
    Caught by app/main.py's exception handler, which turns it into a 401
    JSON response (API routes) or a redirect to /login (page routes)."""


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time compare against the configured operator credential.
    Both fields are always compared via hmac.compare_digest, regardless of
    whether the first already failed, so timing never leaks which field (if
    either) was wrong. Compared as UTF-8 bytes, not str -- compare_digest
    raises TypeError on non-ASCII str input, which would otherwise 500 any
    login attempt using a non-ASCII credential (a real possibility given
    this app's own Hebrew-language login page)."""
    username_ok = hmac.compare_digest(
        username.encode("utf-8"), settings.dashboard_username.encode("utf-8")
    )
    password_ok = hmac.compare_digest(
        password.encode("utf-8"), settings.dashboard_password.encode("utf-8")
    )
    return username_ok and password_ok


def _session_seconds(remember: bool) -> int:
    """Single source of truth for remember -> seconds, shared by
    create_session_token (the JWT's exp claim) and set_session_cookie (the
    cookie's Max-Age) -- these two must always agree, since a cookie that
    outlives its own token (or expires before it) is exactly the kind of
    drift that's invisible until someone hits it."""
    return _REMEMBER_SESSION_SECONDS if remember else _DEFAULT_SESSION_SECONDS


def create_session_token(remember: bool) -> str:
    """A signed JWT carrying only an expiry claim -- no identity to carry,
    since this is a single shared credential, not a per-user account."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_session_seconds(remember))
    return jwt.encode(
        {"exp": expires_at}, settings.dashboard_session_secret, algorithm=_JWT_ALGORITHM
    )


def set_session_cookie(response: Response, token: str, remember: bool) -> None:
    max_age = _session_seconds(remember)
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
        jwt.decode(
            token,
            settings.dashboard_session_secret,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.exceptions.InvalidTokenError as exc:
        raise SessionRequired() from exc


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
