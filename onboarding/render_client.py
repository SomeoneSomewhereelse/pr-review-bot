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

    try:
        body = response.json()
        if not body:
            return RenderKeyInvalid(reason="invalid_key")
        owner_name = body[0]["owner"]["name"]
    except (ValueError, KeyError, IndexError, TypeError):
        return RenderKeyInvalid(reason="render_unreachable")
    return RenderKeyValid(owner_name=owner_name)


# Mirrors bot/scripts/deploy.py's own status-bucket sets VERBATIM -- onboarding/
# never imports from bot/scripts/ or bot/ (onboarding/CLAUDE.md's no-shared-
# credential-path rule), so this is a deliberate, paired-comment copy, not
# a shared import. Keep in sync with bot/scripts/deploy.py's
# _DEPLOY_IN_FLIGHT_STATUSES / _DEPLOY_FAILED_STATUSES if either changes.
_DEPLOY_IN_FLIGHT_STATUSES = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "deactivated",
}


@dataclasses.dataclass(frozen=True)
class RenderServiceCreated:
    service_id: str
    service_url: str


@dataclasses.dataclass(frozen=True)
class RenderServiceCreationFailed:
    reason: str  # "invalid_key" | "render_unreachable" | "request_rejected"
    message: str = ""


RenderServiceCreation = RenderServiceCreated | RenderServiceCreationFailed


async def _resolve_owner_id(client: httpx.AsyncClient, api_key: str) -> httpx.Response:
    """GET /owners, the same call validate_key() already makes. Returns the
    raw response -- create_service() is responsible for checking its status
    code before parsing the body, same split as validate_key()'s own."""
    return await client.get(
        "/owners",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )


async def create_service(api_key: str, repo_url: str, name: str) -> RenderServiceCreation:
    """Create a free-plan Docker web service from repo_url, with every env
    var left blank -- the visitor's later frames fill them in via
    push_env_vars(). Mirrors bot/Dockerfile's own build shape
    (buildFilter/healthCheckPath match render.yaml's conventions; the
    dockerfilePath points at bot/Dockerfile specifically, since this
    project's own render.yaml builds onboarding/Dockerfile instead -- see
    the 2026-08-29 project-restructure design spec). Never derives the returned
    URL from `name`: Render may normalize it server-side, and a live call
    confirmed the create response carries no `service.url` field at all --
    the URL is built from the response's own `service.slug`.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=30.0) as client:
            owners_response = await _resolve_owner_id(client, api_key)
            if owners_response.status_code in (401, 403):
                return RenderServiceCreationFailed(reason="invalid_key")
            if owners_response.status_code != 200:
                return RenderServiceCreationFailed(reason="render_unreachable")
            try:
                owners_body = owners_response.json()
                if not owners_body:
                    return RenderServiceCreationFailed(reason="invalid_key")
                owner_id = owners_body[0]["owner"]["id"]
            except (ValueError, KeyError, IndexError, TypeError):
                return RenderServiceCreationFailed(reason="render_unreachable")
            response = await client.post(
                "/services",
                headers=headers,
                json={
                    "type": "web_service",
                    "name": name,
                    "ownerId": owner_id,
                    "repo": repo_url,
                    "autoDeploy": "yes",
                    "buildFilter": {"ignoredPaths": ["**/*.md"]},
                    "serviceDetails": {
                        "runtime": "docker",
                        "plan": "free",
                        "healthCheckPath": "/healthz",
                        "envSpecificDetails": {"dockerfilePath": "./bot/Dockerfile"},
                    },
                },
            )
    except httpx.HTTPError:
        return RenderServiceCreationFailed(reason="render_unreachable")

    if response.status_code in (401, 403):
        return RenderServiceCreationFailed(reason="invalid_key")
    if response.status_code >= 500:
        return RenderServiceCreationFailed(reason="render_unreachable")
    if response.status_code not in (200, 201):
        message = ""
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            pass
        return RenderServiceCreationFailed(reason="request_rejected", message=message)

    try:
        body = response.json()
        service = body.get("service") or body
        service_id = service["id"]
        slug = service["slug"]
    except (ValueError, KeyError, TypeError):
        return RenderServiceCreationFailed(reason="render_unreachable")
    return RenderServiceCreated(service_id=service_id, service_url=f"https://{slug}.onrender.com")


@dataclasses.dataclass(frozen=True)
class RenderEnvVarsPushed:
    pushed: list[str]


@dataclasses.dataclass(frozen=True)
class RenderEnvVarsPushFailed:
    reason: str  # "invalid_key" | "service_not_found" | "render_unreachable"
    pushed: list[str]


RenderEnvVarsPush = RenderEnvVarsPushed | RenderEnvVarsPushFailed


async def push_env_vars(api_key: str, service_id: str, values: dict[str, str]) -> RenderEnvVarsPush:
    """Push every (key, value) in `values`, one PUT per key -- never the
    bulk PUT /env-vars endpoint, which replaces the service's whole env-var
    list (same reasoning bot/scripts/deploy.py::sync_env() already documents).
    Stops at the first failure and reports which keys succeeded before it;
    dict iteration order controls push order.
    """
    pushed: list[str] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=15.0) as client:
            for key, value in values.items():
                response = await client.put(
                    f"/services/{service_id}/env-vars/{key}",
                    headers=headers,
                    json={"value": value},
                )
                if response.status_code in (401, 403):
                    return RenderEnvVarsPushFailed(reason="invalid_key", pushed=pushed)
                if response.status_code == 404:
                    return RenderEnvVarsPushFailed(reason="service_not_found", pushed=pushed)
                if response.status_code not in (200, 201):
                    return RenderEnvVarsPushFailed(reason="render_unreachable", pushed=pushed)
                pushed.append(key)
    except httpx.HTTPError:
        return RenderEnvVarsPushFailed(reason="render_unreachable", pushed=pushed)
    return RenderEnvVarsPushed(pushed=pushed)


@dataclasses.dataclass(frozen=True)
class RenderDeployTriggered:
    deploy_id: str


@dataclasses.dataclass(frozen=True)
class RenderDeployTriggerFailed:
    reason: str  # "invalid_key" | "service_not_found" | "render_unreachable"


RenderDeployTrigger = RenderDeployTriggered | RenderDeployTriggerFailed


async def trigger_deploy(api_key: str, service_id: str) -> RenderDeployTrigger:
    """POST an empty-body deploy trigger. Pushing env vars does not
    auto-deploy (bot/scripts/deploy.py::_trigger_and_wait's own docstring), so
    this is what actually makes the pushed values take effect."""
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=15.0) as client:
            response = await client.post(
                f"/services/{service_id}/deploys",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                json={},
            )
    except httpx.HTTPError:
        return RenderDeployTriggerFailed(reason="render_unreachable")
    if response.status_code in (401, 403):
        return RenderDeployTriggerFailed(reason="invalid_key")
    if response.status_code == 404:
        return RenderDeployTriggerFailed(reason="service_not_found")
    if response.status_code not in (200, 201):
        return RenderDeployTriggerFailed(reason="render_unreachable")
    try:
        body = response.json()
        deploy = body.get("deploy") or body
        deploy_id = deploy["id"]
    except (ValueError, KeyError, TypeError):
        return RenderDeployTriggerFailed(reason="render_unreachable")
    return RenderDeployTriggered(deploy_id=deploy_id)


@dataclasses.dataclass(frozen=True)
class RenderDeployStatus:
    status: str  # "in_progress" | "live" | "canceled" | "failed"


@dataclasses.dataclass(frozen=True)
class RenderDeployStatusFailed:
    reason: str  # "invalid_key" | "service_not_found" | "render_unreachable"


RenderDeployStatusResult = RenderDeployStatus | RenderDeployStatusFailed


async def poll_deploy_status(
    api_key: str, service_id: str, deploy_id: str
) -> RenderDeployStatusResult:
    """One status check -- never loops or blocks itself. The browser's own
    polling loop (onboarding/static/index.html) calls the router endpoint
    wrapping this repeatedly; see design spec section 5c step 11."""
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=15.0) as client:
            response = await client.get(
                f"/services/{service_id}/deploys/{deploy_id}",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return RenderDeployStatusFailed(reason="render_unreachable")
    if response.status_code in (401, 403):
        return RenderDeployStatusFailed(reason="invalid_key")
    if response.status_code == 404:
        return RenderDeployStatusFailed(reason="service_not_found")
    if response.status_code != 200:
        return RenderDeployStatusFailed(reason="render_unreachable")
    try:
        body = response.json()
        deploy = body.get("deploy") or body
        raw_status = deploy["status"]
    except (ValueError, KeyError, TypeError):
        return RenderDeployStatusFailed(reason="render_unreachable")
    if raw_status == "live":
        return RenderDeployStatus(status="live")
    if raw_status == "canceled":
        return RenderDeployStatus(status="canceled")
    if raw_status in _DEPLOY_FAILED_STATUSES:
        return RenderDeployStatus(status="failed")
    if raw_status in _DEPLOY_IN_FLIGHT_STATUSES:
        return RenderDeployStatus(status="in_progress")
    # A status this project has not catalogued at all is reported as still
    # in progress too -- never guess "failed" for an unrecognized value and
    # stop the visitor's poll loop early.
    return RenderDeployStatus(status="in_progress")
