"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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
    # Generated client-side (never sent to GitHub, only used later as
    # GITHUB_WEBHOOK_SECRET), so it rides along here purely for session
    # storage -- validate_app()'s own logic never reads it.
    webhook_secret: str = Field(min_length=1, max_length=512)


class SupabaseConnectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)


class SupabaseCreateProjectRequest(BaseModel):
    organization_slug: str = Field(min_length=1, max_length=64)


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
    repo_url: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=64)


class LlmConfirmRequest(BaseModel):
    provider: str = Field(pattern=r"^(gemini|groq|vertex)$")
    credential_value: str = Field(min_length=1, max_length=16384)
    model: str = Field(min_length=1, max_length=256)


class DashboardAuthConfirmRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    session_secret: str = Field(min_length=32, max_length=256)


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


@router.get(SUPABASE_OAUTH_CALLBACK_PATH)
async def supabase_oauth_callback(request: Request):
    """Completes the PKCE code exchange server-side and carries the pending
    `name` forward, then redirects to `/` -- restoring the wizard's state
    from here on is just the ordinary GET /api/session load, not a special
    case. No client-side JS runs for this route at all. See
    docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md
    section 3.4."""
    session_id = _get_session_id(request)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    pending = None
    if session_id is not None:
        supabase_frame = session_store.read_frame(session_id, "supabase")
        pending = (supabase_frame or {}).get("_pending_oauth")
    if session_id is None or pending is None or not code or pending.get("state") != state:
        # No session, no pending OAuth, or a state mismatch (forged/replayed
        # redirect) -- fall back to the ordinary "no session" page load
        # rather than completing anything.
        return RedirectResponse(url="/", status_code=302)

    redirect_uri = f"{request.base_url}{SUPABASE_OAUTH_CALLBACK_PATH.lstrip('/')}"
    token_result = await supabase_client.exchange_oauth_code(
        code, pending["verifier"], redirect_uri
    )
    if not isinstance(token_result, supabase_client.SupabaseTokens):
        return RedirectResponse(url="/", status_code=302)

    session_store.update_frame(
        session_id,
        "supabase",
        {
            "access_token": token_result.access_token,
            "refresh_token": token_result.refresh_token,
            "name": pending["name"],
            "_pending_oauth": None,
        },
    )
    return RedirectResponse(url="/", status_code=302)


@router.get("/api/session")
async def get_session_state(request: Request) -> dict:
    """Maps session_store's backend frame_data keys onto the wizard's UI
    frame ids -- not a 1:1 pass-through, since "render" backs three
    distinct UI frames (render-key, render-service, render-deploy's
    pending_deploy_id) and "supabase" has a genuine in-between state
    (OAuth done, project not yet created) that isn't just "locked" or
    "complete". Never echoes a credential value -- see root CLAUDE.md's
    secret-handling section and this file's own module docstring."""
    session_id = _get_session_id(request)
    if session_id is None:
        return {"frames": {}}
    session = session_store.get_session(session_id)
    if session is None:
        return {"frames": {}}
    data = session.frames
    frames: dict[str, dict] = {}

    render = data.get("render")
    if render and "api_key" in render:
        frames["render-key"] = {
            "complete": True, "display": {"owner_name": render.get("owner_name")}
        }
    if render and "service_id" in render:
        frames["render-service"] = {
            "complete": True, "display": {"service_url": render.get("service_url")}
        }

    if data.get("dashboard_auth"):
        frames["dashboard-auth"] = {"complete": True, "display": {}}

    if data.get("github_app"):
        frames["github-app"] = {"complete": True, "display": {}}

    supabase = data.get("supabase")
    if supabase and "ref" in supabase:
        frames["supabase"] = {"complete": True, "display": {"name": supabase.get("name")}}
    elif supabase and "access_token" in supabase:
        # Authorized but the project hasn't been created yet (visitor
        # returned from Supabase's consent screen but the page reloaded
        # before they picked an org) -- not locked, not complete either.
        frames["supabase"] = {"complete": False, "authorized": True, "display": {}}

    llm_provider = data.get("llm_provider")
    if llm_provider:
        frames["llm-provider"] = {
            "complete": True,
            "display": {
                "provider": llm_provider.get("provider"),
                "model": llm_provider.get("model"),
            },
        }

    if data.get("uptime_pinger"):
        frames["uptime-pinger"] = {"complete": True, "display": {}}

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
async def validate_render_key(
    payload: RenderKeyRequest, request: Request, response: Response
) -> dict:
    result = await render_client.validate_key(payload.api_key)
    if isinstance(result, render_client.RenderKeyValid):
        # The wizard's session entry point -- the ONLY endpoint allowed to
        # call create_session(). Every other endpoint below requires an
        # existing session and fails closed instead (session_store.py's
        # update_frame() enforces this at the storage layer too).
        session_id = _get_session_id(request)
        if session_id is None or session_store.get_session(session_id) is None:
            session_id = session_store.create_session()
            _set_session_cookie(response, session_id)
        session_store.update_frame(
            session_id, "render", {"api_key": payload.api_key, "owner_name": result.owner_name}
        )
        return {"valid": True, "owner_name": result.owner_name}
    return {"valid": False, "reason": result.reason}


@router.post("/api/github/validate-app")
async def validate_github_app(payload: GithubValidateAppRequest, request: Request) -> dict:
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
        installation_id = result.installation.installation_id
    elif isinstance(result.installation, github_client.MultipleInstallationsFound):
        installation = {"status": "multiple", "account_logins": result.installation.account_logins}
        installation_ok = False
        installation_id = None
    else:
        installation = {"status": "none"}
        installation_ok = False
        installation_id = None

    all_ok = (
        all(p.ok for p in result.permissions)
        and all(e.ok for e in result.events)
        and installation_ok
        and result.webhook.ok
    )
    if all_ok:
        # Best-effort: a directly-called endpoint with no session yet (e.g.
        # a visitor re-validating before frame 1 is done) still gets a live
        # checklist result -- the frontend's own frame-lock sequencing is
        # what normally prevents reaching this frame without an earlier
        # session existing.
        session_id = _get_session_id(request)
        if session_id is not None and session_store.get_session(session_id) is not None:
            session_store.update_frame(
                session_id,
                "github_app",
                {
                    "app_id": payload.app_id,
                    "private_key_b64": payload.private_key_b64,
                    "webhook_secret": payload.webhook_secret,
                    "installation_id": installation_id,
                },
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


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


@router.post("/api/supabase/connect")
async def connect_supabase(payload: SupabaseConnectRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or session_store.get_session(session_id) is None:
        return {"valid": False, "reason": "no_session"}
    state = _secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    session_store.update_frame(
        session_id,
        "supabase",
        {"_pending_oauth": {"state": state, "verifier": verifier, "name": payload.name}},
    )
    redirect_uri = f"{request.base_url}{SUPABASE_OAUTH_CALLBACK_PATH.lstrip('/')}"
    params = {
        "client_id": settings.supabase_oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {"valid": True, "authorize_url": f"https://api.supabase.com/v1/oauth/authorize?{urlencode(params)}"}


@router.post("/api/supabase/list-organizations")
async def list_supabase_organizations(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or "access_token" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.list_organizations(supabase_frame["access_token"])
    if isinstance(result, supabase_client.SupabaseOrgsListed):
        return {"valid": True, "orgs": [{"slug": o.slug, "name": o.name} for o in result.orgs]}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/create-project")
async def create_supabase_project(payload: SupabaseCreateProjectRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or "access_token" not in supabase_frame or "name" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    db_pass = _secrets.token_urlsafe(24)
    result = await supabase_client.create_project(
        supabase_frame["access_token"], payload.organization_slug, supabase_frame["name"], db_pass
    )
    if isinstance(result, supabase_client.SupabaseProjectCreated):
        session_store.update_frame(
            session_id,
            "supabase",
            {
                "ref": result.ref,
                "status": result.status,
                "db_pass": db_pass,
                "organization_slug": payload.organization_slug,
            },
        )
        return {"valid": True, "ref": result.ref, "status": result.status}
    if isinstance(result, supabase_client.SupabaseProjectRejected):
        return {"valid": False, "reason": "project_creation_rejected", "message": result.message}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/project-status")
async def get_supabase_project_status(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or "access_token" not in supabase_frame or "ref" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.get_project_status(
        supabase_frame["access_token"], supabase_frame["ref"]
    )
    if isinstance(result, supabase_client.SupabaseProjectStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/connection-info")
async def get_supabase_connection_info(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    required = ("access_token", "ref", "db_pass")
    if not supabase_frame or not all(k in supabase_frame for k in required):
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.get_connection_info(
        supabase_frame["access_token"], supabase_frame["ref"], session_id=session_id
    )
    if isinstance(result, supabase_client.SupabaseConnectionInfo):
        database_url = (
            f"postgresql://{result.db_user}:{supabase_frame['db_pass']}"
            f"@{result.db_host}:{result.db_port}/{result.db_name}"
        )
        session_store.update_frame(session_id, "supabase", {"database_url": database_url})
        return {"valid": True}
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


@router.post("/api/llm/confirm")
async def confirm_llm_provider(payload: LlmConfirmRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or session_store.get_session(session_id) is None:
        return {"valid": False, "reason": "no_session"}
    session_store.update_frame(
        session_id,
        "llm_provider",
        {
            "provider": payload.provider,
            "credential_value": payload.credential_value,
            "model": payload.model,
        },
    )
    return {"valid": True}


@router.post("/api/uptimerobot/create-monitor")
async def create_uptimerobot_monitor(
    payload: UptimeRobotCreateMonitorRequest, request: Request
) -> dict:
    result = await uptimerobot_client.create_or_reuse_monitor(
        payload.api_key, payload.render_service_url
    )
    if isinstance(result, uptimerobot_client.UptimeRobotMonitorResult):
        session_id = _get_session_id(request)
        if session_id is not None and session_store.get_session(session_id) is not None:
            session_store.update_frame(
                session_id, "uptime_pinger", {"monitor_id": result.monitor_id}
            )
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
async def create_render_service(payload: RenderServiceCreateRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or "api_key" not in render_frame:
        return {"valid": False, "reason": "no_session"}
    result = await render_client.create_service(
        render_frame["api_key"], payload.repo_url, payload.name
    )
    if isinstance(result, render_client.RenderServiceCreated):
        session_store.update_frame(
            session_id,
            "render",
            {"service_id": result.service_id, "service_url": result.service_url},
        )
        return {"valid": True, "service_id": result.service_id, "service_url": result.service_url}
    if result.message:
        return {"valid": False, "reason": result.reason, "message": result.message}
    return {"valid": False, "reason": result.reason}


def _push_result(result) -> dict:
    if isinstance(result, render_client.RenderEnvVarsPushed):
        return {"valid": True, "pushed": result.pushed}
    return {"valid": False, "reason": result.reason, "pushed": result.pushed}


@router.post("/api/dashboard-auth/confirm")
async def confirm_dashboard_auth(payload: DashboardAuthConfirmRequest, request: Request) -> dict:
    # No external API to validate against -- this value is wizard/visitor-
    # chosen, never checked against anything else. This endpoint's only job
    # is persisting it to the session for the final bulk push.
    session_id = _get_session_id(request)
    if session_id is None or session_store.get_session(session_id) is None:
        return {"valid": False, "reason": "no_session"}
    session_store.update_frame(
        session_id, "dashboard_auth",
        {
            "username": payload.username,
            "password": payload.password,
            "session_secret": payload.session_secret,
        },
    )
    return {"valid": True}


@router.post("/api/render/bulk-push-env-vars")
async def bulk_push_render_env_vars(request: Request) -> dict:
    """Replaces the four now-removed per-frame push-render-vars endpoints:
    assembles every completed frame's env vars from the session and pushes
    them to Render in one call, from the final ("render-deploy") frame --
    per the decision made alongside this redesign, no earlier frame pushes
    to Render incrementally anymore."""
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame:
        return {"valid": False, "reason": "no_session"}

    env_vars: dict[str, str] = {}

    github_app = session_store.read_frame(session_id, "github_app")
    if github_app:
        env_vars["GITHUB_APP_ID"] = str(github_app["app_id"])
        env_vars["GITHUB_APP_PRIVATE_KEY"] = github_app["private_key_b64"]
        env_vars["GITHUB_WEBHOOK_SECRET"] = github_app["webhook_secret"]
        env_vars["GITHUB_APP_INSTALLATION_ID"] = str(github_app["installation_id"])

    supabase = session_store.read_frame(session_id, "supabase")
    if supabase and "database_url" in supabase:
        env_vars["DATABASE_URL"] = supabase["database_url"]

    llm_provider = session_store.read_frame(session_id, "llm_provider")
    if llm_provider:
        credential_var, model_var = _LLM_ENV_VAR_NAMES[llm_provider["provider"]]
        env_vars["LLM_PROVIDER"] = llm_provider["provider"]
        env_vars[credential_var] = llm_provider["credential_value"]
        env_vars[model_var] = llm_provider["model"]

    dashboard_auth = session_store.read_frame(session_id, "dashboard_auth")
    if dashboard_auth:
        env_vars["DASHBOARD_USERNAME"] = dashboard_auth["username"]
        env_vars["DASHBOARD_PASSWORD"] = dashboard_auth["password"]
        env_vars["DASHBOARD_SESSION_SECRET"] = dashboard_auth["session_secret"]

    result = await render_client.push_env_vars(
        render_frame["api_key"], render_frame["service_id"], env_vars
    )
    return _push_result(result)


@router.post("/api/render/trigger-deploy")
async def trigger_render_deploy(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame:
        return {"valid": False, "reason": "no_session"}
    result = await render_client.trigger_deploy(render_frame["api_key"], render_frame["service_id"])
    if isinstance(result, render_client.RenderDeployTriggered):
        session_store.update_frame(session_id, "render", {"pending_deploy_id": result.deploy_id})
        return {"valid": True, "deploy_id": result.deploy_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/deploy-status")
async def get_render_deploy_status(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    required = ("api_key", "service_id", "pending_deploy_id")
    if not render_frame or not all(k in render_frame for k in required):
        return {"valid": False, "reason": "no_session"}
    result = await render_client.poll_deploy_status(
        render_frame["api_key"], render_frame["service_id"], render_frame["pending_deploy_id"]
    )
    if isinstance(result, render_client.RenderDeployStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}
