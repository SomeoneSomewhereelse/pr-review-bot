"""Tests for onboarding/main.py — GET/HEAD /healthz on the standalone
onboarding service (a separate FastAPI app from bot/main.py)."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from onboarding import session_store
from onboarding.config import settings
from onboarding.main import app, lifespan


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_healthz_get_returns_ok():
    client = await _client()
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_healthz_head_returns_ok():
    client = await _client()
    resp = await client.head("/healthz")
    assert resp.status_code == 200


@pytest.fixture(autouse=True)
def _configure_everything_set(monkeypatch):
    """Every lifespan test below starts from a fully-configured baseline and
    monkeypatches away exactly the one thing it wants unset/malformed --
    keeps each test focused on the one check it's exercising."""
    monkeypatch.setattr(settings, "database_url", "postgresql://sentinel")
    monkeypatch.setattr(
        settings, "onboarding_session_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(session_store, "init_pool", lambda: None)
    monkeypatch.setattr(session_store, "close_pool", lambda: None)


async def test_lifespan_starts_with_everything_set():
    async with lifespan(app):
        pass


async def test_lifespan_refuses_to_start_without_database_url(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        async with lifespan(app):
            pass


async def test_lifespan_refuses_to_start_without_session_encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", "")
    with pytest.raises(RuntimeError, match="ONBOARDING_SESSION_ENCRYPTION_KEY"):
        async with lifespan(app):
            pass


async def test_lifespan_refuses_to_start_with_a_malformed_session_encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        async with lifespan(app):
            pass


async def test_lifespan_malformed_key_error_never_echoes_the_value(monkeypatch):
    monkeypatch.setattr(
        settings, "onboarding_session_encryption_key", "not-a-fernet-key-xyz-sentinel"
    )
    with pytest.raises(RuntimeError) as exc_info:
        async with lifespan(app):
            pass
    assert "not-a-fernet-key-xyz-sentinel" not in str(exc_info.value)


async def test_lifespan_calls_init_pool_and_close_pool(monkeypatch):
    calls = []
    monkeypatch.setattr(session_store, "init_pool", lambda: calls.append("init"))
    monkeypatch.setattr(session_store, "close_pool", lambda: calls.append("close"))
    async with lifespan(app):
        assert calls == ["init"]
    assert calls == ["init", "close"]
