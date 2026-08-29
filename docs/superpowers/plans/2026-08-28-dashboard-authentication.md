# Dashboard Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate `app/dashboard.py`'s router behind a login page + signed session
cookie, so the dashboard (soon to gain a feature that can read/write live
Render secrets and the `runtime_config` DB table) is no longer reachable by
anyone who knows the URL.

**Architecture:** A new `app/auth.py` module owns credential verification, JWT
session-token issue/verify, cookie helpers, and the login/logout HTTP routes.
`app/main.py` mounts it, adds one `Depends(require_session)` at
`include_router()` time to protect every route on `dashboard_router`
(current and future), and refuses to boot if any of the three new credential
env vars is empty.

**Tech Stack:** FastAPI, `pyjwt` (already a dependency — no new library),
`hmac` (stdlib), pytest/httpx/ASGITransport (existing test stack).

**Spec:** `docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md`
— read it in full before starting; this plan implements it exactly. Executors
should treat a conflict between this plan's code and the spec's prose as a
signal to stop and ask, not to silently pick one.

## Global Constraints

- Single shared operator credential — no user table, no registration flow, no
  per-operator identity (spec §2).
- Session token is a `pyjwt`-signed JWT, secret independent of the password
  (`DASHBOARD_SESSION_SECRET`) — no new library dependency (spec §2).
- Session cookie flags: `HttpOnly`, `Secure`, `SameSite=Strict` — no separate
  CSRF-token scheme (spec §2).
- Session duration: 12h default, 30d with "remember me" (spec §2).
- Login failure: fixed ~1s delay, one generic `invalid_credentials` reason
  regardless of which field was wrong — no IP-based lockout (spec §2).
- The three new env vars are secrets (`DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`,
  `DASHBOARD_SESSION_SECRET`) — they belong in `.env`, and must **never** be
  added to `app/config.py`'s `OPERATIONAL_KEYS`.
- Every step that runs tests uses `uv run pytest <path> -v` (this repo's own
  convention — see README). Every step that runs ruff uses `uv run ruff check .`.
- Follow root `CLAUDE.md`'s "Secret handling" section at all times: never print
  a credential value, never pass one as a literal CLI argument, never open
  `.env` with any tool.

---

## Task 1: Wire `DASHBOARD_*` credentials through config, deploy tooling, and generated docs

**Files:**
- Modify: `app/config.py` (add 3 fields, ~line 79-80)
- Modify: `scripts/deploy.py` (`_ALWAYS_SYNCED` ~line 55, `_wanted_env()` ~line 970,
  `check_config()` ~line 218, `_BOOT_CREDENTIAL_NAMES` ~line 316)
- Modify: `render.yaml` (~line 24-25)
- Modify: `.env.example` (~line 27)
- Test: `tests/test_config.py` (new test)
- Test: `tests/test_deploy_script.py` (modify `complete_config`/`sync_ready`
  fixtures, `test_check_config_names_every_missing_key_at_once`'s neighbor,
  `test_boot_credentials_live_passes_when_all_present`; add 2 new tests)
- Generate: `guide/reference/config.md`, `guide/reference/sync-env.md` (via
  `uv run python -m bot.scripts.gen_docs` — never hand-edit)

**Interfaces:**
- Produces: `Settings.dashboard_username: str` (default `""`),
  `Settings.dashboard_password: str` (default `""`),
  `Settings.dashboard_session_secret: str` (default `""`) — all absent from
  `OPERATIONAL_KEYS`, all consumed by Task 2's `app/auth.py`.

- [ ] **Step 1: Write the failing Settings-field test**

Append to `tests/test_config.py` (same file/pattern as the neighboring
`test_vertex_settings_default_to_derive_everything_from_the_key`):

```python
def test_dashboard_credential_fields_default_to_empty_and_are_not_operational(monkeypatch):
    """_env_file=None plus delenv because these defaults must be asserted
    against the code, not against whatever this working copy's .env happens
    to say."""
    for name in ("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD", "DASHBOARD_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.dashboard_username == ""
    assert settings.dashboard_password == ""
    assert settings.dashboard_session_secret == ""
    assert "DASHBOARD_USERNAME" not in OPERATIONAL_KEYS
    assert "DASHBOARD_PASSWORD" not in OPERATIONAL_KEYS
    assert "DASHBOARD_SESSION_SECRET" not in OPERATIONAL_KEYS
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `uv run pytest tests/test_config.py::test_dashboard_credential_fields_default_to_empty_and_are_not_operational -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'dashboard_username'`.

- [ ] **Step 3: Add the fields to `app/config.py`**

In `app/config.py`, right after the existing `public_base_url: str = ""` line
(the last line of the GitHub-App/identity block, just before the
`llm_provider` block starts), insert:

```python

    # --- Dashboard authentication. A single shared operator credential (no
    # per-user accounts) gates app/dashboard.py's router -- see
    # docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md.
    # dashboard_session_secret signs the session-cookie JWT and is
    # independent of the password: rotating it invalidates every active
    # session at once, the deliberate "revoke everything" lever if a session
    # is ever suspected compromised.
    dashboard_username: str = ""
    dashboard_password: str = ""
    dashboard_session_secret: str = ""
```

- [ ] **Step 4: Run it again, confirm it passes**

Run: `uv run pytest tests/test_config.py::test_dashboard_credential_fields_default_to_empty_and_are_not_operational -v`
Expected: PASS.

- [ ] **Step 5: Update `tests/test_deploy_script.py`'s fixtures and existing assertions (write first, expect new failures)**

In `tests/test_deploy_script.py`, extend the `complete_config` fixture
(~line 175) by adding three lines at its end:

```python
    monkeypatch.setattr(settings, "dashboard_username", "dash-user")
    monkeypatch.setattr(settings, "dashboard_password", "dash-pass")
    monkeypatch.setattr(settings, "dashboard_session_secret", "dash-session-secret")
```

Extend the `sync_ready` fixture (~line 1549) the same way, adding after its
`monkeypatch.setattr(settings, "groq_api_key", "gsk_x")` line:

```python
    monkeypatch.setattr(settings, "dashboard_username", "dash-user")
    monkeypatch.setattr(settings, "dashboard_password", "dash-pass")
    monkeypatch.setattr(settings, "dashboard_session_secret", "dash-session-secret")
```

Add a new test near `test_check_config_names_every_missing_key_at_once`:

```python
def test_check_config_requires_dashboard_credentials(complete_config, monkeypatch):
    """check_config()'s own purpose is 'every setting the service needs is
    resolvable locally' -- app/main.py's lifespan now refuses to boot without
    these three, so this doctor check must name them too, or an operator
    would see a clean preflight report right before a crash-on-boot deploy."""
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")
    monkeypatch.setattr(settings, "dashboard_session_secret", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "DASHBOARD_USERNAME" in result.detail
    assert "DASHBOARD_PASSWORD" in result.detail
    assert "DASHBOARD_SESSION_SECRET" in result.detail
```

Update `test_boot_credentials_live_passes_when_all_present` (~line 448): add
three entries to its mocked env-var dict so it still legitimately passes once
`_BOOT_CREDENTIAL_NAMES` grows:

```python
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_INSTALLATION_ID": "155887152",
                        "GITHUB_APP_PRIVATE_KEY": "aGVsbG8=",
                        "GITHUB_WEBHOOK_SECRET": "s3cret",
                        "LLM_PROVIDER": "groq",
                        "DATABASE_URL": "postgresql://u:p@h/db",
                        "DASHBOARD_USERNAME": "dash-user",
                        "DASHBOARD_PASSWORD": "dash-pass",
                        "DASHBOARD_SESSION_SECRET": "dash-session-secret",
                    }
                ),
```

Add a new regression test right after
`test_boot_credentials_live_also_requires_installation_id_and_llm_provider`
(mirrors that test's own docstring pattern):

```python
def test_boot_credentials_live_also_requires_dashboard_credentials(monkeypatch):
    """Same regression shape as the installation-id/llm-provider case above:
    app/main.py's lifespan also refuses to boot without the three DASHBOARD_*
    vars now, so a live Render service missing any of them would crash-loop
    while this check still reported PASS if it didn't know about them."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {
                        "GITHUB_APP_ID": "999999",
                        "GITHUB_APP_INSTALLATION_ID": "155887152",
                        "GITHUB_APP_PRIVATE_KEY": "aGVsbG8=",
                        "GITHUB_WEBHOOK_SECRET": "s3cret",
                        "LLM_PROVIDER": "groq",
                        "DATABASE_URL": "postgresql://u:p@h/db",
                    }
                ),
            )
        )
        result = deploy.check_boot_credentials_live()
    assert result.status == "FAIL"
    assert "DASHBOARD_USERNAME" in result.detail
    assert "DASHBOARD_PASSWORD" in result.detail
    assert "DASHBOARD_SESSION_SECRET" in result.detail
```

- [ ] **Step 6: Run the deploy-script tests, confirm the new/changed ones fail**

Run: `uv run pytest tests/test_deploy_script.py -k "dashboard_credentials or boot_credentials_live_passes_when_all_present" -v`
Expected: FAIL — `_wanted_env()`/`check_config()`/`_BOOT_CREDENTIAL_NAMES`
don't know about the new vars yet.

- [ ] **Step 7: Implement the `scripts/deploy.py` changes**

In `_ALWAYS_SYNCED` (~line 55), add the three names at the end of the tuple:

```python
_ALWAYS_SYNCED = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_SECRET",
)
```

In `_wanted_env()`'s literal `wanted` dict (~line 970), add three entries
right after `"GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,`:

```python
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "DASHBOARD_USERNAME": settings.dashboard_username,
        "DASHBOARD_PASSWORD": settings.dashboard_password,
        "DASHBOARD_SESSION_SECRET": settings.dashboard_session_secret,
        "LLM_PROVIDER": settings.llm_provider,
```

In `check_config()` (~line 218), add three checks right after the
`GITHUB_WEBHOOK_SECRET` check:

```python
    if not settings.github_webhook_secret:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not settings.dashboard_username:
        missing.append("DASHBOARD_USERNAME")
    if not settings.dashboard_password:
        missing.append("DASHBOARD_PASSWORD")
    if not settings.dashboard_session_secret:
        missing.append("DASHBOARD_SESSION_SECRET")
```

In `_BOOT_CREDENTIAL_NAMES` (~line 316), add the three names:

```python
_BOOT_CREDENTIAL_NAMES = (
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "LLM_PROVIDER",
    "DATABASE_URL",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_SECRET",
)
```

- [ ] **Step 8: Run the deploy-script tests again, confirm they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: PASS. (`test_wanted_env_is_always_a_superset_of_the_always_synced_names`
and `test_render_yaml_declares_every_synced_var` should now FAIL instead —
that's expected and fixed in the next step.)

- [ ] **Step 9: Update `render.yaml`**

Insert two new entries right after the `GITHUB_WEBHOOK_SECRET` entry
(~line 24-25):

```yaml
      - key: GITHUB_WEBHOOK_SECRET
        sync: false
      - key: DASHBOARD_USERNAME
        sync: false
      - key: DASHBOARD_PASSWORD
        sync: false
      - key: DASHBOARD_SESSION_SECRET
        sync: false
      - key: GITHUB_TARGET_REPO
        sync: false
```

- [ ] **Step 10: Update `.env.example`**

Insert a new block right after the existing `GITHUB_WEBHOOK_SECRET=` line
(~line 27):

```
GITHUB_WEBHOOK_SECRET=

# --- Dashboard authentication ---
# Single shared operator credential gating the dashboard (GET / and its
# API). All three are required -- the service refuses to boot if any is
# empty. DASHBOARD_SESSION_SECRET signs the session cookie; generate a
# random value for it (it is not meant to be memorable), e.g.:
# python -c "import secrets; print(secrets.token_urlsafe(32))"
DASHBOARD_USERNAME=
DASHBOARD_PASSWORD=
DASHBOARD_SESSION_SECRET=
```

- [ ] **Step 11: Run the full deploy/render/config test surface, confirm green**

Run: `uv run pytest tests/test_deploy_script.py tests/test_config.py -v`
Expected: PASS, including `test_wanted_env_is_always_a_superset_of_the_always_synced_names`
and `test_render_yaml_declares_every_synced_var`.

- [ ] **Step 12: Regenerate the docs**

Run: `uv run python -m bot.scripts.gen_docs`
This rewrites `guide/reference/config.md` and `guide/reference/sync-env.md`
from the code (their content is fully derived from `Settings.model_fields`,
`OPERATIONAL_KEYS`, and `_ALWAYS_SYNCED` — see `scripts/gen_docs.py`'s own
docstring). Do not hand-edit either file.

- [ ] **Step 13: Confirm the docs are no longer stale**

Run: `uv run pytest tests/test_gen_docs.py -v`
Expected: PASS, including `test_committed_reference_files_are_up_to_date`.

- [ ] **Step 14: Run the full suite and commit**

Run: `uv run pytest -v`
Expected: PASS, 0 failures.

```bash
git add app/config.py scripts/deploy.py render.yaml .env.example \
  guide/reference/config.md guide/reference/sync-env.md \
  tests/test_config.py tests/test_deploy_script.py
git commit -m "feat: add DASHBOARD_USERNAME/PASSWORD/SESSION_SECRET settings and wire them through deploy tooling"
```

---

## Task 2: `app/auth.py` core — credential check, session token, cookie helpers

**Files:**
- Create: `app/auth.py`
- Modify: `tests/conftest.py` (new autouse fixture, appended near
  `_quarantine_operator_apis`)
- Test: `tests/test_auth.py` (new)

**Interfaces:**
- Consumes: `Settings.dashboard_username`, `Settings.dashboard_password`,
  `Settings.dashboard_session_secret` (Task 1).
- Produces: `app.auth.SESSION_COOKIE_NAME: str`; `app.auth.SessionRequired`
  (exception class); `app.auth.verify_credentials(username: str, password: str) -> bool`;
  `app.auth.create_session_token(remember: bool) -> str`;
  `app.auth.set_session_cookie(response: Response, token: str, remember: bool) -> None`;
  `app.auth.clear_session_cookie(response: Response) -> None`;
  `async app.auth.require_session(request: Request) -> None` (raises
  `SessionRequired` on a missing/invalid/expired cookie).

- [ ] **Step 1: Add the shared test credential fixture to `tests/conftest.py`**

This has to land before the tests below, since every test in this new file
needs a real, known, non-empty credential to sign/verify against. Append
right after the existing `live_operator_apis_allowed` fixture:

```python
@pytest.fixture(autouse=True)
def _dashboard_credentials(monkeypatch):
    """A fixed, known-good operator credential for every test. app/main.py's
    lifespan (Task 4) refuses to boot with any of these empty, and app/auth.py's
    session-token functions need a real value to sign against -- fixed
    literal strings (not e.g. a random token) so tests that assert exact
    credential-check behavior have a known value to check against."""
    monkeypatch.setattr(settings, "dashboard_username", "test-operator")
    monkeypatch.setattr(settings, "dashboard_password", "test-password")
    monkeypatch.setattr(settings, "dashboard_session_secret", "test-session-secret")
```

(`settings` and `monkeypatch`/`pytest` are already imported at the top of
`tests/conftest.py` — no new imports needed for this step.)

- [ ] **Step 2: Write the failing tests for `app/auth.py`**

Create `tests/test_auth.py`:

```python
"""Tests for app/auth.py: credential check, session-token issue/verify, and
cookie helpers. Route-level (login/logout HTTP) tests are added in Task 3;
require_session's HTTP-gate behavior is added in Task 4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import auth
from app.config import settings


def _request_with_cookie(cookie_header: str | None) -> Request:
    headers = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode()))
    return Request({"type": "http", "headers": headers})


def test_verify_credentials_accepts_the_right_username_and_password():
    assert auth.verify_credentials("test-operator", "test-password") is True


def test_verify_credentials_rejects_wrong_username():
    assert auth.verify_credentials("wrong", "test-password") is False


def test_verify_credentials_rejects_wrong_password():
    assert auth.verify_credentials("test-operator", "wrong") is False


def test_verify_credentials_rejects_both_wrong():
    assert auth.verify_credentials("wrong", "wrong") is False


def test_create_session_token_defaults_to_a_12_hour_expiry():
    token = auth.create_session_token(remember=False)
    payload = jwt.decode(token, settings.dashboard_session_secret, algorithms=["HS256"])
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=11, minutes=59) < delta <= timedelta(hours=12)


def test_create_session_token_remember_extends_to_30_days():
    token = auth.create_session_token(remember=True)
    payload = jwt.decode(token, settings.dashboard_session_secret, algorithms=["HS256"])
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)


def test_set_session_cookie_sets_httponly_secure_samesite_strict():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=False)
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "secure" in cookie_header
    assert "samesite=strict" in cookie_header


def test_set_session_cookie_default_uses_the_12_hour_max_age():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=False)
    assert f"Max-Age={12 * 60 * 60}" in response.headers["set-cookie"]


def test_set_session_cookie_remember_uses_the_30_day_max_age():
    response = JSONResponse({})
    auth.set_session_cookie(response, "tok", remember=True)
    assert f"Max-Age={30 * 24 * 60 * 60}" in response.headers["set-cookie"]


def test_clear_session_cookie_expires_immediately():
    response = JSONResponse({})
    auth.clear_session_cookie(response)
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_require_session_accepts_a_freshly_issued_token():
    token = auth.create_session_token(remember=False)
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}")
    await auth.require_session(request)  # must not raise


async def test_require_session_rejects_a_missing_cookie():
    request = _request_with_cookie(None)
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_a_tampered_token():
    token = auth.create_session_token(remember=False)
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}x")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_an_expired_token():
    expired = jwt.encode(
        {"exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        settings.dashboard_session_secret,
        algorithm="HS256",
    )
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={expired}")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)


async def test_require_session_rejects_a_token_signed_with_a_different_secret():
    token = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "a-completely-different-secret",
        algorithm="HS256",
    )
    request = _request_with_cookie(f"{auth.SESSION_COOKIE_NAME}={token}")
    with pytest.raises(auth.SessionRequired):
        await auth.require_session(request)
```

- [ ] **Step 3: Run it, confirm it fails**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth'`.

- [ ] **Step 4: Implement `app/auth.py`**

```python
"""Dashboard authentication: a single shared operator credential gates
app/dashboard.py's router. Knows nothing about tickets, reviews, or
providers -- only app.config.settings for the three credential fields. See
docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Request, Response

from app.config import settings

SESSION_COOKIE_NAME = "dashboard_session"

_JWT_ALGORITHM = "HS256"
_DEFAULT_SESSION_SECONDS = 12 * 60 * 60
_REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60


class SessionRequired(Exception):
    """Raised by require_session when no valid session cookie is present.
    Caught by app/main.py's exception handler (Task 4), which turns it into
    a 401 JSON response (API routes) or a redirect to /login (page routes)."""


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time compare against the configured operator credential.
    Both fields are always compared via hmac.compare_digest, regardless of
    whether the first already failed, so timing never leaks which field (if
    either) was wrong."""
    username_ok = hmac.compare_digest(username, settings.dashboard_username)
    password_ok = hmac.compare_digest(password, settings.dashboard_password)
    return username_ok and password_ok


def create_session_token(remember: bool) -> str:
    """A signed JWT carrying only an expiry claim -- no identity to carry,
    since this is a single shared credential, not a per-user account."""
    seconds = _REMEMBER_SESSION_SECONDS if remember else _DEFAULT_SESSION_SECONDS
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return jwt.encode(
        {"exp": expires_at}, settings.dashboard_session_secret, algorithm=_JWT_ALGORITHM
    )


def set_session_cookie(response: Response, token: str, remember: bool) -> None:
    max_age = _REMEMBER_SESSION_SECONDS if remember else _DEFAULT_SESSION_SECONDS
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="strict"
    )


async def require_session(request: Request) -> None:
    """FastAPI dependency: raises SessionRequired unless the request carries
    a session cookie with a valid signature and an unexpired exp claim."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        raise SessionRequired()
    try:
        jwt.decode(token, settings.dashboard_session_secret, algorithms=[_JWT_ALGORITHM])
    except jwt.exceptions.InvalidTokenError as exc:
        raise SessionRequired() from exc
```

- [ ] **Step 5: Run it again, confirm it passes**

Run: `uv run pytest tests/test_auth.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Run the full suite (confirms the new conftest fixture doesn't
  break anything else) and commit**

Run: `uv run pytest -v`
Expected: PASS, 0 failures.

```bash
git add app/auth.py tests/conftest.py tests/test_auth.py
git commit -m "feat: add app/auth.py -- credential check, session-token issue/verify, cookie helpers"
```

---

## Task 3: Login/logout HTTP routes and the login page

**Files:**
- Modify: `app/auth.py` (append router + routes)
- Create: `app/static/login.html`
- Modify: `app/main.py` (mount `auth_router`, unprotected)
- Modify: `tests/test_auth.py` (append route-level tests)
- Test: `tests/test_login_page.py` (new)

**Interfaces:**
- Consumes: everything Task 2 produced in `app/auth.py`.
- Produces: `app.auth.router` (an `APIRouter` with `GET /login`,
  `POST /api/login`, `POST /api/logout`); `app.auth._delay_after_login_failure() -> None`
  (async, the patchable seam for the fixed post-failure delay).

- [ ] **Step 1: Write the failing route tests (appended to `tests/test_auth.py`)**

Add these imports at the top of `tests/test_auth.py` (alongside the existing
ones):

```python
from httpx import ASGITransport, AsyncClient

from app.main import app
```

Append to the end of the file:

```python
async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _no_login_delay(monkeypatch):
    """Every test in this file gets a no-op delay by default; the one test
    that verifies the delay actually fires overrides this itself."""
    async def _noop() -> None:
        return None

    monkeypatch.setattr(auth, "_delay_after_login_failure", _noop)


async def test_login_with_correct_credentials_sets_a_session_cookie():
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "test-password", "remember": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert auth.SESSION_COOKIE_NAME in resp.cookies


async def test_login_with_wrong_password_returns_the_generic_reason_and_no_cookie():
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "wrong", "remember": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "invalid_credentials"}
    assert auth.SESSION_COOKIE_NAME not in resp.cookies


async def test_login_with_wrong_username_returns_the_identical_generic_reason():
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "wrong", "password": "test-password", "remember": False},
    )
    assert resp.json() == {"valid": False, "reason": "invalid_credentials"}


async def test_login_failure_triggers_the_fixed_delay(monkeypatch):
    calls = []

    async def _record() -> None:
        calls.append(1)

    monkeypatch.setattr(auth, "_delay_after_login_failure", _record)
    client = await _client()
    await client.post(
        "/api/login", json={"username": "wrong", "password": "wrong", "remember": False}
    )
    assert calls == [1]


async def test_login_remember_true_sets_the_30_day_max_age():
    client = await _client()
    resp = await client.post(
        "/api/login",
        json={"username": "test-operator", "password": "test-password", "remember": True},
    )
    assert f"Max-Age={30 * 24 * 60 * 60}" in resp.headers["set-cookie"]


async def test_logout_clears_the_session_cookie():
    client = await _client()
    resp = await client.post("/api/logout")
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert "Max-Age=0" in resp.headers["set-cookie"]
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — `/api/login` and `/api/logout` don't exist yet (404s).

- [ ] **Step 3: Write the failing login-page test**

Create `tests/test_login_page.py` (mirrors `tests/test_dashboard_page.py`'s
own shape and shallow structural-assertion style):

```python
"""Tests for GET /login — the static HTML login page shell."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


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
```

- [ ] **Step 4: Run it, confirm it fails**

Run: `uv run pytest tests/test_login_page.py -v`
Expected: FAIL — `GET /login` 404s.

- [ ] **Step 5: Create `app/static/login.html`**

```html
<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR Review Engine — Login</title>
<style>
  :root {
    --bg: #f5f6f8;
    --surface: #ffffff;
    --surface-2: #eef0f3;
    --text: #1f2933;
    --text-muted: #5c6773;
    --border: #dde2e7;
    --accent: #3a6ea5;
    --ok: #2f7d4f;
    --fail: #b3454b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #12161b;
      --surface: #1a1f26;
      --surface-2: #22282f;
      --text: #e6e9ec;
      --text-muted: #9aa5b1;
      --border: #2b323a;
      --accent: #7ba7d9;
      --ok: #5fbf87;
      --fail: #e08086;
    }
  }
  :root[data-theme="dark"] {
    --bg: #12161b;
    --surface: #1a1f26;
    --surface-2: #22282f;
    --text: #e6e9ec;
    --text-muted: #9aa5b1;
    --border: #2b323a;
    --accent: #7ba7d9;
    --ok: #5fbf87;
    --fail: #e08086;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  header.topbar {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border);
  }
  button.control {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.4rem 0.9rem;
    font-size: 0.9rem;
    cursor: pointer;
  }
  button.control:hover { border-color: var(--accent); }
  .popup-backdrop {
    position: fixed;
    inset: 0;
    background: transparent;
    display: none;
    z-index: 10;
  }
  .popup-backdrop.open { display: block; }
  .popup {
    position: absolute;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    padding: 1rem 1.25rem;
    min-width: 220px;
    max-width: min(90vw, 320px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
  }
  .popup h2 { margin: 0 0 0.75rem; font-size: 1rem; }
  .radio-group { display: flex; flex-direction: column; gap: 0.5rem; }
  .radio-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.25rem;
    cursor: pointer;
    font-size: 0.95rem;
  }
  main.login-main { max-width: 360px; margin: 12vh auto 0; padding: 0 1rem; }
  .login-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    padding: 1.5rem;
  }
  h1.login-title { font-size: 1.2rem; margin: 0 0 1.25rem; text-align: center; }
  .field-group { margin-bottom: 1rem; }
  label.field-label {
    display: block;
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-bottom: 0.3rem;
  }
  input[type="text"], input[type="password"] {
    width: 100%;
    padding: 0.5rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 0.4rem;
    background: var(--surface);
    color: var(--text);
    font-size: 0.95rem;
  }
  .remember-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 1rem;
    font-size: 0.9rem;
  }
  button.submit-btn {
    width: 100%;
    padding: 0.6rem;
    border: none;
    border-radius: 0.4rem;
    background: var(--accent);
    color: #fff;
    font-size: 0.95rem;
    cursor: pointer;
  }
  #loginError { color: var(--fail); font-size: 0.85rem; margin-top: 0.75rem; text-align: center; }
  #loginError:empty { display: none; }
</style>
</head>
<body>
  <header class="topbar">
    <button id="themeToggleBtn" class="control" type="button" aria-haspopup="dialog"></button>
    <button id="langToggleBtn" class="control" type="button" aria-haspopup="dialog"></button>
  </header>

  <div id="themePopupBackdrop" class="popup-backdrop">
    <div class="popup" role="dialog" aria-modal="true" aria-labelledby="themePopupTitle">
      <h2 id="themePopupTitle"></h2>
      <div class="radio-group">
        <label><input type="radio" name="theme" value="light"> <span data-i18n="theme_light"></span></label>
        <label><input type="radio" name="theme" value="dark"> <span data-i18n="theme_dark"></span></label>
        <label><input type="radio" name="theme" value="system"> <span data-i18n="theme_system"></span></label>
      </div>
    </div>
  </div>

  <div id="langPopupBackdrop" class="popup-backdrop">
    <div class="popup" role="dialog" aria-modal="true" aria-labelledby="langPopupTitle">
      <h2 id="langPopupTitle"></h2>
      <div class="radio-group">
        <label><input type="radio" name="lang" value="en"> 🇺🇸 English</label>
        <label><input type="radio" name="lang" value="he"> 🇮🇱 עברית</label>
      </div>
    </div>
  </div>

  <main class="login-main">
    <div class="login-card">
      <h1 class="login-title" data-i18n="login_title"></h1>
      <form id="loginForm">
        <div class="field-group">
          <label class="field-label" for="usernameInput" data-i18n="username_label"></label>
          <input type="text" id="usernameInput" name="username" autocomplete="username" required>
        </div>
        <div class="field-group">
          <label class="field-label" for="passwordInput" data-i18n="password_label"></label>
          <input type="password" id="passwordInput" name="password" autocomplete="current-password" required>
        </div>
        <div class="remember-row">
          <input type="checkbox" id="rememberInput" name="remember">
          <label for="rememberInput" data-i18n="remember_label"></label>
        </div>
        <button type="submit" class="submit-btn" data-i18n="submit_label"></button>
        <div id="loginError" role="alert"></div>
      </form>
    </div>
  </main>

  <script>
    const STRINGS = {
      en: {
        login_title: "Sign in",
        username_label: "Username",
        password_label: "Password",
        remember_label: "Remember me",
        submit_label: "Sign in",
        error_invalid_credentials: "Incorrect username or password.",
        theme_light: "Light",
        theme_dark: "Dark",
        theme_system: "System",
        theme_popup_title: "Theme",
        lang_popup_title: "Language",
      },
      he: {
        login_title: "התחברות",
        username_label: "שם משתמש",
        password_label: "סיסמה",
        remember_label: "זכור אותי",
        submit_label: "התחבר",
        error_invalid_credentials: "שם משתמש או סיסמה שגויים.",
        theme_light: "בהיר",
        theme_dark: "כהה",
        theme_system: "מערכת",
        theme_popup_title: "עיצוב",
        lang_popup_title: "שפה",
      },
    };

    const THEME_ICON = { light: "☀️", dark: "🌙", system: "🖥️" };
    const LANG_LABEL = { en: "🇺🇸 English", he: "🇮🇱 עברית" };

    const KNOWN_LANGS = ["en", "he"];
    const KNOWN_THEMES = ["light", "dark", "system"];

    function readStoredLang() {
      const stored = localStorage.getItem("dashboard_lang");
      return KNOWN_LANGS.includes(stored) ? stored : "en";
    }

    function readStoredTheme() {
      const stored = localStorage.getItem("dashboard_theme");
      return KNOWN_THEMES.includes(stored) ? stored : "system";
    }

    let currentLang = readStoredLang();
    let currentTheme = readStoredTheme();

    function t(key) {
      return STRINGS[currentLang][key] || STRINGS.en[key] || key;
    }

    function applyTheme(theme) {
      currentTheme = theme;
      localStorage.setItem("dashboard_theme", theme);
      document.documentElement.setAttribute("data-theme", theme === "system" ? "" : theme);
      document.getElementById("themeToggleBtn").textContent =
        `${THEME_ICON[theme]} ${t("theme_" + theme)}`;
      document.querySelector(`input[name="theme"][value="${theme}"]`).checked = true;
    }

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem("dashboard_lang", lang);
      document.documentElement.setAttribute("lang", lang);
      document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr");
      document.getElementById("langToggleBtn").textContent = LANG_LABEL[lang];
      document.querySelectorAll("[data-i18n]").forEach((el) => {
        el.textContent = t(el.getAttribute("data-i18n"));
      });
      document.getElementById("themePopupTitle").textContent = t("theme_popup_title");
      document.getElementById("langPopupTitle").textContent = t("lang_popup_title");
      document.querySelector(`input[name="lang"][value="${lang}"]`).checked = true;
      applyTheme(currentTheme);
    }

    function positionPopup(popup, anchorBtn) {
      const margin = 8;
      const rect = anchorBtn.getBoundingClientRect();
      const isRtl = document.documentElement.getAttribute("dir") === "rtl";
      popup.style.top = `${rect.bottom + margin}px`;
      popup.style.left = "auto";
      popup.style.right = "auto";
      const popupWidth = popup.offsetWidth;
      if (isRtl) {
        const maxRight = window.innerWidth - popupWidth - margin;
        const right = Math.min(Math.max(window.innerWidth - rect.right, margin), maxRight);
        popup.style.right = `${right}px`;
      } else {
        const maxLeft = window.innerWidth - popupWidth - margin;
        const left = Math.max(margin, Math.min(rect.left, maxLeft));
        popup.style.left = `${left}px`;
      }
    }

    function openPopup(id, anchorBtn) {
      closeAllPopups();
      const backdrop = document.getElementById(id);
      backdrop.classList.add("open");
      positionPopup(backdrop.querySelector(".popup"), anchorBtn);
    }

    function closeAllPopups() {
      document.querySelectorAll(".popup-backdrop").forEach((el) => el.classList.remove("open"));
    }

    document.getElementById("themeToggleBtn").addEventListener("click", (event) => openPopup("themePopupBackdrop", event.currentTarget));
    document.getElementById("langToggleBtn").addEventListener("click", (event) => openPopup("langPopupBackdrop", event.currentTarget));

    document.querySelectorAll(".popup-backdrop").forEach((backdrop) => {
      backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) closeAllPopups();
      });
    });

    document.querySelectorAll('input[name="theme"]').forEach((radio) => {
      radio.addEventListener("change", (event) => {
        applyTheme(event.target.value);
        closeAllPopups();
      });
    });

    document.querySelectorAll('input[name="lang"]').forEach((radio) => {
      radio.addEventListener("change", (event) => {
        applyLanguage(event.target.value);
        closeAllPopups();
      });
    });

    document.getElementById("loginForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorEl = document.getElementById("loginError");
      errorEl.textContent = "";
      const username = document.getElementById("usernameInput").value;
      const password = document.getElementById("passwordInput").value;
      const remember = document.getElementById("rememberInput").checked;
      try {
        const response = await fetch("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password, remember }),
        });
        const data = await response.json();
        if (data.valid) {
          window.location.href = "/";
        } else {
          errorEl.textContent = t("error_invalid_credentials");
        }
      } catch (err) {
        errorEl.textContent = t("error_invalid_credentials");
      }
    });

    applyLanguage(currentLang);
  </script>
</body>
</html>
```

- [ ] **Step 6: Append the router and routes to `app/auth.py`**

Add these imports at the top of `app/auth.py` (alongside the existing ones):

```python
import asyncio
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
```

Append to the end of `app/auth.py`:

```python
_LOGIN_FAILURE_DELAY_SECONDS = 1.0
_STATIC_DIR = Path(__file__).parent / "static"
_LOGIN_HTML = (_STATIC_DIR / "login.html").read_text(encoding="utf-8")

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


async def _delay_after_login_failure() -> None:
    """Isolated so tests can patch out the real wait without touching every
    other asyncio.sleep call in the process. Deliberately asyncio.sleep, not
    time.sleep: app/queue/dispatcher.py's serial loop runs in this same
    process's single event loop, and a blocking time.sleep(1) here would
    stall it (and every other in-flight request) for the duration of every
    single failed login attempt."""
    await asyncio.sleep(_LOGIN_FAILURE_DELAY_SECONDS)


@router.get("/login")
async def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML)


@router.post("/api/login")
async def login(payload: LoginRequest) -> JSONResponse:
    if not verify_credentials(payload.username, payload.password):
        await _delay_after_login_failure()
        return JSONResponse({"valid": False, "reason": "invalid_credentials"})
    token = create_session_token(remember=payload.remember)
    response = JSONResponse({"valid": True})
    set_session_cookie(response, token, remember=payload.remember)
    return response


@router.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"valid": True})
    clear_session_cookie(response)
    return response
```

- [ ] **Step 7: Mount `auth_router` in `app/main.py` (unprotected)**

Add this import alongside the existing `from app.dashboard import router as dashboard_router`:

```python
from app.auth import router as auth_router
```

Change:

```python
app = FastAPI(title="pr-review-engine", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(dashboard_router)
```

to:

```python
app = FastAPI(title="pr-review-engine", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
```

(`dashboard_router` is not yet gated — that's Task 4.)

- [ ] **Step 8: Run the new tests, confirm they pass**

Run: `uv run pytest tests/test_auth.py tests/test_login_page.py -v`
Expected: PASS, all tests.

- [ ] **Step 9: Run the full suite and commit**

Run: `uv run pytest -v`
Expected: PASS, 0 failures.

```bash
git add app/auth.py app/static/login.html app/main.py tests/test_auth.py tests/test_login_page.py
git commit -m "feat: add login/logout routes and the login page (unprotected; dashboard gating lands next)"
```

---

## Task 4: Gate `dashboard_router`, add the startup guard, rework existing dashboard tests

**Files:**
- Modify: `app/main.py` (exception handler, `dependencies=[Depends(require_session)]`,
  lifespan guard)
- Modify: `tests/test_main_lifespan.py` (3 new tests)
- Modify: `tests/test_dashboard_page.py` (auth cookie in `_client()`)
- Modify: `tests/test_dashboard_api.py` (auth cookie in `_client()`)
- Modify: `tests/test_auth.py` (2 new gate-behavior tests)

**Interfaces:**
- Consumes: `app.auth.SessionRequired`, `app.auth.require_session`,
  `app.auth.SESSION_COOKIE_NAME`, `app.auth.create_session_token` (Tasks 2-3).
- Produces: the complete, gated feature — `GET /` and `GET /api/dashboard`
  now require a valid session.

- [ ] **Step 1: Write the failing gate tests (appended to `tests/test_auth.py`)**

```python
async def test_unauthenticated_api_dashboard_request_gets_401_json():
    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 401
    assert resp.json() == {"valid": False, "reason": "unauthenticated"}


async def test_unauthenticated_dashboard_page_request_redirects_to_login():
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)
    resp = await client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `uv run pytest tests/test_auth.py -k unauthenticated -v`
Expected: FAIL — `/` and `/api/dashboard` currently return 200 with no
cookie at all (not yet gated).

- [ ] **Step 3: Write the failing lifespan tests (appended to `tests/test_main_lifespan.py`)**

```python
async def test_lifespan_fails_loudly_when_dashboard_username_is_empty(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_username", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_dashboard_password_is_empty(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_password", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_dashboard_session_secret_is_empty(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "dashboard_session_secret", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_SESSION_SECRET"):
        async with main.lifespan(main.app):
            pass
```

(No change needed to this file's existing `_env` autouse fixture — Task 2's
new `tests/conftest.py` fixture already gives every test in the suite,
including these, a valid non-empty stand-in for all three fields by default.)

- [ ] **Step 4: Run it, confirm it fails**

Run: `uv run pytest tests/test_main_lifespan.py -k dashboard -v`
Expected: FAIL — lifespan doesn't check these fields yet, so no `RuntimeError`
is raised.

- [ ] **Step 5: Implement the `app/main.py` changes**

Add these imports (alongside the existing `from fastapi import FastAPI`):

```python
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
```

Add this import alongside `from app.auth import router as auth_router`:

```python
from app.auth import SessionRequired, require_session
```

In `lifespan()`, add the new guard right after the existing
`github_app_installation_id` empty-check block (i.e. right before the
`# Verified on every boot, not just when unset:` comment that precedes the
`discover_and_verify_installation_id` call):

```python
    if (
        not settings.dashboard_username
        or not settings.dashboard_password
        or not settings.dashboard_session_secret
    ):
        raise RuntimeError(
            "DASHBOARD_USERNAME, DASHBOARD_PASSWORD, and DASHBOARD_SESSION_SECRET must "
            "all be set -- refusing to start (an empty credential would let any "
            "username/password pair, or any forged session token, through)."
        )
```

Change:

```python
app = FastAPI(title="pr-review-engine", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
```

to:

```python
app = FastAPI(title="pr-review-engine", lifespan=lifespan)


@app.exception_handler(SessionRequired)
async def _handle_session_required(request: Request, exc: SessionRequired) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"valid": False, "reason": "unauthenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router, dependencies=[Depends(require_session)])
```

- [ ] **Step 6: Run the new tests, confirm they pass**

Run: `uv run pytest tests/test_auth.py tests/test_main_lifespan.py -v`
Expected: PASS, all tests.

- [ ] **Step 7: Run the rest of the suite — see the expected dashboard-test breakage**

Run: `uv run pytest tests/test_dashboard_page.py tests/test_dashboard_api.py -v`
Expected: FAIL — every test now redirects (`/`) or 401s (`/api/dashboard`)
instead of returning real content, since these tests send no session cookie.

- [ ] **Step 8: Fix `tests/test_dashboard_page.py`'s `_client()` helper**

Add `from app import auth` to the imports. Change:

```python
async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")
```

to:

```python
async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    )
```

- [ ] **Step 9: Fix `tests/test_dashboard_api.py`'s `_client()` helper**

Same change: add `from app import auth` to the imports, and apply the
identical `_client()` edit as Step 8.

- [ ] **Step 10: Run the dashboard tests again, confirm they pass**

Run: `uv run pytest tests/test_dashboard_page.py tests/test_dashboard_api.py -v`
Expected: PASS, all tests — they're verifying dashboard content again, now
via an authenticated client.

- [ ] **Step 11: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS, 0 failures. This also re-confirms (via the pre-existing,
untouched `tests/test_webhook.py` and `tests/test_skeleton.py`) that
`/webhook` and `/healthz` remain reachable with no session cookie at all,
since neither route is on `dashboard_router`.

- [ ] **Step 12: Run ruff**

Run: `uv run ruff check .`
Expected: no errors. Fix any line-length or import-order issues the new code
introduced before committing (see this repo's own `pyproject.toml`
`[tool.ruff]` config — 100-char limit).

- [ ] **Step 13: Commit**

```bash
git add app/main.py tests/test_main_lifespan.py tests/test_dashboard_page.py \
  tests/test_dashboard_api.py tests/test_auth.py
git commit -m "feat: gate the dashboard behind require_session; refuse to boot with empty dashboard credentials"
```

---

## Post-plan note

Once this merges, the parked "Dashboard 'Environment' feature" entry in
`ISSUES.md`'s Design Gaps section is unblocked — its own brainstorm/spec/plan
cycle can resume on top of a now-authenticated dashboard.
