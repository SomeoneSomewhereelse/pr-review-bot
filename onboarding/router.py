"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md
section 5.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from onboarding import render_client

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


class RenderKeyRequest(BaseModel):
    api_key: str


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@router.post("/api/render/validate-key")
async def validate_render_key(payload: RenderKeyRequest) -> dict:
    result = await render_client.validate_key(payload.api_key)
    if isinstance(result, render_client.RenderKeyValid):
        return {"valid": True, "owner_name": result.owner_name}
    return {"valid": False, "reason": result.reason}
