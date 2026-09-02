# Dashboard Environment Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "Environment" tab to the dashboard that lets an authenticated operator view/edit/delete Render env vars (masked by default, toggle-revealable) and view/edit `runtime_config` overrides, from the browser instead of the CLI.

**Architecture:** `bot/scripts/_render.py` moves to `bot/render_client.py` (a production-importable module, mirroring `onboarding/render_client.py`) and gains `push_env_var`/`delete_env_var`/`trigger_deploy` plus a `PROTECTED_ENV_KEYS` guard. A new `dashboard/environment.py` router exposes `GET`/`PATCH /api/environment/render` and `GET`/`PATCH /api/environment/config`, backed by `bot.render_client` and `bot.queue.store`'s existing `get_*`/`set_*` functions. `dashboard/static/dashboard.html` gains a client-side Status/Environment nav toggle. Onboarding's final bulk env-var push gains one more key (`RENDER_API_KEY`) so the deployed service can use this feature at all.

**Tech Stack:** FastAPI, httpx, pydantic, respx (tests), vanilla JS (existing dashboard.html has no framework).

**Spec:** `docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md`

## Global Constraints

- Never use Render's bulk env-var endpoint (`PUT /v1/services/{id}/env-vars`, plural) — only single-key `PUT`/`DELETE /v1/services/{id}/env-vars/{key}`.
- Never log a secret value — key names and lengths only (matches `bot.render_client.env_vars()`'s existing documented contract).
- `PROTECTED_ENV_KEYS` = `DATABASE_URL`, `RENDER_API_KEY`, `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `DASHBOARD_SESSION_SECRET`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_INSTALLATION_ID` — never deletable via this feature, client or server side.
- Redeploy triggers are fire-and-forget (no blocking poll from inside the request handler that triggers them).
- Partial failure is always visible in every response (`applied`/`failed` lists), never a single opaque error.
- Run `uv run pytest -v` and `uv run ruff check .` before each commit; fix whatever either finds.

---

### Task 1: Move `bot/scripts/_render.py` to `bot/render_client.py`

Pure move, no behavior change. Every existing caller (`deploy.py`, `_override.py`, `reset_queue.py`) imports the new module under the same local name `_render` via an alias, so their `_render.X` call sites and every `monkeypatch.setattr(deploy._render, ...)` in the existing test suite keep working unchanged.

**Files:**
- Create: `bot/render_client.py`
- Delete: `bot/scripts/_render.py`
- Modify: `bot/scripts/deploy.py:37` (import line)
- Modify: `bot/scripts/_override.py:26` (import line)
- Modify: `bot/scripts/reset_queue.py:25` (import line)
- Modify: `bot/tests/test_render_client.py` (import line only — this file already exists, testing the old module; keep its existing tests, just point them at the new one)

**Interfaces:**
- Produces: `bot.render_client.RENDER_API: str`, `bot.render_client.HTTP_TIMEOUT: float`, `bot.render_client.headers() -> dict[str, str]`, `bot.render_client.unwrap(item: dict, key: str) -> dict`, `bot.render_client.find_service_id() -> str | None`, `bot.render_client.env_vars(service_id: str) -> dict[str, str]` — identical signatures to the old `bot.scripts._render` module.

- [ ] **Step 1: Create `bot/render_client.py` with the moved content**

```python
"""Shared Render API access. Used by:
- bot/scripts/deploy.py, bot/scripts/set_override.py (via bot/scripts/_override.py),
  bot/scripts/reset_queue.py -- as CLI/operator support code.
- dashboard/environment.py -- as production runtime code (the dashboard's
  Environment tab). This is why this module lives in bot/, not
  bot/scripts/: bot/scripts/ is operator-CLI-only, and dashboard/environment.py
  must not import from it. See docs/superpowers/specs/
  2026-09-02-dashboard-environment-tab-design.md.

Consolidates what was previously duplicated Render-fetch logic across
scripts; see docs/superpowers/specs/2026-08-10-render-access-consolidation-design.md.
"""

from __future__ import annotations

import httpx

from bot.config import settings

RENDER_API = "https://api.render.com/v1"
HTTP_TIMEOUT = 10.0
_ENV_VARS_PAGE_LIMIT = 100


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def find_service_id() -> str | None:
    resp = httpx.get(f"{RENDER_API}/services", headers=headers(), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    for item in resp.json():
        service = unwrap(item, "service")
        if service.get("name") == settings.render_service_name:
            return service.get("id")
    return None


def env_vars(service_id: str) -> dict[str, str]:
    """The service's live env-vars, key -> value.

    Callers must reduce a returned value to a boolean or an equality result
    immediately -- never store it beyond that computation, print it, or pass
    it to anything that might log it -- UNLESS the caller is
    dashboard/environment.py's GET /api/environment/render, the one
    documented, scoped exception to that rule (root CLAUDE.md's Secret
    handling section). See CLAUDE.md's "no secret is ever logged" and
    docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md
    section 6.

    Render paginates this endpoint (cursor-based, each item carries its own
    "cursor" field) -- a service with more vars than one page silently
    dropped everything past the first page here until this loop was added,
    which made every caller blind to any var that happened to land on page
    2+. Confirmed live: this project's Render service carries 29 vars
    against a 20-per-page default, with DATABASE_URL and
    GCP_SERVICE_ACCOUNT_KEY both on page 2.
    """
    current: dict[str, str] = {}
    params: dict[str, int | str] = {"limit": _ENV_VARS_PAGE_LIMIT}
    while True:
        resp = httpx.get(
            f"{RENDER_API}/services/{service_id}/env-vars",
            headers=headers(),
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json()
        for item in page:
            env_var = unwrap(item, "envVar")
            current[env_var.get("key")] = env_var.get("value")
        if len(page) < _ENV_VARS_PAGE_LIMIT:
            return current
        params = {"limit": _ENV_VARS_PAGE_LIMIT, "cursor": page[-1]["cursor"]}
```

- [ ] **Step 2: Delete the old module**

```bash
git rm bot/scripts/_render.py
```

- [ ] **Step 3: Update the three importers to alias-import the new module**

In `bot/scripts/deploy.py:37`, change:
```python
from bot.scripts import _override, _render
```
to:
```python
from bot import render_client as _render
from bot.scripts import _override
```

In `bot/scripts/_override.py:26`, change:
```python
from bot.scripts import _render
```
to:
```python
from bot import render_client as _render
```

In `bot/scripts/reset_queue.py:25`, change:
```python
from bot.scripts import _render
```
to:
```python
from bot import render_client as _render
```

- [ ] **Step 4: Update `bot/tests/test_render_client.py`'s import**

Change:
```python
from bot.scripts import _render
```
to:
```python
from bot import render_client as _render
```
(Its docstring at the top of the file should also be updated: replace "Direct unit tests for scripts/_render.py, the shared Render API access module used by scripts/deploy.py directly, and by scripts/set_override.py via scripts/_override.py." with "Direct unit tests for bot/render_client.py, the shared Render API access module used by bot/scripts/deploy.py, bot/scripts/set_override.py (via bot/scripts/_override.py), bot/scripts/reset_queue.py, and dashboard/environment.py.")

- [ ] **Step 5: Run the full suite to confirm nothing broke**

Run: `uv run pytest -v`
Expected: PASS, same pass count as before this task (this is a pure rename — `bot/tests/test_deploy_script.py`'s ~15 `monkeypatch.setattr(deploy._render, ...)` calls keep working because `deploy._render` is now just an alias name bound to the `bot.render_client` module object, which the monkeypatches still patch attributes on identically).

- [ ] **Step 6: Run ruff**

Run: `uv run ruff check .`
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add bot/render_client.py bot/scripts/deploy.py bot/scripts/_override.py bot/scripts/reset_queue.py bot/tests/test_render_client.py
git commit -m "refactor: move bot/scripts/_render.py to bot/render_client.py

Production runtime code (the upcoming dashboard Environment tab) needs
this module too, and bot/scripts/ is operator-CLI-only. Every existing
caller alias-imports the new module as _render, so no call site or
test monkeypatch needed to change beyond the import line.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```

---

### Task 2: Add write operations to `bot/render_client.py`

**Files:**
- Modify: `bot/render_client.py`
- Test: `bot/tests/test_render_client.py`

**Interfaces:**
- Consumes: `bot.render_client.RENDER_API`, `headers()` (from Task 1)
- Produces: `bot.render_client.PROTECTED_ENV_KEYS: frozenset[str]`, `bot.render_client.ProtectedEnvKeyError(Exception)`, `bot.render_client.push_env_var(service_id: str, key: str, value: str) -> None` (raises `httpx.HTTPStatusError` on failure), `bot.render_client.delete_env_var(service_id: str, key: str) -> None` (raises `ProtectedEnvKeyError` for a protected key, `httpx.HTTPStatusError` on a Render failure), `bot.render_client.trigger_deploy(service_id: str) -> str` (returns the new deploy's id, never blocks/polls)

- [ ] **Step 1: Write the failing tests**

Append to `bot/tests/test_render_client.py`:

```python
def test_push_env_var_puts_the_single_key_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        route = respx.put(f"{RENDER_SERVICES}/srv-1/env-vars/FOO").mock(
            return_value=httpx.Response(200, json={"envVar": {"key": "FOO", "value": "bar"}})
        )
        _render.push_env_var("srv-1", "FOO", "bar")
    assert route.called
    assert route.calls[0].request.content == b'{"value": "bar"}'


def test_push_env_var_raises_on_a_render_error(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.put(f"{RENDER_SERVICES}/srv-1/env-vars/FOO").mock(
            return_value=httpx.Response(400, json={"message": "bad"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            _render.push_env_var("srv-1", "FOO", "bar")


def test_delete_env_var_deletes_the_single_key_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        route = respx.delete(f"{RENDER_SERVICES}/srv-1/env-vars/FOO").mock(
            return_value=httpx.Response(204)
        )
        _render.delete_env_var("srv-1", "FOO")
    assert route.called


def test_delete_env_var_raises_on_a_render_error(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.delete(f"{RENDER_SERVICES}/srv-1/env-vars/FOO").mock(
            return_value=httpx.Response(500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            _render.delete_env_var("srv-1", "FOO")


def test_delete_env_var_refuses_every_protected_key_without_calling_render(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    for key in _render.PROTECTED_ENV_KEYS:
        with respx.mock:
            # No route registered at all -- respx raises if any HTTP call
            # is attempted, proving the function never reaches out to Render.
            with pytest.raises(_render.ProtectedEnvKeyError):
                _render.delete_env_var("srv-1", key)


def test_trigger_deploy_returns_the_deploy_id_without_polling(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1", "status": "queued"}})
        )
        deploy_id = _render.trigger_deploy("srv-1")
    assert deploy_id == "dep-1"
```

Also add `import pytest` to the top of `bot/tests/test_render_client.py` if not already present (check first — it currently only imports `httpx`, `respx`, `settings`, `_render`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest bot/tests/test_render_client.py -v`
Expected: FAIL — `AttributeError: module 'bot.render_client' has no attribute 'push_env_var'` (and similarly for the other new names).

- [ ] **Step 3: Implement the new functions in `bot/render_client.py`**

Append to `bot/render_client.py`:

```python
# Every var bot/main.py's lifespan() either explicitly refuses to boot
# without, or implicitly hard-depends on (DATABASE_URL for store.init_pool(),
# GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY for the installation-id verification
# call), plus RENDER_API_KEY itself -- deleting it would strand this feature's
# own ability to fix anything else. Never deletable via push_env_var/
# delete_env_var's caller, dashboard/environment.py -- see
# docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md.
PROTECTED_ENV_KEYS = frozenset(
    {
        "DATABASE_URL",
        "RENDER_API_KEY",
        "DASHBOARD_USERNAME",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_SESSION_SECRET",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
    }
)


class ProtectedEnvKeyError(Exception):
    """Raised by delete_env_var() for a PROTECTED_ENV_KEYS member -- the
    caller (dashboard/environment.py) reports this as a per-key "protected"
    failure rather than attempting the delete."""


def push_env_var(service_id: str, key: str, value: str) -> None:
    """Single-key PUT -- never the bulk endpoint (PUT /env-vars, plural,
    silently replaces the whole list). Raises on failure; the caller decides
    how to report it -- see dashboard/environment.py's per-key loop."""
    resp = httpx.put(
        f"{RENDER_API}/services/{service_id}/env-vars/{key}",
        headers=headers(),
        json={"value": value},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def delete_env_var(service_id: str, key: str) -> None:
    """Single-key DELETE. Refuses outright for a PROTECTED_ENV_KEYS member --
    never even issues the HTTP request."""
    if key in PROTECTED_ENV_KEYS:
        raise ProtectedEnvKeyError(key)
    resp = httpx.delete(
        f"{RENDER_API}/services/{service_id}/env-vars/{key}",
        headers=headers(),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def trigger_deploy(service_id: str) -> str:
    """Fire-and-forget: POST the deploy trigger and return its id
    immediately -- no polling. Unlike bot/scripts/deploy.py's own
    _trigger_and_wait (which blocks until "live" for the CLI's benefit), a
    caller running inside the very service being redeployed cannot safely
    block on that -- the container may be torn down mid-poll once the new
    one passes its health check."""
    resp = httpx.post(
        f"{RENDER_API}/services/{service_id}/deploys",
        headers=headers(),
        json={},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return unwrap(resp.json(), "deploy")["id"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest bot/tests/test_render_client.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Run the full suite and ruff**

Run: `uv run pytest -v && uv run ruff check .`
Expected: PASS, no errors.

- [ ] **Step 6: Commit**

```bash
git add bot/render_client.py bot/tests/test_render_client.py
git commit -m "feat: add push/delete/trigger-deploy to bot/render_client.py

Single-key-only writes (never the bulk endpoint), a fire-and-forget
deploy trigger, and a PROTECTED_ENV_KEYS guard that refuses to delete
any var the running service hard-depends on to boot or to reach
Render at all. Backs the dashboard Environment tab (next tasks).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```

---

### Task 3: `dashboard/environment.py` — Render env-var and `runtime_config` endpoints

**Files:**
- Create: `dashboard/environment.py`
- Modify: `bot/main.py` (mount the new router)
- Modify: `dashboard/CLAUDE.md` (document the new write path)
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `bot.render_client.{find_service_id, env_vars, push_env_var, delete_env_var, trigger_deploy, PROTECTED_ENV_KEYS}` (Tasks 1-2); `bot.queue.store.{get_provider_override, set_provider_override, get_cooldown_overrides, set_cooldown_override, get_key_index_override, set_key_index_override, get_all_key_index_overrides, get_model_override, set_model_override, get_all_model_overrides, get_usage_cap_overrides, set_usage_cap_override, get_review_draft_override, set_review_draft_override}` (all pre-existing); `bot.providers.registry.PROVIDERS`; `dashboard.auth.require_session`
- Produces: `dashboard.environment.router: APIRouter` with `GET /api/environment/render`, `PATCH /api/environment/render`, `GET /api/environment/config`, `PATCH /api/environment/config`

- [ ] **Step 1: Write the failing tests**

Create `dashboard/tests/test_environment.py`:

```python
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
    monkeypatch.setattr(render_client, "env_vars", lambda service_id: {"DATABASE_URL": "postgres://x"})
    client = await _client()
    resp = await client.get("/api/environment/render")
    assert resp.json() == {"vars": [{"key": "DATABASE_URL", "value": "postgres://x", "protected": True}]}


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
    resp = await client.patch("/api/environment/render", json={"sets": {"FOO": "bar"}, "deletes": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == ["FOO"]
    assert body["failed"] == []
    assert body["deploy_id"] == "dep-1"
    assert pushed == [("FOO", "bar")]
    assert deploys == ["srv-1"]


async def test_patch_render_env_vars_rejects_a_protected_delete_without_touching_render(monkeypatch):
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

    applied_order = []

    def _push_tracking(service_id, key, value):
        _push(service_id, key, value)
        applied_order.append(key)

    monkeypatch.setattr(render_client, "push_env_var", _push_tracking)
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


async def test_get_environment_config_reflects_current_overrides(db_url, monkeypatch):
    store.init_pool()
    store.set_provider_override("groq", "2026-01-01T00:00:00+00:00")
    client = await _client()
    resp = await client.get("/api/environment/config")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "groq"


async def test_patch_environment_config_sets_provider_override(db_url):
    store.init_pool()
    client = await _client()
    resp = await client.patch("/api/environment/config", json={"provider": "groq"})
    assert resp.status_code == 200
    assert resp.json() == {"applied": ["provider"], "failed": []}
    assert store.get_provider_override() == "groq"


async def test_patch_environment_config_partial_cooldown_merges_with_current_values(db_url):
    store.init_pool()
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
```

`db_url` is this project's existing pytest fixture that points `DATABASE_URL` at a real test Postgres for tests marked as needing the DB — check `bot/tests/conftest.py` for its exact name/marker convention (it is already used throughout `bot/tests/test_deploy_script.py` and elsewhere) and mark these DB-touching tests the same way the rest of the suite does (e.g. `pytestmark = pytest.mark.db` at the top of the DB-touching test group, or per this project's existing per-file convention — match whatever `bot/tests/`'s other store-touching test files do).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard.environment'`.

- [ ] **Step 3: Implement `dashboard/environment.py`**

```python
"""Dashboard Environment tab: fetch/edit Render env vars and runtime_config
overrides. The one place `dashboard/` writes anything -- dashboard/router.py
stays read-only (see dashboard/CLAUDE.md). See docs/superpowers/specs/
2026-09-02-dashboard-environment-tab-design.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot import render_client
from bot.providers import registry
from bot.queue import store

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_render_payload() -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"vars": []}
    values = render_client.env_vars(service_id)
    return {
        "vars": [
            {"key": key, "value": value, "protected": key in render_client.PROTECTED_ENV_KEYS}
            for key, value in values.items()
        ]
    }


@router.get("/api/environment/render")
async def get_render_env_vars() -> JSONResponse:
    payload = await asyncio.to_thread(_build_render_payload)
    return JSONResponse(payload)


class EnvironmentRenderPatch(BaseModel):
    sets: dict[str, str] = {}
    deletes: list[str] = []


def _apply_render_patch(payload: EnvironmentRenderPatch) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {
            "applied": [],
            "failed": [{"key": "*", "error": "service_not_found"}],
            "deploy_id": None,
        }

    applied: list[str] = []
    failed: list[dict] = []
    stopped = False

    for key in payload.deletes:
        if stopped:
            break
        if key in render_client.PROTECTED_ENV_KEYS:
            failed.append({"key": key, "error": "protected"})
            continue
        try:
            render_client.delete_env_var(service_id, key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: deleted %s", key)

    for key, value in payload.sets.items():
        if stopped:
            break
        try:
            render_client.push_env_var(service_id, key, value)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: set %s (len %d)", key, len(value))

    deploy_id = None
    if applied:
        try:
            deploy_id = render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after applying %s", applied)

    return {"applied": applied, "failed": failed, "deploy_id": deploy_id}


@router.patch("/api/environment/render")
async def patch_render_env_vars(payload: EnvironmentRenderPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_render_patch, payload)
    return JSONResponse(result)


def _build_config_payload() -> dict:
    base, cap, factor = store.get_cooldown_overrides()
    tokens, reset = store.get_usage_cap_overrides()
    return {
        "provider": store.get_provider_override(),
        "cooldown_base_seconds": base,
        "cooldown_max_seconds": cap,
        "cooldown_factor": factor,
        "usage_cap_tokens": tokens,
        "usage_cap_reset": reset,
        "review_draft_prs": store.get_review_draft_override(),
        "key_index": store.get_all_key_index_overrides(),
        "model": store.get_all_model_overrides(),
    }


@router.get("/api/environment/config")
async def get_environment_config() -> JSONResponse:
    payload = await asyncio.to_thread(_build_config_payload)
    return JSONResponse(payload)


class EnvironmentConfigPatch(BaseModel):
    provider: str | None = None
    cooldown_base_seconds: float | None = None
    cooldown_max_seconds: float | None = None
    cooldown_factor: float | None = None
    usage_cap_tokens: int | None = None
    usage_cap_reset: str | None = None
    review_draft_prs: bool | None = None
    key_index: dict[str, int | None] = {}
    model: dict[str, str | None] = {}


def _apply_config_patch(payload: EnvironmentConfigPatch) -> dict:
    # exclude_unset: a field the caller never sent must not be read as
    # "clear this override" -- only a field explicitly present in the
    # request body (even if its value is null) is applied.
    fields = payload.model_dump(exclude_unset=True)
    now = datetime.now(timezone.utc).isoformat()
    applied: list[str] = []
    failed: list[dict] = []

    if "provider" in fields:
        provider = fields["provider"]
        if provider is not None and provider not in registry.PROVIDERS:
            failed.append({"key": "provider", "error": "unknown_provider"})
        else:
            try:
                store.set_provider_override(provider, now)
                applied.append("provider")
            except Exception as exc:  # noqa: BLE001
                failed.append({"key": "provider", "error": type(exc).__name__})

    cooldown_keys = ("cooldown_base_seconds", "cooldown_max_seconds", "cooldown_factor")
    cooldown_fields = {k: fields[k] for k in cooldown_keys if k in fields}
    if cooldown_fields:
        try:
            current_base, current_cap, current_factor = store.get_cooldown_overrides()
            base = cooldown_fields.get("cooldown_base_seconds", current_base)
            cap = cooldown_fields.get("cooldown_max_seconds", current_cap)
            factor = cooldown_fields.get("cooldown_factor", current_factor)
            store.set_cooldown_override(base, cap, factor, now)
            applied.extend(cooldown_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in cooldown_fields)

    usage_keys = ("usage_cap_tokens", "usage_cap_reset")
    usage_fields = {k: fields[k] for k in usage_keys if k in fields}
    if usage_fields:
        try:
            current_tokens, current_reset = store.get_usage_cap_overrides()
            tokens = usage_fields.get("usage_cap_tokens", current_tokens)
            reset = usage_fields.get("usage_cap_reset", current_reset)
            store.set_usage_cap_override(tokens, reset, now)
            applied.extend(usage_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in usage_fields)

    if "review_draft_prs" in fields:
        try:
            store.set_review_draft_override(fields["review_draft_prs"], now)
            applied.append("review_draft_prs")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": "review_draft_prs", "error": type(exc).__name__})

    for provider, index in fields.get("key_index", {}).items():
        if provider not in registry.PROVIDERS:
            failed.append({"key": f"key_index.{provider}", "error": "unknown_provider"})
            continue
        try:
            store.set_key_index_override(provider, index, now)
            applied.append(f"key_index.{provider}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": f"key_index.{provider}", "error": type(exc).__name__})

    for provider, model in fields.get("model", {}).items():
        if provider not in registry.PROVIDERS:
            failed.append({"key": f"model.{provider}", "error": "unknown_provider"})
            continue
        try:
            store.set_model_override(provider, model, now)
            applied.append(f"model.{provider}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": f"model.{provider}", "error": type(exc).__name__})

    return {"applied": applied, "failed": failed}


@router.patch("/api/environment/config")
async def patch_environment_config(payload: EnvironmentConfigPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_config_patch, payload)
    return JSONResponse(result)
```

- [ ] **Step 4: Mount the router in `bot/main.py`**

In `bot/main.py`, add the import alongside the existing dashboard imports:
```python
from dashboard.environment import router as environment_router
```
And add the include alongside the existing `dashboard_router` include (`bot/main.py:112` area):
```python
app.include_router(environment_router, dependencies=[Depends(require_session)])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS.

- [ ] **Step 6: Update `dashboard/CLAUDE.md`**

In the "Layering" section, change:
```
- `dashboard/router.py` reads `bot.queue.store`, `bot.queue.dispatcher`, and
  `bot.providers.base.KNOWN_PROVIDERS` directly — this is the one place
  `dashboard` depends on `bot`'s internals, and it's read-only (never
  enqueues, never mutates provider state).
```
to:
```
- `dashboard/router.py` reads `bot.queue.store`, `bot.queue.dispatcher`, and
  `bot.providers.base.KNOWN_PROVIDERS` directly, and stays read-only (never
  enqueues, never mutates provider state).
- `dashboard/environment.py` is the one place `dashboard` writes anything —
  Render env vars (via `bot.render_client`) and `runtime_config` overrides
  (via `bot.queue.store`'s existing `get_*`/`set_*` functions). Every value
  it returns from `GET /api/environment/render` is a real Render secret
  value, not reduced to a boolean/length — a documented, scoped exception to
  root `CLAUDE.md`'s "never display a byte of a secret" rule (see that
  file's Secret handling section and
  `docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md`).
  The value only ever reaches the authenticated operator's own browser DOM
  (masked by default, toggle-revealed client-side) — never logged, never
  persisted beyond the response.
```

- [ ] **Step 7: Run the full suite and ruff**

Run: `uv run pytest -v && uv run ruff check .`
Expected: PASS, no errors.

- [ ] **Step 8: Commit**

```bash
git add dashboard/environment.py bot/main.py dashboard/CLAUDE.md dashboard/tests/test_environment.py
git commit -m "feat: add dashboard/environment.py (Render env vars + runtime_config API)

GET/PATCH /api/environment/render and GET/PATCH /api/environment/config,
session-gated like the rest of the dashboard. Per-key partial-failure
reporting, protected-key deletes rejected before any Render call, one
fire-and-forget redeploy per successful render-var save.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```

---

### Task 4: Environment tab UI in `dashboard/static/dashboard.html`

**Files:**
- Modify: `dashboard/static/dashboard.html`
- Test: `dashboard/tests/test_dashboard.py` (content-substring checks, matching this file's existing convention)

**Interfaces:**
- Consumes: `GET/PATCH /api/environment/render`, `GET/PATCH /api/environment/config` (Task 3) — response shapes: `{"vars": [{"key": str, "value": str, "protected": bool}]}` and `{"applied": [str], "failed": [{"key": str, "error": str}], "deploy_id": str | null}` for render; the config `GET` shape from `_build_config_payload` above, `{"applied": [str], "failed": [...]}` for config `PATCH`.

- [ ] **Step 1: Write the failing content tests**

Append to `dashboard/tests/test_dashboard.py`:

```python
def test_environment_panel_markup_is_present():
    html = _DASHBOARD.read_text(encoding="utf-8")
    for token in (
        'id="statusPanel"',
        'id="environmentPanel"',
        'id="renderVarsTable"',
        'id="configForm"',
        'id="saveRenderVarsBtn"',
        'id="saveConfigBtn"',
        'data-panel="statusPanel"',
        'data-panel="environmentPanel"',
    ):
        assert token in html, f"missing Environment tab markup: {token}"


def test_environment_nav_strings_exist_in_both_languages():
    html = _DASHBOARD.read_text(encoding="utf-8")
    for key in ("nav_status", "nav_environment", "env_save", "env_save_config"):
        assert key in html, f"missing i18n key: {key}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_dashboard.py -v`
Expected: FAIL — assertions on missing markup/strings.

- [ ] **Step 3: Add the nav shell and Environment panel markup**

In `dashboard/static/dashboard.html`, replace the `<main>` block:
```html
  <main>
    <h1 data-i18n="dashboard_title"></h1>
    <section id="errorBanner" hidden></section>
    <section id="stats" class="stats-grid"></section>
    <section id="reviews"></section>
  </main>
```
with:
```html
  <main>
    <h1 data-i18n="dashboard_title"></h1>
    <nav class="side-nav">
      <button class="nav-item active" type="button" data-panel="statusPanel" data-i18n="nav_status"></button>
      <button class="nav-item" type="button" data-panel="environmentPanel" data-i18n="nav_environment"></button>
    </nav>
    <section id="statusPanel">
      <section id="errorBanner" hidden></section>
      <section id="stats" class="stats-grid"></section>
      <section id="reviews"></section>
    </section>
    <section id="environmentPanel" hidden>
      <section id="deployBanner" hidden></section>
      <h2 data-i18n="env_render_title"></h2>
      <table id="renderVarsTable">
        <thead>
          <tr>
            <th data-i18n="env_col_key"></th>
            <th data-i18n="env_col_value"></th>
            <th></th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
      <form id="addVarForm">
        <input id="newVarKey" type="text" placeholder="KEY_NAME" required>
        <input id="newVarValue" type="text" placeholder="value" required>
        <button type="submit" data-i18n="env_add"></button>
      </form>
      <button id="saveRenderVarsBtn" type="button" data-i18n="env_save"></button>
      <div id="renderSaveResult"></div>

      <h2 data-i18n="env_config_title"></h2>
      <form id="configForm">
        <label data-i18n="env_config_provider"></label>
        <select id="cfgProvider">
          <option value="">—</option>
          <option value="gemini">gemini</option>
          <option value="groq">groq</option>
          <option value="vertex">vertex</option>
        </select>
        <label data-i18n="env_config_cooldown_base"></label>
        <input id="cfgCooldownBase" type="number" step="any">
        <label data-i18n="env_config_cooldown_max"></label>
        <input id="cfgCooldownMax" type="number" step="any">
        <label data-i18n="env_config_cooldown_factor"></label>
        <input id="cfgCooldownFactor" type="number" step="any">
        <label data-i18n="env_config_usage_cap_tokens"></label>
        <input id="cfgUsageCapTokens" type="number">
        <label data-i18n="env_config_usage_cap_reset"></label>
        <input id="cfgUsageCapReset" type="text" placeholder="HH:MM">
        <label>
          <input id="cfgReviewDraftPrs" type="checkbox">
          <span data-i18n="env_config_review_draft_prs"></span>
        </label>
      </form>
      <button id="saveConfigBtn" type="button" data-i18n="env_save_config"></button>
      <div id="configSaveResult"></div>
    </section>
  </main>
```

- [ ] **Step 4: Add i18n strings**

In the `STRINGS.en` object, add:
```javascript
        nav_status: "Status",
        nav_environment: "Environment",
        env_render_title: "Render environment variables",
        env_col_key: "Key",
        env_col_value: "Value",
        env_add: "Add",
        env_save: "Save changes",
        env_config_title: "Runtime configuration",
        env_config_provider: "Active provider override",
        env_config_cooldown_base: "Cooldown base (s)",
        env_config_cooldown_max: "Cooldown max (s)",
        env_config_cooldown_factor: "Cooldown factor",
        env_config_usage_cap_tokens: "Usage cap (tokens)",
        env_config_usage_cap_reset: "Usage cap reset (UTC)",
        env_config_review_draft_prs: "Review draft PRs",
        env_save_config: "Save configuration",
        env_redeploying: "Redeploying…",
        env_reveal: "Reveal",
        env_hide: "Hide",
        env_delete: "Delete",
```
In `STRINGS.he`, add:
```javascript
        nav_status: "סטטוס",
        nav_environment: "סביבה",
        env_render_title: "משתני סביבה של Render",
        env_col_key: "מפתח",
        env_col_value: "ערך",
        env_add: "הוספה",
        env_save: "שמור שינויים",
        env_config_title: "הגדרות תצורה בזמן ריצה",
        env_config_provider: "דריסת ספק פעיל",
        env_config_cooldown_base: "צינון בסיסי (שנ')",
        env_config_cooldown_max: "צינון מקסימלי (שנ')",
        env_config_cooldown_factor: "מקדם צינון",
        env_config_usage_cap_tokens: "תקרת שימוש (טוקנים)",
        env_config_usage_cap_reset: "איפוס תקרת שימוש (UTC)",
        env_config_review_draft_prs: "בדיקת PR-ים בטיוטה",
        env_save_config: "שמור תצורה",
        env_redeploying: "פורס מחדש…",
        env_reveal: "הצג",
        env_hide: "הסתר",
        env_delete: "מחק",
```

- [ ] **Step 5: Add nav-toggle and Environment-tab JS**

Before the closing `</script>` tag, before the final `applyLanguage(currentLang); setInterval(refreshDashboard, POLL_INTERVAL_MS);` lines, add:

```javascript
    let renderVars = [];   // [{key, value, protected}], as last fetched
    const stagedEdits = new Map();   // key -> new value
    const stagedDeletes = new Set();
    const revealed = new Set();
    let environmentLoaded = false;

    function maskedValue(value) {
      return "•".repeat(Math.min(value.length, 24)) || "—";
    }

    function renderRenderVarsTable() {
      const tbody = document.querySelector("#renderVarsTable tbody");
      const rows = renderVars
        .filter((v) => !stagedDeletes.has(v.key))
        .map((v) => {
          const displayValue = stagedEdits.has(v.key) ? stagedEdits.get(v.key) : v.value;
          const isRevealed = revealed.has(v.key);
          const deleteBtn = v.protected
            ? ""
            : `<button type="button" class="control" data-action="delete" data-key="${esc(v.key)}">${t("env_delete")}</button>`;
          return `
            <tr data-key="${esc(v.key)}">
              <td>${esc(v.key)}</td>
              <td>
                <input type="text" class="env-value-input" data-key="${esc(v.key)}"
                  value="${esc(isRevealed ? displayValue : maskedValue(displayValue))}"
                  ${isRevealed ? "" : "readonly"}>
                <button type="button" class="control" data-action="reveal" data-key="${esc(v.key)}">
                  ${isRevealed ? t("env_hide") : t("env_reveal")}
                </button>
              </td>
              <td>${deleteBtn}</td>
            </tr>
          `;
        });
      tbody.innerHTML = rows.join("");

      tbody.querySelectorAll('[data-action="reveal"]').forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.getAttribute("data-key");
          if (revealed.has(key)) revealed.delete(key); else revealed.add(key);
          renderRenderVarsTable();
        });
      });
      tbody.querySelectorAll('[data-action="delete"]').forEach((btn) => {
        btn.addEventListener("click", () => {
          stagedDeletes.add(btn.getAttribute("data-key"));
          renderRenderVarsTable();
        });
      });
      tbody.querySelectorAll(".env-value-input:not([readonly])").forEach((input) => {
        input.addEventListener("input", () => {
          stagedEdits.set(input.getAttribute("data-key"), input.value);
        });
      });
    }

    async function fetchEnvironmentRender() {
      const resp = await fetch("/api/environment/render");
      if (resp.status === 401) { window.location.href = "/login"; return; }
      const data = await resp.json();
      renderVars = data.vars;
      stagedEdits.clear();
      stagedDeletes.clear();
      renderRenderVarsTable();
    }

    async function pollUntilDashboardRecovers() {
      const banner = document.getElementById("deployBanner");
      const deadline = Date.now() + 5 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 5000));
        try {
          const resp = await fetch("/api/dashboard");
          if (resp.ok) { banner.hidden = true; return; }
        } catch (err) { /* connection dropped mid-redeploy -- keep polling */ }
      }
      banner.hidden = true;
    }

    async function saveRenderVars() {
      const sets = {};
      stagedEdits.forEach((value, key) => { sets[key] = value; });
      const deletes = Array.from(stagedDeletes);
      const resp = await fetch("/api/environment/render", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sets, deletes }),
      });
      const result = await resp.json();
      const resultEl = document.getElementById("renderSaveResult");
      resultEl.textContent =
        `applied: ${result.applied.join(", ") || "-"}; failed: ${result.failed.map((f) => `${f.key} (${f.error})`).join(", ") || "-"}`;
      if (result.deploy_id) {
        document.getElementById("deployBanner").hidden = false;
        document.getElementById("deployBanner").textContent = t("env_redeploying");
        pollUntilDashboardRecovers();
      }
      await fetchEnvironmentRender();
    }

    document.getElementById("addVarForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const keyInput = document.getElementById("newVarKey");
      const valueInput = document.getElementById("newVarValue");
      if (!keyInput.value) return;
      renderVars = renderVars.filter((v) => v.key !== keyInput.value);
      renderVars.push({ key: keyInput.value, value: valueInput.value, protected: false });
      stagedEdits.set(keyInput.value, valueInput.value);
      revealed.add(keyInput.value);
      keyInput.value = "";
      valueInput.value = "";
      renderRenderVarsTable();
    });

    document.getElementById("saveRenderVarsBtn").addEventListener("click", saveRenderVars);

    function populateConfigForm(cfg) {
      document.getElementById("cfgProvider").value = cfg.provider || "";
      document.getElementById("cfgCooldownBase").value = cfg.cooldown_base_seconds ?? "";
      document.getElementById("cfgCooldownMax").value = cfg.cooldown_max_seconds ?? "";
      document.getElementById("cfgCooldownFactor").value = cfg.cooldown_factor ?? "";
      document.getElementById("cfgUsageCapTokens").value = cfg.usage_cap_tokens ?? "";
      document.getElementById("cfgUsageCapReset").value = cfg.usage_cap_reset ?? "";
      document.getElementById("cfgReviewDraftPrs").checked = Boolean(cfg.review_draft_prs);
    }

    async function fetchEnvironmentConfig() {
      const resp = await fetch("/api/environment/config");
      if (resp.status === 401) { window.location.href = "/login"; return; }
      populateConfigForm(await resp.json());
    }

    async function saveConfig() {
      const body = {
        provider: document.getElementById("cfgProvider").value || null,
        cooldown_base_seconds: parseFloat(document.getElementById("cfgCooldownBase").value) || null,
        cooldown_max_seconds: parseFloat(document.getElementById("cfgCooldownMax").value) || null,
        cooldown_factor: parseFloat(document.getElementById("cfgCooldownFactor").value) || null,
        usage_cap_tokens: parseInt(document.getElementById("cfgUsageCapTokens").value, 10) || null,
        usage_cap_reset: document.getElementById("cfgUsageCapReset").value || null,
        review_draft_prs: document.getElementById("cfgReviewDraftPrs").checked,
      };
      const resp = await fetch("/api/environment/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await resp.json();
      document.getElementById("configSaveResult").textContent =
        `applied: ${result.applied.join(", ") || "-"}; failed: ${result.failed.map((f) => `${f.key} (${f.error})`).join(", ") || "-"}`;
    }

    document.getElementById("saveConfigBtn").addEventListener("click", saveConfig);

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", async () => {
        document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const targetId = btn.getAttribute("data-panel");
        document.getElementById("statusPanel").hidden = targetId !== "statusPanel";
        document.getElementById("environmentPanel").hidden = targetId !== "environmentPanel";
        if (targetId === "environmentPanel" && !environmentLoaded) {
          environmentLoaded = true;
          await fetchEnvironmentRender();
          await fetchEnvironmentConfig();
        }
      });
    });
```

- [ ] **Step 6: Add minimal CSS for the new elements**

In the `<style>` block, add (near the existing `.stat-tile`/`button.control` rules):
```css
  .side-nav { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
  .side-nav .nav-item.active { border-color: var(--accent); color: var(--accent); }
  #renderVarsTable { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
  #renderVarsTable th, #renderVarsTable td { text-align: start; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); }
  #renderVarsTable input.env-value-input { width: 100%; max-width: 320px; font-family: monospace; }
  #addVarForm { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
  #configForm { display: grid; grid-template-columns: max-content 1fr; gap: 0.5rem 0.75rem; align-items: center; max-width: 480px; margin-bottom: 1rem; }
```
(`.nav-item` itself reuses the existing `button.control` class in the markup above, so its base styling — background, border, radius, padding — is already covered.)

Update the `<button class="nav-item active" ...>` / `<button class="nav-item" ...>` markup from Step 3 to also carry `class="control nav-item active"` / `class="control nav-item"` so they pick up the existing `button.control` styling; adjust the markup accordingly.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 8: Manually smoke-test in a browser**

Run the project's existing dev-run path (check for a project `run`/dev-server skill or `guide/setup/local/08-run.md`) against a local Postgres, log into the dashboard, click "Environment", confirm the Render vars table and config form load (a `RENDER_API_KEY`-less local dev environment will show an empty vars table via `find_service_id()` returning `None` — expected, not a bug), and confirm the nav toggle switches panels without a page reload.

- [ ] **Step 9: Run the full suite and ruff**

Run: `uv run pytest -v && uv run ruff check .`
Expected: PASS, no errors.

- [ ] **Step 10: Commit**

```bash
git add dashboard/static/dashboard.html dashboard/tests/test_dashboard.py
git commit -m "feat: add Environment tab UI to dashboard.html

Client-side Status/Environment nav toggle; Render vars table
(masked-by-default, per-row reveal toggle, inline edit, delete hidden
for protected keys, add-new-key form, one Save that stages every
change into a single PATCH); runtime_config form over the same
override fields set_override.py exposes today.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```

---

### Task 5: Document the secret-visibility exception in root `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only)

- [ ] **Step 1: Add the exception subsection**

In `CLAUDE.md`, immediately after the "Secret handling" section's bullet list (before the "## Project" heading), add:

```markdown
### Scoped exception: the dashboard Environment tab

`dashboard/environment.py`'s `GET /api/environment/render` is the one
documented exception to "never display any byte of a secret value" in this
file. It returns real Render env-var values (via `bot.render_client.env_vars()`)
to the authenticated operator's own browser session, where
`dashboard/static/dashboard.html` renders them masked by default with a
per-row reveal toggle. This is deliberately narrower than it looks:

- The value never leaves this one authenticated, session-cookie-gated
  endpoint's response — never logged (see `dashboard/environment.py`'s own
  INFO lines, which log key names and lengths only, never values), never
  written to a git commit, PR, Artifact, or subagent prompt, never persisted
  client-side beyond the page's own DOM (no `localStorage`).
- Transport is unchanged HTTPS throughout, identical to every other
  authenticated dashboard route.
- This exception covers only this one endpoint and the page that renders
  its response. It does not license printing a secret value anywhere else in
  this codebase or in an agent's own shell commands — every other rule in
  this section still applies at full strength everywhere else, including
  elsewhere in `dashboard/` and `bot/`.

See `docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md`
for the full design and the reasoning behind this carve-out.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document the dashboard Environment tab's secret-visibility exception

Scopes the carve-out explicitly so it isn't read as a general loosening
of the Secret handling section's 'never display a byte' rule.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```

---

### Task 6: Push `RENDER_API_KEY` from onboarding's final bulk push

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `render_frame["api_key"]` — already read and guaranteed present by `bulk_push_render_env_vars`'s own guard clause (`if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame: return {...}`).

- [ ] **Step 1: Write the failing test change**

In `onboarding/tests/test_onboarding_router.py`, update `test_bulk_push_assembles_every_frame_into_one_push_call` (around line 1535) — add `"RENDER_API_KEY": "rnd_x",` to the expected `captured["values"]` dict:

```python
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
```

And update `test_bulk_push_omits_a_frame_that_was_never_completed` (around line 1580) — `RENDER_API_KEY` is read from the `render` frame itself, which the endpoint's own guard clause already requires to be present, so it is never actually omitted the way other frames' keys are:

```python
    assert captured["values"] == {
        "RENDER_API_KEY": "rnd_x",
        **router._GENERIC_OPERATIONAL_ENV_DEFAULTS,
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -k test_bulk_push -v`
Expected: FAIL — `captured["values"]` missing `"RENDER_API_KEY"`.

- [ ] **Step 3: Add the push in `onboarding/router.py`**

In `bulk_push_render_env_vars` (around line 737), change:
```python
    env_vars: dict[str, str] = {}

    github_app = (await _read_frame(session_id, "github_app"))
```
to:
```python
    env_vars: dict[str, str] = {}

    # The deployed bot service needs its own RENDER_API_KEY to power the
    # dashboard's Environment tab (docs/superpowers/specs/
    # 2026-09-02-dashboard-environment-tab-design.md) -- previously this
    # credential never left the visitor's browser/onboarding session.
    # render_frame["api_key"] is guaranteed present by this function's own
    # guard clause above.
    env_vars["RENDER_API_KEY"] = render_frame["api_key"]

    github_app = (await _read_frame(session_id, "github_app"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -k test_bulk_push -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and ruff**

Run: `uv run pytest -v && uv run ruff check .`
Expected: PASS, no errors.

- [ ] **Step 6: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "feat: push RENDER_API_KEY to the deployed service from onboarding

The dashboard's new Environment tab needs the deployed service to hold
its own RENDER_API_KEY to call Render's API at runtime -- previously
this credential never left the visitor's onboarding session. One-line
addition to the existing bulk env-var push; no new frame needed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```

---

### Task 7: Close the parked `ISSUES.md` entry and log the `deploy.py`/`set_override.py` follow-up

**Files:**
- Modify: `ISSUES.md`

**Interfaces:** none (documentation only)

- [ ] **Step 1: Update the parked Design Gaps entry**

In `ISSUES.md`, find the entry starting `### Dashboard "Environment" feature (fetch/modify Render env vars + config DB) parked pending dashboard authentication` (its `**Status:**` line currently reads `open — deliberately paused`, with a `**Update (2026-08-28):**` note below it). Append one more update paragraph after the existing 2026-08-28 update:

```markdown
- **Update (2026-09-02):** implemented and merged. Design:
  `docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md`.
  Plan: `docs/superpowers/plans/2026-09-02-dashboard-environment-tab.md`.
  The two Environment sections are named "Render environment variables" and
  "Runtime configuration"; Render env vars are full CRUD (add/edit/delete,
  masked-by-default with a per-row reveal toggle — a documented, scoped
  exception to root `CLAUDE.md`'s secret-display rule) except for a fixed
  `PROTECTED_ENV_KEYS` set that can never be deleted from the dashboard.
```
And change the `**Status:**` line from `open — deliberately paused` to `closed — implemented 2026-09-02`.

- [ ] **Step 2: Add the `deploy.py`/`set_override.py` retirement follow-up**

In `ISSUES.md`'s Design Gaps section (same section as the entry just closed), add a new entry:

```markdown
### `bot/scripts/deploy.py --sync-env` and `bot/scripts/set_override.py` are now redundant with the dashboard Environment tab

- **Found during:** `docs/superpowers/plans/2026-09-02-dashboard-environment-tab.md`
- **What:** The dashboard's new Environment tab (`dashboard/environment.py`)
  does live, from-the-browser what `deploy.py --sync-env` and
  `set_override.py` do from the CLI: push Render env vars and edit
  `runtime_config` overrides. `deploy.py`'s other checks (pricing,
  provider-live, health, database, credential-live) are unrelated to
  env-var/config editing and remain useful regardless.
- **Why parked:** Retiring either script is a real deletion/migration task
  (removing dead code paths, updating any doc/guide that still tells an
  operator to run them, deciding whether any check-only functionality needs
  to move somewhere else first) — out of scope for the plan that made them
  redundant.
- **Follow-up:** Decide whether to retire `--sync-env`/`set_override.py`
  outright or keep them as a CLI fallback (e.g. for a fresh deploy before
  the dashboard is reachable at all — `--sync-env` is what makes the very
  first deploy's env vars non-empty). If retired, update
  `guide/operations/overrides.md` and any other doc that references them.
```

- [ ] **Step 3: Commit**

```bash
git add ISSUES.md
git commit -m "docs: close the dashboard Environment feature's parked ISSUES.md entry

Also logs deploy.py --sync-env / set_override.py's now-redundant
status as a follow-up, per the design spec's deploy.py-future decision.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017RRtfVBq1rqYyw9VaS1Tep"
```
