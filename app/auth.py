"""Dashboard authentication: a single shared operator credential gates
app/dashboard.py's router. Knows nothing about tickets, reviews, or
providers -- only app.config.settings for the three credential fields. See
docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Request, Response

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
