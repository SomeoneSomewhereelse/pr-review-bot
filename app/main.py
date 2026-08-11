import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import FastAPI

from app import github_app
from app.config import settings
from app.dashboard import router as dashboard_router
from app.queue import dispatcher, store
from app.webhook import router as webhook_router


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
        # Resolve it once via the App JWT before anything tries to use it.
        # A genuine RuntimeError here (App not installed) is allowed to
        # propagate and fail startup loudly -- same pattern as init_pool()
        # failing loudly on an unreachable Postgres.
        settings.github_app_installation_id = await asyncio.to_thread(
            github_app.discover_installation_id, settings.github_target_repo
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
