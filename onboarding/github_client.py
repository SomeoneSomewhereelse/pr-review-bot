"""Thin async wrapper around GitHub's App Manifest and App-installation
APIs — creates/verifies a visitor's own GitHub App without persisting
anything server-side. See
docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
sections 3-4, 6."""
from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses

import httpx
import jwt
import requests
from github import Auth, Github, GithubException

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
    except (ValueError, jwt.exceptions.InvalidKeyError):
        # A base64-valid but non-PEM string: cryptography's own PEM parser
        # raises ValueError, but PyGithub signs the App JWT via PyJWT, which
        # catches that ValueError internally and re-raises it as
        # jwt.exceptions.InvalidKeyError (confirmed against the installed
        # pygithub==2.9.1 / PyJWT stack) — neither is raised at the
        # base64-decode step above, only once PyGithub tries to parse the
        # key material while signing.
        return InstallationInvalid(reason="invalid_credentials")
    except requests.exceptions.RequestException:
        return InstallationInvalid(reason="github_unreachable")

    try:
        account_login = str(data["account"]["login"])
        repo_scope = str(data["repository_selection"])
    except (KeyError, TypeError):
        return InstallationInvalid(reason="github_unreachable")

    return InstallationVerified(account_login=account_login, repo_scope=repo_scope)


@dataclasses.dataclass(frozen=True)
class WebhookUrlSet:
    pass


@dataclasses.dataclass(frozen=True)
class WebhookUrlSetFailed:
    reason: str  # "invalid_credentials" | "github_unreachable"


WebhookUrlResult = WebhookUrlSet | WebhookUrlSetFailed


def _set_webhook_url(app_id: int, private_key_pem: str, url: str) -> None:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    Same App-JWT-only client construction as _fetch_installation above
    (never app/github_app.py's operator-tied helpers — onboarding/CLAUDE.md's
    no-shared-credential-path rule). Mirrors app/github_app.py::
    set_webhook_url's own PATCH /app/hook/config call shape exactly — this
    is the visitor-credential equivalent of the same operation."""
    gh = Github(auth=Auth.AppAuth(app_id, private_key_pem))
    gh.requester.requestJsonAndCheck("PATCH", "/app/hook/config", input={"url": url})


async def set_webhook_url(app_id: int, private_key_b64: str, url: str) -> WebhookUrlResult:
    """Point the visitor's App's webhook at `url` (their just-deployed
    Render service's own /webhook path). Never logs the private key, in
    full or truncated — same sensitivity tier as verify_installation's."""
    try:
        private_key_pem = base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError):
        return WebhookUrlSetFailed(reason="invalid_credentials")

    try:
        await asyncio.to_thread(_set_webhook_url, app_id, private_key_pem, url)
    except GithubException as exc:
        if exc.status in (401, 403, 404):
            return WebhookUrlSetFailed(reason="invalid_credentials")
        return WebhookUrlSetFailed(reason="github_unreachable")
    except (ValueError, jwt.exceptions.InvalidKeyError):
        # Same base64-valid-but-non-PEM case verify_installation's own
        # docstring explains: PyJWT re-raises cryptography's ValueError as
        # InvalidKeyError while signing the App JWT.
        return WebhookUrlSetFailed(reason="invalid_credentials")
    except requests.exceptions.RequestException:
        return WebhookUrlSetFailed(reason="github_unreachable")

    return WebhookUrlSet()
