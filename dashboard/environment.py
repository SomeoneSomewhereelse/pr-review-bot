"""Dashboard Environment tab: fetch/edit Render env vars and runtime_config
overrides. The one place `dashboard/` writes anything -- dashboard/router.py
stays read-only (see dashboard/CLAUDE.md). See docs/superpowers/specs/
2026-09-02-dashboard-environment-tab-design.md.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot import config_deps, github_app, render_client
from bot.config_deps import CREDENTIAL_FAMILIES, credential_slot_vars
from bot.providers import catalog, credentials, registry, vertex_credentials
from bot.providers.registry import slot_env_name
from bot.queue import store

logger = logging.getLogger(__name__)

router = APIRouter()

_LLM_PROVIDER_FAMILIES = ("gemini", "groq", "vertex")


def _build_render_payload() -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"vars": [], "available_key_slots": {p: [] for p in _LLM_PROVIDER_FAMILIES}}
    values = render_client.env_vars(service_id)
    available_key_slots = {
        provider: [
            i for i, var in enumerate(credential_slot_vars(provider)) if var in values
        ]
        for provider in _LLM_PROVIDER_FAMILIES
    }
    return {
        "vars": [
            {"key": key, "value": value, "protected": key in render_client.PROTECTED_ENV_KEYS}
            for key, value in values.items()
        ],
        "available_key_slots": available_key_slots,
    }


@router.get("/api/environment/render")
async def get_render_env_vars() -> JSONResponse:
    payload = await asyncio.to_thread(_build_render_payload)
    return JSONResponse(payload)


class EnvironmentRenderPatch(BaseModel):
    sets: dict[str, str] = {}
    deletes: list[str] = []


_DIRECT_EDIT_VARS = {
    "LLM_MODEL": "gemini",
    "GROQ_MODEL": "groq",
    "VERTEX_MODEL": "vertex",
    "GCP_PROJECT": "vertex",
    "GCP_LOCATION": "vertex",
}


class ValidateVarRequest(BaseModel):
    value: str


def _validate_model_var(provider: str, candidate: str) -> dict:
    if provider == "vertex":
        slot = store.get_all_key_index_overrides().get("vertex", 0)
        info = vertex_credentials.resolve_service_account_info(slot)
        result = catalog.list_vertex_models(info)
    else:
        slot = store.get_all_key_index_overrides().get(provider, 0)
        _, api_key = credentials.resolve(provider, slot)
        if not api_key:
            return {"ok": False, "error": "no_credential_configured", "models": None}
        result = (
            catalog.list_gemini_models(api_key)
            if provider == "gemini"
            else catalog.list_groq_models(api_key)
        )
    if not result.ok:
        return {"ok": False, "error": result.error, "models": None}
    if candidate not in (result.models or []):
        return {"ok": False, "error": "not_in_catalog", "models": result.models}
    return {"ok": True, "error": None, "models": result.models}


def _validate_gcp_var(var: str, candidate: str) -> dict:
    slot = store.get_all_key_index_overrides().get("vertex", 0)
    info = vertex_credentials.resolve_service_account_info(slot)
    kwargs = (
        {"project_override": candidate}
        if var == "GCP_PROJECT"
        else {"location_override": candidate}
    )
    result = catalog.list_vertex_models(info, **kwargs)
    return {"ok": result.ok, "error": result.error, "models": None}


def _validate_var(var: str, candidate: str) -> dict:
    provider = _DIRECT_EDIT_VARS[var]
    if var in ("GCP_PROJECT", "GCP_LOCATION"):
        return _validate_gcp_var(var, candidate)
    return _validate_model_var(provider, candidate)


@router.post("/api/environment/validate/{var}")
async def validate_var(var: str, payload: ValidateVarRequest) -> JSONResponse:
    if var not in _DIRECT_EDIT_VARS:
        raise HTTPException(status_code=404, detail="not a directly-validatable var")
    result = await asyncio.to_thread(_validate_var, var, payload.value)
    return JSONResponse(result)


def _validate_llm_credential(family: str, api_key: str) -> dict:
    result = (
        catalog.list_gemini_models(api_key)
        if family == "gemini"
        else catalog.list_groq_models(api_key)
    )
    return {
        "ok": result.ok,
        "error": result.error,
        "models": result.models,
        "project_id": None,
        "installation_id": None,
        "conflicts": [],
    }


def _validate_vertex_credential(raw_bytes: bytes) -> dict:
    try:
        info = json.loads(raw_bytes.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ok": False,
            "error": "invalid_service_account_json",
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    result = catalog.list_vertex_models(info)
    project_id = info.get("project_id") if result.ok else None
    conflicts: list[dict] = []
    if result.ok and project_id:
        service_id = render_client.find_service_id()
        current_project = (
            render_client.env_vars(service_id).get("GCP_PROJECT") if service_id else None
        )
        conflicts = config_deps.conflicts_for("vertex", project_id, current_project)
    return {
        "ok": result.ok,
        "error": result.error,
        "models": result.models,
        "project_id": project_id,
        "installation_id": None,
        "conflicts": conflicts,
    }


def _validate_github_app_credential(app_id: int, raw_bytes: bytes) -> dict:
    private_key_b64 = base64.b64encode(raw_bytes).decode()
    empty = {
        "ok": False,
        "models": None,
        "project_id": None,
        "installation_id": None,
        "conflicts": [],
    }
    try:
        gh_client = github_app._app_jwt_client_for(app_id, private_key_b64)
        installation_id = github_app.discover_installation_id_for_app(client=gh_client)
    except github_app.AppNotInstalledError:
        return {**empty, "error": "installation_not_found"}
    except RuntimeError as exc:
        error = "multiple_installations" if "multiple installations" in str(exc) else "unauthorized"
        return {**empty, "error": error}
    except ValueError:
        return {**empty, "error": "invalid_key"}
    return {
        "ok": True,
        "error": None,
        "models": None,
        "project_id": None,
        "installation_id": installation_id,
        "conflicts": [],
    }


@router.post("/api/environment/credential/{family}/validate")
async def validate_credential(
    family: str,
    api_key: str | None = Form(None),
    app_id: str | None = Form(None),
    credential_file: UploadFile | None = File(None),
) -> JSONResponse:
    if family not in CREDENTIAL_FAMILIES:
        raise HTTPException(status_code=404, detail="unknown credential family")

    if family in ("gemini", "groq"):
        if not api_key:
            raise HTTPException(status_code=422, detail="api_key is required")
        payload = await asyncio.to_thread(_validate_llm_credential, family, api_key)
    elif family == "vertex":
        if credential_file is None:
            raise HTTPException(status_code=422, detail="credential_file is required")
        raw_bytes = await credential_file.read()
        payload = await asyncio.to_thread(_validate_vertex_credential, raw_bytes)
    else:  # github_app
        if not app_id or credential_file is None:
            raise HTTPException(status_code=422, detail="app_id and credential_file are required")
        raw_bytes = await credential_file.read()
        payload = await asyncio.to_thread(
            _validate_github_app_credential, int(app_id), raw_bytes
        )
    return JSONResponse(payload)


class ApplyLlmCredentialRequest(BaseModel):
    slot: int = 0
    credential: dict[str, str]
    model: str
    clear_gcp_project: bool = False


class ApplyGithubAppRequest(BaseModel):
    app_id: str
    private_key_b64: str
    installation_id: int


def _apply_llm_credential(family: str, payload: ApplyLlmCredentialRequest) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}

    credential_var = slot_env_name(family, payload.slot)
    model_var = CREDENTIAL_FAMILIES[family]["model"]
    credential_value = (
        payload.credential.get("service_account_b64", "")
        if family == "vertex"
        else payload.credential.get("api_key", "")
    )
    applied: list[str] = []
    failed: list[dict] = []
    for key, value in ((credential_var, credential_value), (model_var, payload.model)):
        try:
            render_client.push_env_var(service_id, key, value)
            applied.append(key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
    if family == "vertex" and payload.clear_gcp_project:
        try:
            render_client.delete_env_var(service_id, "GCP_PROJECT")
            applied.append("GCP_PROJECT")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": "GCP_PROJECT", "error": type(exc).__name__})
    if applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}


def _apply_github_app_credential(payload: ApplyGithubAppRequest) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}
    applied: list[str] = []
    failed: list[dict] = []
    for key, value in (
        ("GITHUB_APP_ID", payload.app_id),
        ("GITHUB_APP_PRIVATE_KEY", payload.private_key_b64),
        ("GITHUB_APP_INSTALLATION_ID", str(payload.installation_id)),
    ):
        try:
            render_client.push_env_var(service_id, key, value)
            applied.append(key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
    if applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}


@router.post("/api/environment/credential/{family}/apply")
async def apply_credential(family: str, payload: dict) -> JSONResponse:
    if family not in ("gemini", "groq", "vertex", "github_app"):
        raise HTTPException(status_code=404, detail="unknown credential family")
    if family == "github_app":
        req = ApplyGithubAppRequest.model_validate(payload)
        result = await asyncio.to_thread(_apply_github_app_credential, req)
    else:
        req = ApplyLlmCredentialRequest.model_validate(payload)
        result = await asyncio.to_thread(_apply_llm_credential, family, req)
    return JSONResponse(result)


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
        if key in _DIRECT_EDIT_VARS:
            check = _validate_var(key, value)
            if not check["ok"]:
                failed.append({"key": key, "error": "failed_validation"})
                continue
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


def _resolve_current_credential(provider: str, slot: int | None) -> tuple[bool, str]:
    """Resolve the currently-stored credential for `provider`+`slot`.

    Returns (has_credential, value) -- a raw API key for gemini/groq. Not
    used for vertex, which resolves via vertex_credentials instead.
    """
    if slot is None:
        slot = store.get_all_key_index_overrides().get(provider, 0)
    _, value = credentials.resolve(provider, slot)
    return bool(value), value


def _fetch_models_for_provider(provider: str, slot: int | None) -> dict:
    if provider == "vertex":
        if slot is None:
            slot = store.get_all_key_index_overrides().get("vertex", 0)
        info = vertex_credentials.resolve_service_account_info(slot)
        result = catalog.list_vertex_models(info)
    else:
        has_credential, api_key = _resolve_current_credential(provider, slot)
        if not has_credential:
            return {"ok": False, "models": None, "error": "no_credential_configured"}
        result = (
            catalog.list_gemini_models(api_key)
            if provider == "gemini"
            else catalog.list_groq_models(api_key)
        )
    return {"ok": result.ok, "models": result.models, "error": result.error}


@router.get("/api/environment/credential/{family}/models")
async def get_credential_models(family: str, slot: int | None = None) -> JSONResponse:
    if family not in _LLM_PROVIDER_FAMILIES:
        raise HTTPException(status_code=404, detail="not an LLM provider family")
    payload = await asyncio.to_thread(_fetch_models_for_provider, family, slot)
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


def _cascade_delete(key: str, confirm: bool) -> tuple[int, dict]:
    if key in render_client.PROTECTED_ENV_KEYS:
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": "protected"}],
            "deploy_id": None,
        }

    key_index_overrides = store.get_all_key_index_overrides()
    provider_override = store.get_provider_override()
    dependents = config_deps.dependents_of(
        key, key_index_overrides=key_index_overrides, provider_override=provider_override
    )

    if dependents is not None and dependents.any() and not confirm:
        return 409, {"dependents": dependents.labels()}

    service_id = render_client.find_service_id()
    if service_id is None:
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": "service_not_found"}],
            "deploy_id": None,
        }
    try:
        render_client.delete_env_var(service_id, key)
    except Exception as exc:  # noqa: BLE001
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": type(exc).__name__}],
            "deploy_id": None,
        }

    now = datetime.now(timezone.utc).isoformat()
    if dependents is not None:
        for family in _LLM_PROVIDER_FAMILIES:
            if config_deps.slot_index_for_var(family, key) is None:
                continue
            if dependents.key_index_override:
                store.set_key_index_override(family, None, now)
            if dependents.provider_override:
                store.set_provider_override(None, now)

    deploy_id = None
    try:
        deploy_id = render_client.trigger_deploy(service_id)
    except Exception:  # noqa: BLE001
        logger.exception("environment: failed to trigger deploy after cascade delete of %s", key)
    logger.info("environment: deleted %s (cascade)", key)
    return 200, {"applied": [key], "failed": [], "deploy_id": deploy_id}


@router.delete("/api/environment/render/{key}")
async def delete_render_env_var(key: str, confirm: bool = False) -> JSONResponse:
    status_code, payload = await asyncio.to_thread(_cascade_delete, key, confirm)
    return JSONResponse(payload, status_code=status_code)
