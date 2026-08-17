import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from app import github_app
from app.config import settings
from app.dashboard import router as dashboard_router
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
    if not settings.github_webhook_secret:
        raise RuntimeError(
            "GITHUB_WEBHOOK_SECRET is unset -- refusing to start "
            "(an empty secret would accept any webhook signature)."
        )
    if not settings.github_app_installation_id:
        # Not set (e.g. on Render, per docs/superpowers/specs/.../design.md
        # §6: the installation id "becomes optional (auto-discovered)").
        # Resolve it once via the App JWT before anything tries to use it --
        # app-level (not repo-scoped), so this works whether or not
        # GITHUB_TARGET_REPO is configured (see docs/superpowers/specs/
        # 2026-08-17-multi-repo-support-design.md). A genuine RuntimeError
        # here (App not installed, or installed on more than one account) is
        # allowed to propagate and fail startup loudly -- same pattern as
        # init_pool() failing loudly on an unreachable Postgres.
        settings.github_app_installation_id = await asyncio.to_thread(
            github_app.discover_installation_id_for_app
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
app.include_router(webhook_router)
app.include_router(dashboard_router)


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
