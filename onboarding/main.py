"""onboarding/ — self-service setup wizard: a separate service from the
review engine in app/. Stateless relay only — no database, no session
store. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from onboarding.router import router

app = FastAPI(title="onboarding-wizard")
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "invalid request"})


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
