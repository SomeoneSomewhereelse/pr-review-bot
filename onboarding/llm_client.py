"""Thin async wrapper around Gemini, Vertex AI, and Groq's model-listing
calls — used to validate a visitor-supplied credential and discover which
models it can actually reach, without persisting anything server-side.
Gemini and Vertex share one internal helper since both go through the same
google-genai SDK, differing only in how genai.Client is constructed; Groq
uses the official groq SDK directly. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4."""
from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import json

import groq
import httpx
from google import genai
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import requests as google_auth_requests
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from google.oauth2 import service_account

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_VERTEX_LOCATION = "us-central1"
_REQUEST_TIMEOUT_MS = 10_000

# The only values from a visitor-supplied service-account JSON that
# google.oauth2.service_account.Credentials uses to pick the destination of
# its own outbound token-refresh request -- token_uri is required by
# from_service_account_info, universe_domain is optional. Left unpinned, a
# visitor who supplies a matching self-generated private key (they always
# can, since they built the JSON themselves) can fully control where this
# server issues that request: an SSRF via a "paste your service-account key"
# feature. Anything other than Google's real values is rejected outright.
_VERTEX_TOKEN_URI = "https://oauth2.googleapis.com/token"
_VERTEX_UNIVERSE_DOMAIN = "googleapis.com"


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
    are resource-name formats google-genai returns; bot/config.py's
    llm_model/vertex_model fields expect the bare id."""
    return name.rsplit("/", 1)[-1]


async def _list_generative_models(client: genai.Client) -> list[str]:
    """Filtered to generateContent-capable models where that capability is
    actually known. Gemini Developer API responses populate
    Model.supported_actions (google/genai/models.py's _Model_from_mldev);
    Vertex responses never do -- _Model_from_vertex has no
    supported_actions mapping at all, verified directly against the
    installed SDK's source, not assumed. A Vertex model therefore always
    has supported_actions=None and is let through rather than dropped --
    dropping it silently empties the entire Vertex catalog regardless of
    credential, which is exactly the bug this check exists to avoid."""
    names = []
    async for model in await client.aio.models.list():
        if not model.name:
            continue
        if model.supported_actions is not None and "generateContent" not in model.supported_actions:
            continue
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
    client = genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
    )
    try:
        models = await _list_generative_models(client)
    except genai_errors.APIError as exc:
        return LlmApiFailed(reason=_reason_for_client_error_code(exc.code))
    except httpx.HTTPError:
        return LlmApiFailed(reason="provider_unreachable")
    finally:
        await client.aio.aclose()
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

    # SSRF guard -- see the module-level comment on _VERTEX_TOKEN_URI. Must
    # run before from_service_account_info() below, which is what actually
    # reads these fields off info and wires them into the credential object.
    submitted_token_uri = info.get("token_uri")
    if submitted_token_uri is not None and submitted_token_uri != _VERTEX_TOKEN_URI:
        return LlmApiFailed(reason="invalid_service_account_json")
    submitted_universe_domain = info.get("universe_domain")
    if submitted_universe_domain not in (None, _VERTEX_UNIVERSE_DOMAIN):
        return LlmApiFailed(reason="invalid_service_account_json")

    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=_VERTEX_SCOPES)
    except ValueError:
        # google.auth.exceptions.MalformedError subclasses ValueError, so
        # this one clause already covers it.
        return LlmApiFailed(reason="invalid_service_account_json")

    client = genai.Client(
        vertexai=True,
        project=project_id,
        location=_VERTEX_LOCATION,
        credentials=creds,
        http_options=genai_types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
    )
    try:
        # google-auth's Credentials.refresh() is synchronous (requests-based
        # transport, per this module's own docs/superpowers spec section 6)
        # -- refreshing it here, off the event loop via asyncio.to_thread,
        # means client.aio.models.list() below finds an already-valid token
        # and skips its own internal, un-thread-wrapped synchronous refresh,
        # which would otherwise block this single-process server's event
        # loop for every other concurrent visitor's request for the
        # round-trip's duration. Same reasoning as github_client.py already
        # wrapping its own blocking PyGithub calls in asyncio.to_thread.
        await asyncio.to_thread(creds.refresh, google_auth_requests.Request())
        models = await _list_generative_models(client)
    except genai_errors.APIError as exc:
        return LlmApiFailed(reason=_reason_for_client_error_code(exc.code))
    except google_auth_exceptions.RefreshError:
        # A bad/unauthorized credential fails to refresh its token.
        return LlmApiFailed(reason="unauthorized")
    except google_auth_exceptions.GoogleAuthError:
        # Any other google-auth failure (e.g. a transport error during
        # token refresh) is a connectivity problem, not a bad credential.
        return LlmApiFailed(reason="provider_unreachable")
    except httpx.HTTPError:
        return LlmApiFailed(reason="provider_unreachable")
    finally:
        await client.aio.aclose()
    return VertexModelsListed(project_id=project_id, models=models)


async def list_groq_models(api_key: str) -> LlmModelsListed | LlmApiFailed:
    """Live models-listing call against Groq's OpenAI-compatible API —
    doubles as credential validation. Deliberately unfiltered (spec
    section 2): Groq's Model type carries no capability field to
    distinguish chat-completion models from Whisper/TTS/moderation ones.
    max_retries=0 matches bot/providers/groq.py's documented "no hidden
    retry layer" convention — root CLAUDE.md counsels stopping on a
    403/429 rather than silently retrying, which the SDK's default
    max_retries=2 would otherwise do behind this function's back. Never
    logs api_key."""
    try:
        async with groq.AsyncGroq(api_key=api_key, max_retries=0, timeout=10.0) as client:
            response = await client.models.list()
    except groq.AuthenticationError:
        return LlmApiFailed(reason="unauthorized")
    except groq.PermissionDeniedError:
        return LlmApiFailed(reason="forbidden")
    except groq.RateLimitError:
        return LlmApiFailed(reason="rate_limited")
    except (groq.InternalServerError, groq.APIConnectionError, groq.APITimeoutError):
        return LlmApiFailed(reason="provider_unreachable")
    return LlmModelsListed(models=[m.id for m in response.data])
