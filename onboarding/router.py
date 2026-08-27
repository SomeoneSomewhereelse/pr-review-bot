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

from onboarding import github_client, render_client, supabase_client
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
    ref: str = Field(max_length=20, pattern=r"^[a-z]{20}$")


class SupabaseConnectionInfoRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    ref: str = Field(max_length=20, pattern=r"^[a-z]{20}$")


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = _INDEX_HTML.replace("__ONBOARDING_BASE_URL__", settings.public_base_url)
    html = html.replace("__SUPABASE_OAUTH_CLIENT_ID__", settings.supabase_oauth_client_id)
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
