"""Tests for dashboard/environment.py: the Environment tab's Render env-var
and runtime_config endpoints. Route-level, using the same authenticated
AsyncClient pattern dashboard/tests/test_dashboard_page.py already uses."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from bot import render_client
from bot.main import app
from bot.queue import store
from dashboard import auth


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    )


async def _unauthenticated_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_unauthenticated_get_render_env_vars_is_rejected():
    client = await _unauthenticated_client()
    resp = await client.get("/api/environment/render")
    assert resp.status_code == 401


async def test_get_render_env_vars_returns_key_and_value(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(render_client, "env_vars", lambda service_id: {"FOO": "bar"})
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.status_code == 200
    assert resp.json() == {"vars": [{"key": "FOO", "value": "bar", "protected": False}]}


async def test_get_render_env_vars_marks_protected_keys(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    monkeypatch.setattr(
        render_client, "env_vars", lambda service_id: {"DATABASE_URL": "postgres://x"}
    )
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.json() == {
        "vars": [{"key": "DATABASE_URL", "value": "postgres://x", "protected": True}]
    }


async def test_patch_render_env_vars_applies_sets_and_fires_one_deploy(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    pushed = []
    monkeypatch.setattr(
        render_client, "push_env_var",
        lambda service_id, key, value: pushed.append((key, value)),
    )
    deploys = []
    monkeypatch.setattr(
        render_client, "trigger_deploy",
        lambda service_id: deploys.append(service_id) or "dep-1",
    )
    client = await _client()
    resp = await client.patch(
        "/api/environment/render", json={"sets": {"FOO": "bar"}, "deletes": []}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == ["FOO"]
    assert body["failed"] == []
    assert body["deploy_id"] == "dep-1"
    assert pushed == [("FOO", "bar")]
    assert deploys == ["srv-1"]


async def test_patch_render_env_vars_rejects_a_protected_delete_without_touching_render(
    monkeypatch,
):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _boom(service_id, key):
        raise AssertionError("delete_env_var must not be called for a protected key")

    monkeypatch.setattr(render_client, "delete_env_var", _boom)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.patch(
        "/api/environment/render", json={"sets": {}, "deletes": ["DATABASE_URL"]}
    )
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "DATABASE_URL", "error": "protected"}]
    # No successful write happened, so no deploy is triggered.
    assert body["deploy_id"] is None


async def test_patch_render_env_vars_a_protected_delete_does_not_block_other_keys(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")
    deleted = []
    monkeypatch.setattr(
        render_client, "delete_env_var",
        lambda service_id, key: deleted.append(key),
    )
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.patch(
        "/api/environment/render",
        json={"sets": {}, "deletes": ["DATABASE_URL", "SOME_OTHER_KEY"]},
    )
    body = resp.json()
    assert body["applied"] == ["SOME_OTHER_KEY"]
    assert {"key": "DATABASE_URL", "error": "protected"} in body["failed"]
    assert deleted == ["SOME_OTHER_KEY"]


async def test_patch_render_env_vars_stops_at_the_first_render_failure(monkeypatch):
    monkeypatch.setattr(render_client, "find_service_id", lambda: "srv-1")

    def _push(service_id, key, value):
        if key == "SECOND":
            raise RuntimeError("boom")

    monkeypatch.setattr(render_client, "push_env_var", _push)
    monkeypatch.setattr(render_client, "trigger_deploy", lambda service_id: "dep-1")
    client = await _client()
    resp = await client.patch(
        "/api/environment/render",
        json={"sets": {"FIRST": "a", "SECOND": "b", "THIRD": "c"}, "deletes": []},
    )
    body = resp.json()
    assert body["applied"] == ["FIRST"]
    assert body["failed"] == [{"key": "SECOND", "error": "RuntimeError"}]
    assert "THIRD" not in body["applied"]


async def test_get_environment_config_reflects_current_overrides(db):
    store.set_provider_override("groq", "2026-01-01T00:00:00+00:00")
    client = await _client()
    resp = await client.get("/api/environment/config")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "groq"


async def test_patch_environment_config_sets_provider_override(db):
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"provider": "groq"})
    assert resp.status_code == 200
    assert resp.json() == {"applied": ["provider"], "failed": []}
    assert store.get_provider_override() == "groq"


async def test_patch_environment_config_partial_cooldown_merges_with_current_values(db):
    store.set_cooldown_override(1.0, 2.0, 3.0, "2026-01-01T00:00:00+00:00")
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"cooldown_base_seconds": 9.0})
    assert resp.status_code == 200
    assert store.get_cooldown_overrides() == (9.0, 2.0, 3.0)


async def test_patch_environment_config_rejects_an_unknown_provider_in_key_index():
    client = await _client()
    resp = await client.patch(
        "/api/environment/config", json={"key_index": {"not-a-provider": 1}}
    )
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "key_index.not-a-provider", "error": "unknown_provider"}]


async def test_patch_environment_config_rejects_an_unknown_top_level_provider():
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"provider": "not-a-provider"})
    body = resp.json()
    assert body["applied"] == []
    assert body["failed"] == [{"key": "provider", "error": "unknown_provider"}]
