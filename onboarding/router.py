"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from onboarding import (
    github_client,
    llm_client,
    render_client,
    session_store,
    supabase_client,
    uptimerobot_client,
)
from onboarding.config import settings

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

SESSION_COOKIE_NAME = "onboarding_session"

# frame -> which of that frame's stored keys are safe to echo back to the
# browser for restore-on-load badges. Never a credential value -- see root
# CLAUDE.md's secret-handling section and this file's own module docstring.
_DISPLAY_FIELDS = {
    "render": ("owner_name", "service_url"),
    "github_app": (),
    "supabase": ("name",),
    "llm_provider": ("provider", "model"),
    "dashboard_auth": (),
    "uptime_pinger": ("monitor_id",),
}


def _get_session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=int(session_store.SESSION_TTL.total_seconds()),
    )


class RenderKeyRequest(BaseModel):
    api_key: str = Field(max_length=512)


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


class SupabaseExchangeCodeRequest(BaseModel):
    # The browser sends back the exact redirect_uri it used on the authorize
    # leg. OAuth requires the two to match byte-for-byte, and deriving them
    # separately (browser from its own origin, server from a hand-set env
    # var) is precisely how they drift apart — a trailing slash was enough to
    # break this in practice. Supabase validates the value against the app's
    # registered list regardless, so accepting it from the caller cannot
    # redirect anything anywhere; the shape check is to avoid relaying
    # arbitrary junk outbound.
    code: str = Field(max_length=512)
    code_verifier: str = Field(max_length=256)
    redirect_uri: str = Field(min_length=1, max_length=2048, pattern=r"^https?://[^\s\"'<>\\]+$")


class SupabaseRefreshTokenRequest(BaseModel):
    refresh_token: str = Field(max_length=2048)


class SupabaseListOrgsRequest(BaseModel):
    access_token: str = Field(max_length=4096)


class SupabaseCreateProjectRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    organization_slug: str = Field(min_length=1, max_length=64)
    name: str = Field(max_length=256)
    db_pass: str = Field(max_length=256)


class SupabaseProjectStatusRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    ref: str = Field(max_length=20, pattern=r"^[a-z]{20}$")


class SupabaseConnectionInfoRequest(BaseModel):
    access_token: str = Field(max_length=4096)
    ref: str = Field(max_length=20, pattern=r"^[a-z]{20}$")


class LlmGeminiListModelsRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class LlmGroqListModelsRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class LlmVertexListModelsRequest(BaseModel):
    service_account_key_b64: str = Field(min_length=1, max_length=16384)


class UptimeRobotDeleteMonitorRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    monitor_id: int = Field(gt=0)


class UptimeRobotCreateMonitorRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    render_service_url: str = Field(min_length=1, max_length=2048)

    @field_validator("render_service_url")
    @classmethod
    def _normalize_render_service_url(cls, value: str) -> str:
        """Strip first, then require non-empty -- min_length=1 alone does not
        survive stripping.

        A whitespace-only value passes min_length=1, and
        uptimerobot_client._target_url's own .strip() then derives the bare
        relative path "/healthz", which would be POSTed to UptimeRobot as a
        monitor URL. Rejecting it here turns a nonsense monitor (or an
        opaque provider-side 400 surfaced as `request_rejected`) into an
        honest 422. Same strip-then-validate shape as onboarding/config.py's
        public_base_url validator; no shape/regex check beyond that, since
        this value is written by the wizard itself (sub-project 6's forward
        contract), not typed by the visitor.
        """
        value = value.strip()
        if not value:
            raise ValueError("render_service_url must not be empty or whitespace-only")
        return value


class RenderServiceCreateRequest(BaseModel):
    api_key: str = Field(max_length=512)
    repo_url: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=64)


class RenderPushEnvVarsRequest(BaseModel):
    render_api_key: str = Field(max_length=512)
    render_service_id: str = Field(min_length=1, max_length=64)


class GithubPushRenderVarsRequest(RenderPushEnvVarsRequest):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    webhook_secret: str = Field(max_length=512)
    installation_id: int = Field(gt=0)


class SupabasePushRenderVarRequest(RenderPushEnvVarsRequest):
    database_url: str = Field(min_length=1, max_length=2048)


class LlmPushRenderVarsRequest(RenderPushEnvVarsRequest):
    provider: str = Field(pattern=r"^(gemini|groq|vertex)$")
    credential_value: str = Field(min_length=1, max_length=16384)
    model: str = Field(min_length=1, max_length=256)


class DashboardAuthPushRenderVarsRequest(RenderPushEnvVarsRequest):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    session_secret: str = Field(min_length=32, max_length=256)


class RenderTriggerDeployRequest(BaseModel):
    api_key: str = Field(max_length=512)
    service_id: str = Field(min_length=1, max_length=64)


class RenderDeployStatusRequest(BaseModel):
    api_key: str = Field(max_length=512)
    service_id: str = Field(min_length=1, max_length=64)
    deploy_id: str = Field(min_length=1, max_length=64)


# Paired comment with app/providers/registry.py::PROVIDERS -- onboarding/
# never imports from app/ (onboarding/CLAUDE.md's no-shared-credential-
# path rule), so this 3-entry mapping is a deliberate copy, not a shared
# import. Keep in sync if a provider's env var names ever change there.
_LLM_ENV_VAR_NAMES = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL"),
}


# Supabase's OAuth app registration matches redirect URIs exactly, so the
# callback is a bare path: no query string to be normalised or dropped, and
# no trailing-slash ambiguity. The wizard is a single page, so this route
# serves the same document as "/" and the page routes on its own pathname.
SUPABASE_OAUTH_CALLBACK_PATH = "/oauth/supabase/callback"


def _render_index() -> HTMLResponse:
    html = _INDEX_HTML.replace("__SUPABASE_OAUTH_CLIENT_ID__", settings.supabase_oauth_client_id)
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return _render_index()


@router.get(SUPABASE_OAUTH_CALLBACK_PATH, response_class=HTMLResponse)
async def supabase_oauth_callback() -> HTMLResponse:
    return _render_index()


@router.get("/api/session")
async def get_session_state(request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None:
        return {"frames": {}}
    session = session_store.get_session(session_id)
    if session is None:
        return {"frames": {}}
    frames = {}
    for frame, data in session.frames.items():
        display = {k: data[k] for k in _DISPLAY_FIELDS.get(frame, ()) if k in data}
        frames[frame] = {"complete": True, "display": display}
    return {"frames": frames}


@router.post("/api/session/reset")
async def reset_session(request: Request, response: Response) -> Response:
    session_id = _get_session_id(request)
    if session_id is not None:
        session_store.delete_session(session_id)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.status_code = 204
    return response


@router.post("/api/render/validate-key")
async def validate_render_key(payload: RenderKeyRequest) -> dict:
    result = await render_client.validate_key(payload.api_key)
    if isinstance(result, render_client.RenderKeyValid):
        return {"valid": True, "owner_name": result.owner_name}
    return {"valid": False, "reason": result.reason}


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


@router.post("/api/supabase/exchange-oauth-code")
async def exchange_supabase_oauth_code(payload: SupabaseExchangeCodeRequest) -> dict:
    result = await supabase_client.exchange_oauth_code(
        payload.code, payload.code_verifier, payload.redirect_uri
    )
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


@router.post("/api/llm/gemini/list-models")
async def list_gemini_models(payload: LlmGeminiListModelsRequest) -> dict:
    result = await llm_client.list_gemini_models(payload.api_key)
    if isinstance(result, llm_client.LlmModelsListed):
        return {"valid": True, "models": result.models}
    return {"valid": False, "reason": result.reason}


@router.post("/api/llm/groq/list-models")
async def list_groq_models(payload: LlmGroqListModelsRequest) -> dict:
    result = await llm_client.list_groq_models(payload.api_key)
    if isinstance(result, llm_client.LlmModelsListed):
        return {"valid": True, "models": result.models}
    return {"valid": False, "reason": result.reason}


@router.post("/api/llm/vertex/list-models")
async def list_vertex_models(payload: LlmVertexListModelsRequest) -> dict:
    result = await llm_client.list_vertex_models(payload.service_account_key_b64)
    if isinstance(result, llm_client.VertexModelsListed):
        return {"valid": True, "project_id": result.project_id, "models": result.models}
    return {"valid": False, "reason": result.reason}


@router.post("/api/uptimerobot/create-monitor")
async def create_uptimerobot_monitor(payload: UptimeRobotCreateMonitorRequest) -> dict:
    result = await uptimerobot_client.create_or_reuse_monitor(
        payload.api_key, payload.render_service_url
    )
    if isinstance(result, uptimerobot_client.UptimeRobotMonitorResult):
        return {"valid": True, "created": result.created, "monitor_id": result.monitor_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/uptimerobot/delete-monitor")
async def delete_uptimerobot_monitor(payload: UptimeRobotDeleteMonitorRequest) -> dict:
    """Best-effort cleanup, called when an earlier frame change (render-key
    or render-service) invalidates a monitor a visitor already created for
    the old service URL -- see onboarding/static/index.html's
    cleanupOrphanedUptimeMonitor()."""
    result = await uptimerobot_client.delete_monitor(payload.api_key, payload.monitor_id)
    if isinstance(result, uptimerobot_client.UptimeRobotMonitorDeleted):
        return {"valid": True}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/create-service")
async def create_render_service(payload: RenderServiceCreateRequest) -> dict:
    result = await render_client.create_service(payload.api_key, payload.repo_url, payload.name)
    if isinstance(result, render_client.RenderServiceCreated):
        return {"valid": True, "service_id": result.service_id, "service_url": result.service_url}
    if result.message:
        return {"valid": False, "reason": result.reason, "message": result.message}
    return {"valid": False, "reason": result.reason}


def _push_result(result) -> dict:
    if isinstance(result, render_client.RenderEnvVarsPushed):
        return {"valid": True, "pushed": result.pushed}
    return {"valid": False, "reason": result.reason, "pushed": result.pushed}


@router.post("/api/github/push-render-vars")
async def push_github_render_vars(payload: GithubPushRenderVarsRequest) -> dict:
    result = await render_client.push_env_vars(
        payload.render_api_key,
        payload.render_service_id,
        {
            "GITHUB_APP_ID": str(payload.app_id),
            "GITHUB_APP_PRIVATE_KEY": payload.private_key_b64,
            "GITHUB_WEBHOOK_SECRET": payload.webhook_secret,
            "GITHUB_APP_INSTALLATION_ID": str(payload.installation_id),
        },
    )
    return _push_result(result)


@router.post("/api/supabase/push-render-var")
async def push_supabase_render_var(payload: SupabasePushRenderVarRequest) -> dict:
    result = await render_client.push_env_vars(
        payload.render_api_key, payload.render_service_id, {"DATABASE_URL": payload.database_url}
    )
    return _push_result(result)


@router.post("/api/llm/push-render-vars")
async def push_llm_render_vars(payload: LlmPushRenderVarsRequest) -> dict:
    credential_var, model_var = _LLM_ENV_VAR_NAMES[payload.provider]
    result = await render_client.push_env_vars(
        payload.render_api_key,
        payload.render_service_id,
        {
            "LLM_PROVIDER": payload.provider,
            credential_var: payload.credential_value,
            model_var: payload.model,
        },
    )
    return _push_result(result)


@router.post("/api/dashboard-auth/push-render-vars")
async def push_dashboard_auth_render_vars(payload: DashboardAuthPushRenderVarsRequest) -> dict:
    result = await render_client.push_env_vars(
        payload.render_api_key,
        payload.render_service_id,
        {
            "DASHBOARD_USERNAME": payload.username,
            "DASHBOARD_PASSWORD": payload.password,
            "DASHBOARD_SESSION_SECRET": payload.session_secret,
        },
    )
    return _push_result(result)


@router.post("/api/render/trigger-deploy")
async def trigger_render_deploy(payload: RenderTriggerDeployRequest) -> dict:
    result = await render_client.trigger_deploy(payload.api_key, payload.service_id)
    if isinstance(result, render_client.RenderDeployTriggered):
        return {"valid": True, "deploy_id": result.deploy_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/deploy-status")
async def get_render_deploy_status(payload: RenderDeployStatusRequest) -> dict:
    result = await render_client.poll_deploy_status(
        payload.api_key, payload.service_id, payload.deploy_id
    )
    if isinstance(result, render_client.RenderDeployStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}
