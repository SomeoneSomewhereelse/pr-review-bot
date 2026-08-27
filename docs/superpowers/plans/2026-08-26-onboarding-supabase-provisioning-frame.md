# Onboarding Wizard — Supabase Provisioning (Frame 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the onboarding wizard's frame 3 fully functional — a visitor
authorizes the wizard against their own Supabase account via OAuth, the
wizard creates a real Supabase project on their behalf, and captures a
ready-to-use `DATABASE_URL`-shaped connection string into `sessionStorage`,
ready for sub-project 6 to push into a Render service's env vars.

**Architecture:** One new backend module (`onboarding/supabase_client.py`)
wrapping Supabase's OAuth2 flow and Management API, six new relay endpoints
in `router.py`, and frontend JS that drives one full-page round trip to
`api.supabase.com` and back (PKCE + CSRF `state`, the same pattern
established for GitHub's manifest flow), followed by a client-side polling
loop against this service's own relay while the project provisions.

**Tech Stack:** FastAPI, httpx (all Supabase calls — OAuth token exchange
and every Management API call), pydantic, vanilla JS (no build step,
including `crypto.subtle` for PKCE's SHA-256 challenge), pytest +
pytest-asyncio + respx (no PyGithub-style transport patching needed —
everything here is plain `httpx`).

**Spec:**
`docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md`

## Global Constraints

- `onboarding/` is a **stateless relay** — no database, no session store, no
  server-side persistence of any visitor credential, ever (onboarding/CLAUDE.md).
- **Never log a visitor-supplied, freshly-minted, or browser-generated
  credential**, in full or truncated — this includes the OAuth
  access/refresh tokens and the browser-generated `db_pass`.
- Every relay endpoint returns a verdict, never a submitted or held
  credential — **except** `/api/supabase/exchange-oauth-code` and
  `/api/supabase/refresh-access-token`, which mint and return tokens that
  belong to the visitor who just authorized (spec section 4), and
  `/api/supabase/create-project`, which on a business-rule rejection
  relays Supabase's own error `message` text verbatim (spec section 4) —
  never a guessed/hardcoded reason for *which* rule was violated, since
  that cannot be verified without a live authenticated call this project's
  testing-hygiene discipline (root `CLAUDE.md`) counsels against making
  speculatively.
- `SUPABASE_OAUTH_CLIENT_ID` / `SUPABASE_OAUTH_CLIENT_SECRET` are this
  service's **first operator-level secrets** — set once on the onboarding
  Render service, never visitor-supplied, checked for presence in
  `onboarding/main.py`'s `lifespan` (same pattern as `public_base_url`, no
  pydantic-required field — that would raise at import time, breaking
  pytest collection before a clear error could ever be reported).
  `supabase_oauth_client_id` is also templated into the served page as
  `window.SUPABASE_OAUTH_CLIENT_ID` — this is **not** a secret exposure: a
  `client_id` is the public half of OAuth credentials by design (only
  `client_secret` is confidential, and it never leaves the backend).
- **`create-project`'s `db_pass` is generated client-side by the browser**
  (Web Crypto API, alphanumeric-only, 32 characters), never minted by the
  backend — deliberately different from the GitHub frame's private-key
  mint-and-return pattern, because `db_pass` is a value *we* choose, not
  one only Supabase can produce. Keeping it browser-originated avoids
  growing the mint-and-return exception list for a value that doesn't need
  it (spec section 5).
- **`connection-info` never returns Supabase's own `connection_string` /
  `connectionString` fields** — whether they embed the real password or a
  masked placeholder cannot be verified from documentation. It returns only
  `{db_user, db_host, db_port, db_name}`; the browser (which already holds
  `db_pass`) assembles the final connection string itself.
- **Token refresh is reactive, not proactive**: any Supabase relay call
  that comes back `reason: "unauthorized"` triggers exactly one
  refresh-then-retry attempt; no client-side expiry-timer bookkeeping.
- New external-service integrations follow the existing relay shape:
  browser holds the token, backend is a stateless pass-through per request.
- `onboarding/` and `app/` never share credential-handling code paths.
- No bare `except Exception` anywhere — narrow, explicit exception handling
  only, matching `render_client.py`'s and `github_client.py`'s existing
  convention.
- No live Supabase API calls in tests — mock `httpx` responses with `respx`.
- A visitor's credential lives in `sessionStorage`, never `localStorage`.
- `GET /`'s CSP does **not** need a new `form-action` origin — this frame's
  only full-page navigation (`GET /v1/oauth/authorize`) is a plain redirect
  (`location.href = ...`), not a form POST, unlike GitHub's manifest step.

---

### Task 1: `onboarding/config.py` + `onboarding/main.py` — Supabase OAuth app settings

**Files:**
- Modify: `onboarding/config.py`
- Modify: `onboarding/main.py`
- Test: `tests/test_onboarding_config.py` (modify)
- Test: `tests/test_onboarding_main.py` (modify)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `settings.supabase_oauth_client_id: str` and
  `settings.supabase_oauth_client_secret: str` (both default `""`, read
  from `SUPABASE_OAUTH_CLIENT_ID`/`SUPABASE_OAUTH_CLIENT_SECRET`).
  `onboarding.main.lifespan` now also raises `RuntimeError` if either is
  empty — used by Task 5's endpoints and Task 6's frontend templating.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_config.py` (after the existing
`test_public_base_url_reads_from_environment`):
```python
def test_supabase_oauth_client_id_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("SUPABASE_OAUTH_CLIENT_ID", raising=False)
    assert Settings().supabase_oauth_client_id == ""


def test_supabase_oauth_client_id_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_ID", "66666666-6666-4666-8666-666666666666")
    assert Settings().supabase_oauth_client_id == "66666666-6666-4666-8666-666666666666"


def test_supabase_oauth_client_secret_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("SUPABASE_OAUTH_CLIENT_SECRET", raising=False)
    assert Settings().supabase_oauth_client_secret == ""


def test_supabase_oauth_client_secret_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_SECRET", "sb_secret_sentinel")
    assert Settings().supabase_oauth_client_secret == "sb_secret_sentinel"
```

Replace the existing lifespan test in `tests/test_onboarding_main.py`:
```python
async def test_lifespan_starts_with_public_base_url_set(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    async with lifespan(app):
        pass
```
with:
```python
async def test_lifespan_starts_with_everything_set(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    monkeypatch.setattr(settings, "supabase_oauth_client_id", "sentinel-client-id")
    monkeypatch.setattr(settings, "supabase_oauth_client_secret", "sentinel-client-secret")
    async with lifespan(app):
        pass


async def test_lifespan_refuses_to_start_without_supabase_client_id(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    monkeypatch.setattr(settings, "supabase_oauth_client_id", "")
    monkeypatch.setattr(settings, "supabase_oauth_client_secret", "sentinel-client-secret")
    with pytest.raises(RuntimeError, match="SUPABASE_OAUTH_CLIENT_ID"):
        async with lifespan(app):
            pass


async def test_lifespan_refuses_to_start_without_supabase_client_secret(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    monkeypatch.setattr(settings, "supabase_oauth_client_id", "sentinel-client-id")
    monkeypatch.setattr(settings, "supabase_oauth_client_secret", "")
    with pytest.raises(RuntimeError, match="SUPABASE_OAUTH_CLIENT_SECRET"):
        async with lifespan(app):
            pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_config.py tests/test_onboarding_main.py -v`
Expected: FAIL — `AttributeError` for the new `Settings` fields, and the
new lifespan tests fail because the check doesn't exist yet (the
"starts_with_everything_set" test currently fails for the opposite reason:
it would pass today since the check doesn't exist, but the two new RED
tests fail because no `RuntimeError` is raised).

- [ ] **Step 3: Modify `onboarding/config.py`**

Replace:
```python
    public_base_url: str = ""

    @field_validator("public_base_url")
```
with:
```python
    public_base_url: str = ""

    # This service's first operator-level secrets: set once by the operator
    # after manually registering an OAuth app in Supabase org settings ->
    # OAuth Apps (Supabase has no self-registration mechanism, unlike
    # GitHub's App Manifest flow). Never visitor-supplied. No shape
    # validator like public_base_url's: a malformed client_id/secret fails
    # visibly at OAuth-authorize time before any credential is created,
    # a much lower-stakes failure mode than public_base_url's (an
    # unrecoverable orphaned GitHub App).
    supabase_oauth_client_id: str = ""
    supabase_oauth_client_secret: str = ""

    @field_validator("public_base_url")
```

- [ ] **Step 4: Modify `onboarding/main.py`**

Replace:
```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.public_base_url:
        raise RuntimeError(
            "PUBLIC_BASE_URL is unset — refusing to start. Frame 2's GitHub "
            "App manifest needs this service's own real public URL to build "
            "redirect_url/setup_url; without it the manifest flow cannot work."
        )
    yield
```
with:
```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.public_base_url:
        raise RuntimeError(
            "PUBLIC_BASE_URL is unset — refusing to start. Frame 2's GitHub "
            "App manifest needs this service's own real public URL to build "
            "redirect_url/setup_url; without it the manifest flow cannot work."
        )
    if not settings.supabase_oauth_client_id:
        raise RuntimeError(
            "SUPABASE_OAUTH_CLIENT_ID is unset — refusing to start. Frame 3's "
            "Supabase OAuth flow needs this service's registered OAuth app id; "
            "without it the authorize redirect cannot work."
        )
    if not settings.supabase_oauth_client_secret:
        raise RuntimeError(
            "SUPABASE_OAUTH_CLIENT_SECRET is unset — refusing to start. Frame "
            "3's Supabase OAuth token exchange needs this service's registered "
            "OAuth app secret; without it the exchange cannot work."
        )
    yield
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_config.py tests/test_onboarding_main.py -v`
Expected: PASS (all tests, including the pre-existing `healthz` and
`public_base_url` ones).

- [ ] **Step 6: Commit**

```bash
git add onboarding/config.py onboarding/main.py tests/test_onboarding_config.py tests/test_onboarding_main.py
git commit -m "feat: add Supabase OAuth app settings with boot-time presence check"
```

---

### Task 2: `onboarding/supabase_client.py` — OAuth token exchange + refresh

**Files:**
- Create: `onboarding/supabase_client.py`
- Test: `tests/test_onboarding_supabase_client.py` (create)

**Interfaces:**
- Consumes: `onboarding.config.settings.supabase_oauth_client_id`,
  `.supabase_oauth_client_secret` (Task 1).
- Produces: `SupabaseTokens(access_token: str, refresh_token: str | None,
  expires_in: int)`, `SupabaseOAuthFailed(reason: str)` (reason ∈
  `"invalid_code"` | `"unauthorized"` | `"supabase_unreachable"`),
  `SupabaseTokenResult = SupabaseTokens | SupabaseOAuthFailed`,
  `async def exchange_oauth_code(code: str, code_verifier: str,
  redirect_uri: str) -> SupabaseTokenResult`,
  `async def refresh_access_token(refresh_token: str) -> SupabaseTokenResult`
  — used by Task 5's router.

- [ ] **Step 1: Write the failing tests**

`tests/test_onboarding_supabase_client.py`:
```python
"""Tests for onboarding/supabase_client.py — Supabase's OAuth token
endpoint is form-encoded (application/x-www-form-urlencoded), not JSON,
and client_id/client_secret are body fields — verified directly against
the raw OpenAPI schema (not prose docs, which incorrectly describe HTTP
Basic Auth) during this sub-project's brainstorm. See
docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md
sections 3-5."""
from __future__ import annotations

import httpx
import pytest
import respx

from onboarding import supabase_client
from onboarding.config import settings

TOKEN_URL = "https://api.supabase.com/v1/oauth/token"


@pytest.fixture(autouse=True)
def _oauth_app_credentials(monkeypatch):
    monkeypatch.setattr(settings, "supabase_oauth_client_id", "sentinel-client-id")
    monkeypatch.setattr(settings, "supabase_oauth_client_secret", "sentinel-client-secret")


async def test_valid_code_returns_tokens():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "sentinel-access",
                    "refresh_token": "sentinel-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        result = await supabase_client.exchange_oauth_code(
            "sentinel-code", "sentinel-verifier",
            "https://onboarding.example.com/?supabase_step=oauth_callback",
        )
    assert result == supabase_client.SupabaseTokens(
        access_token="sentinel-access", refresh_token="sentinel-refresh", expires_in=3600
    )


async def test_exchange_sends_form_encoded_body_not_json():
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "a", "expires_in": 1, "token_type": "Bearer"}
            )
        )
        await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    body = request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "client_id=sentinel-client-id" in body
    assert "client_secret=sentinel-client-secret" in body
    assert "code=c" in body
    assert "code_verifier=v" in body


async def test_response_missing_refresh_token_is_tolerated():
    """OAuthTokenResponse's schema does not require refresh_token — some
    grant types omit it entirely; the caller must handle None."""
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "sentinel-access", "expires_in": 3600, "token_type": "Bearer"}
            )
        )
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseTokens(
        access_token="sentinel-access", refresh_token=None, expires_in=3600
    )


async def test_rejected_code_is_invalid_code():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        result = await supabase_client.exchange_oauth_code("bad", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="invalid_code")


async def test_exchange_5xx_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_exchange_timeout_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_exchange_malformed_200_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_exchange_response_missing_access_token_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"expires_in": 1, "token_type": "Bearer"})
        )
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_refresh_valid_token_returns_new_tokens():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        result = await supabase_client.refresh_access_token("sentinel-refresh")
    assert result == supabase_client.SupabaseTokens(
        access_token="new-access", refresh_token="new-refresh", expires_in=3600
    )


async def test_refresh_sends_refresh_token_grant():
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 1, "token_type": "Bearer"})
        )
        await supabase_client.refresh_access_token("sentinel-refresh")
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=sentinel-refresh" in body


async def test_refresh_rejected_is_unauthorized():
    """Unlike exchange_oauth_code, a rejected refresh maps to 'unauthorized'
    (the reactive-refresh-then-retry vocabulary), not 'invalid_code' — no
    'code' is involved in a refresh grant."""
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        result = await supabase_client.refresh_access_token("stale-refresh")
    assert result == supabase_client.SupabaseOAuthFailed(reason="unauthorized")


async def test_refresh_5xx_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.refresh_access_token("sentinel-refresh")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'onboarding.supabase_client'`

- [ ] **Step 3: Write `onboarding/supabase_client.py`**

```python
"""Thin async wrapper around Supabase's OAuth2 flow and Management API —
authorizes as a visitor and provisions their own Supabase project, without
persisting any credential server-side. See
docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md
sections 3-5."""
from __future__ import annotations

import dataclasses

import httpx

from onboarding.config import settings

SUPABASE_OAUTH_BASE = "https://api.supabase.com/v1/oauth"
SUPABASE_API_BASE = "https://api.supabase.com/v1"
SUPABASE_REGION_CODE = "us-east-1"


@dataclasses.dataclass(frozen=True)
class SupabaseTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclasses.dataclass(frozen=True)
class SupabaseOAuthFailed:
    reason: str  # "invalid_code" | "unauthorized" | "supabase_unreachable"


SupabaseTokenResult = SupabaseTokens | SupabaseOAuthFailed


def _parse_token_response(response: httpx.Response, invalid_reason: str) -> SupabaseTokenResult:
    if response.status_code >= 500:
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    if response.status_code >= 400:
        return SupabaseOAuthFailed(reason=invalid_reason)
    try:
        body = response.json()
        access_token = str(body["access_token"])
        expires_in = int(body["expires_in"])
    except (ValueError, KeyError, TypeError):
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    refresh_token = body.get("refresh_token")
    return SupabaseTokens(
        access_token=access_token,
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_in=expires_in,
    )


async def exchange_oauth_code(code: str, code_verifier: str, redirect_uri: str) -> SupabaseTokenResult:
    """Trade the OAuth authorization code for tokens (POST /v1/oauth/token,
    grant_type=authorization_code). Form-encoded per Supabase's own schema
    — NOT JSON — and client_id/client_secret are body fields, not an
    Authorization header. Never logs the response body, which carries the
    visitor's access/refresh tokens."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{SUPABASE_OAUTH_BASE}/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.supabase_oauth_client_id,
                    "client_secret": settings.supabase_oauth_client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                },
            )
    except httpx.HTTPError:
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    return _parse_token_response(response, invalid_reason="invalid_code")


async def refresh_access_token(refresh_token: str) -> SupabaseTokenResult:
    """Trade a refresh token for a new access token (grant_type=refresh_token).
    Never logs the refresh token or the response body."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{SUPABASE_OAUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.supabase_oauth_client_id,
                    "client_secret": settings.supabase_oauth_client_secret,
                    "refresh_token": refresh_token,
                },
            )
    except httpx.HTTPError:
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    return _parse_token_response(response, invalid_reason="unauthorized")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add onboarding/supabase_client.py tests/test_onboarding_supabase_client.py
git commit -m "feat: add Supabase OAuth token exchange and refresh"
```

---

### Task 3: `onboarding/supabase_client.py` — organization listing + project creation

**Files:**
- Modify: `onboarding/supabase_client.py`
- Modify: `tests/test_onboarding_supabase_client.py`

**Interfaces:**
- Consumes: nothing new from Task 2 (independent functions).
- Produces: `SupabaseOrg(slug: str, name: str)`,
  `SupabaseOrgsListed(orgs: list[SupabaseOrg])`,
  `SupabaseApiFailed(reason: str)` (reason ∈ `"unauthorized"` |
  `"forbidden"` | `"rate_limited"` | `"supabase_unreachable"` — this same
  type is reused with additional reason values by Task 4),
  `SupabaseOrgsResult = SupabaseOrgsListed | SupabaseApiFailed`,
  `async def list_organizations(access_token: str) -> SupabaseOrgsResult`;
  `SupabaseProjectCreated(ref: str, status: str)`,
  `SupabaseProjectRejected(message: str)`,
  `async def create_project(access_token: str, organization_slug: str,
  name: str, db_pass: str) -> SupabaseProjectCreated |
  SupabaseProjectRejected | SupabaseApiFailed` — both used by Task 5's router.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_supabase_client.py`:
```python
import json as json_module

ORGS_URL = "https://api.supabase.com/v1/organizations"
PROJECTS_URL = "https://api.supabase.com/v1/projects"


async def test_list_organizations_returns_orgs():
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
        result = await supabase_client.list_organizations("sentinel-access")
    assert result == supabase_client.SupabaseOrgsListed(
        orgs=[
            supabase_client.SupabaseOrg(slug="org-one", name="Org One"),
            supabase_client.SupabaseOrg(slug="org-two", name="Org Two"),
        ]
    )


async def test_list_organizations_sends_bearer_token():
    with respx.mock:
        route = respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
        await supabase_client.list_organizations("sentinel-access")
    assert route.calls.last.request.headers["authorization"] == "Bearer sentinel-access"


async def test_list_organizations_unauthorized():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.list_organizations("expired")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_list_organizations_rate_limited():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(429))
        result = await supabase_client.list_organizations("a")
    assert result == supabase_client.SupabaseApiFailed(reason="rate_limited")


async def test_list_organizations_unreachable_on_5xx():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.list_organizations("a")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_list_organizations_malformed_body_is_unreachable():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await supabase_client.list_organizations("a")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_create_project_returns_ref_and_status():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(201, json={"ref": "abcdefghijklmnopqrst", "status": "INACTIVE"})
        )
        result = await supabase_client.create_project(
            "sentinel-access", "org-one", "pr-review-bot", "sentinelpass123"
        )
    assert result == supabase_client.SupabaseProjectCreated(ref="abcdefghijklmnopqrst", status="INACTIVE")


async def test_create_project_sends_region_selection_not_deprecated_fields():
    with respx.mock:
        route = respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(201, json={"ref": "x" * 20, "status": "INACTIVE"})
        )
        await supabase_client.create_project("sentinel-access", "org-one", "pr-review-bot", "pw")
    payload = json_module.loads(route.calls.last.request.content)
    assert payload["region_selection"] == {"type": "specific", "code": "us-east-1"}
    assert "region" not in payload
    assert "plan" not in payload
    assert "desired_instance_size" not in payload


async def test_create_project_never_logs_or_returns_the_password(caplog):
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(201, json={"ref": "x" * 20, "status": "INACTIVE"})
        )
        result = await supabase_client.create_project(
            "sentinel-access", "org-one", "pr-review-bot", "SENTINEL_DO_NOT_LOG_PASSWORD"
        )
    assert "SENTINEL_DO_NOT_LOG_PASSWORD" not in caplog.text
    assert "SENTINEL_DO_NOT_LOG_PASSWORD" not in repr(result)


async def test_create_project_unauthorized():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.create_project("expired", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_create_project_rate_limited():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(429))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="rate_limited")


async def test_create_project_business_rule_rejection_relays_the_message():
    """Covers the free-tier-cap case and any other business-rule rejection:
    relay Supabase's own message verbatim rather than guessing which rule
    was violated (spec section 4)."""
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(
                403, json={"message": "This organization already has the maximum number of free projects."}
            )
        )
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseProjectRejected(
        message="This organization already has the maximum number of free projects."
    )


async def test_create_project_rejection_without_a_message_falls_back_to_unreachable():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(403, text="not json"))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_create_project_unreachable_on_5xx():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: FAIL — `AttributeError: module 'onboarding.supabase_client' has
no attribute 'list_organizations'` (and `create_project`, `SupabaseOrg`,
`SupabaseOrgsListed`, `SupabaseApiFailed`, `SupabaseProjectCreated`,
`SupabaseProjectRejected`).

- [ ] **Step 3: Append to `onboarding/supabase_client.py`**

```python
@dataclasses.dataclass(frozen=True)
class SupabaseOrg:
    slug: str
    name: str


@dataclasses.dataclass(frozen=True)
class SupabaseOrgsListed:
    orgs: list[SupabaseOrg]


@dataclasses.dataclass(frozen=True)
class SupabaseApiFailed:
    reason: str
    # "unauthorized" | "forbidden" | "rate_limited" | "supabase_unreachable"
    # | "pooler_config_unavailable" (Task 4 only)


SupabaseOrgsResult = SupabaseOrgsListed | SupabaseApiFailed


async def list_organizations(access_token: str) -> SupabaseOrgsResult:
    """GET /v1/organizations — the orgs the visitor's own token can act on.
    Never logs the access token or the response body."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                "/organizations",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 403:
        return SupabaseApiFailed(reason="forbidden")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code != 200:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        body = response.json()
        orgs = [SupabaseOrg(slug=str(o["slug"]), name=str(o["name"])) for o in body]
    except (ValueError, KeyError, TypeError):
        return SupabaseApiFailed(reason="supabase_unreachable")
    return SupabaseOrgsListed(orgs=orgs)


@dataclasses.dataclass(frozen=True)
class SupabaseProjectCreated:
    ref: str
    status: str


@dataclasses.dataclass(frozen=True)
class SupabaseProjectRejected:
    message: str


async def create_project(
    access_token: str, organization_slug: str, name: str, db_pass: str
) -> SupabaseProjectCreated | SupabaseProjectRejected | SupabaseApiFailed:
    """POST /v1/projects — provisions a new project inside the visitor's own
    organization, on their own token. db_pass is already the browser's own
    value (generated client-side, spec section 5) — relayed through, never
    minted or logged here. Omits the deprecated `region`/`plan` fields and
    `desired_instance_size` (defaults to the smallest tier) per spec
    section 3 step 7."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.post(
                "/projects",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "organization_slug": organization_slug,
                    "name": name,
                    "db_pass": db_pass,
                    "region_selection": {"type": "specific", "code": SUPABASE_REGION_CODE},
                },
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code >= 500:
        return SupabaseApiFailed(reason="supabase_unreachable")
    if response.status_code >= 400:
        # No guaranteed structured error body (spec section 4) — relay
        # Supabase's own message verbatim rather than guessing which
        # business rule (free-tier cap or otherwise) was violated.
        try:
            message = response.json().get("message")
        except ValueError:
            message = None
        if message:
            return SupabaseProjectRejected(message=str(message))
        return SupabaseApiFailed(reason="supabase_unreachable")
    if response.status_code != 201:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        body = response.json()
        ref = str(body["ref"])
        status = str(body["status"])
    except (ValueError, KeyError, TypeError):
        return SupabaseApiFailed(reason="supabase_unreachable")
    return SupabaseProjectCreated(ref=ref, status=status)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: PASS (27 tests total)

- [ ] **Step 5: Commit**

```bash
git add onboarding/supabase_client.py tests/test_onboarding_supabase_client.py
git commit -m "feat: add Supabase organization listing and project creation"
```

---

### Task 4: `onboarding/supabase_client.py` — status polling + connection info

**Files:**
- Modify: `onboarding/supabase_client.py`
- Modify: `tests/test_onboarding_supabase_client.py`

**Interfaces:**
- Consumes: `SupabaseApiFailed` (Task 3, reused with the additional
  `"pooler_config_unavailable"` reason).
- Produces: `SupabaseProjectStatus(status: str)`,
  `async def get_project_status(access_token: str, ref: str) ->
  SupabaseProjectStatus | SupabaseApiFailed`;
  `SupabaseConnectionInfo(db_user: str, db_host: str, db_port: int,
  db_name: str)`,
  `async def get_connection_info(access_token: str, ref: str) ->
  SupabaseConnectionInfo | SupabaseApiFailed` — both used by Task 5's router.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_supabase_client.py`:
```python
PROJECT_STATUS_URL = "https://api.supabase.com/v1/projects/abcdefghijklmnopqrst"
POOLER_URL = "https://api.supabase.com/v1/projects/abcdefghijklmnopqrst/config/database/pooler"

_POOLER_ENTRIES = [
    {
        "identifier": "abcdefghijklmnopqrst", "database_type": "PRIMARY", "is_using_scram_auth": True,
        "db_user": "postgres.abcdefghijklmnopqrst", "db_host": "aws-0-us-east-1.pooler.supabase.com",
        "db_port": 6543, "db_name": "postgres", "connection_string": "postgresql://masked",
        "connectionString": "postgresql://masked", "default_pool_size": None, "max_client_conn": None,
        "pool_mode": "transaction",
    },
    {
        "identifier": "abcdefghijklmnopqrst", "database_type": "PRIMARY", "is_using_scram_auth": True,
        "db_user": "postgres.abcdefghijklmnopqrst", "db_host": "aws-0-us-east-1.pooler.supabase.com",
        "db_port": 5432, "db_name": "postgres", "connection_string": "postgresql://masked",
        "connectionString": "postgresql://masked", "default_pool_size": None, "max_client_conn": None,
        "pool_mode": "session",
    },
]


async def test_get_project_status_returns_status():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"status": "ACTIVE_HEALTHY", "ref": "x"})
        )
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseProjectStatus(status="ACTIVE_HEALTHY")


async def test_get_project_status_unauthorized():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_get_project_status_unreachable_on_5xx():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_get_project_status_malformed_body_is_unreachable():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(return_value=httpx.Response(200, json={}))
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_get_connection_info_selects_the_session_mode_primary_entry():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=_POOLER_ENTRIES))
        result = await supabase_client.get_connection_info("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseConnectionInfo(
        db_user="postgres.abcdefghijklmnopqrst",
        db_host="aws-0-us-east-1.pooler.supabase.com",
        db_port=5432,
        db_name="postgres",
    )


async def test_get_connection_info_never_returns_supabases_own_connection_string_field():
    """Deliberate: whether connection_string embeds the real password or a
    masked placeholder cannot be verified from documentation (spec section
    3 step 9) — the caller assembles the string itself from this shape."""
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=_POOLER_ENTRIES))
        result = await supabase_client.get_connection_info("a", "abcdefghijklmnopqrst")
    assert not hasattr(result, "connection_string")
    assert not hasattr(result, "connectionString")


async def test_get_connection_info_no_session_mode_entry_is_pooler_config_unavailable():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=[_POOLER_ENTRIES[0]]))
        result = await supabase_client.get_connection_info("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")


async def test_get_connection_info_empty_array_is_pooler_config_unavailable():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await supabase_client.get_connection_info("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")


async def test_get_connection_info_unauthorized():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.get_connection_info("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_get_connection_info_unreachable_on_5xx():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.get_connection_info("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: FAIL — `AttributeError` for `get_project_status`,
`get_connection_info`, `SupabaseProjectStatus`, `SupabaseConnectionInfo`.

- [ ] **Step 3: Append to `onboarding/supabase_client.py`**

```python
@dataclasses.dataclass(frozen=True)
class SupabaseProjectStatus:
    status: str


async def get_project_status(access_token: str, ref: str) -> SupabaseProjectStatus | SupabaseApiFailed:
    """GET /v1/projects/{ref} — polled by the browser during provisioning.
    Target status is ACTIVE_HEALTHY; the caller treats INIT_FAILED as a
    terminal failure and every other status as still-provisioning."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                f"/projects/{ref}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 403:
        return SupabaseApiFailed(reason="forbidden")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code != 200:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        status = str(response.json()["status"])
    except (ValueError, KeyError, TypeError):
        return SupabaseApiFailed(reason="supabase_unreachable")
    return SupabaseProjectStatus(status=status)


@dataclasses.dataclass(frozen=True)
class SupabaseConnectionInfo:
    db_user: str
    db_host: str
    db_port: int
    db_name: str


async def get_connection_info(access_token: str, ref: str) -> SupabaseConnectionInfo | SupabaseApiFailed:
    """GET /v1/projects/{ref}/config/database/pooler — selects the
    session-mode (port 5432) PRIMARY entry, matching the manual guide's
    existing "Session-mode pooler, not transaction mode" requirement.
    Deliberately never reads Supabase's own connection_string/
    connectionString fields (see module docstring) — the caller (browser,
    which already holds db_pass) assembles the final connection string
    itself from this non-secret shape."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                f"/projects/{ref}/config/database/pooler",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 403:
        return SupabaseApiFailed(reason="forbidden")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code != 200:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        entries = response.json()
        matched = next(
            e for e in entries
            if e.get("pool_mode") == "session" and e.get("database_type") == "PRIMARY"
        )
        db_user = str(matched["db_user"])
        db_host = str(matched["db_host"])
        db_port = int(matched["db_port"])
        db_name = str(matched["db_name"])
    except (ValueError, KeyError, TypeError, StopIteration):
        return SupabaseApiFailed(reason="pooler_config_unavailable")
    return SupabaseConnectionInfo(db_user=db_user, db_host=db_host, db_port=db_port, db_name=db_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_supabase_client.py -v`
Expected: PASS (36 tests total)

- [ ] **Step 5: Commit**

```bash
git add onboarding/supabase_client.py tests/test_onboarding_supabase_client.py
git commit -m "feat: add Supabase project status polling and connection info"
```

---

### Task 5: `onboarding/router.py` — six relay endpoints + client_id templating

**Files:**
- Modify: `onboarding/router.py`
- Modify: `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `onboarding.config.settings.public_base_url`,
  `.supabase_oauth_client_id` (Task 1); every `onboarding.supabase_client`
  function and result type (Tasks 2-4).
- Produces: `POST /api/supabase/exchange-oauth-code`,
  `POST /api/supabase/refresh-access-token`,
  `POST /api/supabase/list-organizations`,
  `POST /api/supabase/create-project`,
  `POST /api/supabase/project-status`,
  `POST /api/supabase/connection-info` — JSON contracts per spec section 4.
  `GET /`'s body has `__SUPABASE_OAUTH_CLIENT_ID__` replaced with
  `settings.supabase_oauth_client_id` — Task 6's frontend relies on this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_router.py` (add `supabase_client` to the
existing `from onboarding import github_client, render_client` import):
```python
from onboarding import github_client, render_client, supabase_client
```

Then add:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: FAIL — 404s for the routes that don't exist yet, and the
`SUPABASE_OAUTH_CLIENT_ID` templating test fails on the unmodified body.

- [ ] **Step 3: Modify `onboarding/router.py`**

Replace:
```python
from onboarding import github_client, render_client
```
with:
```python
from onboarding import github_client, render_client, supabase_client
```

Replace:
```python
class GithubInstallVerifyRequest(BaseModel):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    installation_id: int = Field(gt=0)
```
with:
```python
class GithubInstallVerifyRequest(BaseModel):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    installation_id: int = Field(gt=0)


class SupabaseExchangeCodeRequest(BaseModel):
    code: str = Field(max_length=512)
    code_verifier: str = Field(max_length=256)


class SupabaseRefreshTokenRequest(BaseModel):
    refresh_token: str = Field(max_length=2048)


class SupabaseListOrgsRequest(BaseModel):
    access_token: str = Field(max_length=4096)


class SupabaseCreateProjectRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    organization_slug: str = Field(max_length=64)
    name: str = Field(max_length=256)
    db_pass: str = Field(max_length=256)


class SupabaseProjectStatusRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    ref: str = Field(max_length=20)


class SupabaseConnectionInfoRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    ref: str = Field(max_length=20)
```

Replace:
```python
@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = _INDEX_HTML.replace("__ONBOARDING_BASE_URL__", settings.public_base_url)
    return HTMLResponse(html, headers={
```
with:
```python
@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = _INDEX_HTML.replace("__ONBOARDING_BASE_URL__", settings.public_base_url)
    html = html.replace("__SUPABASE_OAUTH_CLIENT_ID__", settings.supabase_oauth_client_id)
    return HTMLResponse(html, headers={
```

Append, after `verify_github_installation`:
```python
@router.post("/api/supabase/exchange-oauth-code")
async def exchange_supabase_oauth_code(payload: SupabaseExchangeCodeRequest) -> dict:
    redirect_uri = f"{settings.public_base_url}/?supabase_step=oauth_callback"
    result = await supabase_client.exchange_oauth_code(payload.code, payload.code_verifier, redirect_uri)
    if isinstance(result, supabase_client.SupabaseTokens):
        return {
            "valid": True,
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "expires_in": result.expires_in,
        }
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/refresh-access-token")
async def refresh_supabase_access_token(payload: SupabaseRefreshTokenRequest) -> dict:
    result = await supabase_client.refresh_access_token(payload.refresh_token)
    if isinstance(result, supabase_client.SupabaseTokens):
        return {
            "valid": True,
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "expires_in": result.expires_in,
        }
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/list-organizations")
async def list_supabase_organizations(payload: SupabaseListOrgsRequest) -> dict:
    result = await supabase_client.list_organizations(payload.access_token)
    if isinstance(result, supabase_client.SupabaseOrgsListed):
        return {"valid": True, "orgs": [{"slug": o.slug, "name": o.name} for o in result.orgs]}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/create-project")
async def create_supabase_project(payload: SupabaseCreateProjectRequest) -> dict:
    result = await supabase_client.create_project(
        payload.access_token, payload.organization_slug, payload.name, payload.db_pass
    )
    if isinstance(result, supabase_client.SupabaseProjectCreated):
        return {"valid": True, "ref": result.ref, "status": result.status}
    if isinstance(result, supabase_client.SupabaseProjectRejected):
        return {"valid": False, "reason": "project_creation_rejected", "message": result.message}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/project-status")
async def get_supabase_project_status(payload: SupabaseProjectStatusRequest) -> dict:
    result = await supabase_client.get_project_status(payload.access_token, payload.ref)
    if isinstance(result, supabase_client.SupabaseProjectStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/connection-info")
async def get_supabase_connection_info(payload: SupabaseConnectionInfoRequest) -> dict:
    result = await supabase_client.get_connection_info(payload.access_token, payload.ref)
    if isinstance(result, supabase_client.SupabaseConnectionInfo):
        return {
            "valid": True,
            "db_user": result.db_user,
            "db_host": result.db_host,
            "db_port": result.db_port,
            "db_name": result.db_name,
        }
    return {"valid": False, "reason": result.reason}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS (all tests, including the pre-existing Render-key and
GitHub ones)

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py tests/test_onboarding_router.py
git commit -m "feat: add Supabase relay endpoints and OAuth client_id templating"
```

---

### Task 6: Frame 3 frontend — connect, OAuth callback, org resolution, create-project kickoff

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/supabase/exchange-oauth-code`,
  `POST /api/supabase/refresh-access-token`,
  `POST /api/supabase/list-organizations`,
  `POST /api/supabase/create-project` (Task 5); the
  `__SUPABASE_OAUTH_CLIENT_ID__` template token (Task 5).
- Produces: `sessionStorage["onboarding.supabase"]` holding
  `{access_token, refresh_token, expires_in, name, organization_slug,
  db_pass, ref, status}` after project creation is kicked off.
  `callSupabaseRelay(endpoint, payload)` (the shared reactive-refresh
  helper) and `showSupabaseProvisioning()` — both reused and extended by
  Task 7.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py`:
```python
async def test_oauth_code_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/supabase/exchange-oauth-code"') == 1


async def test_list_organizations_leaves_the_page_exactly_once():
    """This endpoint (and create-project, project-status, connection-info)
    goes through the shared callSupabaseRelay helper rather than a direct
    fetch() call, so the audit target is "the endpoint string appears
    exactly once as a callSupabaseRelay(...) argument" — the same
    one-exit-path property the fetch()-based version of this test checks
    for exchange-oauth-code and refresh-access-token, adapted for the
    indirection this shared helper introduces."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/list-organizations"') == 1


async def test_create_project_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/create-project"') == 1


async def test_refresh_access_token_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/supabase/refresh-access-token"') == 1


async def test_frame3_has_a_name_input_and_connect_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-project-name-input"' in body
    assert 'id="supabase-connect-submit"' in body


async def test_frame3_has_an_org_picker():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-org-select"' in body
    assert 'id="supabase-org-submit"' in body


async def test_frame3_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame3_instructions", "frame3_name_placeholder", "connect_supabase_button",
        "frame3_org_instructions", "create_project_button",
        "err_supabase_name_empty", "err_supabase_callback_invalid",
    ):
        assert f"{key}:" in body
    assert body.count("connect_supabase_button:") == 2  # STRINGS.en + STRINGS.he


async def test_oauth_callback_handler_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function handleSupabaseOauthCallback" in body
    assert "supabase_step" in body


async def test_pkce_challenge_uses_sha256():
    client = await _client()
    body = (await client.get("/")).text
    assert "crypto.subtle.digest(\"SHA-256\"" in body
    assert "code_challenge_method" in body


async def test_generated_db_password_is_alphanumeric_only():
    """The generated password must never need percent-encoding, sidestepping
    the manual guide's existing footgun entirely."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function generateDbPassword" in body
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" in body


async def test_reactive_refresh_helper_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function callSupabaseRelay" in body
    assert '"unauthorized"' in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — the new markup/strings/functions don't exist yet.

- [ ] **Step 3: Modify `onboarding/static/index.html`**

Replace the frame 3 markup block:
```html
    <details id="frame-supabase" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame3_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>
```
with:
```html
    <details id="frame-supabase" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame3_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="supabase" data-i18n="change_button"></button>
      </summary>
      <div class="frame-body">
        <div id="supabase-connect-section">
          <p data-i18n="frame3_instructions"></p>
          <input id="supabase-project-name-input" type="text">
          <button id="supabase-connect-submit" type="button" data-i18n="connect_supabase_button"></button>
        </div>
        <div id="supabase-org-section" style="display: none;">
          <p data-i18n="frame3_org_instructions"></p>
          <select id="supabase-org-select"></select>
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

In the CSS block, replace:
```css
  .frame-body input[type="password"] {
```
with:
```css
  .frame-body input[type="password"], .frame-body input[type="text"], .frame-body select {
```

In the CSS block, replace:
```css
  details.frame[data-status="error"] .frame-badge { color: var(--fail); }
```
with:
```css
  details.frame[data-status="error"] .frame-badge { color: var(--fail); }
  details.frame[data-status="provisioning"] .frame-badge { color: var(--accent); }
```

In `STRINGS.en`, replace:
```js
      account_prefix: "account: ",
    },
```
with:
```js
      account_prefix: "account: ",
      frame3_instructions: "Name your Supabase project, then click Connect Supabase. This authorizes the wizard to create a new project on your own Supabase account.",
      frame3_name_placeholder: "pr-review-bot",
      connect_supabase_button: "Connect Supabase",
      frame3_org_instructions: "Choose which Supabase organization to create the project in.",
      create_project_button: "Create Project",
      frame3_provisioning: "Provisioning your Supabase project… this takes about 2 minutes.",
      check_again_button: "Check again",
      badge_provisioning: "Provisioning…",
      project_prefix: "project: ",
      err_supabase_name_empty: "Name your project first.",
      err_supabase_callback_invalid: "That didn't come back from Supabase as expected. Please try again.",
      err_supabase_invalid_code: "Supabase rejected that — the code may have expired. Try connecting again.",
      err_supabase_reauthorize: "Your Supabase session expired. Please connect again.",
      err_supabase_forbidden: "Supabase rejected that action for your account.",
      err_supabase_rate_limited: "Too many requests to Supabase right now. Try again in a moment.",
      err_supabase_unreachable: "Supabase is unreachable right now. Try again in a moment.",
      err_supabase_pooler_config_unavailable: "Your project's database connection isn't ready yet. Try again in a moment.",
      err_supabase_provisioning_failed: "Supabase couldn't finish setting up your project. Try creating it again.",
      err_supabase_provisioning_timeout: "Still provisioning — this is taking longer than usual. You can check again, or come back later.",
    },
```

In `STRINGS.he`, replace:
```js
      account_prefix: "חשבון: ",
    },
```
with:
```js
      account_prefix: "חשבון: ",
      frame3_instructions: "תנו שם לפרויקט ה-Supabase שלכם, ואז לחצו על חיבור Supabase. פעולה זו מאשרת לאשף ליצור פרויקט חדש בחשבון ה-Supabase שלכם.",
      frame3_name_placeholder: "pr-review-bot",
      connect_supabase_button: "חיבור Supabase",
      frame3_org_instructions: "בחרו לאיזה ארגון ב-Supabase ליצור את הפרויקט.",
      create_project_button: "יצירת פרויקט",
      frame3_provisioning: "הפרויקט שלכם ב-Supabase בהקמה… זה לוקח כשתי דקות.",
      check_again_button: "בדיקה מחדש",
      badge_provisioning: "בהקמה…",
      project_prefix: "פרויקט: ",
      err_supabase_name_empty: "תנו שם לפרויקט קודם.",
      err_supabase_callback_invalid: "התגובה מ-Supabase לא הייתה כצפוי. נסו שוב.",
      err_supabase_invalid_code: "Supabase דחה את הבקשה — ייתכן שהקוד פג תוקף. נסו להתחבר שוב.",
      err_supabase_reauthorize: "ההתחברות ל-Supabase פגה. אנא התחברו שוב.",
      err_supabase_forbidden: "Supabase דחה את הפעולה עבור החשבון שלכם.",
      err_supabase_rate_limited: "יותר מדי בקשות ל-Supabase כרגע. נסו שוב בעוד רגע.",
      err_supabase_unreachable: "Supabase אינו זמין כרגע. נסו שוב בעוד רגע.",
      err_supabase_pooler_config_unavailable: "חיבור מסד הנתונים של הפרויקט עדיין לא מוכן. נסו שוב בעוד רגע.",
      err_supabase_provisioning_failed: "Supabase לא הצליח לסיים את הקמת הפרויקט. נסו ליצור אותו מחדש.",
      err_supabase_provisioning_timeout: "עדיין בהקמה — זה לוקח יותר זמן מהרגיל. אפשר לבדוק שוב, או לחזור מאוחר יותר.",
    },
```

**Already present — no edit needed here.** Task 5's implementer added
`window.SUPABASE_OAUTH_CLIENT_ID = "__SUPABASE_OAUTH_CLIENT_ID__";` (right
after the existing `window.ONBOARDING_BASE_URL` line) as a necessary
one-line deviation from its stated file list, because Task 5's own test
(`test_index_serves_configured_supabase_oauth_client_id`) needed something
for the `__SUPABASE_OAUTH_CLIENT_ID__` templating to substitute against —
this plan originally (incorrectly) deferred that line to this task. Confirm
it's there (`grep 'window.SUPABASE_OAUTH_CLIENT_ID' onboarding/static/index.html`)
and do not re-add it — doing so would duplicate the line.

Replace:
```js
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "github-app": "onboarding.githubApp",
  };
```
with:
```js
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "github-app": "onboarding.githubApp",
    "supabase": "onboarding.supabase",
  };

  const SUPABASE_PENDING_KEY = "onboarding.supabasePending";
```

Replace:
```js
  const FRAME_ORDER = [
    "render-key", "github-app", "supabase", "llm-provider",
    "uptime-pinger", "render-deploy",
  ];
```
Leave this list unchanged — it already includes `"supabase"` (added when
the placeholder frame was scaffolded in sub-project 1).

Replace:
```js
  let currentRenderKeyErrorKey = null;
  let currentGithubAppErrorKey = null;
```
with:
```js
  let currentRenderKeyErrorKey = null;
  let currentGithubAppErrorKey = null;
  let currentSupabaseErrorKey = null;
```

Replace:
```js
  function lockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "true";
    el.open = false;
    setFrameStatus(id, "locked");
    const key = STORAGE_KEYS[id];
    if (key) sessionStorage.removeItem(key);
    // Frame 2's create/install sections are a one-way toggle otherwise: the
    // storage clear above would leave "Install App" on screen with nothing
    // behind it, and clicking it would report a misleading GitHub error.
    if (id === "github-app") resetGithubAppCreateSection();
  }
```
with:
```js
  function lockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "true";
    el.open = false;
    setFrameStatus(id, "locked");
    const key = STORAGE_KEYS[id];
    if (key) sessionStorage.removeItem(key);
    // Frame 2's create/install sections are a one-way toggle otherwise: the
    // storage clear above would leave "Install App" on screen with nothing
    // behind it, and clicking it would report a misleading GitHub error.
    // Frame 3's connect/org-picker/provisioning sections are the same
    // shape, one step longer.
    if (id === "github-app") resetGithubAppCreateSection();
    if (id === "supabase") resetSupabaseConnectSection();
  }
```

Replace:
```js
    if (id === "github-app") {
      sessionStorage.removeItem(STORAGE_KEYS["github-app"]);
      resetGithubAppCreateSection();
    }
  }
```
with:
```js
    if (id === "github-app") {
      sessionStorage.removeItem(STORAGE_KEYS["github-app"]);
      resetGithubAppCreateSection();
    }
    if (id === "supabase") {
      sessionStorage.removeItem(STORAGE_KEYS["supabase"]);
      resetSupabaseConnectSection();
    }
  }
```

Replace (this exact tail is unique in the file only once adjacent to
`restoreFromSession`'s definition — `handleGithubManifestCallback` ends
with the same three inner lines but is followed by `installGithubApp`, not
`restoreFromSession`):
```js
    } else {
      githubAppError("err_github_unreachable");
    }
  }

  function restoreFromSession() {
```
with:
```js
    } else {
      githubAppError("err_github_unreachable");
    }
  }

  function base64UrlEncode(bytes) {
    let binary = "";
    bytes.forEach((b) => { binary += String.fromCharCode(b); });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function generatePkcePair() {
    const verifierBytes = crypto.getRandomValues(new Uint8Array(32));
    const verifier = base64UrlEncode(verifierBytes);
    const challengeBuffer = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    const challenge = base64UrlEncode(new Uint8Array(challengeBuffer));
    return {verifier, challenge};
  }

  function generateDbPassword() {
    const charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    let password = "";
    bytes.forEach((b) => { password += charset[b % charset.length]; });
    return password;
  }

  function resetSupabaseConnectSection() {
    document.getElementById("supabase-org-section").style.display = "none";
    document.getElementById("supabase-provisioning-section").style.display = "none";
    document.getElementById("supabase-check-status-submit").style.display = "none";
    document.getElementById("supabase-connect-section").style.display = "block";
    document.getElementById("supabase-error").textContent = "";
    currentSupabaseErrorKey = null;
  }

  function supabaseError(key) {
    setFrameStatus("supabase", "error");
    currentSupabaseErrorKey = key;
    document.getElementById("supabase-error").textContent = t(key);
  }

  function supabaseErrorForReason(reason, message) {
    if (reason === "project_creation_rejected" && message) {
      // Supabase's own error text, relayed verbatim (spec section 4) —
      // not a translated string, since it isn't one of ours.
      setFrameStatus("supabase", "error");
      currentSupabaseErrorKey = null;
      document.getElementById("supabase-error").textContent = message;
      return;
    }
    const key = {
      unauthorized: "err_supabase_reauthorize",
      invalid_code: "err_supabase_invalid_code",
      forbidden: "err_supabase_forbidden",
      rate_limited: "err_supabase_rate_limited",
      pooler_config_unavailable: "err_supabase_pooler_config_unavailable",
    }[reason] || "err_supabase_unreachable";
    supabaseError(key);
  }

  // Every Supabase relay call after the initial token exchange goes
  // through here: reads the held access_token from sessionStorage, and on
  // a "reason": "unauthorized" response, attempts exactly one
  // refresh-then-retry (spec section 3's "Token refresh" note) rather than
  // tracking a client-side expiry timer.
  async function callSupabaseRelay(endpoint, payload) {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"]) || "null");
    if (!stored || !stored.access_token) {
      return {valid: false, reason: "unauthorized"};
    }
    const attempt = async (accessToken) => {
      const resp = await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({...payload, access_token: accessToken}),
      });
      if (!resp.ok) throw new Error("bad status");
      return resp.json();
    };
    let body;
    try {
      body = await attempt(stored.access_token);
    } catch (err) {
      return {valid: false, reason: "supabase_unreachable"};
    }
    if (body.valid || body.reason !== "unauthorized" || !stored.refresh_token) {
      return body;
    }
    let refreshBody;
    try {
      const refreshResp = await fetch("/api/supabase/refresh-access-token", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({refresh_token: stored.refresh_token}),
      });
      if (!refreshResp.ok) throw new Error("bad status");
      refreshBody = await refreshResp.json();
    } catch (err) {
      return body;
    }
    if (!refreshBody.valid) return body;
    stored.access_token = refreshBody.access_token;
    stored.refresh_token = refreshBody.refresh_token;
    stored.expires_in = refreshBody.expires_in;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    try {
      return await attempt(stored.access_token);
    } catch (err) {
      return {valid: false, reason: "supabase_unreachable"};
    }
  }

  async function connectSupabase() {
    const input = document.getElementById("supabase-project-name-input");
    const name = input.value.trim();
    document.getElementById("supabase-error").textContent = "";
    currentSupabaseErrorKey = null;
    if (!name) {
      supabaseError("err_supabase_name_empty");
      return;
    }
    const state = crypto.randomUUID();
    const {verifier, challenge} = await generatePkcePair();
    sessionStorage.setItem(SUPABASE_PENDING_KEY, JSON.stringify({state, verifier, name}));
    const redirectUri = `${window.ONBOARDING_BASE_URL}/?supabase_step=oauth_callback`;
    const params = new URLSearchParams({
      client_id: window.SUPABASE_OAUTH_CLIENT_ID,
      redirect_uri: redirectUri,
      response_type: "code",
      state: state,
      code_challenge: challenge,
      code_challenge_method: "S256",
    });
    location.href = `https://api.supabase.com/v1/oauth/authorize?${params.toString()}`;
  }

  async function handleSupabaseOauthCallback() {
    const params = new URLSearchParams(location.search);
    if (params.get("supabase_step") !== "oauth_callback") return;
    history.replaceState(null, "", location.pathname);
    const pending = JSON.parse(sessionStorage.getItem(SUPABASE_PENDING_KEY) || "null");
    sessionStorage.removeItem(SUPABASE_PENDING_KEY);
    const code = params.get("code");
    if (!pending || params.get("state") !== pending.state || !code) {
      supabaseError("err_supabase_callback_invalid");
      return;
    }
    let resp;
    try {
      resp = await fetch("/api/supabase/exchange-oauth-code", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code: code, code_verifier: pending.verifier}),
      });
    } catch (err) {
      supabaseError("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      supabaseError("err_supabase_unreachable");
      return;
    }
    if (!body.valid) {
      supabaseErrorForReason(body.reason, body.message);
      return;
    }
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify({
      access_token: body.access_token, refresh_token: body.refresh_token,
      expires_in: body.expires_in, name: pending.name,
    }));
    await fetchSupabaseOrganizations();
  }

  async function fetchSupabaseOrganizations() {
    const body = await callSupabaseRelay("/api/supabase/list-organizations", {});
    if (!body.valid) {
      supabaseErrorForReason(body.reason, body.message);
      return;
    }
    if (body.orgs.length === 1) {
      await kickOffProjectCreation(body.orgs[0].slug);
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
    document.getElementById("supabase-connect-section").style.display = "none";
    document.getElementById("supabase-org-section").style.display = "block";
  }

  async function confirmSupabaseOrg() {
    const select = document.getElementById("supabase-org-select");
    await kickOffProjectCreation(select.value);
  }

  async function kickOffProjectCreation(organizationSlug) {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"]) || "null");
    if (!stored) {
      supabaseError("err_supabase_callback_invalid");
      return;
    }
    const dbPass = generateDbPassword();
    const body = await callSupabaseRelay("/api/supabase/create-project", {
      organization_slug: organizationSlug, name: stored.name, db_pass: dbPass,
    });
    if (!body.valid) {
      supabaseErrorForReason(body.reason, body.message);
      return;
    }
    stored.organization_slug = organizationSlug;
    stored.db_pass = dbPass;
    stored.ref = body.ref;
    stored.status = body.status;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    showSupabaseProvisioning();
  }

  function showSupabaseProvisioning() {
    document.getElementById("supabase-connect-section").style.display = "none";
    document.getElementById("supabase-org-section").style.display = "none";
    document.getElementById("supabase-provisioning-section").style.display = "block";
    frameEl("supabase").open = true;
    setFrameStatus("supabase", "provisioning");
  }

  function restoreFromSession() {
```

In `applyLanguage`, replace:
```js
    document.getElementById("github-app-name-input").placeholder = t("frame2_name_placeholder");
```
with:
```js
    document.getElementById("github-app-name-input").placeholder = t("frame2_name_placeholder");
    document.getElementById("supabase-project-name-input").placeholder = t("frame3_name_placeholder");
```

Replace:
```js
    if (currentGithubAppErrorKey) {
      document.getElementById("github-app-error").textContent = t(currentGithubAppErrorKey);
    }
```
with:
```js
    if (currentGithubAppErrorKey) {
      document.getElementById("github-app-error").textContent = t(currentGithubAppErrorKey);
    }
    if (currentSupabaseErrorKey) {
      document.getElementById("supabase-error").textContent = t(currentSupabaseErrorKey);
    }
```

In `document.addEventListener("DOMContentLoaded", ...)`, replace:
```js
    document.getElementById("github-app-install-submit").addEventListener("click", installGithubApp);
```
with:
```js
    document.getElementById("github-app-install-submit").addEventListener("click", installGithubApp);
    document.getElementById("supabase-connect-submit").addEventListener("click", connectSupabase);
    document.getElementById("supabase-org-submit").addEventListener("click", confirmSupabaseOrg);
```

and replace:
```js
    handleGithubManifestCallback();
    handleGithubInstallCallback();
  });
```
with:
```js
    handleGithubManifestCallback();
    handleGithubInstallCallback();
    handleSupabaseOauthCallback();
  });
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py tests/test_onboarding_router.py tests/test_onboarding_i18n.py -v`
Expected: PASS. (`test_onboarding_i18n.py` content-scans `STRINGS.en`/
`STRINGS.he` for key parity — the new frame 3 keys must satisfy whatever
check it already does; if it fails, add the same keys to whichever side is
missing them until it passes.)

- [ ] **Step 5: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: frame 3 — connect Supabase, resolve org, kick off project creation"
```

---

### Task 7: Frame 3 frontend — provisioning poll, connection info, completion + `onboarding/CLAUDE.md`

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `tests/test_onboarding_page.py`
- Modify: `onboarding/CLAUDE.md`

**Interfaces:**
- Consumes: `sessionStorage["onboarding.supabase"]` (Task 6),
  `POST /api/supabase/project-status`, `POST /api/supabase/connection-info`
  (Task 5), `callSupabaseRelay`, `showSupabaseProvisioning()` (Task 6).
- Produces: frame 3 reaches `done` status via
  `completeFrame("supabase", "project_prefix", name)`, with
  `sessionStorage["onboarding.supabase"].connection_string` holding the
  final `postgresql://` string — ready for sub-project 6 (out of scope
  here) to read and push into Render's env vars.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py`:
```python
async def test_project_status_leaves_the_page_exactly_once():
    """Like Task 6's list-organizations/create-project tests: this endpoint
    goes through the shared callSupabaseRelay helper, not a direct fetch()
    call, so the audit target is the endpoint string appearing exactly once
    as a callSupabaseRelay(...) argument."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/project-status"') == 1


async def test_connection_info_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/connection-info"') == 1


async def test_frame3_has_a_check_again_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-check-status-submit"' in body


async def test_polling_uses_a_five_second_interval_and_five_minute_timeout():
    client = await _client()
    body = (await client.get("/")).text
    assert "SUPABASE_POLL_INTERVAL_MS = 5000" in body
    assert "SUPABASE_POLL_TIMEOUT_MS = 300000" in body


async def test_target_status_is_active_healthy_and_init_failed_is_terminal():
    client = await _client()
    body = (await client.get("/")).text
    assert '"ACTIVE_HEALTHY"' in body
    assert '"INIT_FAILED"' in body


async def test_connection_string_assembled_client_side_from_non_secret_shape():
    """The backend never returns Supabase's own connection_string field
    (spec section 3 step 9) — the browser builds it from db_user/db_host/
    db_port/db_name (returned) plus db_pass (already held)."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function fetchSupabaseConnectionInfo" in body
    assert "postgresql://${body.db_user}:${stored.db_pass}@${body.db_host}:${body.db_port}/${body.db_name}" in body


async def test_supabase_credential_never_persists_to_local_storage():
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.setItem(STORAGE_KEYS["supabase"]' not in body
    assert 'localStorage.getItem(STORAGE_KEYS["supabase"]' not in body


async def test_restore_from_session_resumes_polling_for_a_ref_without_a_connection_string():
    client = await _client()
    body = (await client.get("/")).text
    assert "showSupabaseProvisioning()" in body
    assert "pollUntilReady(Date.now())" in body
    assert "function restoreFromSession" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — polling/connection-info functions and the restore
extension don't exist yet.

- [ ] **Step 3: Modify `onboarding/static/index.html`**

Replace `kickOffProjectCreation`'s body (from Task 6):
```js
    stored.organization_slug = organizationSlug;
    stored.db_pass = dbPass;
    stored.ref = body.ref;
    stored.status = body.status;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    showSupabaseProvisioning();
  }
```
with:
```js
    stored.organization_slug = organizationSlug;
    stored.db_pass = dbPass;
    stored.ref = body.ref;
    stored.status = body.status;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    showSupabaseProvisioning();
    pollUntilReady(Date.now());
  }
```

After `showSupabaseProvisioning`'s definition (still before
`restoreFromSession`), add:
```js
  const SUPABASE_POLL_INTERVAL_MS = 5000;
  const SUPABASE_POLL_TIMEOUT_MS = 300000;

  async function handleProjectStatusResult(body) {
    if (!body.valid) {
      supabaseErrorForReason(body.reason, body.message);
      return "error";
    }
    if (body.status === "ACTIVE_HEALTHY") {
      await fetchSupabaseConnectionInfo();
      return "ready";
    }
    if (body.status === "INIT_FAILED") {
      supabaseError("err_supabase_provisioning_failed");
      return "error";
    }
    return "pending";
  }

  async function pollUntilReady(startedAt) {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"]) || "null");
    if (!stored || !stored.ref) return;
    const body = await callSupabaseRelay("/api/supabase/project-status", {ref: stored.ref});
    const outcome = await handleProjectStatusResult(body);
    if (outcome !== "pending") return;
    if (Date.now() - startedAt >= SUPABASE_POLL_TIMEOUT_MS) {
      supabaseError("err_supabase_provisioning_timeout");
      document.getElementById("supabase-check-status-submit").style.display = "inline-block";
      return;
    }
    setTimeout(() => { pollUntilReady(startedAt); }, SUPABASE_POLL_INTERVAL_MS);
  }

  async function checkSupabaseStatusOnce() {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"]) || "null");
    if (!stored || !stored.ref) return;
    document.getElementById("supabase-error").textContent = "";
    currentSupabaseErrorKey = null;
    const body = await callSupabaseRelay("/api/supabase/project-status", {ref: stored.ref});
    const outcome = await handleProjectStatusResult(body);
    if (outcome === "pending") {
      supabaseError("err_supabase_provisioning_timeout");
      document.getElementById("supabase-check-status-submit").style.display = "inline-block";
    }
  }

  async function fetchSupabaseConnectionInfo() {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"]) || "null");
    if (!stored || !stored.ref || !stored.db_pass) return;
    const body = await callSupabaseRelay("/api/supabase/connection-info", {ref: stored.ref});
    if (!body.valid) {
      supabaseErrorForReason(body.reason, body.message);
      return;
    }
    // Assembled client-side from db_pass (already held) plus the non-secret
    // shape connection-info returned (spec section 3 step 9) — never from
    // Supabase's own connection_string field, whose password-masking
    // behavior cannot be verified from documentation.
    stored.connection_string =
      `postgresql://${body.db_user}:${stored.db_pass}@${body.db_host}:${body.db_port}/${body.db_name}`;
    delete stored.db_pass;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    completeFrame("supabase", "project_prefix", stored.name);
  }
```

Replace `restoreFromSession`:
```js
  function restoreFromSession() {
    if (sessionStorage.getItem(STORAGE_KEYS["render-key"])) {
      completeFrame("render-key", "restored", null);
    }
    const githubApp = readStoredGithubApp();
    if (githubApp && githubApp.installation_id) {
      completeFrame("github-app", "restored", null);
    } else if (githubApp) {
      showGithubAppReadyToInstall();
    }
  }
```
with:
```js
  function restoreFromSession() {
    if (sessionStorage.getItem(STORAGE_KEYS["render-key"])) {
      completeFrame("render-key", "restored", null);
    }
    const githubApp = readStoredGithubApp();
    if (githubApp && githubApp.installation_id) {
      completeFrame("github-app", "restored", null);
    } else if (githubApp) {
      showGithubAppReadyToInstall();
    }
    const supabaseState = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"]) || "null");
    if (supabaseState && supabaseState.connection_string) {
      completeFrame("supabase", "restored", null);
    } else if (supabaseState && supabaseState.ref) {
      showSupabaseProvisioning();
      pollUntilReady(Date.now());
    }
  }
```

In `document.addEventListener("DOMContentLoaded", ...)`, replace:
```js
    document.getElementById("supabase-org-submit").addEventListener("click", confirmSupabaseOrg);
```
with:
```js
    document.getElementById("supabase-org-submit").addEventListener("click", confirmSupabaseOrg);
    document.getElementById("supabase-check-status-submit").addEventListener("click", checkSupabaseStatusOnce);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py tests/test_onboarding_router.py tests/test_onboarding_i18n.py -v`
Expected: PASS.

- [ ] **Step 5: Update `onboarding/CLAUDE.md`**

**Not a plain file-append**: the "What sub-project 2 (GitHub App automation)
adds to these rules" section is followed by an unrelated "The test suite
looks hung on a fresh worktree — it isn't" section, so appending to the end
of the file would wrongly place this new section after that unrelated one.
Insert it between them instead. Replace:
```markdown
  value still normalizes to `""` rather than erroring, so the lifespan check
  stays the one thing that reports a missing value.

## The test suite looks hung on a fresh worktree — it isn't
```
with (do not remove or edit any other existing content):
```markdown
  value still normalizes to `""` rather than erroring, so the lifespan check
  stays the one thing that reports a missing value.

## What sub-project 3 (Supabase provisioning) adds to these rules

- **`SUPABASE_OAUTH_CLIENT_ID`/`SUPABASE_OAUTH_CLIENT_SECRET` are this
  service's first operator-level secrets** — every credential before this
  sub-project was either visitor-typed or minted fresh for the visitor who
  requested it. These two are set once by the operator (a one-time, manual
  Supabase OAuth-app registration; Supabase has no self-registration
  mechanism the way GitHub's App Manifest flow does) and never change per
  visitor. `supabase_oauth_client_id` is also templated into the served
  page as `window.SUPABASE_OAUTH_CLIENT_ID` — this is **not** a secret
  exposure: a `client_id` is the public half of OAuth credentials by
  design, and `client_secret` never leaves the backend.
- **`exchange-oauth-code` and `refresh-access-token` are mint-and-return
  exceptions**, same category as GitHub's manifest exchange: they return
  tokens freshly issued to the visitor who just authorized. `create-project`
  is a *different* kind of exception — on a business-rule rejection it
  relays Supabase's own error `message` text verbatim rather than mapping
  to a fixed reason enum, because guessing which specific rule Supabase
  applied (e.g. the free-tier project cap) would require assuming exact
  API wording this project could not verify without a live authenticated
  call — see the design spec section 4 and `ISSUES.md`'s Design Gaps entry.
- **`db_pass` is generated client-side by the browser, never minted by the
  backend** — deliberately different from the GitHub frame's private-key
  pattern. It's a value *we* choose (Supabase doesn't produce it for us the
  way it produces a private key), so keeping it browser-originated avoids
  growing the mint-and-return exception list for a value that doesn't need
  it.
- **`connection-info` never returns Supabase's own `connection_string`/
  `connectionString` fields.** Whether they embed the real password or a
  masked placeholder could not be verified from documentation during this
  sub-project's brainstorm. The endpoint returns only the non-secret shape
  (`db_user`, `db_host`, `db_port`, `db_name`); the browser, which already
  holds `db_pass`, assembles the final connection string itself. A future
  change that trusts Supabase's own connection-string field needs to
  verify its password-masking behavior with a live call first.
- **Token refresh is reactive, not proactive.** `callSupabaseRelay` in
  `onboarding/static/index.html` retries exactly once, only after a real
  `"unauthorized"` response — there is no client-side expiry-timer
  tracking `expires_in`. Any new Supabase relay call should go through this
  same helper rather than calling `fetch()` directly, to inherit the retry
  behavior for free.
- **The OAuth app is a resource shared across every visitor** — unlike
  every other credential in this service. This is a known, deliberately
  deferred risk; see `ISSUES.md`'s Design Gaps section before changing
  anything about how the OAuth app is used or exposed.

## The test suite looks hung on a fresh worktree — it isn't
```

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS, no failures, no regressions in tests outside `onboarding/`'s
scope.

- [ ] **Step 7: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py onboarding/CLAUDE.md
git commit -m "feat: frame 3 — poll provisioning status, assemble connection string"
```
