# Supabase Frame: OAuth → Personal Access Token — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Supabase frame's shared operator-registered OAuth app with a visitor-pasted Personal Access Token, removing the shared-credential abuse/revocation risk outright and matching every other frame's credential model.

**Architecture:** `supabase_client.py` gains a `validate_key()` mirroring `render_client.validate_key()`'s shape, folding the old two-call (exchange + list-orgs) sequence into one. `router.py` replaces `/api/supabase/connect` + `GET /oauth/supabase/callback` with a single `POST /api/supabase/validate-key`, and `create-project` gains a `name` field since there's no more pre-redirect step to have captured it earlier. The session's `supabase.access_token` field is renamed `api_key` throughout. The frontend swaps the OAuth-redirect button for a paste-and-validate input, and moves the project-name input to appear alongside the org picker (after key validation) instead of before it.

**Tech Stack:** FastAPI, httpx, respx (test mocking), pytest-asyncio, vanilla JS (no framework) in `onboarding/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md`

## Global Constraints

- Never log or echo a visitor-supplied credential, in full or truncated (`onboarding/CLAUDE.md`).
- Every relay endpoint returns a verdict, never the credential itself.
- `onboarding/` never imports from `bot/`'s credential-handling code.
- No test exercises a real Supabase call — every Supabase-facing test is `respx`-mocked.
- Full test suite (`uv run pytest -v`) and `uv run ruff check .` must be green before any push (not required between tasks, but must pass by the end).

---

### Task 1: `onboarding/supabase_client.py` — replace OAuth exchange with `validate_key`

**Files:**
- Modify: `onboarding/supabase_client.py`
- Modify: `onboarding/tests/test_onboarding_supabase_client.py`

**Interfaces:**
- Consumes: nothing new (still uses `httpx.AsyncClient`, `SUPABASE_API_BASE`).
- Produces: `supabase_client.validate_key(pat: str) -> SupabaseKeyValid | SupabaseKeyInvalid`, `SupabaseKeyValid(orgs: list[SupabaseOrg])`, `SupabaseKeyInvalid(reason: str)` where `reason` is `"invalid_key"` or `"supabase_unreachable"`. `SupabaseOrg` is unchanged (already exists). `create_project`, `get_project_status`, `get_connection_info` are unchanged (still take a bearer token string as their first argument — callers just now pass a PAT instead of an OAuth token).

- [ ] **Step 1: Write the failing tests for `validate_key`**

Replace the whole file `onboarding/tests/test_onboarding_supabase_client.py` from its start through the end of the old `test_list_organizations_malformed_body_is_unreachable` (i.e. everything before `async def test_create_project_returns_ref_and_status():`) with:

```python
"""Tests for onboarding/supabase_client.py. validate_key() mirrors
render_client.validate_key()'s shape: one cheap read call doubles as both
credential validation and (since Supabase has no separate "who am I"
endpoint) the org list the frame needs next. See
docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md section 3."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from onboarding import supabase_client

ORGS_URL = "https://api.supabase.com/v1/organizations"
PROJECTS_URL = "https://api.supabase.com/v1/projects"
SENTINEL_PAT = "sbp_SENTINEL_DO_NOT_LOG_9f3a"


async def test_valid_key_returns_orgs():
    with respx.mock:
        respx.get(ORGS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "1", "slug": "org-one", "name": "Org One"},
                    {"id": "2", "slug": "org-two", "name": "Org Two"},
                ],
            )
        )
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyValid(
        orgs=[
            supabase_client.SupabaseOrg(slug="org-one", name="Org One"),
            supabase_client.SupabaseOrg(slug="org-two", name="Org Two"),
        ]
    )


async def test_valid_key_with_zero_orgs_is_still_valid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyValid(orgs=[])


async def test_validate_key_sends_bearer_token():
    with respx.mock:
        route = respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
        await supabase_client.validate_key(SENTINEL_PAT)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SENTINEL_PAT}"


async def test_unauthorized_key_is_invalid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.validate_key("bad")
    assert result == supabase_client.SupabaseKeyInvalid(reason="invalid_key")


async def test_forbidden_key_is_invalid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(403))
        result = await supabase_client.validate_key("bad")
    assert result == supabase_client.SupabaseKeyInvalid(reason="invalid_key")


async def test_5xx_is_unreachable_not_invalid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyInvalid(reason="supabase_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.get(ORGS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyInvalid(reason="supabase_unreachable")


async def test_malformed_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyInvalid(reason="supabase_unreachable")


async def test_validate_key_never_logs_the_key(caplog):
    with caplog.at_level(logging.DEBUG):
        with respx.mock:
            respx.get(ORGS_URL).mock(return_value=httpx.Response(401))
            await supabase_client.validate_key(SENTINEL_PAT)
    assert SENTINEL_PAT not in caplog.text

```

Leave everything from `async def test_create_project_returns_ref_and_status():` onward in the file untouched — `create_project`, `get_project_status`, and `get_connection_info` are unaffected by this task.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd onboarding && uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: FAIL — `AttributeError: module 'onboarding.supabase_client' has no attribute 'validate_key'` (and `SupabaseKeyValid`/`SupabaseKeyInvalid` don't exist yet).

- [ ] **Step 3: Implement `validate_key`, delete the OAuth-exchange and `list_organizations` functions**

In `onboarding/supabase_client.py`:

1. Delete `SUPABASE_OAUTH_BASE` (module constant, line 15).
2. Delete `SupabaseTokens`, `SupabaseOAuthFailed`, `SupabaseTokenResult`, `_parse_token_response`, `exchange_oauth_code`, `refresh_access_token` (lines 20-100 in the current file — everything from the first `@dataclasses.dataclass` through the end of `refresh_access_token`).
3. Delete `list_organizations` (the function whose body does `GET /organizations` — keep `SupabaseOrg`, `SupabaseOrgsListed`, `SupabaseApiFailed`, `SupabaseOrgsResult` type aliases as-is, since `SupabaseOrg` is reused below and `SupabaseApiFailed`/`SupabaseOrgsResult` are still used by nothing new — actually `SupabaseOrgsListed`/`SupabaseOrgsResult` become dead once `list_organizations` is gone, so delete those two as well; keep `SupabaseOrg` and `SupabaseApiFailed`, both still used elsewhere in the file).
4. In their place (right after the module docstring/imports, before `create_project`), add:

```python
@dataclasses.dataclass(frozen=True)
class SupabaseOrg:
    slug: str
    name: str


@dataclasses.dataclass(frozen=True)
class SupabaseKeyValid:
    orgs: list[SupabaseOrg]


@dataclasses.dataclass(frozen=True)
class SupabaseKeyInvalid:
    reason: str  # "invalid_key" | "supabase_unreachable"


SupabaseKeyValidation = SupabaseKeyValid | SupabaseKeyInvalid


async def validate_key(pat: str) -> SupabaseKeyValidation:
    """One cheap read call (GET /organizations) to confirm pat is a live
    Supabase Personal Access Token -- doubles as both validation and the
    org list the frame needs next (Supabase has no separate token-identity
    endpoint). Never logs or returns the token itself. Mirrors
    render_client.validate_key()'s shape exactly."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                "/organizations",
                headers={"Authorization": f"Bearer {pat}"},
            )
    except httpx.HTTPError:
        return SupabaseKeyInvalid(reason="supabase_unreachable")

    if response.status_code in (401, 403):
        return SupabaseKeyInvalid(reason="invalid_key")
    if response.status_code != 200:
        return SupabaseKeyInvalid(reason="supabase_unreachable")

    try:
        body = response.json()
        orgs = [SupabaseOrg(slug=str(o["slug"]), name=str(o["name"])) for o in body]
    except (ValueError, KeyError, TypeError):
        return SupabaseKeyInvalid(reason="supabase_unreachable")
    return SupabaseKeyValid(orgs=orgs)
```

(`SupabaseOrg` moves here from further down the file — remove its old definition where it currently sits, just above the old `list_organizations`, to avoid a duplicate class.)

5. `create_project`, `get_project_status`, `get_connection_info`, and their dataclasses are untouched.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd onboarding && uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: PASS — all tests, including the untouched `create_project`/`get_project_status`/`get_connection_info` tests below the replaced section.

- [ ] **Step 5: Commit**

```bash
git add onboarding/supabase_client.py onboarding/tests/test_onboarding_supabase_client.py
git commit -m "$(cat <<'EOF'
feat: replace Supabase OAuth token exchange with PAT validation

exchange_oauth_code/refresh_access_token/list_organizations are gone;
validate_key() folds credential validation and org listing into one
call, mirroring render_client.validate_key()'s shape. Part of the
OAuth-app-to-PAT redesign (see
docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016s4DyZcniQEipH9QkxJdoD
EOF
)"
```

---

### Task 2: `onboarding/config.py` + `onboarding/main.py` — remove the OAuth app settings

**Files:**
- Modify: `onboarding/config.py`
- Modify: `onboarding/main.py`
- Modify: `onboarding/tests/test_onboarding_config.py`
- Modify: `onboarding/tests/test_onboarding_main.py`
- Modify: `onboarding/.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` with no `supabase_oauth_client_id`/`supabase_oauth_client_secret` fields. `lifespan` no longer checks for them.

- [ ] **Step 1: Delete the now-obsolete tests first**

In `onboarding/tests/test_onboarding_config.py`, delete every test from `test_supabase_oauth_client_id_defaults_to_empty_string` through `test_supabase_oauth_client_secret_surrounding_whitespace_is_stripped` (lines 22-87 in the current file — every test whose name contains `supabase_oauth`). Also delete the `_INJECTION_CHARS_RE`-parametrized `test_malformed_supabase_oauth_client_id_is_rejected` test and its `@pytest.mark.parametrize` block if it tests nothing else after this deletion (check: if that parametrize block is used only for this one test, delete the whole parametrize+test; if reused elsewhere, keep the parametrize and only remove this one test function).

In `onboarding/tests/test_onboarding_main.py`, delete the `_oauth_app_credentials`-style monkeypatch lines for `supabase_oauth_client_id`/`supabase_oauth_client_secret` in the shared fixture (lines 36-37), and delete `test_lifespan_refuses_to_start_without_supabase_client_id` and `test_lifespan_refuses_to_start_without_supabase_client_secret` in full.

- [ ] **Step 2: Run the tests to verify the remaining ones still pass (sanity check before removing the settings)**

Run: `cd onboarding && uv run pytest tests/test_onboarding_config.py tests/test_onboarding_main.py -v`
Expected: PASS — the deleted tests are gone, remaining tests (e.g. `database_url`/`onboarding_session_encryption_key` tests, other lifespan checks) still reference `settings.supabase_oauth_client_id` nowhere else and pass unchanged, since the setting still exists on `Settings` at this point.

- [ ] **Step 3: Remove the settings and the lifespan checks**

In `onboarding/config.py`, delete lines 29-59 (the whole comment block plus `supabase_oauth_client_id`, `supabase_oauth_client_secret`, and their two `@field_validator` methods `_normalize_supabase_oauth_client_id`/`_normalize_supabase_oauth_client_secret`). Also delete the now-unused `_INJECTION_CHARS_RE` regex and its comment (lines 11-15) if nothing else in the file uses it — check first with `grep -n _INJECTION_CHARS_RE onboarding/config.py`; if it's genuinely only used by the deleted validator, remove it.

In `onboarding/main.py`, delete the two `if not settings.supabase_oauth_client_id` / `if not settings.supabase_oauth_client_secret` blocks (lines 24-35).

In `onboarding/.env.example`, delete the `SUPABASE_OAUTH_CLIENT_ID=` and `SUPABASE_OAUTH_CLIENT_SECRET=` lines (56-57) and any comment header immediately above them that describes only this credential.

- [ ] **Step 4: Run the tests to verify everything still passes**

Run: `cd onboarding && uv run pytest tests/test_onboarding_config.py tests/test_onboarding_main.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add onboarding/config.py onboarding/main.py onboarding/.env.example onboarding/tests/test_onboarding_config.py onboarding/tests/test_onboarding_main.py
git commit -m "$(cat <<'EOF'
feat: remove Supabase OAuth app settings

No operator-level Supabase secret remains -- the PAT is entirely
visitor-supplied. Part of the OAuth-app-to-PAT redesign.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016s4DyZcniQEipH9QkxJdoD
EOF
)"
```

---

### Task 3: `onboarding/router.py` — replace connect/callback with `validate-key`, add `name` to create-project

**Files:**
- Modify: `onboarding/router.py`
- Modify: `onboarding/tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `supabase_client.validate_key`, `supabase_client.SupabaseKeyValid`, `supabase_client.SupabaseKeyInvalid` (Task 1). `_get_session_id`, `_get_session`, `_read_frame`, `_update_frame`, `_create_session` (pre-existing wrappers, unchanged).
- Produces: `POST /api/supabase/validate-key` (request `{"key": str}`, response `{"valid": true, "orgs": [...]}` or `{"valid": false, "reason": ...}`). `SupabaseCreateProjectRequest` now has both `organization_slug` and `name`. The session's `supabase` frame stores `api_key` (not `access_token`).

- [ ] **Step 1: Write the failing tests**

In `onboarding/tests/test_onboarding_router.py`:

1. Delete `test_supabase_oauth_callback_with_no_session_redirects_to_root`, `test_supabase_oauth_callback_completes_on_matching_state`, `test_supabase_oauth_callback_rejects_a_mismatched_state`, `test_supabase_oauth_callback_with_no_pending_state_falls_back_gracefully`, `test_supabase_oauth_callback_never_completes_on_a_failed_exchange` (lines 250-334).
2. Delete `test_connect_supabase_stores_pending_oauth_and_returns_authorize_url`, `test_connect_supabase_discards_a_previous_projects_data`, `test_connect_supabase_with_no_session_fails_closed` (lines 337-380).
3. Delete `test_list_organizations_reads_access_token_from_session`, `test_list_organizations_reports_failure_reason`, `test_list_organizations_with_no_session_fails_closed` (lines 383-421).
4. Replace all six with:

```python
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
```

5. Replace `test_create_project_generates_db_pass_server_side` (lines 424-450) with an updated version that submits `name` in the request instead of reading it from the session, and reads `api_key` (not `access_token`) from the session:

```python
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
```

6. In `test_create_project_relays_the_rejection_message`, `test_create_project_with_no_session_fails_closed`, `test_project_status_reads_from_session`, `test_project_status_reports_failure_reason`, `test_connection_info_assembles_and_stores_the_database_url`, `test_connection_info_reports_failure_reason`: replace every `"access_token": "a"` (or `"access_token": "tok"`) in `fake.update_frame(...)` calls with `"api_key": "a"` (or `"api_key": "tok"`), and add `"name": "pr-review-bot"` to the JSON body of the two `create-project` POST calls (`test_create_project_relays_the_rejection_message`, `test_create_project_with_no_session_fails_closed`) since `name` is now required on the request.

7. Update `test_get_session_reports_supabase_authorized_but_not_yet_created` (lines 638-646): this test's whole premise (an "authorized but not yet created" resumable state) no longer exists — delete it. Update `test_get_session_reports_supabase_provisioning_once_project_created` and `test_get_session_reports_supabase_complete_once_database_url_present` to seed the frame with `"api_key"` instead of `"access_token"` (the assertions themselves are unchanged, since `GET /api/session` never echoed the credential field name).

8. Update `test_index_never_templates_the_supabase_oauth_client_id`'s docstring (it currently says `/api/supabase/connect builds the whole authorize URL...` — that endpoint no longer exists) to: `"""No operator-level Supabase secret exists at all anymore -- nothing to template."""`. The assertions themselves are unchanged.

9. Add two "endpoint is gone" tests next to the existing `test_supabase_exchange_oauth_code_endpoint_is_gone`/`test_supabase_refresh_access_token_endpoint_is_gone` (which stay, since those endpoints were already gone before this task):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd onboarding && uv run pytest tests/test_onboarding_router.py -k supabase -v`
Expected: FAIL — `/api/supabase/validate-key` doesn't exist yet (404s where 200 expected), `create-project` still requires the old session shape, `connect`/`callback`/`list-organizations` still exist (404 tests fail because they currently return 200/302, not 404).

- [ ] **Step 3: Implement the router changes**

In `onboarding/router.py`:

1. Delete `SupabaseConnectRequest` (lines 105-107) and add `name: str = Field(min_length=1, max_length=64)` to `SupabaseCreateProjectRequest` (currently line 109-110):

```python
class SupabaseKeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class SupabaseCreateProjectRequest(BaseModel):
    organization_slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
```

2. Delete `SUPABASE_OAUTH_CALLBACK_PATH` (line 208) and its preceding comment.
3. Delete `supabase_oauth_callback` (lines 234-272) in full.
4. Update `_render_index`'s comment (lines 212-214) — it currently says `"No client_id templating anymore -- /api/supabase/connect builds..."`; replace with `"No client_id templating -- there is no operator-level Supabase secret to template at all now."`.
5. In `GET /api/session` (`get_session_state`), replace the Supabase mapping block (lines 318-335):

```python
    supabase = data.get("supabase")
    if supabase and "database_url" in supabase:
        frames["supabase"] = {"complete": True, "display": {"name": supabase.get("name")}}
    elif supabase and "ref" in supabase:
        # Project created, but connection-info hasn't run yet (e.g. a
        # reload during the ~2 minute provisioning wait) -- resume
        # polling, don't report complete without a DATABASE_URL for the
        # final deploy step to find.
        frames["supabase"] = {
            "complete": False,
            "provisioning": True,
            "display": {"ref": supabase.get("ref"), "name": supabase.get("name")},
        }
```

(Delete the third `elif supabase and "access_token" in supabase:` branch — lines 331-335 — entirely. A key-validated-but-no-project-yet session now reports as not present in `frames` at all, the same gap the Render frame already leaves between key validation and service creation.)
6. Delete `_pkce_pair` (lines 461-466) — no longer used.
7. Replace `connect_supabase` (lines 469-499) with:

```python
@router.post("/api/supabase/validate-key")
async def validate_supabase_key(payload: SupabaseKeyRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or (await _get_session(session_id)) is None:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.validate_key(payload.key)
    if isinstance(result, supabase_client.SupabaseKeyValid):
        # replace=True: a resubmitted key (via "Change") must discard any
        # previous ref/db_pass/database_url outright -- see
        # test_validate_supabase_key_discards_a_previous_projects_data.
        write_result = await _update_frame(
            session_id, "supabase", {"api_key": payload.key}, replace=True
        )
        if isinstance(write_result, session_store.SessionNotFound):
            return {"valid": False, "reason": "no_session"}
        return {"valid": True, "orgs": [{"slug": o.slug, "name": o.name} for o in result.orgs]}
    return {"valid": False, "reason": result.reason}
```

8. Delete `list_supabase_organizations` (lines 502-511) in full.
9. Replace `create_supabase_project` (lines 514-549):

```python
@router.post("/api/supabase/create-project")
async def create_supabase_project(payload: SupabaseCreateProjectRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and (await _read_frame(session_id, "supabase"))
    if not supabase_frame or "api_key" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    db_pass = _secrets.token_urlsafe(24)
    result = await supabase_client.create_project(
        supabase_frame["api_key"], payload.organization_slug, payload.name, db_pass
    )
    if isinstance(result, supabase_client.SupabaseProjectCreated):
        write_result = await _update_frame(
            session_id,
            "supabase",
            {
                "ref": result.ref,
                "status": result.status,
                "db_pass": db_pass,
                "organization_slug": payload.organization_slug,
                "name": payload.name,
            },
        )
        if isinstance(write_result, session_store.SessionNotFound):
            return {"valid": False, "reason": "no_session"}
        return {"valid": True, "ref": result.ref, "status": result.status, "name": payload.name}
    if isinstance(result, supabase_client.SupabaseProjectRejected):
        return {"valid": False, "reason": "project_creation_rejected", "message": result.message}
    return {"valid": False, "reason": result.reason}
```

10. In `get_supabase_project_status` (was lines 552-563) and `get_supabase_connection_info` (was lines 566-585), replace every `supabase_frame["access_token"]` / `"access_token" not in supabase_frame` / `"access_token"` tuple entry with `supabase_frame["api_key"]` / `"api_key" not in supabase_frame` / `"api_key"` respectively. No other change to either function.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd onboarding && uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS — full file, not just `-k supabase` (confirms nothing else in the file broke).

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "$(cat <<'EOF'
feat: replace Supabase OAuth connect/callback with a validate-key endpoint

POST /api/supabase/validate-key replaces /api/supabase/connect and
GET /oauth/supabase/callback; create-project now takes name in its
request body instead of reading it from a pre-redirect session write.
The session's supabase.access_token field is renamed api_key. Part of
the OAuth-app-to-PAT redesign.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016s4DyZcniQEipH9QkxJdoD
EOF
)"
```

---

### Task 4: `onboarding/static/index.html` — paste-and-validate UI

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `onboarding/tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/supabase/validate-key` (Task 3), `POST /api/supabase/create-project` (now takes `name`).
- Produces: no new interfaces consumed by other frames — this is the wizard's own UI.

- [ ] **Step 1: Write the failing tests**

In `onboarding/tests/test_onboarding_page.py`:

1. Replace `test_list_organizations_leaves_the_page_exactly_once` (there is no more separate list-organizations call from the page) with a one-exit-path test for the new endpoint:

```python
async def test_validate_supabase_key_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/supabase/validate-key"') == 1
```

(This uses a direct `fetch()`, not `callSupabaseRelay`, mirroring `validateRenderKey`'s pattern — the credential itself is in the request body of this one call, same as Render's `validate-key`.)

2. `test_create_project_leaves_the_page_exactly_once` is unchanged (still `callSupabaseRelay("/api/supabase/create-project"`).

3. Replace `test_frame3_has_a_name_input_and_connect_button`:

```python
async def test_frame3_has_a_key_input_and_validate_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-key-input"' in body
    assert 'id="supabase-key-submit"' in body
```

4. Update `test_frame3_has_an_org_picker` to also check for the relocated name input:

```python
async def test_frame3_has_an_org_picker_and_a_name_input():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-org-select"' in body
    assert 'id="supabase-org-submit"' in body
    assert 'id="supabase-project-name-input"' in body
```

5. Update `test_frame3_strings_present_in_both_languages` — replace `"connect_supabase_button"` with `"validate_supabase_key_button"`, drop `"err_supabase_callback_invalid"` from the tuple only if you also remove that string in Step 3 below (this plan keeps it — it's a genuinely separate "missing local ref state" error unrelated to the OAuth callback route despite its name; see spec section 4 — so leave this key in the tuple unchanged), and update the final count assertion's key name to match:

```python
async def test_frame3_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame3_instructions",
        "frame3_name_placeholder",
        "validate_supabase_key_button",
        "frame3_org_instructions",
        "create_project_button",
        "err_supabase_name_empty",
        "err_supabase_empty_key",
        "err_supabase_invalid_key",
        "err_supabase_callback_invalid",
    ):
        assert f"{key}:" in body
    assert body.count("validate_supabase_key_button:") == 2  # STRINGS.en + STRINGS.he
```

6. Delete `test_org_picker_opens_the_frame_and_updates_its_badge` in full — it tests `fetchSupabaseOrganizations`/`showSupabaseOrgPicker`'s old "resume after OAuth redirect" wiring, which no longer exists (there's no redirect to resume from). Replace it with a test of the new always-shown org+name section:

```python
async def test_org_section_shown_after_key_validation_opens_the_frame_and_updates_its_badge():
    client = await _client()
    body = (await client.get("/")).text
    assert "function showSupabaseOrgSection" in body
    show_body = body[
        body.index("function showSupabaseOrgSection") : body.index(
            "async function confirmSupabaseOrg"
        )
    ]
    assert 'frameEl("supabase").open = true' in show_body
    assert 'setFrameStatus("supabase", "choosing_org")' in show_body
    assert body.count("badge_choosing_org:") == 2  # STRINGS.en + STRINGS.he
```

7. `test_terminal_supabase_errors_reset_the_connect_section`, `test_connection_info_missing_local_state_shows_an_error_not_a_silent_stall`, `test_supabase_credential_never_persists_to_local_storage`, `test_stored_supabase_credentials_are_parsed_defensively`, `test_reactive_refresh_helper_present`, `test_project_status_leaves_the_page_exactly_once`, `test_connection_info_leaves_the_page_exactly_once`, `test_frame3_has_a_check_again_button`, `test_polling_uses_a_five_second_interval_and_five_minute_timeout`, `test_target_status_is_active_healthy_and_init_failed_is_terminal`, `test_supabase_restore_uses_completed_flag_not_connection_string` are all unaffected by this task's changes and stay exactly as-is.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd onboarding && uv run pytest tests/test_onboarding_page.py -k supabase -v`
Expected: FAIL — the new/updated IDs and function names don't exist in the page yet.

- [ ] **Step 3: Implement the markup, JS, and string changes**

In `onboarding/static/index.html`:

1. Replace the `frame-supabase` markup block (lines 357-381):

```html
    <details id="frame-supabase" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame3_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="supabase" data-i18n="change_button"></button>
        <span class="frame-detail"></span>
      </summary>
      <div class="frame-body">
        <div id="supabase-connect-section">
          <p data-i18n="frame3_instructions"></p>
          <input id="supabase-key-input" type="password" autocomplete="off">
          <button id="supabase-key-submit" type="button" data-i18n="validate_supabase_key_button"></button>
        </div>
        <div id="supabase-org-section" style="display: none;">
          <p data-i18n="frame3_org_instructions"></p>
          <select id="supabase-org-select"></select>
          <input id="supabase-project-name-input" type="text">
          <button id="supabase-org-submit" type="button" data-i18n="create_project_button"></button>
        </div>
        <div id="supabase-provisioning-section" style="display: none;">
          <p data-i18n="frame3_provisioning"></p>
          <button id="supabase-check-status-submit" type="button" data-i18n="check_again_button" style="display: none;"></button>
        </div>
        <p id="supabase-error" class="frame-error"></p>
      </div>
    </details>
```

2. In the `STRINGS.en` block, replace:

```javascript
      frame3_instructions: "Name your Supabase project, then click Connect Supabase. This authorizes the wizard to create a new project on your own Supabase account.",
      frame3_name_placeholder: "pr-review-bot",
      connect_supabase_button: "Connect Supabase",
```

with:

```javascript
      frame3_instructions: "Paste a Supabase Personal Access Token (Account → Access Tokens → Generate new token). This lets the wizard create a new project on your own Supabase account.",
      frame3_name_placeholder: "pr-review-bot",
      validate_supabase_key_button: "Validate",
```

and replace:

```javascript
      err_supabase_name_empty: "Name your project first.",
      err_supabase_crypto_failed: "Your browser couldn't generate the required security value. Try a different browser or updating this one.",
      err_supabase_storage_failed: "Couldn't save your Supabase connection in this browser. Try again.",
      err_supabase_callback_invalid: "That didn't come back from Supabase as expected. Please try again.",
      err_supabase_invalid_code: "Supabase rejected that — the code may have expired. Try connecting again.",
      err_supabase_reauthorize: "Your Supabase session expired. Please connect again.",
```

with:

```javascript
      err_supabase_name_empty: "Name your project first.",
      err_supabase_empty_key: "Paste your Supabase Personal Access Token first.",
      err_supabase_invalid_key: "That token was rejected by Supabase. Double-check it and try again.",
      err_supabase_storage_failed: "Couldn't save your Supabase connection in this browser. Try again.",
      err_supabase_callback_invalid: "That didn't come back from Supabase as expected. Please try again.",
      err_supabase_reauthorize: "Your Supabase token was rejected. Reconnect with a valid Personal Access Token.",
```

(`err_supabase_crypto_failed` and `err_supabase_invalid_code` are deleted — both were PKCE/OAuth-code-exchange-specific and unused once this task lands. `err_supabase_callback_invalid` is kept, per Step 1.5 above.)

3. Make the identical three replacements in the `STRINGS.he` block (same key names, Hebrew text — translate naturally: e.g. `frame3_instructions` → something like `"הדביקו Personal Access Token של Supabase (Account → Access Tokens → Generate new token). זה מאפשר לאשף ליצור פרויקט חדש בחשבון ה-Supabase שלכם."`, `validate_supabase_key_button` → `"אימות"`, `err_supabase_empty_key` → `"הדביקו קודם את ה-Personal Access Token של Supabase."`, `err_supabase_invalid_key` → `"הטוקן נדחה על ידי Supabase. בדקו אותו ונסו שוב."`, `err_supabase_reauthorize` → `"הטוקן שלכם ל-Supabase נדחה. התחברו מחדש עם Personal Access Token תקף."` — delete `err_supabase_crypto_failed`/`err_supabase_invalid_code` here too).

4. Update the placeholder-wiring line (was line 802): `document.getElementById("supabase-project-name-input").placeholder = t("frame3_name_placeholder");` stays exactly as-is (same element id, still needs the placeholder wired).

5. Replace the comment block + `connectSupabase` function (lines 2026-2080) with:

```javascript
  // Paste-and-validate, same shape as validateRenderKey() above: the PAT
  // is submitted once, validated server-side, and persisted into the
  // session on success (POST /api/supabase/validate-key) -- it never
  // touches sessionStorage/localStorage. Every later Supabase call in
  // this frame goes through callSupabaseRelay(), which sends no
  // credential at all (the server reads it back from the session).
  async function validateSupabaseKey() {
    const input = document.getElementById("supabase-key-input");
    const key = input.value.trim();
    document.getElementById("supabase-error").textContent = "";
    currentSupabaseErrorKey = null;
    if (!key) {
      supabaseError("err_supabase_empty_key");
      return;
    }
    setFrameStatus("supabase", "checking");
    const submitBtn = document.getElementById("supabase-key-submit");
    submitBtn.disabled = true;
    let resp, body;
    try {
      resp = await fetch("/api/supabase/validate-key", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({key}),
      });
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      setFrameStatus("supabase", "ready");
      submitBtn.disabled = false;
      supabaseError("err_network");
      return;
    }
    if (!body.valid) {
      setFrameStatus("supabase", "ready");
      submitBtn.disabled = false;
      if (body.reason === "invalid_key") {
        supabaseError("err_supabase_invalid_key");
      } else {
        supabaseErrorForReason(body.reason);
      }
      return;
    }
    input.value = "";
    if (body.orgs.length === 0) {
      resetSupabaseConnectSection();
      supabaseError("err_supabase_no_organizations");
      return;
    }
    const select = document.getElementById("supabase-org-select");
    select.innerHTML = "";
    body.orgs.forEach((org) => {
      const option = document.createElement("option");
      option.value = org.slug;
      option.textContent = org.name;
      select.appendChild(option);
    });
    showSupabaseOrgSection();
  }

  function showSupabaseOrgSection() {
    document.getElementById("supabase-connect-section").style.display = "none";
    document.getElementById("supabase-org-section").style.display = "block";
    frameEl("supabase").open = true;
    setFrameStatus("supabase", "choosing_org");
  }
```

(`fetchSupabaseOrganizations` and `showSupabaseOrgPicker` are deleted entirely — folded into `validateSupabaseKey`/`showSupabaseOrgSection` above. The single-org auto-skip branch that used to call `kickOffProjectCreation` directly is intentionally not carried over: a project name always has to be entered now, so there's no longer a case where zero visitor interaction is needed after key validation.)

6. Replace `confirmSupabaseOrg` and `kickOffProjectCreation` (the functions immediately below, currently around lines 2125-2157):

```javascript
  async function confirmSupabaseOrg() {
    const select = document.getElementById("supabase-org-select");
    const nameInput = document.getElementById("supabase-project-name-input");
    const name = nameInput.value.trim();
    document.getElementById("supabase-error").textContent = "";
    currentSupabaseErrorKey = null;
    if (!name) {
      supabaseError("err_supabase_name_empty");
      return;
    }
    const submitBtn = document.getElementById("supabase-org-submit");
    submitBtn.disabled = true;
    const ok = await kickOffProjectCreation(select.value, name);
    if (!ok) submitBtn.disabled = false;
  }

  async function kickOffProjectCreation(organizationSlug, name) {
    const body = await callSupabaseRelay("/api/supabase/create-project", {
      organization_slug: organizationSlug,
      name,
    });
    if (!body.valid) {
      supabaseErrorForReason(body.reason, body.message);
      return false;
    }
    // Non-secret continuity state for this page view (polling needs `ref`;
    // the final completeFrame call needs `name`) -- not a credential, so
    // sessionStorage is fine here. db_pass/api_key never reach the
    // browser at all now (session_store.py holds them server-side).
    try {
      sessionStorage.setItem(
        STORAGE_KEYS["supabase"],
        JSON.stringify({name: body.name, ref: body.ref, status: body.status}),
      );
    } catch (err) {
      supabaseError("err_supabase_storage_failed");
      return false;
    }
    showSupabaseProvisioning();
    pollUntilReady(Date.now(), supabasePollGeneration);
    return true;
  }
```

7. In `supabaseErrorForReason` (the reason-to-key map, was lines 1995-2002), remove the `invalid_code: "err_supabase_invalid_code",` entry — every other entry (`unauthorized`, `no_session`, `forbidden`, `rate_limited`, `pooler_config_unavailable`) stays, since `create-project`/`project-status`/`connection-info` can still return any of those.

8. Update the event-listener wiring (was lines 2618-2619):

```javascript
    document.getElementById("supabase-key-submit").addEventListener("click", validateSupabaseKey);
    document.getElementById("supabase-org-submit").addEventListener("click", confirmSupabaseOrg);
```

9. In `restoreFromSession()`, delete the `else if (supabaseFrame && supabaseFrame.authorized) { ... await fetchSupabaseOrganizations(); }` branch (was lines 2510-2519) in full — that resumable state no longer exists (see Task 3 Step 3.5). The `if (supabaseFrame && supabaseFrame.complete)` and `else if (supabaseFrame && supabaseFrame.provisioning)` branches directly above it are unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd onboarding && uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS — full file.

- [ ] **Step 5: Commit**

```bash
git add onboarding/static/index.html onboarding/tests/test_onboarding_page.py
git commit -m "$(cat <<'EOF'
feat: Supabase frame UI: paste-and-validate PAT instead of OAuth redirect

The project-name input moves from before key validation to alongside
the org picker, since there's no more pre-redirect step to have
captured it earlier. Part of the OAuth-app-to-PAT redesign.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016s4DyZcniQEipH9QkxJdoD
EOF
)"
```

---

### Task 5: `onboarding/CLAUDE.md` — rewrite the sub-project 3 section, full-suite verification

**Files:**
- Modify: `onboarding/CLAUDE.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by code — this is the last task, verifying the whole branch.

- [ ] **Step 1: Rewrite `onboarding/CLAUDE.md`'s "What sub-project 3 (Supabase provisioning) adds to these rules" section**

Replace the entire section (from `## What sub-project 3 (Supabase provisioning) adds to these rules` through the bullet ending `...See ISSUES.md's Design Gaps section before changing anything about how the OAuth app is used or exposed.` — i.e. every bullet in that section) with:

```markdown
## What sub-project 3 (Supabase provisioning) adds to these rules

- **The credential is a visitor-pasted Personal Access Token, not an
  OAuth app (2026-09-04 redesign)** — see
  `docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md`. The
  original design used an operator-registered OAuth app, which made this
  service's one shared credential across every visitor; a follow-up
  brainstorm found the stated reason for that choice ("PAT can't do full
  automation") didn't hold up against Supabase's own docs, so this
  frame now matches every other frame's model (Render, GitHub App, LLM
  provider, UptimeRobot): the visitor supplies their own credential, no
  operator-level Supabase secret exists.
- **`POST /api/supabase/validate-key` does both credential validation and
  org listing in one call** (`supabase_client.validate_key`, one
  `GET /v1/organizations` request) — Supabase has no separate
  token-identity endpoint, so this doubles as both. On success the PAT is
  persisted server-side (`session_store.py`, under the `supabase` frame's
  `api_key` field) via `_update_frame(..., replace=True)` — a resubmitted
  key (via "Change") must discard any previous project's `ref`/`db_pass`/
  `database_url`, same reasoning the Render/GitHub validate-key endpoints
  already document.
- **The project name is captured alongside the org picker, after key
  validation** — not before, since there's no pre-redirect step anymore
  to have captured it earlier (the original OAuth design had the visitor
  type it before authorizing). `create-project`'s request body carries
  both `organization_slug` and `name` now; the session never pre-stores
  `name` on its own.
- **`db_pass` is still generated server-side** (`create-project`,
  `router.py`) — this was already true before this redesign (2026-09-02)
  and is unaffected by the credential swap. It never needs to leave the
  server: `create-project` mints it, passes it directly to
  `supabase_client.create_project()`, and stores it in the session for
  `connection-info` to assemble the final `DATABASE_URL` with later.
- **`connection-info` never returns Supabase's own `connection_string`/
  `connectionString` fields, nor `db_user`/`db_host`/`db_port`/`db_name`
  individually** — unchanged from before this redesign. Since `db_pass`
  already lives server-side, the endpoint assembles the full
  `postgresql://` URL itself and stores it in the session
  (`supabase.database_url`); its response to the browser is just
  `{"valid": true}`.
- **`create-project`, `project-status`, and `connection-info` all read
  `api_key` from the session** (via `session_store.read_frame`), never
  from the request body — set by `validate-key` above. There is no
  client-facing refresh path (there never was one exposed to the
  browser even under OAuth) and nothing to refresh: a PAT doesn't expire
  the way an OAuth access token does.
```

- [ ] **Step 2: Run the full test suite and lint**

Run: `cd onboarding && uv run pytest -v`
Expected: PASS — every test in `onboarding/tests/`, not just the Supabase-related files touched by Tasks 1-4.

Run: `uv run ruff check .` (from the repo root)
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add onboarding/CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: rewrite onboarding/CLAUDE.md's Supabase sub-project 3 section for the PAT redesign

Every OAuth-specific bullet (operator secret, redirect/PKCE/cookie
mechanics, db_pass timing relative to the redirect) either no longer
describes real code or needed restating in PAT terms. Completes the
OAuth-app-to-PAT redesign
(docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016s4DyZcniQEipH9QkxJdoD
EOF
)"
```
