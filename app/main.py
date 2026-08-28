import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app import github_app
from app.auth import SessionRequired, require_session
from app.auth import router as auth_router
from app.config import settings
from app.dashboard import router as dashboard_router
from app.providers import registry
from app.queue import dispatcher, store
from app.webhook import router as webhook_router

# The root logger defaults to WARNING when nothing configures it, so every
# module's logging.getLogger(__name__).info(...) call (app/webhook.py,
# app/orchestrator.py, app/dashboard.py, app/queue/dispatcher.py) was
# silently unreachable in production -- confirmed live via Render's Logs API
# returning no match for a line known to have fired (ISSUES.md 2026-08-17).
# uvicorn's own --log-level flag does NOT fix this: it only configures
# loggers named "uvicorn"/"uvicorn.access"/"uvicorn.error" with
# propagate=False, never the root logger this app's own loggers propagate to.
# force=True is load-bearing, not decoration: basicConfig() is a silent no-op
# if the root logger already has a handler (confirmed under pytest, whose own
# logging plugin attaches one before this module ever imports) -- the exact
# "silently does nothing" failure mode this fix exists to eliminate, just
# from a different cause. Safe in production too: root has no handler there
# until this runs, so force=True removes nothing that wasn't already ours.
logging.basicConfig(level=logging.INFO, force=True)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.llm_provider not in registry.PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER={settings.llm_provider!r} is not a supported provider "
            f"-- refusing to start. Set it in .env.config to one of: "
            f"{', '.join(sorted(registry.PROVIDERS))}."
        )
    if not settings.github_webhook_secret:
        raise RuntimeError(
            "GITHUB_WEBHOOK_SECRET is unset -- refusing to start "
            "(an empty secret would accept any webhook signature)."
        )
    if not settings.github_app_installation_id:
        # GITHUB_APP_INSTALLATION_ID must always be configured explicitly,
        # never guessed on the operator's behalf -- refuse to start rather
        # than auto-discover one, so a stale/missing value is never silently
        # papered over (ISSUES.md 2026-08-21).
        raise RuntimeError(
            "GITHUB_APP_INSTALLATION_ID is unset -- refusing to start. This project "
            "requires it to be configured explicitly; run scripts/deploy.py's "
            "github-app check (or check the GitHub UI) to find the App's current "
            "installation id."
        )
    if (
        not settings.dashboard_username
        or not settings.dashboard_password
        or not settings.dashboard_session_secret
    ):
        raise RuntimeError(
            "DASHBOARD_USERNAME, DASHBOARD_PASSWORD, and DASHBOARD_SESSION_SECRET must "
            "all be set -- refusing to start (an empty credential would let any "
            "username/password pair, or any forged session token, through)."
        )
    # Verified on every boot, not just when unset: a pinned value is exactly
    # as broken as a missing one if the App was uninstalled and reinstalled
    # since it was last set (GitHub assigns a new id), and app-level (not
    # repo-scoped) verification works whether or not GITHUB_TARGET_REPO is
    # configured (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md).
    # A genuine RuntimeError here (not installed, installed on more than one
    # account, or a pinned/actual mismatch) is allowed to propagate and fail
    # startup loudly -- same pattern as init_pool() failing loudly on an
    # unreachable Postgres.
    settings.github_app_installation_id = await asyncio.to_thread(
        github_app.discover_and_verify_installation_id, settings.github_app_installation_id
    )
    store.init_pool()
    store.recover_on_startup(datetime.now(timezone.utc).isoformat())
    task = asyncio.create_task(dispatcher.run_forever())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.close_pool()


app = FastAPI(title="pr-review-engine", lifespan=lifespan)


@app.exception_handler(SessionRequired)
async def _handle_session_required(request: Request, exc: SessionRequired) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"valid": False, "reason": "unauthenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router, dependencies=[Depends(require_session)])


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
