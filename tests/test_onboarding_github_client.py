"""Tests for onboarding/github_client.py — GitHub App manifest-code
exchange never logs or returns anything but the documented fields, and
distinguishes a rejected/expired code from GitHub being unreachable. See
docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
sections 3-4."""
from __future__ import annotations

import base64

import httpx
import respx

from onboarding import github_client

CODE = "SENTINEL_MANIFEST_CODE"
CONVERSIONS_URL = f"https://api.github.com/app-manifests/{CODE}/conversions"


async def test_valid_code_returns_app_credentials():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": 42,
                    "slug": "my-pr-review-bot",
                    "pem": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "webhook_secret": "whsec_sentinel",
                },
            )
        )
        result = await github_client.exchange_manifest_code(CODE)
    assert isinstance(result, github_client.GithubAppCreated)
    assert result.app_id == 42
    assert result.slug == "my-pr-review-bot"
    assert result.webhook_secret == "whsec_sentinel"
    assert base64.b64decode(result.private_key_b64).decode() == (
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
    )


async def test_expired_or_bad_code_is_exchange_failed():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="exchange_failed")


async def test_github_5xx_is_unreachable_not_exchange_failed():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(500))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_malformed_200_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(201, text="not json"))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_response_missing_expected_fields_is_unreachable():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(201, json={"id": 42}))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_exchange_sends_no_authorization_header():
    """The manifest code IS the credential — no auth header is needed or
    sent, matching scripts/create_github_app.py::exchange_code()."""
    with respx.mock:
        route = respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={"id": 1, "slug": "x", "pem": "pem", "webhook_secret": "whsec"},
            )
        )
        await github_client.exchange_manifest_code(CODE)
    assert "authorization" not in {h.lower() for h in route.calls.last.request.headers}
