"""Dashboard Environment tab: fetch/edit Render env vars and runtime_config
overrides. The one place `dashboard/` writes anything -- dashboard/router.py
stays read-only (see dashboard/CLAUDE.md). See docs/superpowers/specs/
2026-09-02-dashboard-environment-tab-design.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot import render_client
from bot.providers import registry
from bot.queue import store

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_render_payload() -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"vars": []}
    values = render_client.env_vars(service_id)
    return {
        "vars": [
            {"key": key, "value": value, "protected": key in render_client.PROTECTED_ENV_KEYS}
            for key, value in values.items()
        ]
    }


@router.get("/api/environment/render")
async def get_render_env_vars() -> JSONResponse:
    payload = await asyncio.to_thread(_build_render_payload)
    return JSONResponse(payload)


class EnvironmentRenderPatch(BaseModel):
    sets: dict[str, str] = {}
    deletes: list[str] = []


def _apply_render_patch(payload: EnvironmentRenderPatch) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {
            "applied": [],
            "failed": [{"key": "*", "error": "service_not_found"}],
            "deploy_id": None,
        }

    applied: list[str] = []
    failed: list[dict] = []
    stopped = False

    for key in payload.deletes:
        if stopped:
            break
        if key in render_client.PROTECTED_ENV_KEYS:
            failed.append({"key": key, "error": "protected"})
            continue
        try:
            render_client.delete_env_var(service_id, key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: deleted %s", key)

    for key, value in payload.sets.items():
        if stopped:
            break
        try:
            render_client.push_env_var(service_id, key, value)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: set %s (len %d)", key, len(value))

    deploy_id = None
    if applied:
        try:
            deploy_id = render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after applying %s", applied)

    return {"applied": applied, "failed": failed, "deploy_id": deploy_id}


@router.patch("/api/environment/render")
async def patch_render_env_vars(payload: EnvironmentRenderPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_render_patch, payload)
    return JSONResponse(result)


def _build_config_payload() -> dict:
    base, cap, factor = store.get_cooldown_overrides()
    tokens, reset = store.get_usage_cap_overrides()
    return {
        "provider": store.get_provider_override(),
        "cooldown_base_seconds": base,
        "cooldown_max_seconds": cap,
        "cooldown_factor": factor,
        "usage_cap_tokens": tokens,
        "usage_cap_reset": reset,
        "review_draft_prs": store.get_review_draft_override(),
        "key_index": store.get_all_key_index_overrides(),
        "model": store.get_all_model_overrides(),
    }


@router.get("/api/environment/config")
async def get_environment_config() -> JSONResponse:
    payload = await asyncio.to_thread(_build_config_payload)
    return JSONResponse(payload)


class EnvironmentConfigPatch(BaseModel):
    provider: str | None = None
    cooldown_base_seconds: float | None = None
    cooldown_max_seconds: float | None = None
    cooldown_factor: float | None = None
    usage_cap_tokens: int | None = None
    usage_cap_reset: str | None = None
    review_draft_prs: bool | None = None
    key_index: dict[str, int | None] = {}
    model: dict[str, str | None] = {}


def _apply_config_patch(payload: EnvironmentConfigPatch) -> dict:
    # exclude_unset: a field the caller never sent must not be read as
    # "clear this override" -- only a field explicitly present in the
    # request body (even if its value is null) is applied.
    fields = payload.model_dump(exclude_unset=True)
    now = datetime.now(timezone.utc).isoformat()
    applied: list[str] = []
    failed: list[dict] = []

    if "provider" in fields:
        provider = fields["provider"]
        if provider is not None and provider not in registry.PROVIDERS:
            failed.append({"key": "provider", "error": "unknown_provider"})
        else:
            try:
                store.set_provider_override(provider, now)
                applied.append("provider")
            except Exception as exc:  # noqa: BLE001
                failed.append({"key": "provider", "error": type(exc).__name__})

    cooldown_keys = ("cooldown_base_seconds", "cooldown_max_seconds", "cooldown_factor")
    cooldown_fields = {k: fields[k] for k in cooldown_keys if k in fields}
    if cooldown_fields:
        try:
            current_base, current_cap, current_factor = store.get_cooldown_overrides()
            base = cooldown_fields.get("cooldown_base_seconds", current_base)
            cap = cooldown_fields.get("cooldown_max_seconds", current_cap)
            factor = cooldown_fields.get("cooldown_factor", current_factor)
            store.set_cooldown_override(base, cap, factor, now)
            applied.extend(cooldown_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in cooldown_fields)

    usage_keys = ("usage_cap_tokens", "usage_cap_reset")
    usage_fields = {k: fields[k] for k in usage_keys if k in fields}
    if usage_fields:
        try:
            current_tokens, current_reset = store.get_usage_cap_overrides()
            tokens = usage_fields.get("usage_cap_tokens", current_tokens)
            reset = usage_fields.get("usage_cap_reset", current_reset)
            store.set_usage_cap_override(tokens, reset, now)
            applied.extend(usage_fields.keys())
        except Exception as exc:  # noqa: BLE001
            failed.extend({"key": k, "error": type(exc).__name__} for k in usage_fields)

    if "review_draft_prs" in fields:
        try:
            store.set_review_draft_override(fields["review_draft_prs"], now)
            applied.append("review_draft_prs")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": "review_draft_prs", "error": type(exc).__name__})

    for provider, index in fields.get("key_index", {}).items():
        if provider not in registry.PROVIDERS:
            failed.append({"key": f"key_index.{provider}", "error": "unknown_provider"})
            continue
        try:
            store.set_key_index_override(provider, index, now)
            applied.append(f"key_index.{provider}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": f"key_index.{provider}", "error": type(exc).__name__})

    for provider, model in fields.get("model", {}).items():
        if provider not in registry.PROVIDERS:
            failed.append({"key": f"model.{provider}", "error": "unknown_provider"})
            continue
        try:
            store.set_model_override(provider, model, now)
            applied.append(f"model.{provider}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": f"model.{provider}", "error": type(exc).__name__})

    return {"applied": applied, "failed": failed}


@router.patch("/api/environment/config")
async def patch_environment_config(payload: EnvironmentConfigPatch) -> JSONResponse:
    result = await asyncio.to_thread(_apply_config_patch, payload)
    return JSONResponse(result)
