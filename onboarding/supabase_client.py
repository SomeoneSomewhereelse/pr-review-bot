"""Thin async wrapper around Supabase's OAuth2 flow and Management API —
authorizes as a visitor and provisions their own Supabase project, without
persisting any credential server-side. See
docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md
sections 3-5."""
from __future__ import annotations

import dataclasses

import httpx

from onboarding.config import settings

SUPABASE_OAUTH_BASE = "https://api.supabase.com/v1/oauth"
SUPABASE_API_BASE = "https://api.supabase.com/v1"
SUPABASE_REGION_CODE = "us-east-1"


@dataclasses.dataclass(frozen=True)
class SupabaseTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclasses.dataclass(frozen=True)
class SupabaseOAuthFailed:
    reason: str  # "invalid_code" | "unauthorized" | "supabase_unreachable"


SupabaseTokenResult = SupabaseTokens | SupabaseOAuthFailed


def _parse_token_response(response: httpx.Response, invalid_reason: str) -> SupabaseTokenResult:
    if response.status_code >= 500:
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    if response.status_code >= 400:
        return SupabaseOAuthFailed(reason=invalid_reason)
    try:
        body = response.json()
        access_token = str(body["access_token"])
        expires_in = int(body["expires_in"])
    except (ValueError, KeyError, TypeError):
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    refresh_token = body.get("refresh_token")
    return SupabaseTokens(
        access_token=access_token,
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_in=expires_in,
    )


async def exchange_oauth_code(code: str, code_verifier: str, redirect_uri: str) -> SupabaseTokenResult:
    """Trade the OAuth authorization code for tokens (POST /v1/oauth/token,
    grant_type=authorization_code). Form-encoded per Supabase's own schema
    — NOT JSON — and client_id/client_secret are body fields, not an
    Authorization header. Never logs the response body, which carries the
    visitor's access/refresh tokens."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{SUPABASE_OAUTH_BASE}/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.supabase_oauth_client_id,
                    "client_secret": settings.supabase_oauth_client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                },
            )
    except httpx.HTTPError:
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    return _parse_token_response(response, invalid_reason="invalid_code")


async def refresh_access_token(refresh_token: str) -> SupabaseTokenResult:
    """Trade a refresh token for a new access token (grant_type=refresh_token).
    Never logs the refresh token or the response body."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{SUPABASE_OAUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.supabase_oauth_client_id,
                    "client_secret": settings.supabase_oauth_client_secret,
                    "refresh_token": refresh_token,
                },
            )
    except httpx.HTTPError:
        return SupabaseOAuthFailed(reason="supabase_unreachable")
    return _parse_token_response(response, invalid_reason="unauthorized")
