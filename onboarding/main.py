"""onboarding/ — self-service setup wizard: a separate service from the
review engine in app/. Stateless relay only — no database, no session
store. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md.
"""
from __future__ import annotations

import contextlib

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from onboarding.config import settings
from onboarding.router import router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.supabase_oauth_client_id:
        raise RuntimeError(
            "SUPABASE_OAUTH_CLIENT_ID is unset — refusing to start. Frame 3's "
            "Supabase OAuth flow needs this service's registered OAuth app id; "
            "without it the authorize redirect cannot work."
        )
    if not settings.supabase_oauth_client_secret:
        raise RuntimeError(
            "SUPABASE_OAUTH_CLIENT_SECRET is unset — refusing to start. Frame "
            "3's Supabase OAuth token exchange needs this service's registered "
            "OAuth app secret; without it the exchange cannot work."
        )
    yield


app = FastAPI(title="onboarding-wizard", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "invalid request"})


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
