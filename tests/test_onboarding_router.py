"""Tests for onboarding/router.py — the JSON contract for
POST /api/render/validate-key never echoes the submitted key, and GET /
serves the wizard page. See design doc section 5."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding import github_client, llm_client, render_client, supabase_client, uptimerobot_client
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


async def test_index_serves_configured_supabase_oauth_client_id(monkeypatch):
    monkeypatch.setattr(settings, "supabase_oauth_client_id", "66666666-6666-4666-8666-666666666666")
    client = await _client()
    resp = await client.get("/")
    assert 'window.SUPABASE_OAUTH_CLIENT_ID = "66666666-6666-4666-8666-666666666666";' in resp.text
    assert "__SUPABASE_OAUTH_CLIENT_ID__" not in resp.text


async def test_exchange_oauth_code_returns_tokens(monkeypatch):
    async def fake_exchange(code, code_verifier, redirect_uri):
        assert (code, code_verifier) == ("SENTINEL_CODE", "SENTINEL_VERIFIER")
        assert redirect_uri.endswith("/?supabase_step=oauth_callback")
        return supabase_client.SupabaseTokens(access_token="at", refresh_token="rt", expires_in=3600)

    monkeypatch.setattr(supabase_client, "exchange_oauth_code", fake_exchange)
    client = await _client()
    resp = await client.post(
        "/api/supabase/exchange-oauth-code",
        json={"code": "SENTINEL_CODE", "code_verifier": "SENTINEL_VERIFIER"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "access_token": "at", "refresh_token": "rt", "expires_in": 3600}


async def test_exchange_oauth_code_reports_failure_reason(monkeypatch):
    async def fake_exchange(code, code_verifier, redirect_uri):
        return supabase_client.SupabaseOAuthFailed(reason="invalid_code")

    monkeypatch.setattr(supabase_client, "exchange_oauth_code", fake_exchange)
    client = await _client()
    resp = await client.post(
        "/api/supabase/exchange-oauth-code", json={"code": "bad", "code_verifier": "v"}
    )
    assert resp.json() == {"valid": False, "reason": "invalid_code"}


async def test_exchange_oauth_code_validation_error_never_echoes_the_verifier():
    sentinel_verifier = "SENTINEL_DO_NOT_ECHO_VERIFIER"
    client = await _client()
    resp = await client.post(
        "/api/supabase/exchange-oauth-code",
        json={"code": "c", "verifier_typo": sentinel_verifier},
    )
    assert resp.status_code == 422
    assert sentinel_verifier not in resp.text
    assert "input" not in resp.text


async def test_refresh_access_token_returns_new_tokens(monkeypatch):
    async def fake_refresh(refresh_token):
        assert refresh_token == "SENTINEL_REFRESH"
        return supabase_client.SupabaseTokens(access_token="at2", refresh_token="rt2", expires_in=3600)

    monkeypatch.setattr(supabase_client, "refresh_access_token", fake_refresh)
    client = await _client()
    resp = await client.post("/api/supabase/refresh-access-token", json={"refresh_token": "SENTINEL_REFRESH"})
    assert resp.json() == {"valid": True, "access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}


async def test_refresh_access_token_reports_failure_reason(monkeypatch):
    async def fake_refresh(refresh_token):
        return supabase_client.SupabaseOAuthFailed(reason="unauthorized")

    monkeypatch.setattr(supabase_client, "refresh_access_token", fake_refresh)
    client = await _client()
    resp = await client.post("/api/supabase/refresh-access-token", json={"refresh_token": "stale"})
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_list_organizations_returns_orgs(monkeypatch):
    async def fake_list(access_token):
        assert access_token == "SENTINEL_ACCESS"
        return supabase_client.SupabaseOrgsListed(
            orgs=[supabase_client.SupabaseOrg(slug="org-one", name="Org One")]
        )

    monkeypatch.setattr(supabase_client, "list_organizations", fake_list)
    client = await _client()
    resp = await client.post("/api/supabase/list-organizations", json={"access_token": "SENTINEL_ACCESS"})
    assert resp.json() == {"valid": True, "orgs": [{"slug": "org-one", "name": "Org One"}]}


async def test_list_organizations_reports_failure_reason(monkeypatch):
    async def fake_list(access_token):
        return supabase_client.SupabaseApiFailed(reason="rate_limited")

    monkeypatch.setattr(supabase_client, "list_organizations", fake_list)
    client = await _client()
    resp = await client.post("/api/supabase/list-organizations", json={"access_token": "a"})
    assert resp.json() == {"valid": False, "reason": "rate_limited"}


async def test_create_project_returns_ref_and_status(monkeypatch):
    async def fake_create(access_token, organization_slug, name, db_pass):
        assert (access_token, organization_slug, name, db_pass) == ("a", "org-one", "pr-review-bot", "pw123")
        return supabase_client.SupabaseProjectCreated(ref="x" * 20, status="INACTIVE")

    monkeypatch.setattr(supabase_client, "create_project", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/supabase/create-project",
        json={"access_token": "a", "organization_slug": "org-one", "name": "pr-review-bot", "db_pass": "pw123"},
    )
    assert resp.json() == {"valid": True, "ref": "x" * 20, "status": "INACTIVE"}


async def test_create_project_relays_the_rejection_message(monkeypatch):
    async def fake_create(access_token, organization_slug, name, db_pass):
        return supabase_client.SupabaseProjectRejected(message="This organization already has the maximum number of free projects.")

    monkeypatch.setattr(supabase_client, "create_project", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/supabase/create-project",
        json={"access_token": "a", "organization_slug": "org-one", "name": "n", "db_pass": "pw"},
    )
    assert resp.json() == {
        "valid": False,
        "reason": "project_creation_rejected",
        "message": "This organization already has the maximum number of free projects.",
    }


async def test_create_project_validation_error_never_echoes_the_password():
    sentinel_pass = "SENTINEL_DO_NOT_ECHO_PASSWORD"
    client = await _client()
    resp = await client.post(
        "/api/supabase/create-project",
        json={"access_token": "a", "organization_slug": "org", "name": "n", "db_pass_typo": sentinel_pass},
    )
    assert resp.status_code == 422
    assert sentinel_pass not in resp.text
    assert "input" not in resp.text


async def test_project_status_returns_status(monkeypatch):
    async def fake_status(access_token, ref):
        assert (access_token, ref) == ("a", "x" * 20)
        return supabase_client.SupabaseProjectStatus(status="ACTIVE_HEALTHY")

    monkeypatch.setattr(supabase_client, "get_project_status", fake_status)
    client = await _client()
    resp = await client.post("/api/supabase/project-status", json={"access_token": "a", "ref": "x" * 20})
    assert resp.json() == {"valid": True, "status": "ACTIVE_HEALTHY"}


async def test_project_status_reports_failure_reason(monkeypatch):
    async def fake_status(access_token, ref):
        return supabase_client.SupabaseApiFailed(reason="unauthorized")

    monkeypatch.setattr(supabase_client, "get_project_status", fake_status)
    client = await _client()
    resp = await client.post("/api/supabase/project-status", json={"access_token": "a", "ref": "x" * 20})
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_connection_info_returns_shape(monkeypatch):
    async def fake_info(access_token, ref):
        return supabase_client.SupabaseConnectionInfo(
            db_user="postgres.x", db_host="aws-0-us-east-1.pooler.supabase.com", db_port=5432, db_name="postgres"
        )

    monkeypatch.setattr(supabase_client, "get_connection_info", fake_info)
    client = await _client()
    resp = await client.post("/api/supabase/connection-info", json={"access_token": "a", "ref": "x" * 20})
    assert resp.json() == {
        "valid": True,
        "db_user": "postgres.x",
        "db_host": "aws-0-us-east-1.pooler.supabase.com",
        "db_port": 5432,
        "db_name": "postgres",
    }


async def test_connection_info_never_carries_a_password_field(monkeypatch):
    async def fake_info(access_token, ref):
        return supabase_client.SupabaseConnectionInfo(
            db_user="postgres.x", db_host="host", db_port=5432, db_name="postgres"
        )

    monkeypatch.setattr(supabase_client, "get_connection_info", fake_info)
    client = await _client()
    resp = await client.post("/api/supabase/connection-info", json={"access_token": "a", "ref": "x" * 20})
    assert "db_pass" not in resp.text
    assert "password" not in resp.text.lower()


async def test_connection_info_reports_failure_reason(monkeypatch):
    async def fake_info(access_token, ref):
        return supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")

    monkeypatch.setattr(supabase_client, "get_connection_info", fake_info)
    client = await _client()
    resp = await client.post("/api/supabase/connection-info", json={"access_token": "a", "ref": "x" * 20})
    assert resp.json() == {"valid": False, "reason": "pooler_config_unavailable"}


async def test_project_status_rejects_a_ref_that_does_not_match_supabases_format():
    """ref is interpolated into a request path
    (GET /v1/projects/{ref}) -- reject anything that isn't Supabase's real
    20-lowercase-letter ref shape before it ever reaches that interpolation."""
    client = await _client()
    resp = await client.post(
        "/api/supabase/project-status", json={"access_token": "a", "ref": "not-a-real-ref"}
    )
    assert resp.status_code == 422


async def test_project_status_rejects_an_uppercase_ref():
    client = await _client()
    resp = await client.post(
        "/api/supabase/project-status", json={"access_token": "a", "ref": "X" * 20}
    )
    assert resp.status_code == 422


async def test_connection_info_rejects_a_ref_that_does_not_match_supabases_format():
    client = await _client()
    resp = await client.post(
        "/api/supabase/connection-info", json={"access_token": "a", "ref": "../../etc/passwd"}
    )
    assert resp.status_code == 422


async def test_gemini_list_models_returns_models(monkeypatch):
    async def fake_list(api_key):
        assert api_key == "SENTINEL_KEY"
        return llm_client.LlmModelsListed(models=["gemini-flash-latest", "gemini-2.5-pro"])

    monkeypatch.setattr(llm_client, "list_gemini_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key": "SENTINEL_KEY"})
    assert resp.json() == {"valid": True, "models": ["gemini-flash-latest", "gemini-2.5-pro"]}


async def test_gemini_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(api_key):
        return llm_client.LlmApiFailed(reason="unauthorized")

    monkeypatch.setattr(llm_client, "list_gemini_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key": "bad"})
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_gemini_list_models_validation_error_never_echoes_the_key():
    sentinel_key = "SENTINEL_DO_NOT_ECHO_KEY"
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key_typo": sentinel_key})
    assert resp.status_code == 422
    assert sentinel_key not in resp.text
    assert "input" not in resp.text


async def test_groq_list_models_returns_models(monkeypatch):
    async def fake_list(api_key):
        assert api_key == "SENTINEL_KEY"
        return llm_client.LlmModelsListed(models=["llama-3.3-70b-versatile"])

    monkeypatch.setattr(llm_client, "list_groq_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key": "SENTINEL_KEY"})
    assert resp.json() == {"valid": True, "models": ["llama-3.3-70b-versatile"]}


async def test_groq_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(api_key):
        return llm_client.LlmApiFailed(reason="rate_limited")

    monkeypatch.setattr(llm_client, "list_groq_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key": "a"})
    assert resp.json() == {"valid": False, "reason": "rate_limited"}


async def test_groq_list_models_validation_error_never_echoes_the_key():
    sentinel_key = "SENTINEL_DO_NOT_ECHO_KEY"
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key_typo": sentinel_key})
    assert resp.status_code == 422
    assert sentinel_key not in resp.text
    assert "input" not in resp.text


async def test_vertex_list_models_returns_models_and_project_id(monkeypatch):
    async def fake_list(service_account_key_b64):
        assert service_account_key_b64 == "SENTINEL_B64"
        return llm_client.VertexModelsListed(project_id="sentinel-project", models=["gemini-2.5-flash"])

    monkeypatch.setattr(llm_client, "list_vertex_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"service_account_key_b64": "SENTINEL_B64"})
    assert resp.json() == {"valid": True, "project_id": "sentinel-project", "models": ["gemini-2.5-flash"]}


async def test_vertex_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(service_account_key_b64):
        return llm_client.LlmApiFailed(reason="invalid_service_account_json")

    monkeypatch.setattr(llm_client, "list_vertex_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"service_account_key_b64": "not-json"})
    assert resp.json() == {"valid": False, "reason": "invalid_service_account_json"}


async def test_vertex_list_models_validation_error_never_echoes_the_key():
    sentinel_key = "SENTINEL_DO_NOT_ECHO_SERVICE_ACCOUNT_KEY"
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"key_typo": sentinel_key})
    assert resp.status_code == 422
    assert sentinel_key not in resp.text
    assert "input" not in resp.text


# An empty credential must be a 422, not something that reaches the SDK:
# genai.Client(api_key="") falls back to reading the *server's* own
# GOOGLE_API_KEY/GEMINI_API_KEY env vars, so an empty submission could
# otherwise validate against the operator's credential instead of failing.


async def test_gemini_list_models_rejects_empty_key():
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key": ""})
    assert resp.status_code == 422


async def test_groq_list_models_rejects_empty_key():
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key": ""})
    assert resp.status_code == 422


async def test_vertex_list_models_rejects_empty_key():
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"service_account_key_b64": ""})
    assert resp.status_code == 422


async def test_uptimerobot_monitor_created_reports_created_true(monkeypatch):
    async def fake_create(api_key, render_service_url):
        assert api_key == SENTINEL_KEY
        assert render_service_url == "https://sentinel-service.onrender.com"
        return uptimerobot_client.UptimeRobotMonitorResult(created=True)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "created": True}


async def test_uptimerobot_monitor_reused_reports_created_false(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotMonitorResult(created=False)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "created": False}


async def test_uptimerobot_failure_reports_the_reason(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_uptimerobot_response_never_echoes_the_submitted_key(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert SENTINEL_KEY not in resp.text


async def test_uptimerobot_validation_error_never_echoes_the_submitted_key():
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 422
    assert SENTINEL_KEY not in resp.text
    assert "input" not in resp.text


async def test_uptimerobot_empty_api_key_is_rejected():
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": "", "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 422


async def test_uptimerobot_empty_render_url_is_rejected():
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": ""},
    )
    assert resp.status_code == 422


async def test_uptimerobot_whitespace_only_render_url_is_rejected():
    """min_length=1 alone lets "   " through, and the client's own .strip()
    then derives a bare relative "/healthz" as the monitor URL."""
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "   \n\t"},
    )
    assert resp.status_code == 422
    assert SENTINEL_KEY not in resp.text


async def test_uptimerobot_render_url_is_stripped_before_the_client_sees_it(monkeypatch):
    seen = {}

    async def fake_create(api_key, render_service_url):
        seen["url"] = render_service_url
        return uptimerobot_client.UptimeRobotMonitorResult(created=True)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "  https://s.onrender.com \n"},
    )
    assert resp.status_code == 200
    assert seen["url"] == "https://s.onrender.com"
