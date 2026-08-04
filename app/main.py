import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import FastAPI

from app.queue import dispatcher, store
from app.webhook import router as webhook_router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
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


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
