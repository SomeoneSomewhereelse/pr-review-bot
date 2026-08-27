"""Thin async wrapper around UptimeRobot's v3 REST API — creates (or
reuses) the keep-warm monitor for a deployed Render service. See
docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
sections 2-3.

Uses v3 (Bearer auth, JSON), not the legacy v2 form-API
scripts/deploy.py's existing read-only check_uptime_pinger already uses:
v2's POST /newMonitor was verified live to reject monitor creation on a
free-plan account (403 "not allowed to use some settings with your
current plan"); v3 was verified live to accept it on the same account
(spec section 1)."""
from __future__ import annotations

import dataclasses

import httpx

UPTIMEROBOT_API_BASE = "https://api.uptimerobot.com/v3"
_MONITOR_INTERVAL_SECONDS = 300
_MONITOR_TIMEOUT_SECONDS = 30


@dataclasses.dataclass(frozen=True)
class UptimeRobotMonitorResult:
    created: bool


@dataclasses.dataclass(frozen=True)
class UptimeRobotApiFailed:
    reason: str  # "unauthorized" | "rate_limited" | "provider_unreachable" | "request_rejected"


UptimeRobotOutcome = UptimeRobotMonitorResult | UptimeRobotApiFailed


def _target_url(render_service_url: str) -> str:
    return f"{render_service_url.strip().rstrip('/')}/healthz"


def _failure_for_status(status_code: int) -> UptimeRobotApiFailed | None:
    if status_code in (200, 201):
        return None
    if status_code == 401:
        return UptimeRobotApiFailed(reason="unauthorized")
    if status_code == 429:
        return UptimeRobotApiFailed(reason="rate_limited")
    if status_code >= 500:
        return UptimeRobotApiFailed(reason="provider_unreachable")
    return UptimeRobotApiFailed(reason="request_rejected")


async def create_or_reuse_monitor(api_key: str, render_service_url: str) -> UptimeRobotOutcome:
    """Never logs or returns api_key. Lists existing monitors first (GET)
    and only creates one (POST) if none already watches the derived target
    URL -- prevents a duplicate monitor from a repeated "Change" resubmit
    (spec section 2)."""
    target_url = _target_url(render_service_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(base_url=UPTIMEROBOT_API_BASE, timeout=10.0) as client:
            list_resp = await client.get("/monitors", headers=headers, params={"limit": 50})
            list_failure = _failure_for_status(list_resp.status_code)
            if list_failure is not None:
                return list_failure
            try:
                monitors = list_resp.json().get("data") or []
            except (ValueError, AttributeError, TypeError):
                return UptimeRobotApiFailed(reason="provider_unreachable")
            if any(m.get("url") == target_url for m in monitors):
                return UptimeRobotMonitorResult(created=False)

            create_resp = await client.post(
                "/monitors",
                headers=headers,
                json={
                    "friendlyName": target_url,
                    "url": target_url,
                    "type": "HTTP",
                    "interval": _MONITOR_INTERVAL_SECONDS,
                    "timeout": _MONITOR_TIMEOUT_SECONDS,
                },
            )
    except httpx.HTTPError:
        return UptimeRobotApiFailed(reason="provider_unreachable")

    create_failure = _failure_for_status(create_resp.status_code)
    if create_failure is not None:
        return create_failure
    return UptimeRobotMonitorResult(created=True)
