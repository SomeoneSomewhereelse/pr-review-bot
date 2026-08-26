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
