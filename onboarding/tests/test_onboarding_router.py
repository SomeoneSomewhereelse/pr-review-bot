"""Tests for onboarding/router.py — the JSON contract for
POST /api/render/validate-key never echoes the submitted key, and GET /
serves the wizard page. See design doc section 5."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding import (
    github_client,
    llm_client,
    render_client,
    router,
    session_store,
    supabase_client,
    uptimerobot_client,
)
from onboarding.main import app

SENTINEL_KEY = "rnd_SENTINEL_DO_NOT_LOG_9f3a"
# PEM-shaped so a leak would be unmistakable in a diff or a response body.
SENTINEL_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----SENTINEL_DO_NOT_ECHO_4c1b-----END RSA PRIVATE KEY-----"
)


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakeSessionStore:
    """An in-memory stand-in for session_store.py's public functions, used
    via `_use_fake_session_store(monkeypatch)` below. Mirrors the real
    module's contract (update_frame merges and fails closed against a
    missing session id; create_session is the only way to mint one) without
    touching Postgres -- session_store.py's own tests (against a real test
    Postgres) are what verify the real implementation actually behaves this
    way."""

    def __init__(self):
        self._sessions: dict[str, dict[str, dict]] = {}
        self._next_id = 0

    def create_session(self) -> str:
        self._next_id += 1
        session_id = f"fake-session-{self._next_id}"
        self._sessions[session_id] = {}
        return session_id

    def get_session(self, session_id: str):
        frames = self._sessions.get(session_id)
        if frames is None:
            return None
        return session_store.SessionData(frames={k: dict(v) for k, v in frames.items()})

    def update_frame(self, session_id: str, frame: str, data: dict, *, replace: bool = False):
        if session_id not in self._sessions:
            return session_store.SessionNotFound()
        existing = {} if replace else self._sessions[session_id].get(frame, {})
        self._sessions[session_id][frame] = {**existing, **data}
        return None

    def read_frame(self, session_id: str, frame: str):
        frames = self._sessions.get(session_id)
        if frames is None:
            return None
        return frames.get(frame)

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def _use_fake_session_store(monkeypatch) -> _FakeSessionStore:
    fake = _FakeSessionStore()
    monkeypatch.setattr(session_store, "create_session", fake.create_session)
    monkeypatch.setattr(session_store, "get_session", fake.get_session)
    monkeypatch.setattr(session_store, "update_frame", fake.update_frame)
    monkeypatch.setattr(session_store, "read_frame", fake.read_frame)
    monkeypatch.setattr(session_store, "delete_session", fake.delete_session)
    return fake


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
    _use_fake_session_store(monkeypatch)

    async def fake_validate_key(api_key: str):
        assert api_key == SENTINEL_KEY
        return render_client.RenderKeyValid(owner_name="Ada Lovelace")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "owner_name": "Ada Lovelace"}


async def test_valid_key_creates_a_session_and_sets_the_cookie(monkeypatch):
    _use_fake_session_store(monkeypatch)

    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyValid(owner_name="Ada Lovelace")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert "onboarding_session=" in resp.headers.get("set-cookie", "")


async def test_valid_key_reports_failure_if_the_session_write_is_lost(monkeypatch):
    """An unpersisted "success" isn't real (design spec section 3.6) -- if
    update_frame can't find the session it was just told exists (a race
    with e.g. a concurrent reset), the endpoint must not still claim
    success even though the external Render check passed."""
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    monkeypatch.setattr(
        session_store, "update_frame", lambda *a, **k: session_store.SessionNotFound()
    )

    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyValid(owner_name="Ada Lovelace")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post(
        "/api/render/validate-key", json={"api_key": SENTINEL_KEY},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_valid_key_reuses_an_existing_session_cookie(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()

    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyValid(owner_name="Ada Lovelace")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post(
        "/api/render/validate-key", json={"api_key": SENTINEL_KEY},
        cookies={"onboarding_session": session_id},
    )
    assert "set-cookie" not in resp.headers
    assert fake.read_frame(session_id, "render")["owner_name"] == "Ada Lovelace"


async def test_valid_key_resubmission_discards_the_previous_render_services_data(monkeypatch):
    """A "Change"-triggered resubmission of the Render key must not leave a
    previous service_id/service_url behind -- it may belong to a different
    Render account under the old key."""
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id, "render",
        {"api_key": "rnd_old", "owner_name": "Old Owner", "service_id": "srv-old", "service_url": "https://old.onrender.com"},
    )

    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyValid(owner_name="New Owner")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    await client.post(
        "/api/render/validate-key", json={"api_key": "rnd_new"},
        cookies={"onboarding_session": session_id},
    )
    assert fake.read_frame(session_id, "render") == {
        "api_key": "rnd_new", "owner_name": "New Owner",
    }


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


async def test_index_derives_its_base_url_in_the_browser():
    """No __ONBOARDING_BASE_URL__ token and no templated value: the page reads
    location.origin, which the browser already knows exactly. A hand-set env
    var was a second source of truth for the same fact and the two drifted --
    one trailing slash broke the Supabase OAuth leg (see ISSUES.md)."""
    client = await _client()
    resp = await client.get("/")
    assert "__ONBOARDING_BASE_URL__" not in resp.text
    assert "window.ONBOARDING_BASE_URL = location.origin;" in resp.text


async def test_validate_supabase_key_creates_no_session_by_itself(monkeypatch):
    """Unlike render's validate-key (the wizard's session entry point),
    Supabase's validate-key is frame 3 -- it requires an existing session
    and fails closed without one, same as every other non-entry-point
    endpoint."""
    client = await _client()
    resp = await client.post("/api/supabase/validate-key", json={"key": "sbp_a"})
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_validate_supabase_key_stores_the_key_and_returns_orgs(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()

    async def fake_validate(pat):
        assert pat == "sbp_SENTINEL"
        return supabase_client.SupabaseKeyValid(
            orgs=[supabase_client.SupabaseOrg(slug="org-one", name="Org One")]
        )

    monkeypatch.setattr(supabase_client, "validate_key", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/supabase/validate-key",
        json={"key": "sbp_SENTINEL"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {"valid": True, "orgs": [{"slug": "org-one", "name": "Org One"}]}
    stored = fake.read_frame(session_id, "supabase")
    assert stored["api_key"] == "sbp_SENTINEL"


async def test_validate_supabase_key_discards_a_previous_projects_data(monkeypatch):
    """A resubmitted key (via "Change") must not leave the OLD project's
    ref/database_url behind -- GET /api/session's completeness check keys
    off database_url's mere presence, so a stale one would report the
    frame as already-done for the wrong project on reload."""
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id, "supabase",
        {
            "api_key": "old-key", "name": "old-proj", "ref": "x" * 20,
            "db_pass": "old-pass", "database_url": "postgresql://old",
        },
    )

    async def fake_validate(pat):
        return supabase_client.SupabaseKeyValid(orgs=[])

    monkeypatch.setattr(supabase_client, "validate_key", fake_validate)
    client = await _client()
    await client.post(
        "/api/supabase/validate-key",
        json={"key": "new-key"},
        cookies={"onboarding_session": session_id},
    )
    stored = fake.read_frame(session_id, "supabase")
    assert "ref" not in stored
    assert "database_url" not in stored
    assert stored["api_key"] == "new-key"


async def test_validate_supabase_key_reports_invalid_key(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()

    async def fake_validate(pat):
        return supabase_client.SupabaseKeyInvalid(reason="invalid_key")

    monkeypatch.setattr(supabase_client, "validate_key", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/supabase/validate-key",
        json={"key": "bad"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {"valid": False, "reason": "invalid_key"}
    assert fake.read_frame(session_id, "supabase") is None


async def test_supabase_connect_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/supabase/connect", json={"name": "x"})
    assert resp.status_code == 404


async def test_supabase_oauth_callback_route_is_gone():
    client = await _client()
    resp = await client.get("/oauth/supabase/callback?code=x&state=y")
    assert resp.status_code == 404


async def test_supabase_list_organizations_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/supabase/list-organizations")
    assert resp.status_code == 404


async def test_create_project_generates_db_pass_server_side(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "supabase", {"api_key": "a"})
    captured = {}

    async def fake_create(access_token, organization_slug, name, db_pass):
        captured["args"] = (access_token, organization_slug, name, db_pass)
        return supabase_client.SupabaseProjectCreated(ref="x" * 20, status="INACTIVE")

    monkeypatch.setattr(supabase_client, "create_project", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/supabase/create-project",
        json={"organization_slug": "org-one", "name": "pr-review-bot"},
        cookies={"onboarding_session": session_id},
    )
    body = resp.json()
    assert body == {"valid": True, "ref": "x" * 20, "status": "INACTIVE", "name": "pr-review-bot"}
    assert "db_pass" not in body
    access_token, organization_slug, name, db_pass = captured["args"]
    assert (access_token, organization_slug, name) == ("a", "org-one", "pr-review-bot")
    assert db_pass  # generated, never supplied by the client
    stored = fake.read_frame(session_id, "supabase")
    assert stored["ref"] == "x" * 20
    assert stored["organization_slug"] == "org-one"
    assert stored["db_pass"] == db_pass


async def test_create_project_relays_the_rejection_message(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "supabase", {"api_key": "a"})

    async def fake_create(access_token, organization_slug, name, db_pass):
        return supabase_client.SupabaseProjectRejected(
            message="This organization already has the maximum number of free projects."
        )

    monkeypatch.setattr(supabase_client, "create_project", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/supabase/create-project",
        json={"organization_slug": "org-one", "name": "n"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {
        "valid": False,
        "reason": "project_creation_rejected",
        "message": "This organization already has the maximum number of free projects.",
    }


async def test_create_project_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post(
        "/api/supabase/create-project", json={"organization_slug": "org-one", "name": "n"}
    )
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_project_status_reads_from_session(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "supabase", {"api_key": "a", "ref": "x" * 20})

    async def fake_status(access_token, ref):
        assert (access_token, ref) == ("a", "x" * 20)
        return supabase_client.SupabaseProjectStatus(status="ACTIVE_HEALTHY")

    monkeypatch.setattr(supabase_client, "get_project_status", fake_status)
    client = await _client()
    resp = await client.post(
        "/api/supabase/project-status", cookies={"onboarding_session": session_id}
    )
    assert resp.json() == {"valid": True, "status": "ACTIVE_HEALTHY"}


async def test_project_status_reports_failure_reason(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "supabase", {"api_key": "a", "ref": "x" * 20})

    async def fake_status(access_token, ref):
        return supabase_client.SupabaseApiFailed(reason="unauthorized")

    monkeypatch.setattr(supabase_client, "get_project_status", fake_status)
    client = await _client()
    resp = await client.post(
        "/api/supabase/project-status", cookies={"onboarding_session": session_id}
    )
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_project_status_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post("/api/supabase/project-status")
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_connection_info_assembles_and_stores_the_database_url(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id, "supabase", {"api_key": "a", "ref": "x" * 20, "db_pass": "pw123"}
    )

    async def fake_info(access_token, ref, session_id):
        return supabase_client.SupabaseConnectionInfo(
            db_user="postgres.x",
            db_host="aws-0-us-east-1.pooler.supabase.com",
            db_port=5432,
            db_name="postgres",
        )

    monkeypatch.setattr(supabase_client, "get_connection_info", fake_info)
    client = await _client()
    resp = await client.post(
        "/api/supabase/connection-info", cookies={"onboarding_session": session_id}
    )
    body = resp.json()
    assert body == {"valid": True}
    assert "db_user" not in resp.text and "db_host" not in resp.text
    stored = fake.read_frame(session_id, "supabase")
    assert stored["database_url"] == (
        "postgresql://postgres.x:pw123@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
    )


async def test_connection_info_reports_failure_reason(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id, "supabase", {"api_key": "a", "ref": "x" * 20, "db_pass": "pw"}
    )

    async def fake_info(access_token, ref, session_id):
        return supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")

    monkeypatch.setattr(supabase_client, "get_connection_info", fake_info)
    client = await _client()
    resp = await client.post(
        "/api/supabase/connection-info", cookies={"onboarding_session": session_id}
    )
    assert resp.json() == {"valid": False, "reason": "pooler_config_unavailable"}


async def test_connection_info_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post("/api/supabase/connection-info")
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_supabase_exchange_oauth_code_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/supabase/exchange-oauth-code", json={})
    assert resp.status_code == 404


async def test_supabase_refresh_access_token_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/supabase/refresh-access-token", json={})
    assert resp.status_code == 404


async def test_supabase_push_render_var_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/supabase/push-render-var", json={})
    assert resp.status_code == 404


async def test_get_session_with_no_cookie_returns_empty_frames():
    client = await _client()
    resp = await client.get("/api/session")
    assert resp.status_code == 200
    assert resp.json() == {"frames": {}}


async def test_get_session_with_unknown_cookie_returns_empty_frames(monkeypatch):
    _use_fake_session_store(monkeypatch)
    client = await _client()
    resp = await client.get("/api/session", cookies={"onboarding_session": "bogus"})
    assert resp.json() == {"frames": {}}


async def test_get_session_reflects_render_key_display_but_never_the_credential(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": SENTINEL_KEY, "owner_name": "alice"})
    client = await _client()
    resp = await client.get("/api/session", cookies={"onboarding_session": session_id})
    body = resp.json()
    assert body == {
        "frames": {"render-key": {"complete": True, "display": {"owner_name": "alice"}}}
    }
    assert SENTINEL_KEY not in resp.text


async def test_get_session_reports_render_key_and_render_service_separately(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x", "owner_name": "alice"})
    client = await _client()
    resp = await client.get("/api/session", cookies={"onboarding_session": session_id})
    assert "render-service" not in resp.json()["frames"]

    fake.update_frame(session_id, "render", {"service_id": "srv-1", "service_url": "https://x.onrender.com"})
    resp = await client.get("/api/session", cookies={"onboarding_session": session_id})
    body = resp.json()["frames"]
    assert body["render-key"]["complete"] is True
    assert body["render-service"] == {
        "complete": True,
        "display": {"service_id": "srv-1", "service_url": "https://x.onrender.com"},
    }


async def test_get_session_reports_supabase_provisioning_once_project_created(monkeypatch):
    """ref alone (project created) is NOT complete -- database_url is what
    the final deploy step actually needs, and that's written later by
    connection-info. A session with ref but no database_url must report a
    resumable "provisioning" state, not complete, so a reload mid-wait
    doesn't unlock the next frame with nothing for bulk-push to find."""
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id, "supabase", {"api_key": "tok", "name": "myproj", "ref": "x" * 20}
    )
    client = await _client()
    resp = await client.get("/api/session", cookies={"onboarding_session": session_id})
    assert resp.json()["frames"]["supabase"] == {
        "complete": False,
        "provisioning": True,
        "display": {"ref": "x" * 20, "name": "myproj"},
    }


async def test_get_session_reports_supabase_complete_once_database_url_present(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id,
        "supabase",
        {
            "api_key": "tok",
            "name": "myproj",
            "ref": "x" * 20,
            "database_url": "postgresql://u:p@h:5432/d",
        },
    )
    client = await _client()
    resp = await client.get("/api/session", cookies={"onboarding_session": session_id})
    assert resp.json()["frames"]["supabase"] == {"complete": True, "display": {"name": "myproj"}}


async def test_reset_session_deletes_the_row_and_clears_the_cookie(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post("/api/session/reset", cookies={"onboarding_session": session_id})
    assert resp.status_code == 204
    assert fake.get_session(session_id) is None
    set_cookie = resp.headers.get("set-cookie", "")
    assert "onboarding_session=" in set_cookie
    assert 'Max-Age=0' in set_cookie or set_cookie.endswith('onboarding_session=""; Path=/')


async def test_reset_session_with_no_cookie_is_a_noop_204():
    client = await _client()
    resp = await client.post("/api/session/reset")
    assert resp.status_code == 204


async def test_index_csp_no_longer_needs_a_github_form_action():
    """No cross-origin form POST remains in this frame -- App creation is
    fully manual now."""
    client = await _client()
    resp = await client.get("/")
    csp = resp.headers["content-security-policy"]
    assert "form-action 'self';" in csp
    assert "github.com" not in csp


async def test_validate_app_returns_the_full_checklist(monkeypatch):
    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        assert (app_id, private_key_b64, expected_webhook_url) == (
            42, "cGVt", "https://my-service.onrender.com/webhook",
        )
        return github_client.AppValidated(
            permissions=[
                github_client.PermissionCheck(
                    name="contents", wanted="read", actual="read", ok=True
                ),
            ],
            events=[github_client.EventCheck(name="pull_request", ok=True)],
            installation=github_client.InstallationFound(
                installation_id=100, account_login="octocat", repo_scope="all"
            ),
            webhook=github_client.WebhookCheck(
                ok=True, actual_url="https://my-service.onrender.com/webhook"
            ),
        )

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42,
            "private_key_b64": "cGVt",
            "expected_webhook_url": "https://my-service.onrender.com/webhook",
            "webhook_secret": "s" * 20,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "valid": True,
        "all_ok": True,
        "permissions": [{"name": "contents", "wanted": "read", "actual": "read", "ok": True}],
        "events": [{"name": "pull_request", "ok": True}],
        "installation": {
            "status": "found", "installation_id": 100,
            "account_login": "octocat", "repo_scope": "all",
        },
        "webhook": {"ok": True, "actual_url": "https://my-service.onrender.com/webhook"},
    }


async def test_validate_app_persists_on_all_ok(monkeypatch):
    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        return github_client.AppValidated(
            permissions=[],
            events=[],
            installation=github_client.InstallationFound(
                installation_id=100, account_login="octocat", repo_scope="all"
            ),
            webhook=github_client.WebhookCheck(ok=True, actual_url="https://x.example/webhook"),
        )

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key_b64": "cGVt",
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
        cookies={"onboarding_session": session_id},
    )
    assert resp.json()["all_ok"] is True
    assert fake.read_frame(session_id, "github_app") == {
        "app_id": 42, "private_key_b64": "cGVt",
        "webhook_secret": "s" * 20, "installation_id": 100,
    }


async def test_validate_app_does_not_persist_when_not_all_ok(monkeypatch):
    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        return github_client.AppValidated(
            permissions=[],
            events=[],
            installation=github_client.InstallationNotFound(),
            webhook=github_client.WebhookCheck(ok=False, actual_url=""),
        )

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key_b64": "cGVt",
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
        cookies={"onboarding_session": session_id},
    )
    assert fake.read_frame(session_id, "github_app") is None


async def test_validate_app_all_ok_is_false_when_anything_fails(monkeypatch):
    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        return github_client.AppValidated(
            permissions=[
                github_client.PermissionCheck(name="issues", wanted="write", actual=None, ok=False),
            ],
            events=[github_client.EventCheck(name="pull_request", ok=True)],
            installation=github_client.InstallationNotFound(),
            webhook=github_client.WebhookCheck(ok=False, actual_url=""),
        )

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key_b64": "cGVt",
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
    )
    body = resp.json()
    assert body["valid"] is True
    assert body["all_ok"] is False
    assert body["installation"] == {"status": "none"}


async def test_validate_app_reports_multiple_installations(monkeypatch):
    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        return github_client.AppValidated(
            permissions=[],
            events=[],
            installation=github_client.MultipleInstallationsFound(
                account_logins=["octocat", "monalisa"]
            ),
            webhook=github_client.WebhookCheck(ok=True, actual_url="https://x.example/webhook"),
        )

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key_b64": "cGVt",
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
    )
    body = resp.json()
    assert body["installation"] == {"status": "multiple", "account_logins": ["octocat", "monalisa"]}
    assert body["all_ok"] is False


async def test_validate_app_reports_credentials_failure_reason(monkeypatch):
    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        return github_client.AppCredentialsInvalid(reason="unauthorized")

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key_b64": "cGVt",
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
    )
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_validate_app_rejects_a_non_positive_app_id():
    client = await _client()
    for bad in (0, -1):
        resp = await client.post(
            "/api/github/validate-app",
            json={
                "app_id": bad, "private_key_b64": "cGVt",
                "expected_webhook_url": "https://x.example/webhook",
                "webhook_secret": "s" * 20,
            },
        )
        assert resp.status_code == 422


async def test_validate_app_rejects_a_malformed_webhook_url():
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42,
            "private_key_b64": "cGVt",
            "expected_webhook_url": "not-a-url",
            "webhook_secret": "s" * 20,
        },
    )
    assert resp.status_code == 422


async def test_validate_app_validation_error_never_echoes_the_private_key():
    """Same guard as every other endpoint carrying a private key: FastAPI's
    default 422 body echoes rejected input verbatim; only main.py's app-wide
    RequestValidationError handler stops that."""
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key": SENTINEL_PRIVATE_KEY,
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
    )
    assert resp.status_code == 422
    assert SENTINEL_PRIVATE_KEY not in resp.text
    assert "SENTINEL_DO_NOT_ECHO" not in resp.text
    assert "input" not in resp.text


async def test_validate_app_response_never_echoes_the_private_key(monkeypatch):
    sentinel_key_b64 = "U0VOVElORUxfUFJJVkFURV9LRVk="

    async def fake_validate(app_id, private_key_b64, expected_webhook_url):
        return github_client.AppCredentialsInvalid(reason="invalid_key")

    monkeypatch.setattr(github_client, "validate_app", fake_validate)
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={
            "app_id": 42, "private_key_b64": sentinel_key_b64,
            "expected_webhook_url": "https://x.example/webhook",
            "webhook_secret": "s" * 20,
        },
    )
    assert sentinel_key_b64 not in resp.text


async def test_set_webhook_url_endpoint_is_gone():
    """Removed along with the placeholder-then-patch flow: the App is created
    already pointing at its real webhook URL. An endpoint that accepts an
    App private key is not something to leave mounted with no caller."""
    client = await _client()
    resp = await client.post(
        "/api/github/set-webhook-url",
        json={"app_id": 123, "private_key_b64": "cGVt", "url": "https://x.onrender.com/webhook"},
    )
    assert resp.status_code == 404


async def test_index_never_templates_the_supabase_oauth_client_id():
    """No operator-level Supabase secret exists at all anymore -- nothing
    to template."""
    client = await _client()
    resp = await client.get("/")
    assert "SUPABASE_OAUTH_CLIENT_ID" not in resp.text
    assert "__SUPABASE_OAUTH_CLIENT_ID__" not in resp.text


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
        return llm_client.VertexModelsListed(
            project_id="sentinel-project", models=["gemini-2.5-flash"]
        )

    monkeypatch.setattr(llm_client, "list_vertex_models", fake_list)
    client = await _client()
    resp = await client.post(
        "/api/llm/vertex/list-models", json={"service_account_key_b64": "SENTINEL_B64"}
    )
    assert resp.json() == {
        "valid": True,
        "project_id": "sentinel-project",
        "models": ["gemini-2.5-flash"],
    }


async def test_vertex_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(service_account_key_b64):
        return llm_client.LlmApiFailed(reason="invalid_service_account_json")

    monkeypatch.setattr(llm_client, "list_vertex_models", fake_list)
    client = await _client()
    resp = await client.post(
        "/api/llm/vertex/list-models", json={"service_account_key_b64": "not-json"}
    )
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
        return uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=42)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={
            "api_key": SENTINEL_KEY,
            "render_service_url": "https://sentinel-service.onrender.com",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "created": True, "monitor_id": 42}


async def test_uptimerobot_create_monitor_persists_to_session(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=99)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://x.onrender.com"},
        cookies={"onboarding_session": session_id},
    )
    assert fake.read_frame(session_id, "uptime_pinger") == {
        "api_key": SENTINEL_KEY, "monitor_id": 99,
    }


async def test_uptimerobot_monitor_reused_reports_created_false(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotMonitorResult(created=False, monitor_id=42)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={
            "api_key": SENTINEL_KEY,
            "render_service_url": "https://sentinel-service.onrender.com",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "created": False, "monitor_id": 42}


async def test_uptimerobot_failure_reports_the_reason(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={
            "api_key": SENTINEL_KEY,
            "render_service_url": "https://sentinel-service.onrender.com",
        },
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
        json={
            "api_key": SENTINEL_KEY,
            "render_service_url": "https://sentinel-service.onrender.com",
        },
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
        return uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=42)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "  https://s.onrender.com \n"},
    )
    assert resp.status_code == 200
    assert seen["url"] == "https://s.onrender.com"


async def test_uptimerobot_delete_monitor_reports_valid_true(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "uptime_pinger", {"api_key": SENTINEL_KEY, "monitor_id": 42})

    async def fake_delete(api_key, monitor_id):
        assert api_key == SENTINEL_KEY
        assert monitor_id == 42
        return uptimerobot_client.UptimeRobotMonitorDeleted()

    monkeypatch.setattr(uptimerobot_client, "delete_monitor", fake_delete)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/delete-monitor", cookies={"onboarding_session": session_id}
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


async def test_uptimerobot_delete_monitor_failure_reports_the_reason(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "uptime_pinger", {"api_key": SENTINEL_KEY, "monitor_id": 42})

    async def fake_delete(api_key, monitor_id):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "delete_monitor", fake_delete)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/delete-monitor", cookies={"onboarding_session": session_id}
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_uptimerobot_delete_monitor_never_echoes_the_key(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "uptime_pinger", {"api_key": SENTINEL_KEY, "monitor_id": 42})

    async def fake_delete(api_key, monitor_id):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "delete_monitor", fake_delete)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/delete-monitor", cookies={"onboarding_session": session_id}
    )
    assert SENTINEL_KEY not in resp.text


async def test_uptimerobot_delete_monitor_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post("/api/uptimerobot/delete-monitor")
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_create_service_endpoint_returns_id_and_url(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x"})

    async def fake_create_service(api_key, repo_url, name):
        assert api_key == "rnd_x"
        return render_client.RenderServiceCreated(
            service_id="srv-1", service_url="https://x.onrender.com"
        )

    monkeypatch.setattr(render_client, "create_service", fake_create_service)
    client = await _client()
    resp = await client.post(
        "/api/render/create-service",
        json={"repo_url": "https://github.com/a/b", "name": "n"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "valid": True,
        "service_id": "srv-1",
        "service_url": "https://x.onrender.com",
    }


async def test_create_service_endpoint_relays_rejection_message(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x"})

    async def fake_create_service(api_key, repo_url, name):
        return render_client.RenderServiceCreationFailed(
            reason="request_rejected", message="name taken"
        )

    monkeypatch.setattr(render_client, "create_service", fake_create_service)
    client = await _client()
    resp = await client.post(
        "/api/render/create-service",
        json={"repo_url": "https://github.com/a/b", "name": "n"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {"valid": False, "reason": "request_rejected", "message": "name taken"}


async def test_create_service_endpoint_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post(
        "/api/render/create-service", json={"repo_url": "https://github.com/a/b", "name": "n"}
    )
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_github_push_render_vars_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/github/push-render-vars", json={})
    assert resp.status_code == 404


async def test_confirm_llm_provider_persists_to_session(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post(
        "/api/llm/confirm",
        json={
            "provider": "gemini",
            "credential_value": "AIzaSy...",
            "model": "gemini-flash-latest",
        },
        cookies={"onboarding_session": session_id},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert fake.read_frame(session_id, "llm_provider") == {
        "provider": "gemini",
        "credential_value": "AIzaSy...",
        "model": "gemini-flash-latest",
    }


async def test_confirm_llm_provider_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post(
        "/api/llm/confirm",
        json={"provider": "gemini", "credential_value": "x", "model": "m"},
    )
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_confirm_llm_provider_rejects_unknown_provider(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post(
        "/api/llm/confirm",
        json={"provider": "openai", "credential_value": "x", "model": "y"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.status_code == 422


async def test_llm_push_render_vars_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/llm/push-render-vars", json={})
    assert resp.status_code == 404


async def test_confirm_dashboard_auth_persists_to_session(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post(
        "/api/dashboard-auth/confirm",
        json={
            "username": "operator",
            "password": "correct-horse-battery",
            "session_secret": "s" * 43,
        },
        cookies={"onboarding_session": session_id},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert fake.read_frame(session_id, "dashboard_auth") == {
        "username": "operator",
        "password": "correct-horse-battery",
        "session_secret": "s" * 43,
    }


async def test_confirm_dashboard_auth_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post(
        "/api/dashboard-auth/confirm",
        json={
            "username": "operator",
            "password": "correct-horse-battery",
            "session_secret": "s" * 43,
        },
    )
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_confirm_dashboard_auth_rejects_short_password(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post(
        "/api/dashboard-auth/confirm",
        json={"username": "operator", "password": "short1", "session_secret": "s" * 43},
        cookies={"onboarding_session": session_id},
    )
    assert resp.status_code == 422


async def test_confirm_dashboard_auth_rejects_short_session_secret(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    client = await _client()
    resp = await client.post(
        "/api/dashboard-auth/confirm",
        json={
            "username": "operator",
            "password": "correct-horse-battery",
            "session_secret": "tooshort",
        },
        cookies={"onboarding_session": session_id},
    )
    assert resp.status_code == 422


async def test_dashboard_auth_push_render_vars_endpoint_is_gone():
    client = await _client()
    resp = await client.post("/api/dashboard-auth/push-render-vars", json={})
    assert resp.status_code == 404


# test_push_render_vars_partial_failure_reports_pushed_keys used to exercise
# _push_result()'s partial-failure shape via the now-removed per-frame
# push-render-vars endpoints. Re-added against /api/render/bulk-push-env-vars
# once that endpoint exists (Task 11) -- _push_result() itself is unchanged.


async def test_trigger_deploy_endpoint(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})

    async def fake_trigger_deploy(api_key, service_id):
        assert (api_key, service_id) == ("rnd_x", "srv-1")
        return render_client.RenderDeployTriggered(deploy_id="dep-1")

    monkeypatch.setattr(render_client, "trigger_deploy", fake_trigger_deploy)
    client = await _client()
    resp = await client.post(
        "/api/render/trigger-deploy", cookies={"onboarding_session": session_id}
    )
    assert resp.json() == {"valid": True, "deploy_id": "dep-1"}
    assert fake.read_frame(session_id, "render")["pending_deploy_id"] == "dep-1"


async def test_trigger_deploy_endpoint_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post("/api/render/trigger-deploy")
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_deploy_status_endpoint(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(
        session_id, "render",
        {"api_key": "rnd_x", "service_id": "srv-1", "pending_deploy_id": "dep-1"},
    )

    async def fake_poll_deploy_status(api_key, service_id, deploy_id):
        assert (api_key, service_id, deploy_id) == ("rnd_x", "srv-1", "dep-1")
        return render_client.RenderDeployStatus(status="live")

    monkeypatch.setattr(render_client, "poll_deploy_status", fake_poll_deploy_status)
    client = await _client()
    resp = await client.post(
        "/api/render/deploy-status", cookies={"onboarding_session": session_id}
    )
    assert resp.json() == {"valid": True, "status": "live"}


async def test_deploy_status_endpoint_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post("/api/render/deploy-status")
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_bulk_push_assembles_every_frame_into_one_push_call(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})
    fake.update_frame(
        session_id, "github_app",
        {"app_id": 1, "private_key_b64": "pk", "webhook_secret": "wh", "installation_id": 42},
    )
    fake.update_frame(session_id, "supabase", {"database_url": "postgresql://x"})
    fake.update_frame(
        session_id, "llm_provider",
        {"provider": "gemini", "credential_value": "AIza-x", "model": "gemini-flash-latest"},
    )
    fake.update_frame(
        session_id, "dashboard_auth",
        {"username": "admin", "password": "pw123456", "session_secret": "s" * 32},
    )
    captured = {}

    async def fake_push_env_vars(api_key, service_id, values):
        captured["values"] = values
        return render_client.RenderEnvVarsPushed(pushed=list(values.keys()))

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    client = await _client()
    resp = await client.post(
        "/api/render/bulk-push-env-vars", cookies={"onboarding_session": session_id}
    )
    assert resp.json()["valid"] is True
    assert captured["values"] == {
        "RENDER_API_KEY": "rnd_x",
        "GITHUB_APP_ID": "1",
        "GITHUB_APP_PRIVATE_KEY": "pk",
        "GITHUB_WEBHOOK_SECRET": "wh",
        "GITHUB_APP_INSTALLATION_ID": "42",
        "DATABASE_URL": "postgresql://x",
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "AIza-x",
        "LLM_MODEL": "gemini-flash-latest",
        "DASHBOARD_USERNAME": "admin",
        "DASHBOARD_PASSWORD": "pw123456",
        "DASHBOARD_SESSION_SECRET": "s" * 32,
        **router._GENERIC_OPERATIONAL_ENV_DEFAULTS,
    }


async def test_bulk_push_omits_a_frame_that_was_never_completed(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})
    captured = {}

    async def fake_push_env_vars(api_key, service_id, values):
        captured["values"] = values
        return render_client.RenderEnvVarsPushed(pushed=list(values.keys()))

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    client = await _client()
    await client.post("/api/render/bulk-push-env-vars", cookies={"onboarding_session": session_id})
    # The generic operational-tuning defaults are always included, unlike
    # every other key (which is gated on its frame being complete).
    # RENDER_API_KEY is read from the render frame itself, which this
    # endpoint's own guard clause already requires to be present -- so it
    # is never actually omitted the way other frames' keys are.
    assert captured["values"] == {
        "RENDER_API_KEY": "rnd_x",
        **router._GENERIC_OPERATIONAL_ENV_DEFAULTS,
    }


async def test_bulk_push_with_no_session_fails_closed():
    client = await _client()
    resp = await client.post("/api/render/bulk-push-env-vars")
    assert resp.json() == {"valid": False, "reason": "no_session"}


async def test_bulk_push_partial_failure_reports_pushed_keys(monkeypatch):
    fake = _use_fake_session_store(monkeypatch)
    session_id = fake.create_session()
    fake.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})
    fake.update_frame(
        session_id, "github_app",
        {"app_id": 1, "private_key_b64": "pk", "webhook_secret": "wh", "installation_id": 42},
    )

    async def fake_push_env_vars(api_key, service_id, values):
        return render_client.RenderEnvVarsPushFailed(reason="invalid_key", pushed=["GITHUB_APP_ID"])

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    client = await _client()
    resp = await client.post(
        "/api/render/bulk-push-env-vars", cookies={"onboarding_session": session_id}
    )
    assert resp.json() == {"valid": False, "reason": "invalid_key", "pushed": ["GITHUB_APP_ID"]}
