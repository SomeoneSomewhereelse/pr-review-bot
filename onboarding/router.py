"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given.
"""

from __future__ import annotations

import asyncio
import secrets as _secrets
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


# session_store.py's functions are sync (real Postgres calls) -- every
# endpoint below awaits them through these thin asyncio.to_thread wrappers
# rather than calling them directly, so a request's DB round-trips never
# block the event loop for every other concurrent request. Looked up via
# `session_store.<name>` at call time (not captured at import time), so
# these stay correct under tests that monkeypatch the module's functions.
async def _create_session() -> str:
    return await asyncio.to_thread(session_store.create_session)


async def _get_session(session_id: str) -> session_store.SessionData | None:
    return await asyncio.to_thread(session_store.get_session, session_id)


async def _read_frame(session_id: str, frame: str) -> dict | None:
    return await asyncio.to_thread(session_store.read_frame, session_id, frame)


async def _update_frame(
    session_id: str, frame: str, data: dict, *, replace: bool = False
) -> session_store.SessionNotFound | None:
    return await asyncio.to_thread(
        session_store.update_frame, session_id, frame, data, replace=replace
    )


async def _delete_session(session_id: str) -> None:
    await asyncio.to_thread(session_store.delete_session, session_id)


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


class SupabaseKeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class SupabaseCreateProjectRequest(BaseModel):
    organization_slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)


class LlmGeminiListModelsRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class LlmGroqListModelsRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class LlmVertexListModelsRequest(BaseModel):
    service_account_key_b64: str = Field(min_length=1, max_length=16384)


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
        honest 422. No shape/regex check beyond strip-then-require-non-empty,
        since this value is written by the wizard itself (sub-project 6's
        forward contract), not typed by the visitor.
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


# Paired comment with bot/providers/registry.py::PROVIDERS -- onboarding/
# never imports from bot/ (onboarding/CLAUDE.md's no-shared-credential-
# path rule), so this 3-entry mapping is a deliberate copy, not a shared
# import. Keep in sync if a provider's env var names ever change there.
_LLM_ENV_VAR_NAMES = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL"),
}

# bot/config.py's OPERATIONAL_KEYS tuning knobs that bot/scripts/deploy.py's
# --sync-env pushes as Render env vars (its _GENERIC_OPERATIONAL_ENV_ATTRS),
# with their bot/config.py Settings field defaults hardcoded here -- same
# duplication-not-import pattern as _LLM_ENV_VAR_NAMES above (onboarding/
# never imports bot/, per onboarding/CLAUDE.md). Render's API rejects an
# empty env-var value outright (ISSUES.md 2026-08-17), so GCP_PROJECT and
# GITHUB_TARGET_REPO -- the two of these twelve keys whose Settings default
# is genuinely blank -- are deliberately excluded rather than pushed as "":
# an operator who wants either set can still do so after the fact (Render
# dashboard, or bot/scripts/deploy.py --sync-env once .env.config names a repo).
# Keep these in sync with bot/config.py's actual field defaults by hand --
# there is no automated check tying the two together.
_GENERIC_OPERATIONAL_ENV_DEFAULTS = {
    "GCP_LOCATION": "us-central1",
    "LLM_REQUEST_TIMEOUT_SECONDS": "45.0",
    "DISPATCHER_IDLE_SLEEP_SECONDS": "1.0",
    "DEFAULT_RETRY_AFTER_SECONDS": "60.0",
    "DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS": "2.0",
    "DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS": "300.0",
    "DISPATCHER_MAX_FAILURE_ATTEMPTS": "5",
    "DISPATCHER_MAX_NOTICE_POST_ATTEMPTS": "3",
    "DISPATCHER_MIN_RETRY_AFTER_SECONDS": "1.0",
    "DISPATCHER_BACKOFF_JITTER_SECONDS": "0.0",
    "DISPATCHER_NOTICE_SWEEP_BATCH_SIZE": "20",
}


def _render_index() -> HTMLResponse:
    # No operator-level Supabase secret to template -- the frame's
    # credential is a visitor-pasted Personal Access Token now.
    return HTMLResponse(
        _INDEX_HTML,
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
    session = (await _get_session(session_id))
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
            "complete": True,
            # service_id isn't a credential (an identifier for the
            # visitor's own Render service, like installation_id is for
            # GitHub) -- the frontend's local render-service mirror needs
            # it to gate later actions (trigger-deploy, bulk-push) the same
            # way it does right after an in-page completion.
            "display": {
                "service_id": render.get("service_id"),
                "service_url": render.get("service_url"),
            },
        }

    if data.get("dashboard_auth"):
        frames["dashboard-auth"] = {"complete": True, "display": {}}

    if data.get("github_app"):
        frames["github-app"] = {"complete": True, "display": {}}

    supabase = data.get("supabase")
    if supabase and "database_url" in supabase:
        frames["supabase"] = {"complete": True, "display": {"name": supabase.get("name")}}
    elif supabase and "ref" in supabase:
        # Project created, but connection-info hasn't run yet (e.g. a
        # reload during the ~2 minute provisioning wait) -- resume
        # polling, don't report complete without a DATABASE_URL for the
        # final deploy step to find.
        frames["supabase"] = {
            "complete": False,
            "provisioning": True,
            "display": {"ref": supabase.get("ref"), "name": supabase.get("name")},
        }
    # A key validated but no project created yet reports as not present in
    # frames at all -- the same gap the Render frame already leaves between
    # key validation and service creation. There's no redirect round-trip
    # to resume from anymore, so no separate "authorized" in-between state
    # is needed here.

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
        (await _delete_session(session_id))
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
        if session_id is None or (await _get_session(session_id)) is None:
            session_id = (await _create_session())
            _set_session_cookie(response, session_id)
        # replace=True: a resubmitted Render key (via the "Change" flow)
        # must discard any previous service_id/service_url/
        # pending_deploy_id -- those belong to whatever Render account the
        # OLD key authenticated as, and may not even exist under the new
        # one. A plain merge would leave them behind for GET /api/session
        # to report as still complete.
        write_result = await _update_frame(
            session_id,
            "render",
            {"api_key": payload.api_key, "owner_name": result.owner_name},
            replace=True,
        )
        if isinstance(write_result, session_store.SessionNotFound):
            # The session was created microseconds ago -- only reachable if
            # something else (a concurrent reset) deleted it in that gap.
            # An unpersisted "success" isn't real (design spec section 3.6).
            return {"valid": False, "reason": "no_session"}
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
        if session_id is not None and (await _get_session(session_id)) is not None:
            await _update_frame(
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


@router.post("/api/supabase/validate-key")
async def validate_supabase_key(payload: SupabaseKeyRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or (await _get_session(session_id)) is None:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.validate_key(payload.key)
    if isinstance(result, supabase_client.SupabaseKeyValid):
        # replace=True: a resubmitted key (via "Change") must discard any
        # previous ref/db_pass/database_url outright -- see
        # test_validate_supabase_key_discards_a_previous_projects_data.
        write_result = await _update_frame(
            session_id, "supabase", {"api_key": payload.key}, replace=True
        )
        if isinstance(write_result, session_store.SessionNotFound):
            return {"valid": False, "reason": "no_session"}
        return {"valid": True, "orgs": [{"slug": o.slug, "name": o.name} for o in result.orgs]}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/create-project")
async def create_supabase_project(payload: SupabaseCreateProjectRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and (await _read_frame(session_id, "supabase"))
    if not supabase_frame or "api_key" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    db_pass = _secrets.token_urlsafe(24)
    result = await supabase_client.create_project(
        supabase_frame["api_key"], payload.organization_slug, payload.name, db_pass
    )
    if isinstance(result, supabase_client.SupabaseProjectCreated):
        write_result = await _update_frame(
            session_id,
            "supabase",
            {
                "ref": result.ref,
                "status": result.status,
                "db_pass": db_pass,
                "organization_slug": payload.organization_slug,
                "name": payload.name,
            },
        )
        if isinstance(write_result, session_store.SessionNotFound):
            # The project WAS created in Supabase -- this only leaves the
            # session unable to find it again (ref/db_pass lost), not the
            # visitor's Supabase account in a bad state. Reporting failure
            # here is about this wizard's own bookkeeping, not Supabase's.
            return {"valid": False, "reason": "no_session"}
        return {"valid": True, "ref": result.ref, "status": result.status, "name": payload.name}
    if isinstance(result, supabase_client.SupabaseProjectRejected):
        return {"valid": False, "reason": "project_creation_rejected", "message": result.message}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/project-status")
async def get_supabase_project_status(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and (await _read_frame(session_id, "supabase"))
    if not supabase_frame or "api_key" not in supabase_frame or "ref" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.get_project_status(
        supabase_frame["api_key"], supabase_frame["ref"]
    )
    if isinstance(result, supabase_client.SupabaseProjectStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/connection-info")
async def get_supabase_connection_info(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and (await _read_frame(session_id, "supabase"))
    required = ("api_key", "ref", "db_pass")
    if not supabase_frame or not all(k in supabase_frame for k in required):
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.get_connection_info(
        supabase_frame["api_key"], supabase_frame["ref"], session_id=session_id
    )
    if isinstance(result, supabase_client.SupabaseConnectionInfo):
        database_url = (
            f"postgresql://{result.db_user}:{supabase_frame['db_pass']}"
            f"@{result.db_host}:{result.db_port}/{result.db_name}"
        )
        write_result = await _update_frame(session_id, "supabase", {"database_url": database_url})
        if isinstance(write_result, session_store.SessionNotFound):
            return {"valid": False, "reason": "no_session"}
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
    if session_id is None or (await _get_session(session_id)) is None:
        return {"valid": False, "reason": "no_session"}
    write_result = await _update_frame(
        session_id,
        "llm_provider",
        {
            "provider": payload.provider,
            "credential_value": payload.credential_value,
            "model": payload.model,
        },
    )
    if isinstance(write_result, session_store.SessionNotFound):
        return {"valid": False, "reason": "no_session"}
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
        if session_id is not None and (await _get_session(session_id)) is not None:
            await _update_frame(
                session_id,
                "uptime_pinger",
                {"api_key": payload.api_key, "monitor_id": result.monitor_id},
            )
        return {"valid": True, "created": result.created, "monitor_id": result.monitor_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/uptimerobot/delete-monitor")
async def delete_uptimerobot_monitor(request: Request) -> dict:
    """Best-effort cleanup, called when an earlier frame change (render-key
    or render-service) invalidates a monitor a visitor already created for
    the old service URL -- see onboarding/static/index.html's
    cleanupOrphanedUptimeMonitor(). Session-backed like every other
    post-redesign endpoint -- the UptimeRobot API key is never resent from
    the browser."""
    session_id = _get_session_id(request)
    uptime_frame = session_id and (await _read_frame(session_id, "uptime_pinger"))
    if not uptime_frame or "api_key" not in uptime_frame or "monitor_id" not in uptime_frame:
        return {"valid": False, "reason": "no_session"}
    result = await uptimerobot_client.delete_monitor(
        uptime_frame["api_key"], uptime_frame["monitor_id"]
    )
    if isinstance(result, uptimerobot_client.UptimeRobotMonitorDeleted):
        return {"valid": True}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/create-service")
async def create_render_service(payload: RenderServiceCreateRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and (await _read_frame(session_id, "render"))
    if not render_frame or "api_key" not in render_frame:
        return {"valid": False, "reason": "no_session"}
    result = await render_client.create_service(
        render_frame["api_key"], payload.repo_url, payload.name
    )
    if isinstance(result, render_client.RenderServiceCreated):
        write_result = await _update_frame(
            session_id,
            "render",
            {"service_id": result.service_id, "service_url": result.service_url},
        )
        if isinstance(write_result, session_store.SessionNotFound):
            # The Render service WAS created -- this only leaves the
            # session unable to find it again, not a dangling external
            # resource the visitor doesn't know about (its URL/id are in
            # this response either way).
            return {"valid": False, "reason": "no_session"}
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
    if session_id is None or (await _get_session(session_id)) is None:
        return {"valid": False, "reason": "no_session"}
    write_result = await _update_frame(
        session_id, "dashboard_auth",
        {
            "username": payload.username,
            "password": payload.password,
            "session_secret": payload.session_secret,
        },
    )
    if isinstance(write_result, session_store.SessionNotFound):
        return {"valid": False, "reason": "no_session"}
    return {"valid": True}


@router.post("/api/render/bulk-push-env-vars")
async def bulk_push_render_env_vars(request: Request) -> dict:
    """Replaces the four now-removed per-frame push-render-vars endpoints:
    assembles every completed frame's env vars from the session and pushes
    them to Render in one call, from the final ("render-deploy") frame --
    per the decision made alongside this redesign, no earlier frame pushes
    to Render incrementally anymore."""
    session_id = _get_session_id(request)
    render_frame = session_id and (await _read_frame(session_id, "render"))
    if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame:
        return {"valid": False, "reason": "no_session"}

    env_vars: dict[str, str] = {}

    # The deployed bot service needs its own RENDER_API_KEY to power the
    # dashboard's Environment tab (docs/superpowers/specs/
    # 2026-09-02-dashboard-environment-tab-design.md) -- previously this
    # credential never left the visitor's browser/onboarding session.
    # render_frame["api_key"] is guaranteed present by this function's own
    # guard clause above.
    env_vars["RENDER_API_KEY"] = render_frame["api_key"]

    github_app = (await _read_frame(session_id, "github_app"))
    if github_app:
        env_vars["GITHUB_APP_ID"] = str(github_app["app_id"])
        env_vars["GITHUB_APP_PRIVATE_KEY"] = github_app["private_key_b64"]
        env_vars["GITHUB_WEBHOOK_SECRET"] = github_app["webhook_secret"]
        env_vars["GITHUB_APP_INSTALLATION_ID"] = str(github_app["installation_id"])

    supabase = (await _read_frame(session_id, "supabase"))
    if supabase and "database_url" in supabase:
        env_vars["DATABASE_URL"] = supabase["database_url"]

    llm_provider = (await _read_frame(session_id, "llm_provider"))
    if llm_provider:
        credential_var, model_var = _LLM_ENV_VAR_NAMES[llm_provider["provider"]]
        env_vars["LLM_PROVIDER"] = llm_provider["provider"]
        env_vars[credential_var] = llm_provider["credential_value"]
        env_vars[model_var] = llm_provider["model"]

    dashboard_auth = (await _read_frame(session_id, "dashboard_auth"))
    if dashboard_auth:
        env_vars["DASHBOARD_USERNAME"] = dashboard_auth["username"]
        env_vars["DASHBOARD_PASSWORD"] = dashboard_auth["password"]
        env_vars["DASHBOARD_SESSION_SECRET"] = dashboard_auth["session_secret"]

    # Always included, not gated on any frame: these are tuning defaults,
    # not visitor-submitted credentials -- see _GENERIC_OPERATIONAL_ENV_DEFAULTS.
    env_vars.update(_GENERIC_OPERATIONAL_ENV_DEFAULTS)

    result = await render_client.push_env_vars(
        render_frame["api_key"], render_frame["service_id"], env_vars
    )
    return _push_result(result)


@router.post("/api/render/trigger-deploy")
async def trigger_render_deploy(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and (await _read_frame(session_id, "render"))
    if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame:
        return {"valid": False, "reason": "no_session"}
    result = await render_client.trigger_deploy(render_frame["api_key"], render_frame["service_id"])
    if isinstance(result, render_client.RenderDeployTriggered):
        # Deliberately does NOT report failure on a lost write here, unlike
        # every other endpoint in this file: the deploy has already been
        # triggered as a real, non-idempotent external side effect (unlike
        # every check `bulk_push_render_env_vars`/earlier steps make, which
        # are cheap to retry) -- reporting failure would invite the visitor
        # to retry and trigger a second deploy. `deploy_id` is still
        # returned either way; a lost write here only costs the ability to
        # resume polling after a reload, not correctness.
        await _update_frame(session_id, "render", {"pending_deploy_id": result.deploy_id})
        return {"valid": True, "deploy_id": result.deploy_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/deploy-status")
async def get_render_deploy_status(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and (await _read_frame(session_id, "render"))
    required = ("api_key", "service_id", "pending_deploy_id")
    if not render_frame or not all(k in render_frame for k in required):
        return {"valid": False, "reason": "no_session"}
    result = await render_client.poll_deploy_status(
        render_frame["api_key"], render_frame["service_id"], render_frame["pending_deploy_id"]
    )
    if isinstance(result, render_client.RenderDeployStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}
