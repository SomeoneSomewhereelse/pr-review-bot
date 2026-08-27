"""Tests for onboarding/supabase_client.py — Supabase's OAuth token
endpoint is form-encoded (application/x-www-form-urlencoded), not JSON,
and client_id/client_secret are body fields — verified directly against
the raw OpenAPI schema (not prose docs, which incorrectly describe HTTP
Basic Auth) during this sub-project's brainstorm. See
docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md
sections 3-5."""
from __future__ import annotations

import httpx
import pytest
import respx

from onboarding import supabase_client
from onboarding.config import settings

TOKEN_URL = "https://api.supabase.com/v1/oauth/token"


@pytest.fixture(autouse=True)
def _oauth_app_credentials(monkeypatch):
    monkeypatch.setattr(settings, "supabase_oauth_client_id", "sentinel-client-id")
    monkeypatch.setattr(settings, "supabase_oauth_client_secret", "sentinel-client-secret")


async def test_valid_code_returns_tokens():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "sentinel-access",
                    "refresh_token": "sentinel-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        result = await supabase_client.exchange_oauth_code(
            "sentinel-code", "sentinel-verifier",
            "https://onboarding.example.com/?supabase_step=oauth_callback",
        )
    assert result == supabase_client.SupabaseTokens(
        access_token="sentinel-access", refresh_token="sentinel-refresh", expires_in=3600
    )


async def test_exchange_sends_form_encoded_body_not_json():
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "a", "expires_in": 1, "token_type": "Bearer"}
            )
        )
        await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    body = request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "client_id=sentinel-client-id" in body
    assert "client_secret=sentinel-client-secret" in body
    assert "code=c" in body
    assert "code_verifier=v" in body


async def test_response_missing_refresh_token_is_tolerated():
    """OAuthTokenResponse's schema does not require refresh_token — some
    grant types omit it entirely; the caller must handle None."""
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "sentinel-access", "expires_in": 3600, "token_type": "Bearer"}
            )
        )
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseTokens(
        access_token="sentinel-access", refresh_token=None, expires_in=3600
    )


async def test_rejected_code_is_invalid_code():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        result = await supabase_client.exchange_oauth_code("bad", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="invalid_code")


async def test_exchange_5xx_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_exchange_timeout_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_exchange_malformed_200_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_exchange_response_missing_access_token_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"expires_in": 1, "token_type": "Bearer"})
        )
        result = await supabase_client.exchange_oauth_code("c", "v", "https://onboarding.example.com/cb")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")


async def test_refresh_valid_token_returns_new_tokens():
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        )
        result = await supabase_client.refresh_access_token("sentinel-refresh")
    assert result == supabase_client.SupabaseTokens(
        access_token="new-access", refresh_token="new-refresh", expires_in=3600
    )


async def test_refresh_sends_refresh_token_grant():
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 1, "token_type": "Bearer"})
        )
        await supabase_client.refresh_access_token("sentinel-refresh")
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=sentinel-refresh" in body


async def test_refresh_rejected_is_unauthorized():
    """Unlike exchange_oauth_code, a rejected refresh maps to 'unauthorized'
    (the reactive-refresh-then-retry vocabulary), not 'invalid_code' — no
    'code' is involved in a refresh grant."""
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        result = await supabase_client.refresh_access_token("stale-refresh")
    assert result == supabase_client.SupabaseOAuthFailed(reason="unauthorized")


async def test_refresh_5xx_is_unreachable():
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.refresh_access_token("sentinel-refresh")
    assert result == supabase_client.SupabaseOAuthFailed(reason="supabase_unreachable")
