"""Tests for GET /login — the static HTML login page shell."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from bot.main import app


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_login_page_serves_html_with_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body


async def test_login_page_has_username_password_and_remember_me_fields():
    client = await _client()
    body = (await client.get("/login")).text
    assert 'id="usernameInput"' in body
    assert 'id="passwordInput"' in body
    assert 'id="rememberInput"' in body
    assert 'type="checkbox"' in body


async def test_login_page_posts_json_to_api_login():
    client = await _client()
    body = (await client.get("/login")).text
    assert '"/api/login"' in body
    assert 'method: "POST"' in body
