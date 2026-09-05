"""onboarding/ — self-service setup wizard: a separate service from the
review engine in bot/. Wizard progress lives in a server-side session (a
dedicated Postgres, see session_store.py) — replaces this service's earlier
stateless-relay design, found fragile against mobile browsers destroying
sessionStorage mid-flow. See
docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md.
"""
from __future__ import annotations

import contextlib

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from onboarding import session_store
from onboarding.config import settings
from onboarding.router import router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is unset — refusing to start. The wizard's "
            "server-side session store needs its own dedicated Postgres "
            "connection string; see onboarding/.env.example."
        )
    if not settings.onboarding_session_encryption_key:
        raise RuntimeError(
            "ONBOARDING_SESSION_ENCRYPTION_KEY is unset — refusing to "
            "start. Needed to encrypt every credential value the session "
            "store writes; see onboarding/.env.example."
        )
    try:
        Fernet(settings.onboarding_session_encryption_key.encode("ascii"))
    except Exception:
        # A plain, hand-written RuntimeError -- not a re-raise/chain of the
        # underlying cryptography exception, and never includes the key
        # itself. config.py's field docstring explains why a pydantic-level
        # ValidationError was rejected for this exact check (it echoes
        # input_value regardless of message).
        raise RuntimeError(
            "ONBOARDING_SESSION_ENCRYPTION_KEY is not a valid Fernet key — "
            "refusing to start. Generate one with: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key()"
            '.decode())"'
        ) from None
    session_store.init_pool()
    yield
    session_store.close_pool()


app = FastAPI(title="onboarding-wizard", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "invalid request"})


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
