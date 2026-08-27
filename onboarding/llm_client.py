"""Thin async wrapper around Gemini and Vertex AI's model-listing calls —
used to validate a visitor-supplied credential and discover which models it
can actually reach, without persisting anything server-side. Gemini and
Vertex share one internal helper since both go through the same
google-genai SDK, differing only in how genai.Client is constructed. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4."""
from __future__ import annotations

import base64
import binascii
import dataclasses
import json

import httpx
from google import genai
from google.auth import exceptions as google_auth_exceptions
from google.genai import errors as genai_errors
from google.oauth2 import service_account

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_VERTEX_LOCATION = "us-central1"


@dataclasses.dataclass(frozen=True)
class LlmModelsListed:
    models: list[str]


@dataclasses.dataclass(frozen=True)
class VertexModelsListed:
    project_id: str
    models: list[str]


@dataclasses.dataclass(frozen=True)
class LlmApiFailed:
    reason: str
    # "unauthorized" | "forbidden" | "rate_limited" | "provider_unreachable"
    # | "invalid_service_account_json" (vertex only)


def _strip_model_prefix(name: str) -> str:
    """"models/gemini-flash-latest" -> "gemini-flash-latest";
    "publishers/google/models/gemini-2.5-flash" -> "gemini-2.5-flash" — both
    are resource-name formats google-genai returns; app/config.py's
    llm_model/vertex_model fields expect the bare id."""
    return name.rsplit("/", 1)[-1]


async def _list_generative_models(client: genai.Client) -> list[str]:
    """Filtered to generateContent-capable models only — the SDK call
    app/providers/google_genai.py actually makes (spec section 2)."""
    names = []
    async for model in await client.aio.models.list():
        if model.name and model.supported_actions and "generateContent" in model.supported_actions:
            names.append(_strip_model_prefix(model.name))
    return names


def _reason_for_client_error_code(code: int) -> str:
    if code == 401:
        return "unauthorized"
    if code == 403:
        return "forbidden"
    if code == 429:
        return "rate_limited"
    return "provider_unreachable"


async def list_gemini_models(api_key: str) -> LlmModelsListed | LlmApiFailed:
    """Live models-listing call against the Gemini Developer API (AI
    Studio) — doubles as credential validation. Never logs api_key."""
    client = genai.Client(api_key=api_key)
    try:
        models = await _list_generative_models(client)
    except genai_errors.ClientError as exc:
        return LlmApiFailed(reason=_reason_for_client_error_code(exc.code))
    except genai_errors.ServerError:
        return LlmApiFailed(reason="provider_unreachable")
    except httpx.HTTPError:
        return LlmApiFailed(reason="provider_unreachable")
    return LlmModelsListed(models=models)


async def list_vertex_models(service_account_key_b64: str) -> VertexModelsListed | LlmApiFailed:
    """Live models-listing call against Vertex AI, authenticated as the
    submitted GCP service account. Location is fixed to us-central1 (spec
    section 2); project is read from the key's own project_id field. Never
    logs the decoded key or its contents."""
    try:
        decoded = base64.b64decode(service_account_key_b64, validate=True)
        info = json.loads(decoded)
        project_id = str(info["project_id"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        return LlmApiFailed(reason="invalid_service_account_json")

    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=_VERTEX_SCOPES)
    except (google_auth_exceptions.MalformedError, ValueError):
        return LlmApiFailed(reason="invalid_service_account_json")

    client = genai.Client(vertexai=True, project=project_id, location=_VERTEX_LOCATION, credentials=creds)
    try:
        models = await _list_generative_models(client)
    except genai_errors.ClientError as exc:
        return LlmApiFailed(reason=_reason_for_client_error_code(exc.code))
    except genai_errors.ServerError:
        return LlmApiFailed(reason="provider_unreachable")
    except google_auth_exceptions.GoogleAuthError:
        return LlmApiFailed(reason="unauthorized")
    except httpx.HTTPError:
        return LlmApiFailed(reason="provider_unreachable")
    return VertexModelsListed(project_id=project_id, models=models)
