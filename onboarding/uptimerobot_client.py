"""Thin async wrapper around UptimeRobot's v3 REST API — creates (or
reuses) the keep-warm monitor for a deployed Render service. See
docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
sections 2-3.

Uses v3 (Bearer auth, JSON), not the legacy v2 form-API
bot/scripts/deploy.py's existing read-only check_uptime_pinger already uses:
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
_LIST_PAGE_SIZE = 200  # v3's documented max for the `limit` query param
# Defensive cap on how many pages the dedupe scan will follow via
# nextLink -- not expected to be hit for a real account (200/page already
# covers thousands of monitors); guards against a malformed or looping
# nextLink rather than any realistic account size.
_MAX_LIST_PAGES = 20


@dataclasses.dataclass(frozen=True)
class UptimeRobotMonitorResult:
    created: bool
    monitor_id: int


@dataclasses.dataclass(frozen=True)
class UptimeRobotMonitorDeleted:
    pass


@dataclasses.dataclass(frozen=True)
class UptimeRobotApiFailed:
    reason: str  # "unauthorized" | "rate_limited" | "provider_unreachable" | "request_rejected"


UptimeRobotOutcome = UptimeRobotMonitorResult | UptimeRobotApiFailed
UptimeRobotDeleteOutcome = UptimeRobotMonitorDeleted | UptimeRobotApiFailed


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


async def _find_existing_monitor_id(
    client: httpx.AsyncClient, headers: dict, target_url: str
) -> tuple[int | None, UptimeRobotApiFailed | None]:
    """Pages through GET /monitors via v3's cursor-based `nextLink` until it
    finds a monitor watching target_url, or runs out of pages. A single
    page (the old behavior) only covers an account's first 50-200 monitors
    -- past that, dedupe silently misses a real match and a resubmit
    creates an orphaned duplicate, which is exactly what dedupe-before-
    create exists to prevent (onboarding/CLAUDE.md's "load-bearing, not an
    optimization" note)."""
    url, params = "/monitors", {"limit": _LIST_PAGE_SIZE}
    for _ in range(_MAX_LIST_PAGES):
        resp = await client.get(url, headers=headers, params=params)
        failure = _failure_for_status(resp.status_code)
        if failure is not None:
            return None, failure
        # Stays INSIDE this try: `data` holding non-dict entries (or being a
        # dict, whose iteration yields str keys) makes `m.get` an
        # AttributeError, which is not an httpx.HTTPError and would
        # otherwise escape as a 500 -- breaking this module's contract that
        # every failure maps to a fixed reason and no exception message
        # ever gets out.
        try:
            body = resp.json()
            monitors = body.get("data") or []
            existing = next((m for m in monitors if m.get("url") == target_url), None)
            next_link = body.get("nextLink")
        except (ValueError, AttributeError, TypeError):
            return None, UptimeRobotApiFailed(reason="provider_unreachable")
        if existing is not None:
            monitor_id = existing.get("id")
            if not isinstance(monitor_id, int):
                return None, UptimeRobotApiFailed(reason="provider_unreachable")
            return monitor_id, None
        if not next_link:
            return None, None
        # nextLink is a ready-to-fetch URL (relative or absolute); httpx
        # resolves either correctly against the client's base_url, so no
        # params are re-sent -- they're already encoded into the link.
        url, params = next_link, None
    return None, None


async def create_or_reuse_monitor(api_key: str, render_service_url: str) -> UptimeRobotOutcome:
    """Never logs or returns api_key. Lists existing monitors first (GET,
    paginated) and only creates one (POST) if none already watches the
    derived target URL -- prevents a duplicate monitor from a repeated
    "Change" resubmit (spec section 2)."""
    target_url = _target_url(render_service_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(base_url=UPTIMEROBOT_API_BASE, timeout=10.0) as client:
            existing_id, list_failure = await _find_existing_monitor_id(client, headers, target_url)
            if list_failure is not None:
                return list_failure
            if existing_id is not None:
                return UptimeRobotMonitorResult(created=False, monitor_id=existing_id)

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
    try:
        created_id = create_resp.json().get("id")
    except (ValueError, AttributeError, TypeError):
        return UptimeRobotApiFailed(reason="provider_unreachable")
    if not isinstance(created_id, int):
        return UptimeRobotApiFailed(reason="provider_unreachable")
    return UptimeRobotMonitorResult(created=True, monitor_id=created_id)


async def delete_monitor(api_key: str, monitor_id: int) -> UptimeRobotDeleteOutcome:
    """Never logs or returns api_key. Best-effort cleanup call -- callers
    treat any UptimeRobotApiFailed here as non-fatal, the same way
    onboarding/static/index.html's push-and-clear helpers treat a Render
    push failure: attempted is good enough, nothing in this flow blocks on
    it succeeding."""
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(base_url=UPTIMEROBOT_API_BASE, timeout=10.0) as client:
            resp = await client.delete(f"/monitors/{monitor_id}", headers=headers)
    except httpx.HTTPError:
        return UptimeRobotApiFailed(reason="provider_unreachable")
    failure = _failure_for_status(resp.status_code)
    if failure is not None:
        return failure
    return UptimeRobotMonitorDeleted()
