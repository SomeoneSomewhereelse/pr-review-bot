"""Shared Render API access for scripts/deploy.py and scripts/set_provider.py.

Not a CLI entry point -- support code for the scripts/ CLIs. Consolidates
what was previously duplicated Render-fetch logic (service lookup, env-var
fetch) across the two scripts; see
docs/superpowers/specs/2026-08-10-render-access-consolidation-design.md.
"""

from __future__ import annotations

import httpx

from app.config import settings

RENDER_API = "https://api.render.com/v1"
HTTP_TIMEOUT = 10.0


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def find_service_id() -> str | None:
    resp = httpx.get(f"{RENDER_API}/services", headers=headers(), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    for item in resp.json():
        service = unwrap(item, "service")
        if service.get("name") == settings.render_service_name:
            return service.get("id")
    return None


def env_vars(service_id: str) -> dict[str, str]:
    """The service's live env-vars, key -> value.

    Callers must reduce a returned value to a boolean or an equality result
    immediately -- never store it beyond that computation, print it, or pass
    it to anything that might log it. See CLAUDE.md's "no secret is ever
    logged" and docs/superpowers/specs/
    2026-08-10-provider-live-credential-verification-design.md section 6.
    """
    resp = httpx.get(
        f"{RENDER_API}/services/{service_id}/env-vars",
        headers=headers(),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    current: dict[str, str] = {}
    for item in resp.json():
        env_var = unwrap(item, "envVar")
        current[env_var.get("key")] = env_var.get("value")
    return current
