"""Tests for app/auth.py: credential check, session-token issue/verify, and
cookie helpers. Route-level (login/logout HTTP) tests are added in Task 3;
require_session's HTTP-gate behavior is added in Task 4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import auth
from app.config import settings


def _request_with_cookie(cookie_header: str | None) -> Request:
    headers = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode()))
    return Request({"type": "http", "headers": headers})


def test_verify_credentials_accepts_the_right_username_and_password():
    assert auth.verify_credentials("test-operator", "test-password") is True


def test_verify_credentials_rejects_wrong_username():
    assert auth.verify_credentials("wrong", "test-password") is False


def test_verify_credentials_rejects_wrong_password():
    assert auth.verify_credentials("test-operator", "wrong") is False


def test_verify_credentials_rejects_both_wrong():
    assert auth.verify_credentials("wrong", "wrong") is False


def test_create_session_token_defaults_to_a_12_hour_expiry():
    token = auth.create_session_token(remember=False)
    payload = jwt.decode(token, settings.dashboard_session_secret, algorithms=["HS256"])
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=11, minutes=59) < delta <= timedelta(hours=12)


def test_create_session_token_remember_extends_to_30_days():
    token = auth.create_session_token(remember=True)
    payload = jwt.decode(token, settings.dashboard_session_secret, algorithms=["HS256"])
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)


def test_set_session_cookie_sets_httponly_secure_samesite_strict():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=False)
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "secure" in cookie_header
    assert "samesite=strict" in cookie_header


def test_set_session_cookie_default_uses_the_12_hour_max_age():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=False)
    assert f"Max-Age={12 * 60 * 60}" in response.headers["set-cookie"]


def test_set_session_cookie_remember_uses_the_30_day_max_age():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=True)
    assert f"Max-Age={30 * 24 * 60 * 60}" in response.headers["set-cookie"]


def test_clear_session_cookie_expires_immediately():
    response = JSONResponse({})
    auth.clear_session_cookie(response)
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_require_session_accepts_a_freshly_issued_token():
    token = auth.create_session_token(remember=False)
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}")
    await auth.require_session(request)  # must not raise


async def test_require_session_rejects_a_missing_cookie():
    request = _request_with_cookie(None)
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_a_tampered_token():
    token = auth.create_session_token(remember=False)
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}x")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_an_expired_token():
    expired = jwt.encode(
        {"exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        settings.dashboard_session_secret,
        algorithm="HS256",
    )
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={expired}")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_a_token_signed_with_a_different_secret():
    token = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "a-completely-different-secret-value-for-testing-only",
        algorithm="HS256",
    )
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)
