"""Tests for onboarding/router.py — the JSON contract for
POST /api/render/validate-key never echoes the submitted key, and GET /
serves the wizard page. See design doc section 5."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding import render_client
from onboarding.main import app

SENTINEL_KEY = "rnd_SENTINEL_DO_NOT_LOG_9f3a"


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_index_serves_html():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_valid_key_returns_owner_name(monkeypatch):
    async def fake_validate_key(api_key: str):
        assert api_key == SENTINEL_KEY
        return render_client.RenderKeyValid(owner_name="Ada Lovelace")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "owner_name": "Ada Lovelace"}


async def test_invalid_key_reports_the_reason(monkeypatch):
    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyInvalid(reason="invalid_key")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "invalid_key"}


async def test_unreachable_reports_the_reason(monkeypatch):
    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyInvalid(reason="render_unreachable")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "render_unreachable"}


async def test_response_never_echoes_the_submitted_key(monkeypatch):
    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyInvalid(reason="invalid_key")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert SENTINEL_KEY not in resp.text


async def test_validation_error_never_echoes_the_submitted_key():
    """Verify that malformed requests (e.g., wrong field name) never echo
    the credential in the 422 validation error response."""
    client = await _client()
    # Send request with wrong field name (typo) to trigger validation error
    resp = await client.post("/api/render/validate-key", json={"key": SENTINEL_KEY})
    assert resp.status_code == 422
    # Credential must not appear in response text
    assert SENTINEL_KEY not in resp.text
    # Response must use generic handler (no "input" field from FastAPI's default)
    assert "input" not in resp.text
