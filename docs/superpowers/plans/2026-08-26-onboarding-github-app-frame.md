# Onboarding Wizard — GitHub App Automation (Frame 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the onboarding wizard's frame 2 fully functional — a visitor
creates a GitHub App via the Manifest flow, installs it, and the wizard
captures the App's credentials + installation id into `sessionStorage`,
exactly the four values `scripts/deploy.py::sync_env()` already knows how to
push into a Render service's env vars.

**Architecture:** Two new backend relay endpoints
(`onboarding/github_client.py` + two `router.py` routes) plus frontend JS
that drives two sequential full-page round trips to `github.com` and back,
using `sessionStorage` to survive each navigation — the same
stateless-relay/`sessionStorage` shape sub-project 1 already established for
the Render-key frame.

**Tech Stack:** FastAPI, httpx (manifest-code exchange), PyGithub (App-JWT
installation verification — already an approved `pygithub>=2.4` dependency),
pydantic, vanilla JS (no build step), pytest + pytest-asyncio + respx +
`requests.adapters.HTTPAdapter` transport patching (PyGithub's own HTTP
library, which respx cannot intercept).

**Spec:**
`docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`

## Global Constraints

- `onboarding/` is a **stateless relay** — no database, no session store, no
  server-side persistence of any visitor credential, ever (onboarding/CLAUDE.md).
- **Never log a visitor-supplied or freshly-minted credential**, in full or
  truncated.
- Every relay endpoint returns a verdict, never a submitted credential —
  **except** `/api/github/exchange-manifest-code`, a documented exception:
  it mints and returns App credentials that belong to the visitor who just
  created them (spec section 4).
- New external-service integrations follow the existing relay shape:
  browser holds the token, backend is a stateless pass-through per request.
- `onboarding/` and `app/` never share credential-handling code paths — a
  new independent PyGithub client is built here, not imported from
  `app/github_app.py`.
- `onboarding/config.py`'s `public_base_url` defaults to `""`, is validated
  explicitly (not via a pydantic-required field) in `onboarding/main.py`'s
  `lifespan`, matching `app/config.py`/`app/main.py`'s own established
  pattern for exactly this reason: a pydantic-required field would raise the
  moment anything imports the module (including pytest collection), before
  a clear error could ever be reported.
- `GET /`'s CSP must include `form-action 'self' https://github.com;`
  alongside its existing directives — the manifest form POST to
  `github.com` is otherwise blocked by the current `default-src 'none'`.
- PyGithub's HTTP calls go through `requests`, not `httpx` — `respx` cannot
  mock them. Tests that exercise PyGithub calls patch
  `requests.adapters.HTTPAdapter.send` instead, mirroring
  `tests/test_github_app.py`'s own `FakeGithubTransport` pattern.
- No bare `except Exception` anywhere — narrow, explicit exception handling
  only, matching `render_client.py`'s existing convention.
- No live GitHub API calls in tests — mock `httpx`/`requests` responses.
- A visitor's credential lives in `sessionStorage`, never `localStorage`.

---

### Task 1: `onboarding/config.py` + boot-time validation

**Files:**
- Create: `onboarding/config.py`
- Modify: `onboarding/main.py`
- Test: `tests/test_onboarding_config.py` (create)
- Test: `tests/test_onboarding_main.py` (modify)

**Interfaces:**
- Produces: `onboarding.config.settings` — a `Settings` instance with
  `.public_base_url: str` (defaults to `""`, reads from the `PUBLIC_BASE_URL`
  env var). `onboarding.main.lifespan` — an `@contextlib.asynccontextmanager`
  async function taking `app: FastAPI`, raising `RuntimeError` if
  `settings.public_base_url` is empty.

- [ ] **Step 1: Write the failing tests**

`tests/test_onboarding_config.py`:
```python
"""Tests for onboarding/config.py — public_base_url reads from the real
process environment only (no .env/.env.config file: onboarding/ is a
separate deployed service, not sharing the review engine's config files).
See docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
section 5."""
from __future__ import annotations

from onboarding.config import Settings


def test_public_base_url_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert Settings().public_base_url == ""


def test_public_base_url_reads_from_environment(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://onboarding.example.com")
    assert Settings().public_base_url == "https://onboarding.example.com"
```

Add to `tests/test_onboarding_main.py` (existing file — keep its current
`healthz` tests untouched, add these):
```python
import pytest

from onboarding.config import settings
from onboarding.main import app, lifespan


async def test_lifespan_refuses_to_start_without_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        async with lifespan(app):
            pass


async def test_lifespan_starts_with_public_base_url_set(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    async with lifespan(app):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_config.py tests/test_onboarding_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'onboarding.config'`
(config tests) and `ImportError: cannot import name 'lifespan'` (main tests).

- [ ] **Step 3: Write `onboarding/config.py`**

```python
"""onboarding/'s own Settings — a separate deployed service from app/, so
this does NOT import app/config.py's Settings (per onboarding/CLAUDE.md's
no-shared-credential-path rule) even though public_base_url is conceptually
similar to app/config.py's own field of the same name."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # No pydantic-required (no-default) field: that would raise the moment
    # anything first imports this module — including pytest collection —
    # before onboarding/main.py's lifespan could report the problem with a
    # clear message. Same reasoning as app/config.py's own public_base_url
    # field. Validated explicitly in the lifespan instead (Task 1 step 5).
    public_base_url: str = ""


settings = Settings()
```

- [ ] **Step 4: Modify `onboarding/main.py`**

Replace the file's contents with:
```python
"""onboarding/ — self-service setup wizard: a separate service from the
review engine in app/. Stateless relay only — no database, no session
store. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md.
"""
from __future__ import annotations

import contextlib

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from onboarding.config import settings
from onboarding.router import router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.public_base_url:
        raise RuntimeError(
            "PUBLIC_BASE_URL is unset — refusing to start. Frame 2's GitHub "
            "App manifest needs this service's own real public URL to build "
            "redirect_url/setup_url; without it the manifest flow cannot work."
        )
    yield


app = FastAPI(title="onboarding-wizard", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "invalid request"})


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_config.py tests/test_onboarding_main.py -v`
Expected: PASS (all tests, including the pre-existing `healthz` ones).

- [ ] **Step 6: Commit**

```bash
git add onboarding/config.py onboarding/main.py tests/test_onboarding_config.py tests/test_onboarding_main.py
git commit -m "feat: add onboarding/ config with boot-time public_base_url check"
```

---

### Task 2: `onboarding/github_client.py` — manifest code exchange

**Files:**
- Create: `onboarding/github_client.py`
- Test: `tests/test_onboarding_github_client.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `GithubAppCreated(app_id: int, slug: str, private_key_b64: str,
  webhook_secret: str)`, `GithubAppExchangeFailed(reason: str)` (reason ∈
  `"exchange_failed"` | `"github_unreachable"`),
  `GithubAppResult = GithubAppCreated | GithubAppExchangeFailed`,
  `async def exchange_manifest_code(code: str) -> GithubAppResult` — used by
  Task 4's router.

- [ ] **Step 1: Write the failing tests**

`tests/test_onboarding_github_client.py`:
```python
"""Tests for onboarding/github_client.py — GitHub App manifest-code
exchange never logs or returns anything but the documented fields, and
distinguishes a rejected/expired code from GitHub being unreachable. See
docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
sections 3-4."""
from __future__ import annotations

import base64

import httpx
import respx

from onboarding import github_client

CODE = "SENTINEL_MANIFEST_CODE"
CONVERSIONS_URL = f"https://api.github.com/app-manifests/{CODE}/conversions"


async def test_valid_code_returns_app_credentials():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": 42,
                    "slug": "my-pr-review-bot",
                    "pem": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "webhook_secret": "whsec_sentinel",
                },
            )
        )
        result = await github_client.exchange_manifest_code(CODE)
    assert isinstance(result, github_client.GithubAppCreated)
    assert result.app_id == 42
    assert result.slug == "my-pr-review-bot"
    assert result.webhook_secret == "whsec_sentinel"
    assert base64.b64decode(result.private_key_b64).decode() == (
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
    )


async def test_expired_or_bad_code_is_exchange_failed():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="exchange_failed")


async def test_github_5xx_is_unreachable_not_exchange_failed():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(500))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_malformed_200_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(201, text="not json"))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_response_missing_expected_fields_is_unreachable():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(201, json={"id": 42}))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_exchange_sends_no_authorization_header():
    """The manifest code IS the credential — no auth header is needed or
    sent, matching scripts/create_github_app.py::exchange_code()."""
    with respx.mock:
        route = respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={"id": 1, "slug": "x", "pem": "pem", "webhook_secret": "whsec"},
            )
        )
        await github_client.exchange_manifest_code(CODE)
    assert "authorization" not in {h.lower() for h in route.calls.last.request.headers}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_github_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'onboarding.github_client'`

- [ ] **Step 3: Write `onboarding/github_client.py`**

```python
"""Thin async wrapper around GitHub's App Manifest and App-installation
APIs — creates/verifies a visitor's own GitHub App without persisting
anything server-side. See
docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
sections 3-4, 6."""
from __future__ import annotations

import base64
import dataclasses

import httpx

GITHUB_API_BASE = "https://api.github.com"


@dataclasses.dataclass(frozen=True)
class GithubAppCreated:
    app_id: int
    slug: str
    private_key_b64: str
    webhook_secret: str


@dataclasses.dataclass(frozen=True)
class GithubAppExchangeFailed:
    reason: str  # "exchange_failed" | "github_unreachable"


GithubAppResult = GithubAppCreated | GithubAppExchangeFailed


async def exchange_manifest_code(code: str) -> GithubAppResult:
    """Trade GitHub's one-time manifest code for the newly created App's
    credentials (POST /app-manifests/{code}/conversions — no auth header
    needed, the code itself is the one-time credential, matching
    scripts/create_github_app.py::exchange_code()). Never logs the response
    body, which carries the App's private key and webhook secret."""
    try:
        async with httpx.AsyncClient(base_url=GITHUB_API_BASE, timeout=10.0) as client:
            response = await client.post(
                f"/app-manifests/{code}/conversions",
                headers={"Accept": "application/vnd.github+json"},
            )
    except httpx.HTTPError:
        return GithubAppExchangeFailed(reason="github_unreachable")

    if response.status_code >= 500:
        return GithubAppExchangeFailed(reason="github_unreachable")
    if response.status_code >= 400:
        return GithubAppExchangeFailed(reason="exchange_failed")

    try:
        body = response.json()
        app_id = int(body["id"])
        slug = str(body["slug"])
        pem = str(body["pem"])
        webhook_secret = str(body["webhook_secret"])
    except (ValueError, KeyError, TypeError):
        return GithubAppExchangeFailed(reason="github_unreachable")

    private_key_b64 = base64.b64encode(pem.encode()).decode()
    return GithubAppCreated(
        app_id=app_id, slug=slug, private_key_b64=private_key_b64, webhook_secret=webhook_secret
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_github_client.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add onboarding/github_client.py tests/test_onboarding_github_client.py
git commit -m "feat: add GitHub App manifest-code exchange"
```

---

### Task 3: `onboarding/github_client.py` — installation verification

**Files:**
- Modify: `onboarding/github_client.py`
- Modify: `tests/test_onboarding_github_client.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks (independent of `exchange_manifest_code`).
- Produces: `InstallationVerified(account_login: str, repo_scope: str)`,
  `InstallationInvalid(reason: str)` (reason ∈ `"installation_not_found"` |
  `"invalid_credentials"` | `"github_unreachable"`),
  `InstallationResult = InstallationVerified | InstallationInvalid`,
  `async def verify_installation(app_id: int, private_key_b64: str,
  installation_id: int) -> InstallationResult` — used by Task 4's router.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_github_client.py`:
```python
import json
import time

import pytest
import requests as requests_lib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="module")
def _throwaway_key_material() -> str:
    """A throwaway RSA key, base64-encoded like the real
    exchange_manifest_code output would be. Only used for local JWT
    signing in these tests — every HTTP call is mocked below, so nothing
    is ever sent anywhere with it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


@pytest.fixture(autouse=True)
def _no_pygithub_rate_limit_sleep(monkeypatch):
    """Mirrors tests/test_github_app.py's own fixture of the same name:
    PyGithub's Requester paces real requests with time.sleep(); every call
    here goes through fake_transport below, so the throttle protects
    nothing and only wastes wall-clock."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class _FakeGithubTransport:
    """Routes requests by (method, url-substring) to canned JSON responses.
    PyGithub calls through `requests`, not `httpx` — respx cannot intercept
    it — so this patches the same requests.adapters.HTTPAdapter.send
    transport boundary tests/test_github_app.py's own FakeGithubTransport
    uses, scoped down to what onboarding/github_client.py actually needs
    (one App-JWT endpoint, no installation-token exchange)."""

    def __init__(self):
        self.routes: list[tuple[str, str, dict, int]] = []

    def route(self, method: str, url_substring: str, json_body, status_code: int = 200):
        self.routes.append((method.upper(), url_substring, json_body, status_code))

    def send(self, request: requests_lib.PreparedRequest, **kwargs) -> requests_lib.Response:
        for method, url_substring, json_body, status_code in sorted(
            self.routes, key=lambda r: -len(r[1])
        ):
            if request.method == method and url_substring in request.url:
                resp = requests_lib.Response()
                resp.status_code = status_code
                resp.headers["Content-Type"] = "application/json"
                resp._content = json.dumps(json_body).encode("utf-8")
                resp.encoding = "utf-8"
                resp.url = request.url
                resp.reason = "OK"
                resp.request = request
                return resp
        raise AssertionError(f"Unmocked request: {request.method} {request.url}")


@pytest.fixture
def fake_transport(monkeypatch):
    transport = _FakeGithubTransport()
    monkeypatch.setattr(requests_lib.adapters.HTTPAdapter, "send", transport.send)
    return transport


async def test_valid_installation_returns_account_and_scope(fake_transport, _throwaway_key_material):
    fake_transport.route(
        "GET",
        "/app/installations/456",
        {"id": 456, "account": {"login": "octocat"}, "repository_selection": "selected"},
    )
    result = await github_client.verify_installation(
        app_id=999, private_key_b64=_throwaway_key_material, installation_id=456
    )
    assert result == github_client.InstallationVerified(
        account_login="octocat", repo_scope="selected"
    )


async def test_installation_not_found_is_reported(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app/installations/456", {"message": "Not Found"}, 404)
    result = await github_client.verify_installation(
        app_id=999, private_key_b64=_throwaway_key_material, installation_id=456
    )
    assert result == github_client.InstallationInvalid(reason="installation_not_found")


async def test_unauthorized_is_invalid_credentials(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app/installations/456", {"message": "Bad credentials"}, 401)
    result = await github_client.verify_installation(
        app_id=999, private_key_b64=_throwaway_key_material, installation_id=456
    )
    assert result == github_client.InstallationInvalid(reason="invalid_credentials")


async def test_server_error_is_unreachable(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app/installations/456", {}, 500)
    result = await github_client.verify_installation(
        app_id=999, private_key_b64=_throwaway_key_material, installation_id=456
    )
    assert result == github_client.InstallationInvalid(reason="github_unreachable")


async def test_malformed_base64_private_key_is_invalid_credentials():
    result = await github_client.verify_installation(
        app_id=999, private_key_b64="not-valid-base64!!", installation_id=456
    )
    assert result == github_client.InstallationInvalid(reason="invalid_credentials")


async def test_valid_base64_but_not_a_real_pem_is_invalid_credentials():
    garbage_pem_b64 = base64.b64encode(b"not a real PEM").decode()
    result = await github_client.verify_installation(
        app_id=999, private_key_b64=garbage_pem_b64, installation_id=456
    )
    assert result == github_client.InstallationInvalid(reason="invalid_credentials")


async def test_response_missing_expected_fields_is_unreachable(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app/installations/456", {"id": 456})
    result = await github_client.verify_installation(
        app_id=999, private_key_b64=_throwaway_key_material, installation_id=456
    )
    assert result == github_client.InstallationInvalid(reason="github_unreachable")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_github_client.py -v`
Expected: FAIL — `AttributeError: module 'onboarding.github_client' has no
attribute 'verify_installation'` (and `InstallationVerified`/`InstallationInvalid`).

- [ ] **Step 3: Add to `onboarding/github_client.py`**

Change the top-of-file imports from:
```python
from __future__ import annotations

import base64
import dataclasses

import httpx

GITHUB_API_BASE = "https://api.github.com"
```
to:
```python
from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses

import httpx
import requests
from github import Auth, Github, GithubException

GITHUB_API_BASE = "https://api.github.com"
```

Then append:

```python
@dataclasses.dataclass(frozen=True)
class InstallationVerified:
    account_login: str
    repo_scope: str  # "all" | "selected"


@dataclasses.dataclass(frozen=True)
class InstallationInvalid:
    reason: str  # "installation_not_found" | "invalid_credentials" | "github_unreachable"


InstallationResult = InstallationVerified | InstallationInvalid


def _fetch_installation(app_id: int, private_key_pem: str, installation_id: int) -> dict:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    Builds its own independent client from the visitor's just-minted
    app_id/private_key (never app/github_app.py's operator-tied helpers —
    onboarding/CLAUDE.md's no-shared-credential-path rule). App-JWT only
    (Auth.AppAuth, not an installation access token) — this call only needs
    to read the installation's own metadata."""
    gh = Github(auth=Auth.AppAuth(app_id, private_key_pem))
    _, data = gh.requester.requestJsonAndCheck("GET", f"/app/installations/{installation_id}")
    return data


async def verify_installation(
    app_id: int, private_key_b64: str, installation_id: int
) -> InstallationResult:
    """Confirm installation_id really belongs to app_id's App. Never logs
    the private key, in full or truncated — same sensitivity tier as this
    project's own GITHUB_APP_PRIVATE_KEY."""
    try:
        private_key_pem = base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError):
        return InstallationInvalid(reason="invalid_credentials")

    try:
        data = await asyncio.to_thread(
            _fetch_installation, app_id, private_key_pem, installation_id
        )
    except GithubException as exc:
        if exc.status == 404:
            return InstallationInvalid(reason="installation_not_found")
        if exc.status in (401, 403):
            return InstallationInvalid(reason="invalid_credentials")
        return InstallationInvalid(reason="github_unreachable")
    except ValueError:
        # A base64-valid but non-PEM string: PyGithub/cryptography raises
        # ValueError when it tries to parse the key material, not when it
        # was decoded above.
        return InstallationInvalid(reason="invalid_credentials")
    except requests.exceptions.RequestException:
        return InstallationInvalid(reason="github_unreachable")

    try:
        account_login = str(data["account"]["login"])
        repo_scope = str(data["repository_selection"])
    except (KeyError, TypeError):
        return InstallationInvalid(reason="github_unreachable")

    return InstallationVerified(account_login=account_login, repo_scope=repo_scope)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_github_client.py -v`
Expected: PASS (15 tests total)

- [ ] **Step 5: Commit**

```bash
git add onboarding/github_client.py tests/test_onboarding_github_client.py
git commit -m "feat: add GitHub App installation verification"
```

---

### Task 4: `onboarding/router.py` — routes, CSP fix, base-URL templating

**Files:**
- Modify: `onboarding/router.py`
- Modify: `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `onboarding.config.settings.public_base_url` (Task 1);
  `onboarding.github_client.exchange_manifest_code`,
  `onboarding.github_client.verify_installation`, and their result types
  (Tasks 2-3).
- Produces: `POST /api/github/exchange-manifest-code`,
  `POST /api/github/verify-installation` — JSON contracts per spec section 4.
  `GET /`'s CSP header gains `form-action 'self' https://github.com;` and its
  body has `__ONBOARDING_BASE_URL__` replaced with
  `settings.public_base_url` — Task 5's frontend work relies on both.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_router.py` (it already imports `render_client`
and `app` — add `github_client`, `settings`, and the `router` module itself
too):
```python
from onboarding import github_client, router as router_module
from onboarding.config import settings


async def test_index_serves_configured_base_url(monkeypatch):
    """The real onboarding/static/index.html doesn't gain the
    __ONBOARDING_BASE_URL__ token until Task 5 — this task only implements
    the substitution mechanism, so it verifies that mechanism directly by
    patching the module-level _INDEX_HTML constant, rather than depending on
    Task 5's page content already existing."""
    monkeypatch.setattr(settings, "public_base_url", "https://onboarding.example.com")
    monkeypatch.setattr(router_module, "_INDEX_HTML", "<html>__ONBOARDING_BASE_URL__</html>")
    client = await _client()
    resp = await client.get("/")
    assert "https://onboarding.example.com" in resp.text
    assert "__ONBOARDING_BASE_URL__" not in resp.text


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: FAIL — `AttributeError`/404s for the routes that don't exist yet,
and the CSP/base-URL tests fail on the unmodified header/body.

- [ ] **Step 3: Modify `onboarding/router.py`**

Replace the file's contents with:
```python
"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given — except
POST /api/github/exchange-manifest-code, a documented exception (see
docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
section 4: it mints and returns credentials belonging to the caller who
just created them).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from onboarding import github_client, render_client
from onboarding.config import settings

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


class RenderKeyRequest(BaseModel):
    api_key: str = Field(max_length=512)


class GithubManifestCodeRequest(BaseModel):
    code: str = Field(max_length=128)


class GithubInstallVerifyRequest(BaseModel):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    installation_id: int = Field(gt=0)


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = _INDEX_HTML.replace("__ONBOARDING_BASE_URL__", settings.public_base_url)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; "
            "form-action 'self' https://github.com; frame-ancestors 'none'"
        ),
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


@router.post("/api/render/validate-key")
async def validate_render_key(payload: RenderKeyRequest) -> dict:
    result = await render_client.validate_key(payload.api_key)
    if isinstance(result, render_client.RenderKeyValid):
        return {"valid": True, "owner_name": result.owner_name}
    return {"valid": False, "reason": result.reason}


@router.post("/api/github/exchange-manifest-code")
async def exchange_github_manifest_code(payload: GithubManifestCodeRequest) -> dict:
    result = await github_client.exchange_manifest_code(payload.code)
    if isinstance(result, github_client.GithubAppCreated):
        return {
            "valid": True,
            "app_id": result.app_id,
            "slug": result.slug,
            "private_key_b64": result.private_key_b64,
            "webhook_secret": result.webhook_secret,
        }
    return {"valid": False, "reason": result.reason}


@router.post("/api/github/verify-installation")
async def verify_github_installation(payload: GithubInstallVerifyRequest) -> dict:
    result = await github_client.verify_installation(
        payload.app_id, payload.private_key_b64, payload.installation_id
    )
    if isinstance(result, github_client.InstallationVerified):
        return {
            "valid": True,
            "account_login": result.account_login,
            "repo_scope": result.repo_scope,
        }
    return {"valid": False, "reason": result.reason}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS (all tests, including the pre-existing Render-key ones)

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py tests/test_onboarding_router.py
git commit -m "feat: add GitHub App relay endpoints, CSP form-action, base-URL templating"
```

---

### Task 5: Frame 2 frontend — phase 1 (create the App)

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/github/exchange-manifest-code` (Task 4); the
  `__ONBOARDING_BASE_URL__` template token `router.py`'s `index()` now
  substitutes (Task 4).
- Produces: `sessionStorage["onboarding.githubApp"]` holding
  `{app_id, slug, private_key_b64, webhook_secret}` after a successful
  phase 1 — Task 6 reads this key and merges `installation_id`/
  `account_login` into it. `showGithubAppReadyToInstall()` (toggles the
  create/install sub-sections) — Task 6's install handler and
  `restoreFromSession()` extension both call it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py` (its existing
`test_render_key_leaves_the_page_exactly_once` is narrowed here — the
docstring already explains why, matching `onboarding/CLAUDE.md`'s
anticipated evolution):

Replace:
```python
async def test_render_key_leaves_the_page_exactly_once():
    """The key must only ever transit the one relay call — anything else
    would be a second, unaudited exit path for a visitor's credential."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'fetch("/api/render/validate-key"' in body
    assert body.count("fetch(") == 1
```

with:
```python
async def test_render_key_leaves_the_page_exactly_once():
    """The key must only ever transit the one relay call — anything else
    would be a second, unaudited exit path for a visitor's credential."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/render/validate-key"') == 1


async def test_manifest_code_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/github/exchange-manifest-code"') == 1
```

Then add:
```python
async def test_frame2_has_a_name_input_and_create_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="github-app-name-input"' in body
    assert 'id="github-app-create-submit"' in body


async def test_frame2_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame2_instructions", "frame2_name_placeholder", "create_app_button",
        "err_github_name_empty", "err_github_callback_invalid",
        "err_github_exchange_failed",
    ):
        assert f'{key}:' in body
    assert body.count("create_app_button:") == 2  # STRINGS.en + STRINGS.he


async def test_manifest_callback_handler_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function handleGithubManifestCallback" in body
    assert "gh_step" in body


async def test_manifest_permissions_match_the_cli_script():
    """Mirrors scripts/create_github_app.py's MANIFEST_PERMISSIONS/
    MANIFEST_EVENTS — kept in sync by this test, not a shared module (there
    is no shared JS/Python boundary to put one in)."""
    client = await _client()
    body = (await client.get("/")).text
    assert '"pull_requests": "write"' in body or "pull_requests: \"write\"" in body
    assert '"contents": "read"' in body or "contents: \"read\"" in body
    assert '"issues": "write"' in body or "issues: \"write\"" in body
    assert '"metadata": "read"' in body or "metadata: \"read\"" in body
    assert "public: false" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — the new markup/strings/functions don't exist yet.

- [ ] **Step 3: Modify `onboarding/static/index.html`**

Replace the frame 2 markup block:
```html
    <details id="frame-github-app" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame2_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>
```
with:
```html
    <details id="frame-github-app" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame2_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="github-app" data-i18n="change_button"></button>
      </summary>
      <div class="frame-body">
        <div id="github-app-create-section">
          <p data-i18n="frame2_instructions"></p>
          <input id="github-app-name-input" type="text">
          <button id="github-app-create-submit" type="button" data-i18n="create_app_button"></button>
        </div>
        <div id="github-app-install-section" style="display: none;">
          <p data-i18n="frame2_install_instructions"></p>
          <button id="github-app-install-submit" type="button" data-i18n="install_button"></button>
        </div>
        <p id="github-app-error" class="frame-error"></p>
      </div>
    </details>
```

In `STRINGS.en`, add (after `owner_prefix: "owner: ",`):
```js
      frame2_instructions: "Name your GitHub App (must be unique on GitHub), then click Create App. This will be created under whichever GitHub account you're currently logged into in this browser — check that's the right one before continuing.",
      frame2_name_placeholder: "my-pr-review-bot",
      create_app_button: "Create App",
      frame2_install_instructions: "Your App was created. Now install it on the account/repos you want it to review.",
      install_button: "Install App",
      err_github_name_empty: "Name your App first.",
      err_github_callback_invalid: "That didn't come back from GitHub as expected. Please try again.",
      err_github_exchange_failed: "GitHub rejected that — the code may have expired. Try creating the App again.",
      err_github_unreachable: "GitHub is unreachable right now. Try again in a moment.",
      err_github_installation_not_found: "GitHub couldn't find that installation. Try installing again.",
      err_github_invalid_credentials: "GitHub rejected the App's credentials. Try creating the App again.",
      account_prefix: "account: ",
```

In `STRINGS.he`, add (after `owner_prefix: "בעלים: ",`):
```js
      frame2_instructions: "תנו שם לאפליקציית ה-GitHub שלכם (חייב להיות ייחודי ב-GitHub), ואז לחצו על יצירת אפליקציה. האפליקציה תיווצר תחת חשבון ה-GitHub שאליו אתם מחוברים כרגע בדפדפן זה — ודאו שזה החשבון הנכון לפני שתמשיכו.",
      frame2_name_placeholder: "my-pr-review-bot",
      create_app_button: "יצירת אפליקציה",
      frame2_install_instructions: "האפליקציה נוצרה. כעת התקינו אותה על החשבון/המאגרים שברצונכם שתבדוק.",
      install_button: "התקנת אפליקציה",
      err_github_name_empty: "תנו שם לאפליקציה קודם.",
      err_github_callback_invalid: "התגובה מ-GitHub לא הייתה כצפוי. נסו שוב.",
      err_github_exchange_failed: "GitHub דחה את הבקשה — ייתכן שהקוד פג תוקף. נסו ליצור את האפליקציה מחדש.",
      err_github_unreachable: "GitHub אינו זמין כרגע. נסו שוב בעוד רגע.",
      err_github_installation_not_found: "GitHub לא מצא את ההתקנה. נסו להתקין שוב.",
      err_github_invalid_credentials: "GitHub דחה את פרטי האפליקציה. נסו ליצור את האפליקציה מחדש.",
      account_prefix: "חשבון: ",
```

Replace:
```js
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
  };
```
with:
```js
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "github-app": "onboarding.githubApp",
  };

  const MANIFEST_PERMISSIONS = {
    pull_requests: "write", contents: "read", issues: "write", metadata: "read",
  };
  const MANIFEST_EVENTS = ["pull_request"];
  const GITHUB_MANIFEST_STATE_KEY = "onboarding.githubManifestState";
```

Replace:
```js
  let currentRenderKeyErrorKey = null;
```
with:
```js
  let currentRenderKeyErrorKey = null;
  let currentGithubAppErrorKey = null;
```

After the existing `validateRenderKey` function, add:
```js
  function buildManifest(appName) {
    return {
      name: appName,
      url: window.ONBOARDING_BASE_URL,
      public: false,
      hook_attributes: {url: "https://example.invalid/webhook", active: true},
      redirect_url: `${window.ONBOARDING_BASE_URL}/?gh_step=manifest`,
      setup_url: `${window.ONBOARDING_BASE_URL}/?gh_step=install`,
      default_events: MANIFEST_EVENTS,
      default_permissions: MANIFEST_PERMISSIONS,
    };
  }

  function submitManifestForm(manifest, state) {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = `https://github.com/settings/apps/new?state=${encodeURIComponent(state)}`;
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "manifest";
    input.value = JSON.stringify(manifest);
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
  }

  function showGithubAppReadyToInstall() {
    document.getElementById("github-app-create-section").style.display = "none";
    document.getElementById("github-app-install-section").style.display = "block";
  }

  function githubAppError(key) {
    setFrameStatus("github-app", "error");
    currentGithubAppErrorKey = key;
    document.getElementById("github-app-error").textContent = t(key);
  }

  function createGithubApp() {
    const input = document.getElementById("github-app-name-input");
    const name = input.value.trim();
    document.getElementById("github-app-error").textContent = "";
    currentGithubAppErrorKey = null;
    if (!name) {
      githubAppError("err_github_name_empty");
      return;
    }
    const state = crypto.randomUUID();
    sessionStorage.setItem(GITHUB_MANIFEST_STATE_KEY, state);
    submitManifestForm(buildManifest(name), state);
  }

  async function handleGithubManifestCallback() {
    const params = new URLSearchParams(location.search);
    if (params.get("gh_step") !== "manifest") return;
    history.replaceState(null, "", location.pathname);
    const expectedState = sessionStorage.getItem(GITHUB_MANIFEST_STATE_KEY);
    sessionStorage.removeItem(GITHUB_MANIFEST_STATE_KEY);
    const code = params.get("code");
    if (!expectedState || params.get("state") !== expectedState || !code) {
      githubAppError("err_github_callback_invalid");
      return;
    }
    let resp;
    try {
      resp = await fetch("/api/github/exchange-manifest-code", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code: code}),
      });
    } catch (err) {
      githubAppError("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      githubAppError("err_github_unreachable");
      return;
    }
    if (body.valid) {
      sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify({
        app_id: body.app_id, slug: body.slug,
        private_key_b64: body.private_key_b64, webhook_secret: body.webhook_secret,
      }));
      showGithubAppReadyToInstall();
    } else if (body.reason === "exchange_failed") {
      githubAppError("err_github_exchange_failed");
    } else {
      githubAppError("err_github_unreachable");
    }
  }
```

In `applyLanguage`, replace:
```js
    document.getElementById("render-key-input").placeholder = t("frame1_placeholder");
```
with:
```js
    document.getElementById("render-key-input").placeholder = t("frame1_placeholder");
    document.getElementById("github-app-name-input").placeholder = t("frame2_name_placeholder");
```

and, replace:
```js
    if (currentRenderKeyErrorKey) {
      document.getElementById("render-key-error").textContent = t(currentRenderKeyErrorKey);
    }
```
with:
```js
    if (currentRenderKeyErrorKey) {
      document.getElementById("render-key-error").textContent = t(currentRenderKeyErrorKey);
    }
    if (currentGithubAppErrorKey) {
      document.getElementById("github-app-error").textContent = t(currentGithubAppErrorKey);
    }
```

Near the top of the `<script>` block (right after the opening `<script>`
tag, before `const STRINGS = {`), add the base-URL bridge Task 4's
`router.py` fills in at request time:
```js
  window.ONBOARDING_BASE_URL = "__ONBOARDING_BASE_URL__";
```

In `document.addEventListener("DOMContentLoaded", ...)`, replace:
```js
    document.getElementById("render-key-submit").addEventListener("click", validateRenderKey);
```
with:
```js
    document.getElementById("render-key-submit").addEventListener("click", validateRenderKey);
    document.getElementById("github-app-create-submit").addEventListener("click", createGithubApp);
```

and replace:
```js
    applyLanguage(currentLang);
    restoreFromSession();
```
with:
```js
    applyLanguage(currentLang);
    restoreFromSession();
    handleGithubManifestCallback();
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py tests/test_onboarding_router.py tests/test_onboarding_i18n.py -v`
Expected: PASS. (`test_onboarding_i18n.py` is included here since it
content-scans `STRINGS.en`/`STRINGS.he` for key parity — the new frame 2
keys must satisfy whatever check it already does; if it fails, add the
same keys to whichever side is missing them until it passes.)

- [ ] **Step 5: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: frame 2 phase 1 — create the GitHub App via the manifest flow"
```

---

### Task 6: Frame 2 frontend — phase 2 (install) + `onboarding/CLAUDE.md`

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `tests/test_onboarding_page.py`
- Modify: `onboarding/CLAUDE.md`

**Interfaces:**
- Consumes: `sessionStorage["onboarding.githubApp"]` (Task 5),
  `POST /api/github/verify-installation` (Task 4), `showGithubAppReadyToInstall()`
  (Task 5).
- Produces: frame 2 reaches `done` status via the existing generic
  `completeFrame("github-app", "account_prefix", account_login)` — nothing
  new for later frames to consume (frame 3 is still a locked placeholder).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py`:
```python
async def test_installation_verify_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/github/verify-installation"') == 1


async def test_frame2_has_an_install_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="github-app-install-submit"' in body


async def test_install_callback_handler_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function handleGithubInstallCallback" in body
    assert '"install"' in body or "'install'" in body


async def test_github_app_credential_never_persists_to_local_storage():
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.setItem(STORAGE_KEYS["github-app"]' not in body
    assert 'localStorage.getItem(STORAGE_KEYS["github-app"]' not in body


async def test_restore_from_session_handles_partial_github_app_state():
    client = await _client()
    body = (await client.get("/")).text
    assert "showGithubAppReadyToInstall()" in body
    assert "function restoreFromSession" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — install button/handler don't exist yet, `restoreFromSession`
doesn't reference frame 2 yet.

- [ ] **Step 3: Modify `onboarding/static/index.html`**

After `handleGithubManifestCallback`, add:
```js
  function installGithubApp() {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["github-app"]) || "null");
    if (!stored || !stored.slug) {
      githubAppError("err_github_callback_invalid");
      return;
    }
    const state = crypto.randomUUID();
    sessionStorage.setItem(GITHUB_MANIFEST_STATE_KEY, state);
    location.href = `https://github.com/apps/${encodeURIComponent(stored.slug)}/installations/new?state=${encodeURIComponent(state)}`;
  }

  async function handleGithubInstallCallback() {
    const params = new URLSearchParams(location.search);
    if (params.get("gh_step") !== "install") return;
    history.replaceState(null, "", location.pathname);
    const expectedState = sessionStorage.getItem(GITHUB_MANIFEST_STATE_KEY);
    sessionStorage.removeItem(GITHUB_MANIFEST_STATE_KEY);
    const installationId = params.get("installation_id");
    if (!expectedState || params.get("state") !== expectedState || !installationId) {
      githubAppError("err_github_callback_invalid");
      return;
    }
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["github-app"]) || "null");
    if (!stored) {
      githubAppError("err_github_callback_invalid");
      return;
    }
    let resp;
    try {
      resp = await fetch("/api/github/verify-installation", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          app_id: stored.app_id,
          private_key_b64: stored.private_key_b64,
          installation_id: parseInt(installationId, 10),
        }),
      });
    } catch (err) {
      githubAppError("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      githubAppError("err_github_unreachable");
      return;
    }
    if (body.valid) {
      stored.installation_id = parseInt(installationId, 10);
      stored.account_login = body.account_login;
      sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
      completeFrame("github-app", "account_prefix", body.account_login);
    } else if (body.reason === "installation_not_found") {
      githubAppError("err_github_installation_not_found");
    } else if (body.reason === "invalid_credentials") {
      githubAppError("err_github_invalid_credentials");
    } else {
      githubAppError("err_github_unreachable");
    }
  }
```

Replace `restoreFromSession`:
```js
  function restoreFromSession() {
    if (sessionStorage.getItem(STORAGE_KEYS["render-key"])) {
      completeFrame("render-key", "restored", null);
    }
  }
```
with:
```js
  function restoreFromSession() {
    if (sessionStorage.getItem(STORAGE_KEYS["render-key"])) {
      completeFrame("render-key", "restored", null);
    }
    const githubApp = JSON.parse(sessionStorage.getItem(STORAGE_KEYS["github-app"]) || "null");
    if (githubApp && githubApp.installation_id) {
      completeFrame("github-app", "restored", null);
    } else if (githubApp) {
      showGithubAppReadyToInstall();
    }
  }
```

In `document.addEventListener("DOMContentLoaded", ...)`, replace:
```js
    document.getElementById("github-app-create-submit").addEventListener("click", createGithubApp);
```
with:
```js
    document.getElementById("github-app-create-submit").addEventListener("click", createGithubApp);
    document.getElementById("github-app-install-submit").addEventListener("click", installGithubApp);
```

and replace:
```js
    applyLanguage(currentLang);
    restoreFromSession();
    handleGithubManifestCallback();
```
with:
```js
    applyLanguage(currentLang);
    restoreFromSession();
    handleGithubManifestCallback();
    handleGithubInstallCallback();
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py tests/test_onboarding_router.py tests/test_onboarding_i18n.py -v`
Expected: PASS.

- [ ] **Step 5: Update `onboarding/CLAUDE.md`**

Append (after the existing "What the implementation adds to these rules"
section — do not remove or edit any existing content):
```markdown

## What sub-project 2 (GitHub App automation) adds to these rules

- **`exchange-manifest-code` is a deliberate exception to "never echo a
  credential."** It mints and returns App credentials that belong to the
  visitor who just created them — this is different from every other
  endpoint in this service, which only ever verifies a credential already
  submitted to it. A new endpoint that returns secret material needs the
  same justification this one has (the value is freshly created for, and
  belongs to, the caller) before following this exception rather than the
  default rule.
- **`verify-installation`'s request body carries a GitHub App's full
  private key** — the same sensitivity tier as this project's own
  `GITHUB_APP_PRIVATE_KEY`. Treat it accordingly: never logged, never in an
  unhandled exception's message, narrow `except` clauses only.
- **Multi-navigation flows use a `sessionStorage`-held random `state`,
  generated client-side before each redirect and checked on return before
  any exchange happens.** This is the CSRF pattern for any frame that
  involves a full-page round trip to an external site (this one, and
  Supabase's OAuth in sub-project 3) — reuse it rather than inventing a
  per-frame variant.
- **`GET /`'s CSP carries `form-action 'self' https://github.com;`**
  specifically for this frame's manifest-creation form POST. A future frame
  that needs to form-POST to a *different* external origin adds that origin
  to this same directive rather than loosening `default-src`.
- **`onboarding/config.py`'s `public_base_url` is validated in
  `onboarding/main.py`'s `lifespan`, not as a pydantic-required field** —
  same reasoning as `app/config.py`/`app/main.py`'s own pattern: a
  required field would raise at import time, breaking pytest collection
  before a clear error could ever be reported. Frame 2 cannot construct a
  working manifest without it, so the service still refuses to boot without
  it — just via an explicit check, not a schema constraint.
```

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS, no failures, no regressions in tests outside `onboarding/`'s
scope.

- [ ] **Step 7: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py onboarding/CLAUDE.md
git commit -m "feat: frame 2 phase 2 — install the GitHub App and verify it"
```
