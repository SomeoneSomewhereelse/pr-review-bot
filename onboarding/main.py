"""onboarding/ — self-service setup wizard: a separate service from the
review engine in app/. Stateless relay only — no database, no session
store. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md.
"""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="onboarding-wizard")


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
