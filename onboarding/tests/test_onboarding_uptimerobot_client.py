"""Tests for onboarding/uptimerobot_client.py — monitor creation checks for
an existing monitor before creating a duplicate, the /healthz path is
derived server-side (never trusted from the caller), and every error maps
to a fixed reason vocabulary, never a raw exception message. See
docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
sections 2, 3, 6."""
from __future__ import annotations

import json

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


def _sent_json(route):
    """Decode a captured request body instead of substring-matching its raw
    bytes. A raw-bytes check would silently be asserting httpx's JSON
    separator style (0.28 emits compact `,`/`:`; 0.27, still allowed by
    pyproject's `httpx>=0.27`, emits `, `/`: `) rather than this module's
    own behaviour."""
    return json.loads(route.calls.last.request.content)


async def test_creates_monitor_when_none_exists():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=1)
    sent = _sent_json(create_route)
    assert sent["url"] == TARGET_URL
    assert sent["friendlyName"] == TARGET_URL


async def test_reuses_existing_monitor_without_creating():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [_monitor(TARGET_URL, monitor_id=42)]})
        )
        # No POST route registered on purpose: respx.mock raises if an
        # unmocked call is attempted, so this also proves create was never
        # called, not just that the right value came back.
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=False, monitor_id=42)


async def test_dedupe_check_ignores_a_monitor_on_a_different_url():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [_monitor("https://someone-elses-service.onrender.com/healthz")]})
        )
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=1)
    assert create_route.called


async def test_dedupe_check_follows_nextlink_past_the_first_page():
    """A match past the first page must not be missed -- a single-page scan
    (the old behavior) would fall through to creating a duplicate for any
    account with more monitors than fit in one page."""
    # A distinct path, not a query string on the same path -- next_link is
    # treated as an opaque URL in the implementation, and using a different
    # path here sidesteps respx's loose (query-agnostic) URL matching,
    # which would otherwise let the page-1 route "win" for a page-2 request
    # too (both are the same path) regardless of ordering.
    page_2_url = f"{MONITORS_URL}-page-2"
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        _monitor("https://someone-elses-service.onrender.com/healthz", monitor_id=1)
                    ],
                    "nextLink": page_2_url,
                },
            )
        )
        respx.get(page_2_url).mock(
            return_value=httpx.Response(
                200, json={"data": [_monitor(TARGET_URL, monitor_id=99)], "nextLink": None}
            )
        )
        # No POST route registered on purpose -- proves create was never
        # reached once the match was found on page 2.
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=False, monitor_id=99)


async def test_dedupe_check_gives_up_after_the_page_cap_without_looping_forever():
    """A nextLink that never terminates (malformed, or a pathological
    provider response) must not hang or loop indefinitely -- it should stop
    after the defensive page cap and fall through to creating a monitor."""
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [], "nextLink": MONITORS_URL})
        )
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=1)
    assert create_route.called


async def test_strips_trailing_slash_before_appending_healthz():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL + "/")
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=1)
    # Exact equality, not a substring/`"//healthz" not in` check: only an
    # exact match rules out a doubled slash *and* a stray suffix at once.
    assert _sent_json(create_route)["url"] == TARGET_URL


async def test_strips_trailing_whitespace_before_deriving_target_url():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL + " \n")
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True, monitor_id=1)
    # The old form of this assertion was `b" " not in sent_body`, which only
    # held because httpx 0.28 serialises JSON with compact separators -- it
    # was testing httpx, not this module. Exact field equality carries the
    # same guarantee (no surviving whitespace) version-independently.
    sent = _sent_json(create_route)
    assert sent["url"] == TARGET_URL
    assert sent["friendlyName"] == TARGET_URL


async def test_unauthorized_key_is_reported():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")


async def test_rate_limited_is_reported():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(429, json={"message": "rate limited"})
        )
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


async def test_non_dict_entry_in_the_data_array_is_unreachable_not_a_crash():
    """The dedupe scan calls m.get("url") on every entry. A non-dict entry
    makes that an AttributeError, which is NOT an httpx.HTTPError -- before
    the guard, it escaped uncaught and became a 500 with a traceback instead
    of this module's documented reason vocabulary."""
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": ["not-a-monitor", None]})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_data_object_instead_of_array_is_unreachable_not_a_crash():
    """Same AttributeError shape by a different route: iterating a dict
    yields its str keys, so every "monitor" is a string."""
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": {"monitors": []}})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_missing_id_in_create_response_is_unreachable_not_a_crash():
    """A create response with no usable id (missing/wrong type) must not be
    reported as success -- the caller (uptimerobot_client's own delete path,
    and the frame's own orphan-cleanup logic) needs a real id to ever act
    on this monitor again."""
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        respx.post(MONITORS_URL).mock(return_value=httpx.Response(201, json={"url": TARGET_URL}))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_missing_id_on_an_existing_match_is_unreachable_not_a_crash():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"url": TARGET_URL}]})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_api_key_never_appears_in_the_returned_result():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert SENTINEL_KEY not in repr(result)


async def test_delete_monitor_success():
    with respx.mock:
        respx.delete(f"{MONITORS_URL}/42").mock(return_value=httpx.Response(200, json={}))
        result = await uptimerobot_client.delete_monitor(SENTINEL_KEY, 42)
    assert result == uptimerobot_client.UptimeRobotMonitorDeleted()


async def test_delete_monitor_sends_bearer_token():
    with respx.mock:
        route = respx.delete(f"{MONITORS_URL}/42").mock(return_value=httpx.Response(200, json={}))
        await uptimerobot_client.delete_monitor(SENTINEL_KEY, 42)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SENTINEL_KEY}"


async def test_delete_monitor_unauthorized_is_reported():
    with respx.mock:
        respx.delete(f"{MONITORS_URL}/42").mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.delete_monitor(SENTINEL_KEY, 42)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")


async def test_delete_monitor_not_found_is_request_rejected_not_a_crash():
    """A monitor already gone (e.g. deleted twice, or manually removed) is a
    4xx the module maps like any other unexpected client error -- this
    cleanup call is best-effort, so callers never treat it as fatal either
    way."""
    with respx.mock:
        respx.delete(f"{MONITORS_URL}/42").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        result = await uptimerobot_client.delete_monitor(SENTINEL_KEY, 42)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="request_rejected")


async def test_delete_monitor_network_error_is_unreachable():
    with respx.mock:
        respx.delete(f"{MONITORS_URL}/42").mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await uptimerobot_client.delete_monitor(SENTINEL_KEY, 42)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_delete_monitor_api_key_never_appears_in_the_returned_result():
    with respx.mock:
        respx.delete(f"{MONITORS_URL}/42").mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.delete_monitor(SENTINEL_KEY, 42)
    assert SENTINEL_KEY not in repr(result)
