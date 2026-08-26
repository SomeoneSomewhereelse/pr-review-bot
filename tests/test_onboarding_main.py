"""Tests for onboarding/main.py — GET/HEAD /healthz on the standalone
onboarding service (a separate FastAPI app from app/main.py)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

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


async def test_lifespan_refuses_to_start_without_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        async with lifespan(app):
            pass


async def test_lifespan_starts_with_public_base_url_set(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    async with lifespan(app):
        pass
