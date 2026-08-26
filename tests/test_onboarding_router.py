"""Tests for onboarding/router.py — the JSON contract for
POST /api/render/validate-key never echoes the submitted key, and GET /
serves the wizard page. See design doc section 5."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding import github_client, render_client
from onboarding.config import Settings, settings
from onboarding.main import app

SENTINEL_KEY = "rnd_SENTINEL_DO_NOT_LOG_9f3a"
# PEM-shaped so a leak would be unmistakable in a diff or a response body.
SENTINEL_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----SENTINEL_DO_NOT_ECHO_4c1b-----END RSA PRIVATE KEY-----"
)


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_index_serves_html():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_index_sets_security_headers():
    """This page's whole purpose is collecting a visitor's Render API key;
    without these headers any site could iframe it for a clickjacking or
    credential-phishing overlay."""
    client = await _client()
    resp = await client.get("/")
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


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


async def test_index_serves_configured_base_url(monkeypatch):
    """Exercises the REAL onboarding/static/index.html, which carries the
    __ONBOARDING_BASE_URL__ token as of Task 5 — no _INDEX_HTML stand-in, so
    the page a visitor actually gets is what's under test."""
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    client = await _client()
    resp = await client.get("/")
    assert 'window.ONBOARDING_BASE_URL = "https://onboarding.example.com";' in resp.text
    assert "__ONBOARDING_BASE_URL__" not in resp.text


async def test_index_never_serves_a_trailing_slash_base_url(monkeypatch):
    """End-to-end cover for onboarding/config.py's normalization, through the
    real page: index.html's buildManifest() appends `/?gh_step=...` to this
    value, so a surviving trailing slash would produce a `//?gh_step=...`
    redirect_url that Starlette will not route back to `/` — a 404 arriving
    only AFTER the visitor has created a real GitHub App whose one-time
    credentials are then unrecoverable."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://onboarding.example.com/")
    monkeypatch.setattr(settings, "public_base_url", Settings().public_base_url)
    client = await _client()
    resp = await client.get("/")
    assert 'window.ONBOARDING_BASE_URL = "https://onboarding.example.com";' in resp.text
    assert 'https://onboarding.example.com/"' not in resp.text


async def test_index_csp_allows_form_post_to_github():
    client = await _client()
    resp = await client.get("/")
    assert "form-action 'self' https://github.com" in resp.headers["content-security-policy"]


async def test_manifest_code_exchange_returns_app_credentials(monkeypatch):
    async def fake_exchange(code: str):
        assert code == "SENTINEL_CODE"
        return github_client.GithubAppCreated(
            app_id=42, slug="my-app", private_key_b64="cGVt", webhook_secret="whsec"
        )

    monkeypatch.setattr(github_client, "exchange_manifest_code", fake_exchange)
    client = await _client()
    resp = await client.post("/api/github/exchange-manifest-code", json={"code": "SENTINEL_CODE"})
    assert resp.status_code == 200
    assert resp.json() == {
        "valid": True,
        "app_id": 42,
        "slug": "my-app",
        "private_key_b64": "cGVt",
        "webhook_secret": "whsec",
    }


async def test_manifest_code_exchange_reports_failure_reason(monkeypatch):
    async def fake_exchange(code: str):
        return github_client.GithubAppExchangeFailed(reason="exchange_failed")

    monkeypatch.setattr(github_client, "exchange_manifest_code", fake_exchange)
    client = await _client()
    resp = await client.post("/api/github/exchange-manifest-code", json={"code": "bad"})
    assert resp.json() == {"valid": False, "reason": "exchange_failed"}


async def test_verify_installation_returns_account_details(monkeypatch):
    async def fake_verify(app_id, private_key_b64, installation_id):
        assert (app_id, private_key_b64, installation_id) == (42, "cGVt", 100)
        return github_client.InstallationVerified(account_login="octocat", repo_scope="all")

    monkeypatch.setattr(github_client, "verify_installation", fake_verify)
    client = await _client()
    resp = await client.post(
        "/api/github/verify-installation",
        json={"app_id": 42, "private_key_b64": "cGVt", "installation_id": 100},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "account_login": "octocat", "repo_scope": "all"}


async def test_verify_installation_reports_failure_reason(monkeypatch):
    async def fake_verify(app_id, private_key_b64, installation_id):
        return github_client.InstallationInvalid(reason="installation_not_found")

    monkeypatch.setattr(github_client, "verify_installation", fake_verify)
    client = await _client()
    resp = await client.post(
        "/api/github/verify-installation",
        json={"app_id": 42, "private_key_b64": "cGVt", "installation_id": 100},
    )
    assert resp.json() == {"valid": False, "reason": "installation_not_found"}


async def test_verify_installation_validation_error_never_echoes_the_private_key():
    """The same guard as test_validation_error_never_echoes_the_submitted_key,
    for the one endpoint whose body carries a GitHub App's full private key —
    the most sensitive artifact in this wizard. FastAPI's *default* 422 body
    includes the rejected input verbatim; only onboarding/main.py's app-wide
    RequestValidationError handler stops that, and it protects this router
    solely because main.py mounts the router on the app the handler is
    registered on (onboarding/CLAUDE.md calls this out as a non-obvious
    cross-file dependency a future remount could lose silently)."""
    client = await _client()
    # Misnamed field ("private_key" rather than "private_key_b64") fails
    # pydantic validation with the credential sitting in the rejected input.
    resp = await client.post(
        "/api/github/verify-installation",
        json={"app_id": 42, "private_key": SENTINEL_PRIVATE_KEY, "installation_id": 100},
    )
    assert resp.status_code == 422
    assert SENTINEL_PRIVATE_KEY not in resp.text
    assert "SENTINEL_DO_NOT_ECHO" not in resp.text
    assert "input" not in resp.text


async def test_verify_installation_response_never_echoes_the_private_key(monkeypatch):
    sentinel_key_b64 = "U0VOVElORUxfUFJJVkFURV9LRVk="

    async def fake_verify(app_id, private_key_b64, installation_id):
        return github_client.InstallationInvalid(reason="invalid_credentials")

    monkeypatch.setattr(github_client, "verify_installation", fake_verify)
    client = await _client()
    resp = await client.post(
        "/api/github/verify-installation",
        json={"app_id": 42, "private_key_b64": sentinel_key_b64, "installation_id": 100},
    )
    assert sentinel_key_b64 not in resp.text
