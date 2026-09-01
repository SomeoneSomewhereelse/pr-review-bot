# Onboarding GitHub App: fully manual creation + validation checklist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace frame 4's automated GitHub App-manifest creation flow with
a fully manual process (visitor creates the App by hand in GitHub's own UI,
following on-page instructions with zero GitHub URLs anywhere on the page)
plus a doctor.py-style validation checklist that reads the App's actual
configuration back from GitHub and gates the frame's unlock on every
requirement passing.

**Architecture:** `onboarding/github_client.py` gains a `validate_app()`
orchestrator (App-JWT reads of `/app`, `/app/installations`,
`/app/hook/config` via PyGithub, mirroring `bot/github_app.py`'s doctor
checks but built independently) replacing the old manifest-exchange and
single-installation-verify functions. `onboarding/router.py` swaps two old
endpoints for one new `POST /api/github/validate-app`. `onboarding/static/index.html`
replaces frame 4's two-section create/install UI with one instructions +
credential-entry + per-item checklist section.

**Tech Stack:** FastAPI + Pydantic (backend), PyGithub (GitHub API client),
vanilla JS (frontend), pytest + respx + a `requests`-transport fake for
PyGithub-backed tests (existing pattern in `test_onboarding_github_client.py`).

**Spec:** `docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md`

## Global Constraints

- No URL of any kind referencing `github.com` may appear anywhere in
  `onboarding/static/index.html` — enforced by
  `test_page_offers_no_route_to_the_install_page_at_all` (existing) plus a
  new sibling test in Task 3.
- `REQUIRED_PERMISSIONS`/`REQUIRED_EVENTS` values must exactly equal
  `bot/scripts/create_github_app.py`'s `MANIFEST_PERMISSIONS`/`MANIFEST_EVENTS`
  (`{"pull_requests": "write", "contents": "read", "issues": "write", "metadata": "read"}`,
  `("pull_request",)`) — this project's one source of truth for what the App
  needs, unchanged by this plan.
- `onboarding/` never imports from `bot/`'s credential-handling code
  (`onboarding/CLAUDE.md`'s no-shared-credential-path rule) — every new
  GitHub-API-reading function in `onboarding/github_client.py` is a fresh
  implementation, not an import of `bot/github_app.py`'s equivalents.
- Never log, echo, or leave in an exception message the App's private key,
  in full or truncated — same sensitivity tier as this project's own
  `GITHUB_APP_PRIVATE_KEY`.
- `bot/scripts/create_github_app.py`, its CLI flag, and
  `guide/setup/02-github-app.md`/`03-install-app.md` are out of scope —
  untouched by every task in this plan.
- Before pushing (per root `CLAUDE.md`): run the full suite
  (`uv run pytest -v`) and `uv run ruff check .`, fix anything either finds.

---

### Task 1: `onboarding/github_client.py` — validation functions

**Files:**
- Modify: `onboarding/github_client.py` (full rewrite of its body; module
  docstring, imports, and everything below them)
- Test: `onboarding/tests/test_onboarding_github_client.py` (full rewrite)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (used by Task 2):
  - `github_client.REQUIRED_PERMISSIONS: dict[str, str]`
  - `github_client.REQUIRED_EVENTS: tuple[str, ...]`
  - `github_client.AppCredentialsInvalid(reason: str)` — `reason` ∈
    `{"invalid_key", "unauthorized", "github_unreachable"}`
  - `github_client.PermissionCheck(name: str, wanted: str, actual: str | None, ok: bool)`
  - `github_client.EventCheck(name: str, ok: bool)`
  - `github_client.InstallationFound(installation_id: int, account_login: str, repo_scope: str)`
  - `github_client.InstallationNotFound()`
  - `github_client.MultipleInstallationsFound(account_logins: list[str])`
  - `github_client.WebhookCheck(ok: bool, actual_url: str)`
  - `github_client.AppValidated(permissions: list[PermissionCheck], events: list[EventCheck], installation: InstallationFound | InstallationNotFound | MultipleInstallationsFound, webhook: WebhookCheck)`
  - `async def github_client.validate_app(app_id: int, private_key_b64: str, expected_webhook_url: str) -> AppCredentialsInvalid | AppValidated`

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `onboarding/tests/test_onboarding_github_client.py`
with:

```python
"""Tests for onboarding/github_client.py — validate_app() reads a visitor's
hand-created GitHub App's actual configuration back from GitHub and reports
per-item pass/fail against REQUIRED_PERMISSIONS/REQUIRED_EVENTS, installation,
and webhook URL. Never logs or echoes the private key. See
docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md."""

from __future__ import annotations

import base64
import json
import time

import pytest
import requests as requests_lib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onboarding import github_client

WEBHOOK_URL = "https://my-service.onrender.com/webhook"


@pytest.fixture(scope="module")
def _throwaway_key_material() -> str:
    """A throwaway RSA key, base64-encoded like a real downloaded .pem's
    base64-encoded bytes would be. Only used for local JWT signing in these
    tests — every HTTP call is mocked below, so nothing is ever sent
    anywhere with it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


@pytest.fixture(autouse=True)
def _no_pygithub_rate_limit_sleep(monkeypatch):
    """PyGithub's Requester paces real requests with time.sleep(); every
    call here goes through fake_transport below, so the throttle protects
    nothing and only wastes wall-clock."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class _FakeGithubTransport:
    """Routes requests by (method, url-substring) to canned JSON responses.
    PyGithub calls through `requests`, not `httpx` — this patches the
    requests.adapters.HTTPAdapter.send transport boundary, same pattern as
    tests/test_github_app.py's own FakeGithubTransport."""

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


REQUIRED_PERMISSIONS_RESPONSE = {
    "permissions": {
        "pull_requests": "write",
        "contents": "read",
        "issues": "write",
        "metadata": "read",
    },
    "events": ["pull_request"],
}


def _route_all_passing(fake_transport):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})


async def test_all_checks_pass_when_everything_matches(fake_transport, _throwaway_key_material):
    _route_all_passing(fake_transport)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert isinstance(result, github_client.AppValidated)
    assert all(p.ok for p in result.permissions)
    assert all(e.ok for e in result.events)
    assert result.installation == github_client.InstallationFound(
        installation_id=456, account_login="octocat", repo_scope="all"
    )
    assert result.webhook == github_client.WebhookCheck(ok=True, actual_url=WEBHOOK_URL)


async def test_missing_permission_is_reported_not_ok(fake_transport, _throwaway_key_material):
    fake_transport.route(
        "GET",
        "/app",
        {
            "permissions": {"pull_requests": "write", "contents": "read", "metadata": "read"},
            "events": ["pull_request"],
        },
    )
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert isinstance(result, github_client.AppValidated)
    issues = {p.name: p for p in result.permissions}
    assert issues["issues"] == github_client.PermissionCheck(
        name="issues", wanted="write", actual=None, ok=False
    )
    assert issues["pull_requests"].ok


async def test_broader_than_needed_permission_still_passes(fake_transport, _throwaway_key_material):
    """Rank-based: admin satisfies a write requirement -- least-privilege is
    an operator-side doctor.py WARN concern, not a blocker here."""
    response = dict(REQUIRED_PERMISSIONS_RESPONSE)
    response["permissions"] = dict(response["permissions"], pull_requests="admin")
    fake_transport.route("GET", "/app", response)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert next(p for p in result.permissions if p.name == "pull_requests").ok


async def test_missing_event_is_reported_not_ok(fake_transport, _throwaway_key_material):
    response = dict(REQUIRED_PERMISSIONS_RESPONSE)
    response["events"] = []
    fake_transport.route("GET", "/app", response)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.events == [github_client.EventCheck(name="pull_request", ok=False)]


async def test_no_installation_is_reported(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route("GET", "/app/installations", [])
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.installation == github_client.InstallationNotFound()


async def test_multiple_installations_is_reported_distinctly(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [
            {"id": 1, "account": {"login": "octocat"}, "repository_selection": "all"},
            {"id": 2, "account": {"login": "monalisa"}, "repository_selection": "all"},
        ],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.installation == github_client.MultipleInstallationsFound(
        account_logins=["octocat", "monalisa"]
    )


async def test_mismatched_webhook_url_is_not_ok(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": "https://wrong.example/webhook"})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.webhook == github_client.WebhookCheck(
        ok=False, actual_url="https://wrong.example/webhook"
    )


async def test_unset_webhook_is_empty_string_not_the_synthetic_path(
    fake_transport, _throwaway_key_material
):
    """PyGithub's Requester injects a synthetic `url` key (the literal
    request path) into a GET response dict that lacks one -- an
    unconfigured webhook must not be mistaken for one pointed at that path."""
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.webhook == github_client.WebhookCheck(ok=False, actual_url="")


async def test_bad_credentials_short_circuits_with_no_checklist_data(
    fake_transport, _throwaway_key_material
):
    fake_transport.route("GET", "/app", {"message": "Bad credentials"}, 401)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="unauthorized")


async def test_forbidden_is_unauthorized(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", {"message": "Forbidden"}, 403)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="unauthorized")


async def test_server_error_is_unreachable(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", {}, 500)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="github_unreachable")


async def test_malformed_base64_private_key_is_invalid_key():
    result = await github_client.validate_app(999, "not-valid-base64!!", WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="invalid_key")


async def test_valid_base64_but_not_a_real_pem_is_invalid_key():
    garbage_pem_b64 = base64.b64encode(b"not a real PEM").decode()
    result = await github_client.validate_app(999, garbage_pem_b64, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="invalid_key")


async def test_installation_lookup_failure_does_not_hide_permission_results(
    fake_transport, _throwaway_key_material
):
    """No check may abort the run: a transient failure fetching installations
    must not suppress the permissions/events results already fetched from
    GET /app."""
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route("GET", "/app/installations", {}, 500)
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert isinstance(result, github_client.AppValidated)
    assert all(p.ok for p in result.permissions)
    assert result.installation == github_client.InstallationNotFound()
    assert result.webhook.ok


def test_required_permissions_and_events_match_the_cli_script():
    from bot.scripts.create_github_app import MANIFEST_EVENTS, MANIFEST_PERMISSIONS

    assert github_client.REQUIRED_PERMISSIONS == MANIFEST_PERMISSIONS
    assert github_client.REQUIRED_EVENTS == MANIFEST_EVENTS


def test_no_manifest_exchange_or_single_installation_verify_leftover():
    """These functions belonged to the removed manifest-flow/typed-
    installation-id design -- their reappearance means half of that flow
    came back without the other half."""
    assert not hasattr(github_client, "exchange_manifest_code")
    assert not hasattr(github_client, "verify_installation")
    assert not hasattr(github_client, "GithubAppCreated")
    assert not hasattr(github_client, "InstallationVerified")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest onboarding/tests/test_onboarding_github_client.py -v`
Expected: collection error or failures — `github_client.validate_app` etc.
don't exist yet.

- [ ] **Step 3: Replace `onboarding/github_client.py`**

```python
"""Thin async wrapper around GitHub's App-configuration APIs — validates a
visitor's own, hand-created GitHub App against this project's requirements
without persisting anything server-side. App creation and installation are
both fully manual (guide/setup/02-github-app.md's process, followed inside
GitHub's own UI); this module's job is reading the App's actual state back
and reporting it, the doctor.py-style compensation for losing the old
manifest flow's built-in correctness. See
docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md."""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses

import jwt
import requests
from github import Auth, Github, GithubException

# GitHub's API guidelines require every request to carry a descriptive
# User-Agent naming the application -- PyGithub's default is a bare
# "PyGithub/Python", exactly the generic-library shape those guidelines
# single out.
USER_AGENT = "pr-review-bot-onboarding"

# SOURCE OF TRUTH: bot/scripts/create_github_app.py's MANIFEST_PERMISSIONS /
# MANIFEST_EVENTS. Kept in sync by hand -- onboarding/ never imports from
# bot/ (onboarding/CLAUDE.md's no-shared-credential-path rule) --
# tests/test_onboarding_github_client.py::test_required_permissions_and_events_match_the_cli_script
# and tests/test_onboarding_page.py::test_required_permissions_match_the_cli_script
# both fail if this copy or the JS copy in static/index.html drifts.
REQUIRED_PERMISSIONS = {
    "pull_requests": "write",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
}
REQUIRED_EVENTS = ("pull_request",)

_PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}


@dataclasses.dataclass(frozen=True)
class AppCredentialsInvalid:
    reason: str  # "invalid_key" | "unauthorized" | "github_unreachable"


@dataclasses.dataclass(frozen=True)
class PermissionCheck:
    name: str
    wanted: str
    actual: str | None
    ok: bool


@dataclasses.dataclass(frozen=True)
class EventCheck:
    name: str
    ok: bool


@dataclasses.dataclass(frozen=True)
class InstallationFound:
    installation_id: int
    account_login: str
    repo_scope: str  # "all" | "selected"


@dataclasses.dataclass(frozen=True)
class InstallationNotFound:
    pass


@dataclasses.dataclass(frozen=True)
class MultipleInstallationsFound:
    account_logins: list[str]


InstallationCheck = InstallationFound | InstallationNotFound | MultipleInstallationsFound


@dataclasses.dataclass(frozen=True)
class WebhookCheck:
    ok: bool
    actual_url: str


@dataclasses.dataclass(frozen=True)
class AppValidated:
    permissions: list[PermissionCheck]
    events: list[EventCheck]
    installation: InstallationCheck
    webhook: WebhookCheck


AppValidationResult = AppCredentialsInvalid | AppValidated


def _app_jwt_client(app_id: int, private_key_pem: str) -> Github:
    """Client authenticated as the visitor's own App (JWT) -- built fresh
    from their just-submitted credentials, never bot/github_app.py's
    operator-tied helpers (onboarding/CLAUDE.md's no-shared-credential-path
    rule)."""
    return Github(auth=Auth.AppAuth(app_id, private_key_pem), user_agent=USER_AGENT)


def _fetch_app(app_id: int, private_key_pem: str) -> tuple[dict[str, str], list[str]]:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    GET /app under the App JWT: (permissions, events) the App ACTUALLY has
    recorded on GitHub right now, mirroring bot/github_app.py::get_app_permissions."""
    gh = _app_jwt_client(app_id, private_key_pem)
    _, data = gh.requester.requestJsonAndCheck("GET", "/app")
    return data.get("permissions", {}), data.get("events", [])


def _fetch_installations(app_id: int, private_key_pem: str) -> list[dict]:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    GET /app/installations under the App JWT."""
    gh = _app_jwt_client(app_id, private_key_pem)
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/installations")
    return data


def _fetch_webhook_config(app_id: int, private_key_pem: str) -> str:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    GET /app/hook/config under the App JWT. Returns "" when unset — mirrors
    bot/github_app.py::get_webhook_url's handling of PyGithub's synthetic
    `url` key on an unconfigured webhook (only an absolute http(s) URL
    counts as configured)."""
    gh = _app_jwt_client(app_id, private_key_pem)
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/hook/config")
    url = (data or {}).get("url") or ""
    return url if url.startswith(("http://", "https://")) else ""


def diff_required_permissions(
    actual_permissions: dict[str, str], actual_events: list[str]
) -> tuple[list[PermissionCheck], list[EventCheck]]:
    """One PermissionCheck per REQUIRED_PERMISSIONS entry and one EventCheck
    per REQUIRED_EVENTS entry — structured, not the human-readable
    under/over string lists bot/github_app.py::diff_app_permissions returns,
    since the UI renders one colored line per item. Rank-based comparison
    (read < write < admin), so a broader-than-needed permission still
    passes — least-privilege is a doctor.py WARN concern for an operator,
    not a blocker for a visitor whose App already works."""
    permissions = []
    for name, wanted in REQUIRED_PERMISSIONS.items():
        actual = actual_permissions.get(name)
        actual_rank = _PERMISSION_LEVELS.get(actual or "", 0)
        wanted_rank = _PERMISSION_LEVELS[wanted]
        permissions.append(
            PermissionCheck(name=name, wanted=wanted, actual=actual, ok=actual_rank >= wanted_rank)
        )
    events = [EventCheck(name=name, ok=name in actual_events) for name in REQUIRED_EVENTS]
    return permissions, events


async def _discover_installation(app_id: int, private_key_pem: str) -> InstallationCheck:
    """Isolated from the other checks: a failure here reports
    InstallationNotFound rather than raising, so it can never suppress the
    permissions/events/webhook results validate_app already has in hand
    (doctor.py's _safe principle — "no check may abort the run")."""
    try:
        data = await asyncio.to_thread(_fetch_installations, app_id, private_key_pem)
    except (GithubException, requests.exceptions.RequestException):
        return InstallationNotFound()
    if not data:
        return InstallationNotFound()
    if len(data) > 1:
        try:
            return MultipleInstallationsFound(
                account_logins=[str(i["account"]["login"]) for i in data]
            )
        except (KeyError, TypeError):
            return InstallationNotFound()
    try:
        return InstallationFound(
            installation_id=int(data[0]["id"]),
            account_login=str(data[0]["account"]["login"]),
            repo_scope=str(data[0]["repository_selection"]),
        )
    except (KeyError, TypeError, ValueError):
        return InstallationNotFound()


async def _check_webhook(app_id: int, private_key_pem: str, expected_url: str) -> WebhookCheck:
    """Isolated the same way _discover_installation is -- a failed webhook
    read reports a not-ok, empty-URL check rather than raising."""
    try:
        actual_url = await asyncio.to_thread(_fetch_webhook_config, app_id, private_key_pem)
    except (GithubException, requests.exceptions.RequestException):
        actual_url = ""
    return WebhookCheck(ok=actual_url == expected_url, actual_url=actual_url)


async def validate_app(
    app_id: int, private_key_b64: str, expected_webhook_url: str
) -> AppValidationResult:
    """Reads the visitor's hand-created App's actual configuration back from
    GitHub and checks it against REQUIRED_PERMISSIONS/REQUIRED_EVENTS plus
    installation and webhook state.

    A credentials-level failure (bad base64, not a real PEM, or GET /app
    itself rejected) returns AppCredentialsInvalid immediately — there is
    nothing to diff without it. Otherwise every other check
    (permissions/events, installation, webhook) runs regardless of the
    others' outcomes — a visitor fixing multiple things in GitHub's UI
    should see the full picture in one Validate click, not one failure at a
    time.

    Never logs the private key, in full or truncated — same sensitivity
    tier as this project's own GITHUB_APP_PRIVATE_KEY."""
    try:
        private_key_pem = base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError):
        return AppCredentialsInvalid(reason="invalid_key")

    try:
        actual_permissions, actual_events = await asyncio.to_thread(
            _fetch_app, app_id, private_key_pem
        )
    except GithubException as exc:
        if exc.status in (401, 403):
            return AppCredentialsInvalid(reason="unauthorized")
        return AppCredentialsInvalid(reason="github_unreachable")
    except (ValueError, jwt.exceptions.InvalidKeyError):
        # A base64-valid but non-PEM string: cryptography's own PEM parser
        # raises ValueError, but PyGithub signs the App JWT via PyJWT, which
        # catches that ValueError internally and re-raises it as
        # jwt.exceptions.InvalidKeyError (confirmed against the installed
        # pygithub==2.9.1 / PyJWT stack) — neither is raised at the
        # base64-decode step above, only once PyGithub tries to parse the
        # key material while signing.
        return AppCredentialsInvalid(reason="invalid_key")
    except requests.exceptions.RequestException:
        return AppCredentialsInvalid(reason="github_unreachable")

    permissions, events = diff_required_permissions(actual_permissions, actual_events)
    installation, webhook = await asyncio.gather(
        _discover_installation(app_id, private_key_pem),
        _check_webhook(app_id, private_key_pem, expected_webhook_url),
    )
    return AppValidated(permissions=permissions, events=events, installation=installation, webhook=webhook)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest onboarding/tests/test_onboarding_github_client.py -v`
Expected: all PASS.

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check onboarding/github_client.py onboarding/tests/test_onboarding_github_client.py`
Expected: no errors. Fix any and re-run.

- [ ] **Step 6: Commit**

```bash
git add onboarding/github_client.py onboarding/tests/test_onboarding_github_client.py
git commit -m "Replace GitHub manifest-exchange/install-verify with a doctor.py-style validate_app()"
```

---

### Task 2: `onboarding/router.py` — `/api/github/validate-app` endpoint

**Files:**
- Modify: `onboarding/router.py:1-8` (module docstring), `:31-43`
  (`GithubManifestCodeRequest`/`GithubInstallVerifyRequest` → replaced),
  `:196-199` (CSP header), `:225-251` (the two endpoint functions → replaced)
- Test: `onboarding/tests/test_onboarding_router.py` (remove/replace the
  GitHub-manifest/verify-installation tests; CSP test updated)

**Interfaces:**
- Consumes: `github_client.validate_app`, `github_client.AppCredentialsInvalid`,
  `github_client.AppValidated`, `github_client.InstallationFound`,
  `github_client.MultipleInstallationsFound` (Task 1).
- Produces (used by Task 3): `POST /api/github/validate-app` accepting
  `{app_id: int, private_key_b64: str, expected_webhook_url: str}`, returning
  either `{"valid": false, "reason": str}` or
  `{"valid": true, "all_ok": bool, "permissions": [{"name", "wanted", "actual", "ok"}, ...], "events": [{"name", "ok"}, ...], "installation": {"status": "found"|"none"|"multiple", "installation_id"?, "account_login"?, "repo_scope"?, "account_logins"?}, "webhook": {"ok": bool, "actual_url": str}}`.

- [ ] **Step 1: Write the failing tests**

In `onboarding/tests/test_onboarding_router.py`, delete the following (lines
122–233 in the current file): `test_index_csp_allows_form_post_to_github`,
`test_manifest_code_exchange_returns_app_credentials`,
`test_manifest_code_exchange_reports_failure_reason`,
`test_verify_installation_returns_account_details`,
`test_verify_installation_reports_failure_reason`,
`test_verify_installation_rejects_a_non_positive_installation_id`,
`test_verify_installation_validation_error_never_echoes_the_private_key`,
`test_verify_installation_response_never_echoes_the_private_key`. Keep
`test_set_webhook_url_endpoint_is_gone` (still valid, unrelated to this
change) exactly as-is. Replace the deleted block with:

```python
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
                github_client.PermissionCheck(name="contents", wanted="read", actual="read", ok=True),
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
        json={"app_id": 42, "private_key_b64": "cGVt", "expected_webhook_url": "https://x.example/webhook"},
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
        json={"app_id": 42, "private_key_b64": "cGVt", "expected_webhook_url": "https://x.example/webhook"},
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
        json={"app_id": 42, "private_key_b64": "cGVt", "expected_webhook_url": "https://x.example/webhook"},
    )
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_validate_app_rejects_a_non_positive_app_id():
    client = await _client()
    for bad in (0, -1):
        resp = await client.post(
            "/api/github/validate-app",
            json={"app_id": bad, "private_key_b64": "cGVt", "expected_webhook_url": "https://x.example/webhook"},
        )
        assert resp.status_code == 422


async def test_validate_app_rejects_a_malformed_webhook_url():
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={"app_id": 42, "private_key_b64": "cGVt", "expected_webhook_url": "not-a-url"},
    )
    assert resp.status_code == 422


async def test_validate_app_validation_error_never_echoes_the_private_key():
    """Same guard as every other endpoint carrying a private key: FastAPI's
    default 422 body echoes rejected input verbatim; only main.py's app-wide
    RequestValidationError handler stops that."""
    client = await _client()
    resp = await client.post(
        "/api/github/validate-app",
        json={"app_id": 42, "private_key": SENTINEL_PRIVATE_KEY, "expected_webhook_url": "https://x.example/webhook"},
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
        json={"app_id": 42, "private_key_b64": sentinel_key_b64, "expected_webhook_url": "https://x.example/webhook"},
    )
    assert sentinel_key_b64 not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: failures — `/api/github/validate-app` doesn't exist yet, CSP
still carries `https://github.com`.

- [ ] **Step 3: Update `onboarding/router.py`**

Replace the module docstring (lines 1–8) with:

```python
"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given.
"""
```

Replace `GithubManifestCodeRequest`/`GithubInstallVerifyRequest` (lines
31–43) with:

```python
class GithubValidateAppRequest(BaseModel):
    # App ID + private key are pasted in by the visitor after hand-creating
    # the App in GitHub's UI (see onboarding/CLAUDE.md) -- validate_app()
    # reads the App's actual live configuration back from GitHub rather
    # than trusting anything about how it was created.
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    # Computed client-side from the already-known Render service URL, sent
    # up rather than recomputed server-side -- this service holds no state
    # to recompute it from.
    expected_webhook_url: str = Field(
        min_length=1, max_length=2048, pattern=r"^https?://[^\s\"'<>\\]+$"
    )
```

Update the CSP header (lines 196–199) — remove the now-unnecessary
`https://github.com` origin, since there is no cross-origin form POST left
in this frame:

```python
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
```

Replace the two endpoint functions (lines 225–251) with:

```python
@router.post("/api/github/validate-app")
async def validate_github_app(payload: GithubValidateAppRequest) -> dict:
    result = await github_client.validate_app(
        payload.app_id, payload.private_key_b64, payload.expected_webhook_url
    )
    if isinstance(result, github_client.AppCredentialsInvalid):
        return {"valid": False, "reason": result.reason}

    if isinstance(result.installation, github_client.InstallationFound):
        installation = {
            "status": "found",
            "installation_id": result.installation.installation_id,
            "account_login": result.installation.account_login,
            "repo_scope": result.installation.repo_scope,
        }
        installation_ok = True
    elif isinstance(result.installation, github_client.MultipleInstallationsFound):
        installation = {"status": "multiple", "account_logins": result.installation.account_logins}
        installation_ok = False
    else:
        installation = {"status": "none"}
        installation_ok = False

    all_ok = (
        all(p.ok for p in result.permissions)
        and all(e.ok for e in result.events)
        and installation_ok
        and result.webhook.ok
    )
    return {
        "valid": True,
        "all_ok": all_ok,
        "permissions": [
            {"name": p.name, "wanted": p.wanted, "actual": p.actual, "ok": p.ok}
            for p in result.permissions
        ],
        "events": [{"name": e.name, "ok": e.ok} for e in result.events],
        "installation": installation,
        "webhook": {"ok": result.webhook.ok, "actual_url": result.webhook.actual_url},
    }
```

(`GithubPushRenderVarsRequest` and `push_github_render_vars` further down
the file are unchanged — still take `app_id`, `private_key_b64`,
`webhook_secret`, `installation_id` directly, unrelated to this endpoint.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: all PASS.

- [ ] **Step 5: Run ruff**

Run: `uv run ruff check onboarding/router.py onboarding/tests/test_onboarding_router.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "Add POST /api/github/validate-app, remove manifest-exchange/verify-installation endpoints"
```

---

### Task 3: `onboarding/static/index.html` — manual instructions + checklist UI

**Files:**
- Modify: `onboarding/static/index.html` (frame markup, i18n strings,
  `MANIFEST_PERMISSIONS`/`MANIFEST_EVENTS` → `REQUIRED_PERMISSIONS`/`REQUIRED_EVENTS`,
  all frame-4 JS functions)
- Test: `onboarding/tests/test_onboarding_page.py` (remove/replace frame-4
  tests)

**Interfaces:**
- Consumes: `POST /api/github/validate-app` (Task 2), `readFileAsBase64`
  (existing helper, `onboarding/static/index.html`), `base64UrlEncode`
  (existing helper), `readStoredRenderService` (existing helper).
- Produces: nothing further downstream — this is the last task touching
  frame 4's own code (Task 4 only touches docs).

- [ ] **Step 1: Write the failing tests**

In `onboarding/tests/test_onboarding_page.py`, delete these tests entirely:
`test_frame2_has_a_name_input_and_create_button`,
`test_frame2_strings_present_in_both_languages`,
`test_frame4_gates_unlock_on_a_verified_installation_id`,
`test_setup_url_return_only_prefills_the_id_it_never_bypasses_verification`,
`test_manifest_callback_handler_present`,
`test_manifest_permissions_match_the_cli_script`,
`test_frame2_has_an_install_button`,
`test_restore_from_session_handles_partial_github_app_state`,
`test_created_but_not_installed_state_is_visible_to_the_visitor`,
`test_manifest_carries_the_real_webhook_url_not_a_placeholder`,
`test_app_creation_refuses_without_a_render_service_url`.

Keep unchanged: `test_page_offers_no_route_to_the_install_page_at_all`,
`test_github_app_credential_never_persists_to_local_storage`,
`test_stored_github_app_credentials_are_parsed_defensively`,
`test_render_service_frame_precedes_the_github_app_frame`,
`test_webhook_patch_flow_is_fully_removed`,
`test_push_helpers_skip_entirely_without_a_render_service` (its count of 4
does not change — this frame's push helper is untouched).

Update `test_frame2_has_a_reset_path_wired_into_lock_and_change` (rename the
function it looks for):

```python
async def test_frame2_has_a_reset_path_wired_into_lock_and_change():
    """A locked-then-reopened or Change-clicked frame 4 must not strand a
    stale App ID/checklist on screen from a previous attempt."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function resetGithubAppSetupSection" in body

    lock_body = body[body.index("function lockFrame") : body.index("function unlockFrame")]
    assert "resetGithubAppSetupSection()" in lock_body
    assert 'id === "github-app"' in lock_body

    change_body = body[
        body.index("function beginChange") : body.index("async function validateRenderKey")
    ]
    assert "resetGithubAppSetupSection()" in change_body
    assert 'sessionStorage.removeItem(STORAGE_KEYS["github-app"])' in change_body
```

Update the import at the top of the test file (already
`from bot.scripts.create_github_app import MANIFEST_EVENTS, MANIFEST_PERMISSIONS`
at line 12 — leave that import as-is, it now feeds the new test below) and
add these new tests (near where the deleted ones were):

```python
async def test_frame2_has_app_id_and_key_file_inputs():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="github-app-id-input"' in body
    assert 'id="github-app-key-file-input"' in body
    assert 'type="file"' in body[body.index('id="github-app-key-file-input"') - 40 :]
    assert 'id="github-app-validate-submit"' in body


async def test_frame2_no_longer_has_a_name_or_install_section():
    """The old two-section (create/install) shape is fully replaced by one
    instructions+credentials+checklist section."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="github-app-name-input"' not in body
    assert 'id="github-app-create-submit"' not in body
    assert 'id="github-app-install-section"' not in body
    assert 'id="github-app-installation-id-input"' not in body
    assert 'id="github-app-install-submit"' not in body


async def test_frame2_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame2_instructions",
        "frame2_step_create",
        "frame2_step_webhook_url",
        "frame2_step_webhook_secret",
        "frame2_step_permissions",
        "frame2_step_install",
        "frame2_app_id_label",
        "frame2_private_key_label",
        "err_github_app_id_invalid",
        "err_github_no_file",
        "err_github_invalid_key_file",
    ):
        assert f"{key}:" in body
        assert body.count(f"{key}:") == 2, f"{key} missing a translation"


async def test_page_offers_no_route_to_github_app_creation_either():
    """Extends the existing install-page policy to App creation too: after
    another suspension during this frame even with the install-page fix
    already shipped (see ISSUES.md), no github.com URL of any kind is
    rendered on the page — creation is described only as breadcrumb text."""
    client = await _client()
    body = (await client.get("/")).text
    assert "github.com/settings/apps" not in body
    assert "github.com" not in body


async def test_required_permissions_match_the_cli_script():
    """The page's JS REQUIRED_PERMISSIONS/REQUIRED_EVENTS must mirror
    scripts/create_github_app.py's, which is the single source of truth (a
    paired comment in each file points at the other; there is no shared
    JS/Python boundary a real shared constant could live in).

    This reads the actual Python constants rather than re-listing the same
    literals a second time: a copy of the literals here would keep passing
    after someone edited only the CLI script, which is exactly the drift
    this test exists to catch."""
    client = await _client()
    body = (await client.get("/")).text
    for name, level in MANIFEST_PERMISSIONS.items():
        assert f'{name}: "{level}"' in body, f"page is missing permission {name}: {level}"
    for event in MANIFEST_EVENTS:
        assert f'"{event}"' in body, f"page is missing required event {event}"
    js_perms = body[body.index("const REQUIRED_PERMISSIONS = {") :]
    js_perms = js_perms[: js_perms.index("};")]
    assert js_perms.count(":") == len(MANIFEST_PERMISSIONS)
    js_events = body[body.index("const REQUIRED_EVENTS = [") :]
    js_events = js_events[: js_events.index("];")]
    assert js_events.count('"') == 2 * len(MANIFEST_EVENTS)


async def test_validate_github_app_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/github/validate-app"') == 1


async def test_validate_github_app_refuses_without_a_render_service_url():
    """The webhook URL instruction and the request both depend on the
    Render service's URL already existing -- unreachable in normal
    sequential flow (that frame completes two frames earlier), but guards
    the same corrupted/hand-edited sessionStorage case the UptimeRobot and
    Supabase frames' own equivalents guard."""
    client = await _client()
    body = (await client.get("/")).text
    validate_fn = body[
        body.index("async function validateGithubApp") : body.index("function renderGithubAppChecklist")
    ]
    assert "if (!renderService || !renderService.service_url) {" in validate_fn
    assert 'githubAppError("err_github_no_render_service");' in validate_fn


async def test_validate_github_app_runs_every_check_before_pushing():
    """all_ok gates the push-and-clear step -- a partial pass must not push
    stale/incomplete credentials to Render."""
    client = await _client()
    body = (await client.get("/")).text
    validate_fn = body[
        body.index("async function validateGithubApp") : body.index("function renderGithubAppChecklist")
    ]
    assert "if (!body.all_ok)" in validate_fn
    assert validate_fn.index("if (!body.all_ok)") < validate_fn.index("finishGithubAppSetup")


async def test_webhook_secret_is_generated_client_side_not_pasted_back():
    client = await _client()
    body = (await client.get("/")).text
    assert "function ensureGithubAppWebhookSecret" in body
    assert 'id="github-app-webhook-secret"' in body
    # No input field asks the visitor to type/paste a webhook secret back.
    assert 'id="github-app-webhook-secret-input"' not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest onboarding/tests/test_onboarding_page.py -v`
Expected: many failures/errors (old functions/markup gone from the test
file's expectations, new ones not yet in the page).

- [ ] **Step 3: Replace the frame markup**

In `onboarding/static/index.html`, replace lines 313–336 (the entire
`<details id="frame-github-app">` ... `</details>` block) with:

```html
    <details id="frame-github-app" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame2_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="github-app" data-i18n="change_button"></button>
        <span class="frame-detail"></span>
      </summary>
      <div class="frame-body">
        <p data-i18n="frame2_instructions"></p>
        <ol>
          <li data-i18n="frame2_step_create"></li>
          <li><span data-i18n="frame2_step_homepage_url"></span> <code id="github-app-homepage-url"></code></li>
          <li><span data-i18n="frame2_step_webhook_url"></span> <code id="github-app-webhook-url"></code></li>
          <li><span data-i18n="frame2_step_webhook_secret"></span> <code id="github-app-webhook-secret"></code></li>
          <li>
            <span data-i18n="frame2_step_permissions"></span>
            <table>
              <thead><tr><th data-i18n="frame2_permission_col"></th><th data-i18n="frame2_access_col"></th></tr></thead>
              <tbody id="github-app-permissions-table-body"></tbody>
            </table>
          </li>
          <li data-i18n="frame2_step_events"></li>
          <li data-i18n="frame2_step_private"></li>
          <li data-i18n="frame2_step_install"></li>
        </ol>
        <label data-i18n="frame2_app_id_label"></label>
        <input id="github-app-id-input" type="text" inputmode="numeric" autocomplete="off">
        <label data-i18n="frame2_private_key_label"></label>
        <input id="github-app-key-file-input" type="file" accept=".pem,application/x-pem-file">
        <button id="github-app-validate-submit" type="button" data-i18n="validate_button"></button>
        <ul id="github-app-checklist"></ul>
        <p id="github-app-error" class="frame-error"></p>
      </div>
    </details>
```

- [ ] **Step 4: Replace the i18n strings**

In the `en` strings block, replace lines 510–524 (`frame2_instructions`
through `err_github_no_render_service`) with:

```javascript
      frame2_instructions: "Create a GitHub App by hand and install it, following the steps below — this is done entirely in GitHub's own UI. The wizard never navigates you there.",
      frame2_step_create: "Go to Settings → Developer settings → GitHub Apps → New GitHub App on GitHub, and fill in the form. Pick any unique name.",
      frame2_step_homepage_url: "Homepage URL:",
      frame2_step_webhook_url: "Webhook → Webhook URL (and check Webhook → Active):",
      frame2_step_webhook_secret: "Webhook → Webhook secret — paste this exact value:",
      frame2_step_permissions: "Repository permissions — set exactly:",
      frame2_permission_col: "Permission",
      frame2_access_col: "Access",
      frame2_access_read: "Read-only",
      frame2_access_write: "Read and write",
      frame2_step_events: "Subscribe to events: Pull request only.",
      frame2_step_private: "Where can this GitHub App be installed?: Only on this account.",
      frame2_step_install: "Click Create GitHub App, then install it on your account/repos from the App's own settings page in GitHub's UI (Settings → Developer settings → GitHub Apps → your App → Install App).",
      frame2_app_id_label: "App ID",
      frame2_private_key_label: "Private key — click \"Generate a private key\" on the App's page to download a .pem file, then choose it here:",
      err_github_app_id_invalid: "Enter the App ID — the number on your App's General settings page.",
      err_github_no_file: "Choose your App's private key .pem file first.",
      err_github_invalid_key_file: "That file isn't a valid private key. Check you picked the right .pem file.",
      err_github_unreachable: "GitHub is unreachable right now. Try again in a moment.",
      err_github_invalid_credentials: "GitHub rejected the App ID/private key pair. Double-check both and try again.",
      err_github_no_render_service: "Your Render service isn't set up yet, and the App needs its address before it can be validated. Complete the Render service step above first.",
      frame2_check_event_prefix: "event: ",
      frame2_check_installation_found_prefix: "Installation found: ",
      frame2_check_installation_multiple: "Multiple installations found — this wizard expects exactly one",
      frame2_check_installation_none: "No installation found yet — install the App first, then Validate again",
      frame2_check_webhook: "Webhook URL matches",
```

Delete the now-unused `badge_app_created` entry (line 503) from the `en`
block.

In the `he` strings block, replace lines 641–655 (the Hebrew equivalents of
the deleted keys) with:

```javascript
      frame2_instructions: "צרו אפליקציית GitHub באופן ידני והתקינו אותה, לפי השלבים למטה. הכול נעשה בממשק של GitHub עצמו — האשף לעולם לא ינווט אתכם לשם.",
      frame2_step_create: "עברו ב-GitHub אל Settings ← Developer settings ← GitHub Apps ← New GitHub App ומלאו את הטופס. בחרו שם ייחודי כלשהו.",
      frame2_step_homepage_url: "כתובת דף הבית (Homepage URL):",
      frame2_step_webhook_url: "Webhook ← Webhook URL (וסמנו את Webhook ← Active):",
      frame2_step_webhook_secret: "Webhook ← Webhook secret — הדביקו בדיוק את הערך הזה:",
      frame2_step_permissions: "הרשאות המאגר (Repository permissions) — הגדירו בדיוק כך:",
      frame2_permission_col: "הרשאה",
      frame2_access_col: "גישה",
      frame2_access_read: "קריאה בלבד",
      frame2_access_write: "קריאה וכתיבה",
      frame2_step_events: "הירשמו לאירועים (Subscribe to events): Pull request בלבד.",
      frame2_step_private: "היכן ניתן להתקין את האפליקציה? רק בחשבון הזה (Only on this account).",
      frame2_step_install: "לחצו Create GitHub App, ואז התקינו אותה על החשבון/המאגרים שלכם מתוך דף ההגדרות של האפליקציה ב-GitHub (Settings ← Developer settings ← GitHub Apps ← האפליקציה שלכם ← Install App).",
      frame2_app_id_label: "מזהה האפליקציה (App ID)",
      frame2_private_key_label: "מפתח פרטי — לחצו על \"Generate a private key\" בדף האפליקציה כדי להוריד קובץ .pem, ואז בחרו אותו כאן:",
      err_github_app_id_invalid: "הזינו את מזהה האפליקציה — המספר שבדף ההגדרות הכלליות של האפליקציה שלכם.",
      err_github_no_file: "בחרו תחילה את קובץ המפתח הפרטי (.pem) של האפליקציה.",
      err_github_invalid_key_file: "הקובץ הזה אינו מפתח פרטי תקין. ודאו שבחרתם את קובץ ה-.pem הנכון.",
      err_github_unreachable: "GitHub אינו זמין כרגע. נסו שוב בעוד רגע.",
      err_github_invalid_credentials: "GitHub דחה את צירוף מזהה האפליקציה והמפתח הפרטי. בדקו את שניהם ונסו שוב.",
      err_github_no_render_service: "שירות ה-Render שלכם עדיין לא הוגדר, והאפליקציה זקוקה לכתובת שלו לפני שניתן לאמת אותה. השלימו קודם את שלב שירות ה-Render למעלה.",
      frame2_check_event_prefix: "אירוע: ",
      frame2_check_installation_found_prefix: "נמצאה התקנה: ",
      frame2_check_installation_multiple: "נמצאו מספר התקנות — האשף מצפה בדיוק לאחת",
      frame2_check_installation_none: "לא נמצאה התקנה עדיין — התקינו את האפליקציה קודם, ואז בדקו שוב",
      frame2_check_webhook: "כתובת ה-Webhook תואמת",
```

Delete the now-unused `badge_app_created` entry from the `he` block too.

- [ ] **Step 5: Rename `MANIFEST_PERMISSIONS`/`MANIFEST_EVENTS`**

Find the existing block (around where `MANIFEST_PERMISSIONS` is defined,
alongside `GITHUB_MANIFEST_STATE_KEY`) and replace it with:

```javascript
    // SOURCE OF TRUTH: bot/scripts/create_github_app.py's MANIFEST_PERMISSIONS /
    // MANIFEST_EVENTS. These two lists must stay identical -- the CLI script's
    // manifest and this wizard's validation checklist both describe the same
    // requirement. Change them there first, then here;
    // tests/test_onboarding_page.py::test_required_permissions_match_the_cli_script
    // imports the Python constants and fails if these drift.
    const REQUIRED_PERMISSIONS = {
      pull_requests: "write", contents: "read", issues: "write", metadata: "read",
    };
    const REQUIRED_EVENTS = ["pull_request"];
```

(Delete `const GITHUB_MANIFEST_STATE_KEY = "onboarding.githubManifestState";`
entirely — no more manifest-creation navigation to protect with a CSRF
state value.)

- [ ] **Step 6: Replace the frame-4 JS functions**

Find and delete entirely: `buildManifest`, `submitManifestForm`,
`showGithubAppReadyToInstall`, `resetGithubAppCreateSection`,
`createGithubApp`, `handleGithubManifestCallback`, `markGithubAppInstalled`,
`handleGithubInstallReturn`. Keep unchanged: `githubAppError`,
`pushGithubAppToRenderService`, `finishGithubAppSetup`, `readStoredGithubApp`.

Replace the deleted block with:

```javascript
  function resetGithubAppSetupSection() {
    document.getElementById("github-app-error").textContent = "";
    document.getElementById("github-app-checklist").innerHTML = "";
    document.getElementById("github-app-id-input").value = "";
    document.getElementById("github-app-key-file-input").value = "";
    document.getElementById("github-app-validate-submit").disabled = false;
    currentGithubAppErrorKey = null;
  }

  function githubAppError(key) {
    setFrameStatus("github-app", "error");
    currentGithubAppErrorKey = key;
    document.getElementById("github-app-error").textContent = t(key);
  }

  // Generated once per session and persisted immediately -- GitHub's API
  // never returns a webhook secret to check it against, so this value only
  // needs to exist long enough for the visitor to copy it into GitHub's
  // form and for this frame to push it to Render once validation passes.
  function ensureGithubAppWebhookSecret() {
    let stored = readStoredGithubApp() || {};
    if (!stored.webhook_secret) {
      stored.webhook_secret = base64UrlEncode(crypto.getRandomValues(new Uint8Array(32)));
      try {
        sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
      } catch (err) {
        // best-effort -- validateGithubApp still works this session even if
        // the value can't be persisted across a reload.
      }
    }
    return stored.webhook_secret;
  }

  function renderGithubAppInstructions() {
    const renderService = readStoredRenderService();
    const webhookUrl = renderService && renderService.service_url
      ? `${renderService.service_url}/webhook` : "—";
    document.getElementById("github-app-homepage-url").textContent = window.ONBOARDING_BASE_URL;
    document.getElementById("github-app-webhook-url").textContent = webhookUrl;
    document.getElementById("github-app-webhook-secret").textContent = ensureGithubAppWebhookSecret();
    const ACCESS_LABELS = {read: t("frame2_access_read"), write: t("frame2_access_write")};
    const tbody = document.getElementById("github-app-permissions-table-body");
    tbody.innerHTML = "";
    Object.entries(REQUIRED_PERMISSIONS).forEach(([name, level]) => {
      const tr = document.createElement("tr");
      const nameTd = document.createElement("td");
      nameTd.textContent = name;
      const accessTd = document.createElement("td");
      accessTd.textContent = ACCESS_LABELS[level] || level;
      tr.appendChild(nameTd);
      tr.appendChild(accessTd);
      tbody.appendChild(tr);
    });
  }

  async function validateGithubApp() {
    document.getElementById("github-app-error").textContent = "";
    currentGithubAppErrorKey = null;
    document.getElementById("github-app-checklist").innerHTML = "";
    const renderService = readStoredRenderService();
    if (!renderService || !renderService.service_url) {
      githubAppError("err_github_no_render_service");
      return;
    }
    const appIdRaw = document.getElementById("github-app-id-input").value.trim();
    if (!/^[0-9]+$/.test(appIdRaw) || Number(appIdRaw) <= 0) {
      githubAppError("err_github_app_id_invalid");
      return;
    }
    const fileInput = document.getElementById("github-app-key-file-input");
    const file = fileInput.files[0];
    if (!file) {
      githubAppError("err_github_no_file");
      return;
    }
    let privateKeyB64;
    try {
      privateKeyB64 = await readFileAsBase64(file);
    } catch (err) {
      githubAppError("err_github_invalid_key_file");
      return;
    }
    const btn = document.getElementById("github-app-validate-submit");
    btn.disabled = true;
    setFrameStatus("github-app", "checking");
    const expectedWebhookUrl = `${renderService.service_url}/webhook`;
    let resp;
    try {
      resp = await fetch("/api/github/validate-app", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          app_id: Number(appIdRaw),
          private_key_b64: privateKeyB64,
          expected_webhook_url: expectedWebhookUrl,
        }),
      });
    } catch (err) {
      btn.disabled = false;
      githubAppError("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      btn.disabled = false;
      githubAppError("err_github_unreachable");
      return;
    }
    btn.disabled = false;
    if (!body.valid) {
      setFrameStatus("github-app", "error");
      if (body.reason === "unauthorized") {
        githubAppError("err_github_invalid_credentials");
      } else if (body.reason === "invalid_key") {
        githubAppError("err_github_invalid_key_file");
      } else {
        githubAppError("err_github_unreachable");
      }
      return;
    }
    renderGithubAppChecklist(body);
    if (!body.all_ok) {
      setFrameStatus("github-app", "error");
      return;
    }
    const stored = readStoredGithubApp() || {};
    stored.app_id = Number(appIdRaw);
    stored.private_key_b64 = privateKeyB64;
    stored.installation_id = body.installation.installation_id;
    stored.account_login = body.installation.account_login;
    sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
    await finishGithubAppSetup(stored, body.installation.account_login);
  }

  function renderGithubAppChecklist(body) {
    const list = document.getElementById("github-app-checklist");
    list.innerHTML = "";
    const addRow = (label, ok) => {
      const li = document.createElement("li");
      li.className = ok ? "check-pass" : "check-fail";
      li.textContent = `${ok ? "✓" : "✗"} ${label}`;
      list.appendChild(li);
    };
    body.permissions.forEach((p) => {
      addRow(`${p.name}: ${p.wanted}${p.ok ? "" : ` (have: ${p.actual || "none"})`}`, p.ok);
    });
    body.events.forEach((e) => {
      addRow(`${t("frame2_check_event_prefix")}${e.name}`, e.ok);
    });
    if (body.installation.status === "found") {
      addRow(`${t("frame2_check_installation_found_prefix")}${body.installation.account_login}`, true);
    } else if (body.installation.status === "multiple") {
      addRow(t("frame2_check_installation_multiple"), false);
    } else {
      addRow(t("frame2_check_installation_none"), false);
    }
    addRow(t("frame2_check_webhook"), body.webhook.ok);
  }
```

Note: `validateGithubApp` calls `renderGithubAppChecklist(body)` before this
point in the file defines it — that's fine, JS function declarations are
hoisted, so definition order doesn't affect runtime behavior. The order
here only matters for the test boundary slices in Step 1, which assume
`validateGithubApp` comes first.

- [ ] **Step 7: Update `unlockFrame`, `lockFrame`, `beginChange`, `restoreFromSession`, and event wiring**

In `unlockFrame` (currently `if (id === "uptime-pinger") refreshUptimePingerBlockedState(); if (id === "render-service") prefillRenderServiceDefaults();`),
add a third line:

```javascript
    if (id === "github-app") renderGithubAppInstructions();
```

In `lockFrame`, change `if (id === "github-app") resetGithubAppCreateSection();`
to `if (id === "github-app") resetGithubAppSetupSection();`.

In `beginChange`, change the block
```javascript
    if (id === "github-app") {
      sessionStorage.removeItem(STORAGE_KEYS["github-app"]);
      resetGithubAppCreateSection();
    }
```
to
```javascript
    if (id === "github-app") {
      sessionStorage.removeItem(STORAGE_KEYS["github-app"]);
      resetGithubAppSetupSection();
      renderGithubAppInstructions();
    }
```

In `restoreFromSession`, replace:
```javascript
    const githubApp = readStoredGithubApp();
    if (githubApp && githubApp.completed) {
      completeFrame("github-app", "restored", null);
    } else if (githubApp && githubApp.installation_id) {
      // Installed, but the push-and-clear step never finished (e.g. a reload
      // in the middle of it) -- resume it rather than falsely marking the
      // frame done, the same way the Supabase branch below resumes an
      // interrupted poll instead of completing on partial data.
      frameEl("github-app").open = true;
      finishGithubAppSetup(githubApp, githubApp.account_login);
    } else if (githubApp) {
      showGithubAppReadyToInstall();
    }
```
with:
```javascript
    const githubApp = readStoredGithubApp();
    if (githubApp && githubApp.completed) {
      completeFrame("github-app", "restored", null);
    } else if (githubApp && githubApp.installation_id) {
      // Validated, but the push-and-clear step never finished (e.g. a
      // reload in the middle of it) -- resume it rather than falsely
      // marking the frame done, the same way the Supabase branch below
      // resumes an interrupted poll instead of completing on partial data.
      frameEl("github-app").open = true;
      finishGithubAppSetup(githubApp, githubApp.account_login);
    }
```

Find the DOMContentLoaded event-listener wiring block containing
`document.getElementById("github-app-create-submit").addEventListener("click", createGithubApp);`
and
`document.getElementById("github-app-install-submit").addEventListener("click", markGithubAppInstalled);`
— replace both lines with:
```javascript
    document.getElementById("github-app-validate-submit").addEventListener("click", validateGithubApp);
```

Find the call to `handleGithubManifestCallback()` (in the same startup
sequence as `handleGithubInstallReturn()`, likely near
`handleSupabaseCallback()` or similar page-load dispatch) and delete both
calls — there is no more callback to handle.

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest onboarding/tests/test_onboarding_page.py -v`
Expected: all PASS. If a test fails on an exact function-boundary string
match (e.g. `body.index("function renderGithubAppChecklist")`), adjust the
test's boundary marker to match the actual function order you wrote, not
the implementation — the ordering shown in Step 6 is a suggestion, not a
contract.

- [ ] **Step 9: Manually smoke-test in a browser**

Reaching frame 4 normally requires completing frames 1–3 first (a real
Render API key, a created Render service, dashboard credentials) — not
practical to set up for a smoke test. Instead: run
`uv run uvicorn onboarding.main:app --reload --port 8010` (this project's
established dev-server invocation for `onboarding/`, per prior
onboarding-frame plans), open `http://localhost:8010/`, and in the
browser's devtools console call `unlockFrame("github-app")` directly (the
page's `<script>` is not wrapped in an IIFE, so its top-level functions are
callable from the console) to reach the frame without the earlier steps.
Confirm: the frame renders instructions with no GitHub link anywhere (view
page source and search for `github.com` to double check), the App ID/file
inputs and Validate button appear, and choosing any small file plus a
numeric App ID and clicking Validate issues exactly one request to
`/api/github/validate-app` (check the Network tab) without a JS console
error — it will come back with `{"valid": false, "reason": "invalid_key"}`
or similar since there's no real GitHub App behind it, which is fine; the
goal here is confirming the request fires and the page renders/handles the
response without erroring, not a full live validation (that needs a real
App and is out of scope for this automated step).

- [ ] **Step 10: Run ruff (JS has no ruff target, but re-run the Python suite)**

Run: `uv run pytest onboarding/ -v` (full onboarding suite, catches any
cross-file breakage) and `uv run ruff check onboarding/`.
Expected: all PASS, no lint errors.

- [ ] **Step 11: Commit**

```bash
git add onboarding/static/index.html onboarding/tests/test_onboarding_page.py
git commit -m "Frame 4: fully manual App creation with a doctor.py-style validation checklist"
```

---

### Task 4: Documentation — `onboarding/CLAUDE.md` and `ISSUES.md`

**Files:**
- Modify: `onboarding/CLAUDE.md` ("What sub-project 2 (GitHub App
  automation) adds to these rules" section)
- Modify: `ISSUES.md` (new entry)

**Interfaces:**
- Consumes: nothing (documentation only, no code dependency).
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Rewrite `onboarding/CLAUDE.md`'s sub-project 2 section**

Read the current section first (`grep -n "sub-project 2" onboarding/CLAUDE.md`
to find its exact bounds — it runs from the `## What sub-project 2 (GitHub
App automation) adds to these rules` heading to the next `##` heading).
Replace its entire body with:

```markdown
## What sub-project 2 (GitHub App automation) adds to these rules

- **App creation is fully manual (2026-09-01), not just installation.**
  The wizard used to automate creation via GitHub's App Manifest flow (a
  JS-constructed form POST to `github.com/settings/apps/new`). A GitHub
  account was suspended during this frame a second time, with the
  install-page fix (below) already shipped — pointing at the manifest
  flow's own automated navigation as the next most likely remaining
  source, though this is **not confirmed**: the account's age, this
  project's own App-creation rate during testing, or some other factor
  could equally explain it. Automation was removed as a precaution given
  that uncertainty, not as a diagnosed fix — see
  `docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md`
  and `ISSUES.md`. There is no manifest, no `redirect_url`/`state` CSRF
  dance, and no cross-origin form POST left in this frame at all.
- **No URL referencing `github.com` appears anywhere on the page, for
  either creation or installation.** The 2026-08-31 install-page fix
  (below) already banned URLs for that one step; this extends the same
  policy to App creation, on the theory that any correlation between this
  page and a GitHub navigation — however it's initiated — is the thing
  worth avoiding, not just a JS-driven redirect specifically. Instructions
  are breadcrumb text only ("Settings → Developer settings → GitHub Apps →
  New GitHub App"), mirroring `guide/setup/02-github-app.md`'s own wording
  for the same step. `test_page_offers_no_route_to_the_install_page_at_all`
  and its sibling `test_page_offers_no_route_to_github_app_creation_either`
  both enforce this.
- **A doctor.py-style validation checklist compensates for losing the
  manifest flow's built-in correctness.** A Manifest always creates an App
  with exactly the requested permissions/events/webhook URL; a hand-created
  App can have any of those wrong by a missed checkbox. The visitor pastes
  back App ID + private key (the private key via a file picker, converted
  to base64 client-side — never typed/pasted as text), and
  `github_client.validate_app()` reads the App's actual configuration back
  from GitHub (`GET /app`, `GET /app/installations`, `GET /app/hook/config`,
  all under the visitor's own App JWT) and reports one pass/fail line per
  requirement. The frame unlocks — and only then pushes credentials to
  Render — once every line passes. This is a fresh, independent
  implementation of the same idea `bot/github_app.py`'s doctor checks
  already use for the operator-side CLI/deploy path, not a shared import
  (`onboarding/` never imports from `bot/`'s credential-handling code, per
  this file's own no-shared-credential-path rule).
- **Installation is auto-discovered, not typed.** `validate_app()` calls
  `GET /app/installations` itself rather than asking the visitor for an
  installation ID — mirrors `bot/github_app.py::discover_installation_id_for_app`'s
  exactly-one-installation expectation. Zero installations and multiple
  installations are both distinct, reported failure states, not folded
  together into one generic "not found."
- **The webhook secret is generated by the wizard, not invented by the
  visitor.** `ensureGithubAppWebhookSecret()` (client-side,
  `crypto.getRandomValues`, same shape as the dashboard frame's session
  secret) generates and persists one the first time this frame's
  instructions render, and displays it for the visitor to copy into
  GitHub's form. GitHub's API never returns a webhook secret to check it
  against, so this field is the one requirement `validate_app()` cannot
  verify — its presence in the pushed env vars is all that's checked.
- **`verify-installation`'s request body carries a GitHub App's full
  private key** — the same sensitivity tier as this project's own
  `GITHUB_APP_PRIVATE_KEY`. Treat it accordingly: never logged, never in an
  unhandled exception's message, narrow `except` clauses only. (This rule
  now applies to `validate-app`'s request body, `verify-installation`'s
  successor.)
- **The wizard offers no route to GitHub's App-install page — no redirect,
  no link, and no URL text to copy either (2026-08-31).** Five separate
  throwaway GitHub accounts were suspended for a ToS violation at exactly
  this step: three via `location.href`, one via an `<a>` carrying both
  `rel="noreferrer"` and `referrerpolicy="no-referrer"`, and one via the
  visitor pasting the URL into their own address bar. The fourth run rules
  out the `Referer` header (a click still sends `Sec-Fetch-Site:
  cross-site`, which no page can suppress); the fifth rules out the
  navigation's initiator too. What the surviving runs have in common is
  that the visitor reached the install page by navigating *inside GitHub*.
  See `ISSUES.md`.
```

- [ ] **Step 2: Add the `ISSUES.md` entry**

Run `grep -n "^## " ISSUES.md | head -5` first to confirm the file's section
heading style and where new entries go (top of the incident log, or
appended — match the existing convention exactly). Add a new entry
(adjust its heading level/placement to match what that `grep` shows):

```markdown
## Frame 4 caused another GitHub account suspension despite the install-page fix

- **What happened:** A GitHub account was suspended for a ToS violation
  during onboarding wizard frame 4 (GitHub App creation), after the
  2026-08-31 fix that removed all navigation to the App-*install* page.
  This time the automated *creation* flow (a JS-constructed form POST to
  `github.com/settings/apps/new` via GitHub's App Manifest API) was the
  only GitHub-directed automation left running in this frame.
- **What's uncertain:** Whether the manifest-flow's automated navigation
  caused this, as opposed to the account's age, this project's own
  App-creation rate during testing, or some other factor — there is no
  diagnostic feedback from GitHub's suspension system to confirm a cause,
  the same limitation the original install-page incident had.
- **Response:** Removed the automated navigation as a precaution (same
  posture as the install-page fix), not as a diagnosed fix. App creation
  is now fully manual, compensated for by a doctor.py-style validation
  checklist that reads the App's actual configuration back from GitHub.
  See `docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md`.
- **Cost:** A full redesign of frame 4 (backend validation functions, one
  new endpoint, frontend instructions/checklist UI) — tracked in that
  design doc and its accompanying implementation plan.
```

- [ ] **Step 3: Commit**

```bash
git add onboarding/CLAUDE.md ISSUES.md
git commit -m "Document the manual GitHub App creation + validation checklist redesign"
```
