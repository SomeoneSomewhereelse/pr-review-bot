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


import json as json_module

ORGS_URL = "https://api.supabase.com/v1/organizations"
PROJECTS_URL = "https://api.supabase.com/v1/projects"


async def test_list_organizations_returns_orgs():
    with respx.mock:
        respx.get(ORGS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "1", "slug": "org-one", "name": "Org One"},
                    {"id": "2", "slug": "org-two", "name": "Org Two"},
                ],
            )
        )
        result = await supabase_client.list_organizations("sentinel-access")
    assert result == supabase_client.SupabaseOrgsListed(
        orgs=[
            supabase_client.SupabaseOrg(slug="org-one", name="Org One"),
            supabase_client.SupabaseOrg(slug="org-two", name="Org Two"),
        ]
    )


async def test_list_organizations_sends_bearer_token():
    with respx.mock:
        route = respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
        await supabase_client.list_organizations("sentinel-access")
    assert route.calls.last.request.headers["authorization"] == "Bearer sentinel-access"


async def test_list_organizations_unauthorized():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.list_organizations("expired")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_list_organizations_rate_limited():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(429))
        result = await supabase_client.list_organizations("a")
    assert result == supabase_client.SupabaseApiFailed(reason="rate_limited")


async def test_list_organizations_unreachable_on_5xx():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.list_organizations("a")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_list_organizations_malformed_body_is_unreachable():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await supabase_client.list_organizations("a")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_create_project_returns_ref_and_status():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(201, json={"ref": "abcdefghijklmnopqrst", "status": "INACTIVE"})
        )
        result = await supabase_client.create_project(
            "sentinel-access", "org-one", "pr-review-bot", "sentinelpass123"
        )
    assert result == supabase_client.SupabaseProjectCreated(ref="abcdefghijklmnopqrst", status="INACTIVE")


async def test_create_project_sends_region_selection_not_deprecated_fields():
    with respx.mock:
        route = respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(201, json={"ref": "x" * 20, "status": "INACTIVE"})
        )
        await supabase_client.create_project("sentinel-access", "org-one", "pr-review-bot", "pw")
    payload = json_module.loads(route.calls.last.request.content)
    assert payload["region_selection"] == {"type": "specific", "code": "us-east-1"}
    assert "region" not in payload
    assert "plan" not in payload
    assert "desired_instance_size" not in payload


async def test_create_project_never_logs_or_returns_the_password(caplog):
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(201, json={"ref": "x" * 20, "status": "INACTIVE"})
        )
        result = await supabase_client.create_project(
            "sentinel-access", "org-one", "pr-review-bot", "SENTINEL_DO_NOT_LOG_PASSWORD"
        )
    assert "SENTINEL_DO_NOT_LOG_PASSWORD" not in caplog.text
    assert "SENTINEL_DO_NOT_LOG_PASSWORD" not in repr(result)


async def test_create_project_unauthorized():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.create_project("expired", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_create_project_rate_limited():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(429))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="rate_limited")


async def test_create_project_business_rule_rejection_relays_the_message():
    """Covers the free-tier-cap case and any other business-rule rejection:
    relay Supabase's own message verbatim rather than guessing which rule
    was violated (spec section 4)."""
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(
                403, json={"message": "This organization already has the maximum number of free projects."}
            )
        )
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseProjectRejected(
        message="This organization already has the maximum number of free projects."
    )


async def test_create_project_rejection_without_a_message_falls_back_to_unreachable():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(403, text="not json"))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_create_project_unreachable_on_5xx():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")
