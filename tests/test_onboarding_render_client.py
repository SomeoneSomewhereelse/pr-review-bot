"""Tests for onboarding/render_client.py — Render key validation never logs
or returns the raw key, and distinguishes an invalid key from Render being
unreachable (design doc sections 6 and 8)."""
from __future__ import annotations

import httpx
import respx

from onboarding import render_client

SENTINEL_KEY = "rnd_SENTINEL_DO_NOT_LOG_9f3a"
OWNERS_URL = f"{render_client.RENDER_API_BASE}/owners"


async def test_valid_key_returns_owner_name():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "owner": {
                            "id": "usr-1",
                            "name": "Ada Lovelace",
                            "email": "ada@example.com",
                            "type": "user",
                        },
                        "cursor": "abc",
                    }
                ],
            )
        )
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyValid(owner_name="Ada Lovelace")


async def test_unauthorized_key_is_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="invalid_key")


async def test_forbidden_key_is_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(403, json={"message": "nope"}))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="invalid_key")


async def test_render_5xx_is_unreachable_not_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(500))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="render_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="render_unreachable")


async def test_empty_owners_list_is_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="invalid_key")
