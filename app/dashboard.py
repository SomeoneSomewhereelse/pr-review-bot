"""Ops/demo dashboard: GET /api/dashboard (JSON) backing GET /dashboard's
static page. Knows nothing about LLM providers or GitHub — only reads
app.queue.store and app.queue.dispatcher, same separation formatting.py
keeps from the LLM layer.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.queue import dispatcher, store

logger = logging.getLogger(__name__)

router = APIRouter()

_REVIEWS_LIMIT = 50
# Kept local rather than imported from app/providers/factory.py: factory.py
# has no shared constant for this list, and the dashboard has no other
# reason to depend on it.
_KNOWN_PROVIDERS = ("gemini", "groq", "github_models")


def build_dashboard_payload() -> dict:
    """Assemble the /api/dashboard JSON body. Each section degrades to
    {"error": "data unavailable"} independently on failure."""
    payload: dict = {}

    try:
        payload["stats"] = store.dashboard_stats()
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: failed to load stats")
        payload["stats"] = {"error": "data unavailable"}

    backoff_raw = dispatcher.backoff_status()
    backoff = {provider: backoff_raw.get(provider) for provider in _KNOWN_PROVIDERS}
    try:
        by_status = store.dashboard_queue_counts()
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: failed to load queue counts")
        by_status = {"error": "data unavailable"}
    payload["queue"] = {"by_status": by_status, "backoff": backoff}

    try:
        payload["reviews"] = store.dashboard_reviews(limit=_REVIEWS_LIMIT)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: failed to load reviews")
        payload["reviews"] = {"error": "data unavailable"}

    return payload


@router.get("/api/dashboard")
async def api_dashboard() -> JSONResponse:
    payload = await asyncio.to_thread(build_dashboard_payload)
    return JSONResponse(payload)
