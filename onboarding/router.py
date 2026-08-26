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
