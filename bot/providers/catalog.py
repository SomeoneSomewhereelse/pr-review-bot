"""Live model-catalog listing per LLM provider, for the dashboard's guided
credential setup/replace flow and its per-provider model picker.

Reimplemented independently of onboarding/llm_client.py (structurally
similar, deliberately not imported -- bot/ may become its own repo). Each
function makes exactly one deliberate live listing call, synchronously
(matching bot/render_client.py's and bot/github_app.py's sync style, so
dashboard/environment.py can wrap these in asyncio.to_thread like every
other write path it already has).

See docs/superpowers/specs/2026-09-03-dashboard-env-credential-guardrails-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from google import genai
from google.genai import types
from google.oauth2 import service_account
from groq import Groq

from bot.config import settings

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_LIST_TIMEOUT_MS = 10_000


@dataclass
class CatalogResult:
    ok: bool
    models: list[str] | None
    error: str | None


def _classify_status(status: int | None) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    return "provider_unreachable"


def _classify_exception(exc: Exception) -> str:
    # Duck-typed on purpose: rather than depend on each SDK's own exception
    # class hierarchy (google-genai's and groq's differ, and either could
    # change shape across versions), read whichever HTTP-status-shaped
    # attribute is present. Every real SDK error we've seen carries one of
    # these two names.
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return _classify_status(status)


def _list_generative_models(client: genai.Client) -> list[str]:
    names: list[str] = []
    for model in client.models.list():
        name = model.name or ""
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        actions = getattr(model, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        names.append(name)
    return names


def list_gemini_models(api_key: str) -> CatalogResult:
    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS)
        )
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001 -- classified into a structural error below
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=models, error=None)


def list_groq_models(api_key: str) -> CatalogResult:
    try:
        client = Groq(api_key=api_key, max_retries=0, timeout=10.0)
        response = client.models.list()
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=[m.id for m in response.data], error=None)


def list_vertex_models(
    service_account_info: dict | None,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    project = (
        project_override
        or settings.gcp_project
        or (service_account_info or {}).get("project_id", "")
    )
    if not project:
        return CatalogResult(ok=False, models=None, error="invalid_service_account_json")
    location = location_override or settings.gcp_location

    creds = None
    if service_account_info is not None:
        try:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=_VERTEX_SCOPES
            )
        except Exception:  # noqa: BLE001 -- malformed key content, not an HTTP failure
            return CatalogResult(ok=False, models=None, error="invalid_service_account_json")

    try:
        client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS),
        )
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=models, error=None)
