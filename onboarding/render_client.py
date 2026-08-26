"""Thin async wrapper around Render's REST API — validates that a
visitor-supplied API key is live, without persisting it anywhere. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md
sections 5-6.
"""
from __future__ import annotations

import dataclasses

import httpx

RENDER_API_BASE = "https://api.render.com/v1"


@dataclasses.dataclass(frozen=True)
class RenderKeyValid:
    owner_name: str


@dataclasses.dataclass(frozen=True)
class RenderKeyInvalid:
    reason: str  # "invalid_key" | "render_unreachable"


RenderValidation = RenderKeyValid | RenderKeyInvalid


async def validate_key(api_key: str) -> RenderValidation:
    """One cheap read call (GET /owners) to confirm api_key is a live Render
    API key. Never logs or returns the key itself — only a boolean verdict
    and, on success, the display name of the account it belongs to."""
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=10.0) as client:
            response = await client.get(
                "/owners",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return RenderKeyInvalid(reason="render_unreachable")

    if response.status_code in (401, 403):
        return RenderKeyInvalid(reason="invalid_key")
    if response.status_code != 200:
        return RenderKeyInvalid(reason="render_unreachable")

    body = response.json()
    if not body:
        return RenderKeyInvalid(reason="invalid_key")
    return RenderKeyValid(owner_name=body[0]["owner"]["name"])
