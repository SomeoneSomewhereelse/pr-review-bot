"""Tests for onboarding/main.py — GET/HEAD /healthz on the standalone
onboarding service (a separate FastAPI app from app/main.py)."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app


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
