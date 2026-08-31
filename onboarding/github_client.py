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

# GitHub's API guidelines require every request to carry a descriptive
# User-Agent naming the application. Without this, httpx sends a bare
# "python-httpx/<version>" and PyGithub a bare "PyGithub/Python" -- exactly
# the generic-library shape those guidelines single out, and the shape
# anti-scraping heuristics score worst. This is not known to have caused the
# install-step account suspensions logged in ISSUES.md (those happened during
# a browser navigation, before any of these calls run), so treat it as
# API-citizenship hygiene rather than a fix for that incident.
USER_AGENT = "pr-review-bot-onboarding"


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
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": USER_AGENT,
                },
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
class InstallationFound:
    installation_id: int
    account_login: str
    repo_scope: str  # "all" | "selected"


@dataclasses.dataclass(frozen=True)
class InstallationInvalid:
    reason: str  # "not_installed" | "invalid_credentials" | "github_unreachable"


InstallationResult = InstallationFound | InstallationInvalid


def _list_installations(app_id: int, private_key_pem: str) -> object:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    Builds its own independent client from the visitor's just-minted
    app_id/private_key (never bot/github_app.py's operator-tied helpers —
    onboarding/CLAUDE.md's no-shared-credential-path rule). App-JWT only
    (Auth.AppAuth, not an installation access token), which is exactly what
    GET /app/installations authenticates as: an App listing its own
    installations, so the result can only ever describe this App."""
    gh = Github(auth=Auth.AppAuth(app_id, private_key_pem), user_agent=USER_AGENT)
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/installations")
    return data


async def find_installation(app_id: int, private_key_b64: str) -> InstallationResult:
    """Ask GitHub which installations this App has, rather than trusting an
    installation_id handed to us in a redirect's query string.

    GitHub's own setup-URL docs warn that "bad actors can hit this URL with a
    spoofed installation_id... you should not rely on the validity of the
    installation_id parameter." Discovering it through the App's own JWT
    sidesteps that entirely, and — the reason this exists — it works no
    matter how the visitor installed the App: same tab, a new tab, or
    straight from GitHub's own UI days later. The wizard no longer drives
    that navigation at all (see ISSUES.md), so it cannot assume a round trip
    it controls.

    Never logs the private key, in full or truncated — same sensitivity tier
    as this project's own GITHUB_APP_PRIVATE_KEY."""
    try:
        private_key_pem = base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError):
        return InstallationInvalid(reason="invalid_credentials")

    try:
        data = await asyncio.to_thread(_list_installations, app_id, private_key_pem)
    except GithubException as exc:
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

    if not isinstance(data, list) or not data:
        return InstallationInvalid(reason="not_installed")

    # A wizard-created App is private (the manifest sets public: false), so
    # only its own owner can install it and one installation is the norm.
    # Take the first rather than paging: there is no second one to miss.
    try:
        first = data[0]
        installation_id = int(first["id"])
        account_login = str(first["account"]["login"])
        repo_scope = str(first["repository_selection"])
    except (KeyError, TypeError, ValueError):
        return InstallationInvalid(reason="github_unreachable")

    return InstallationFound(
        installation_id=installation_id, account_login=account_login, repo_scope=repo_scope
    )


# No set_webhook_url here: the wizard bakes the App's real webhook URL into
# the manifest at creation time (onboarding/static/index.html's
# buildManifest), so there is nothing to correct afterwards. bot/github_app.py
# keeps its own operator-side set_webhook_url for the CLI/deploy path.
