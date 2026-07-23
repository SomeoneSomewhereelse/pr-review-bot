from fastapi import FastAPI

from app.webhook import router as webhook_router

app = FastAPI(title="pr-review-engine")
app.include_router(webhook_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
