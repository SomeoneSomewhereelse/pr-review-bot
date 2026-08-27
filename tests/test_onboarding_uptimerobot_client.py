"""Tests for onboarding/uptimerobot_client.py — monitor creation checks for
an existing monitor before creating a duplicate, the /healthz path is
derived server-side (never trusted from the caller), and every error maps
to a fixed reason vocabulary, never a raw exception message. See
docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
sections 2, 3, 6."""
from __future__ import annotations

import httpx
import respx

from onboarding import uptimerobot_client

SENTINEL_KEY = "ur_SENTINEL_DO_NOT_LOG_9f3a"
MONITORS_URL = f"{uptimerobot_client.UPTIMEROBOT_API_BASE}/monitors"
RENDER_URL = "https://sentinel-service.onrender.com"
TARGET_URL = "https://sentinel-service.onrender.com/healthz"


def _monitor(url, monitor_id=1):
    return {
        "id": monitor_id, "friendlyName": url, "url": url, "type": "HTTP",
        "interval": 300, "timeout": 30, "status": "STARTED",
    }


async def test_creates_monitor_when_none_exists():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True)
    sent_body = create_route.calls.last.request.content
    assert TARGET_URL.encode() in sent_body


async def test_reuses_existing_monitor_without_creating():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [_monitor(TARGET_URL)]})
        )
        # No POST route registered on purpose: respx.mock raises if an
        # unmocked call is attempted, so this also proves create was never
        # called, not just that the right value came back.
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=False)


async def test_dedupe_check_ignores_a_monitor_on_a_different_url():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [_monitor("https://someone-elses-service.onrender.com/healthz")]})
        )
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True)
    assert create_route.called


async def test_strips_trailing_slash_before_appending_healthz():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL + "/")
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True)
    sent_body = create_route.calls.last.request.content
    assert TARGET_URL.encode() in sent_body
    assert b"//healthz" not in sent_body


async def test_unauthorized_key_is_reported():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")


async def test_rate_limited_is_reported():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(429, json={"message": "rate limited"}))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="rate_limited")


async def test_server_error_is_unreachable_not_invalid():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(500))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_network_error_is_unreachable():
    with respx.mock:
        respx.get(MONITORS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_unexpected_client_error_on_create_is_request_rejected():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        respx.post(MONITORS_URL).mock(return_value=httpx.Response(400, json={"message": "bad"}))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="request_rejected")


async def test_malformed_list_body_is_unreachable_not_a_crash():
    """A non-JSON 200 body from the list call must not let a traceback
    (which could carry request/response context) escape the function."""
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_api_key_never_appears_in_the_returned_result():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert SENTINEL_KEY not in repr(result)
