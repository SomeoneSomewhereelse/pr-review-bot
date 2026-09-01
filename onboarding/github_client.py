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
    return AppValidated(
        permissions=permissions, events=events, installation=installation, webhook=webhook
    )
