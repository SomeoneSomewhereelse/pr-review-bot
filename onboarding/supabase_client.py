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


@dataclasses.dataclass(frozen=True)
class SupabaseOrg:
    slug: str
    name: str


@dataclasses.dataclass(frozen=True)
class SupabaseOrgsListed:
    orgs: list[SupabaseOrg]


@dataclasses.dataclass(frozen=True)
class SupabaseApiFailed:
    reason: str
    # "unauthorized" | "forbidden" | "rate_limited" | "supabase_unreachable"
    # | "pooler_config_unavailable" (Task 4 only)


SupabaseOrgsResult = SupabaseOrgsListed | SupabaseApiFailed


async def list_organizations(access_token: str) -> SupabaseOrgsResult:
    """GET /v1/organizations — the orgs the visitor's own token can act on.
    Never logs the access token or the response body."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                "/organizations",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 403:
        return SupabaseApiFailed(reason="forbidden")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code != 200:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        body = response.json()
        orgs = [SupabaseOrg(slug=str(o["slug"]), name=str(o["name"])) for o in body]
    except (ValueError, KeyError, TypeError):
        return SupabaseApiFailed(reason="supabase_unreachable")
    return SupabaseOrgsListed(orgs=orgs)


@dataclasses.dataclass(frozen=True)
class SupabaseProjectCreated:
    ref: str
    status: str


@dataclasses.dataclass(frozen=True)
class SupabaseProjectRejected:
    message: str


async def create_project(
    access_token: str, organization_slug: str, name: str, db_pass: str
) -> SupabaseProjectCreated | SupabaseProjectRejected | SupabaseApiFailed:
    """POST /v1/projects — provisions a new project inside the visitor's own
    organization, on their own token. db_pass is already the browser's own
    value (generated client-side, spec section 5) — relayed through, never
    minted or logged here. Omits the deprecated `region`/`plan` fields and
    `desired_instance_size` (defaults to the smallest tier) per spec
    section 3 step 7."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.post(
                "/projects",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "organization_slug": organization_slug,
                    "name": name,
                    "db_pass": db_pass,
                    "region_selection": {"type": "specific", "code": SUPABASE_REGION_CODE},
                },
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code >= 500:
        return SupabaseApiFailed(reason="supabase_unreachable")
    if response.status_code >= 400:
        # No guaranteed structured error body (spec section 4) — relay
        # Supabase's own message verbatim rather than guessing which
        # business rule (free-tier cap or otherwise) was violated.
        try:
            message = response.json().get("message")
        except (ValueError, AttributeError):
            message = None
        if message:
            return SupabaseProjectRejected(message=str(message))
        return SupabaseApiFailed(reason="supabase_unreachable")
    if response.status_code != 201:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        body = response.json()
        ref = str(body["ref"])
        status = str(body["status"])
    except (ValueError, KeyError, TypeError):
        return SupabaseApiFailed(reason="supabase_unreachable")
    return SupabaseProjectCreated(ref=ref, status=status)


@dataclasses.dataclass(frozen=True)
class SupabaseProjectStatus:
    status: str


async def get_project_status(access_token: str, ref: str) -> SupabaseProjectStatus | SupabaseApiFailed:
    """GET /v1/projects/{ref} — polled by the browser during provisioning.
    Target status is ACTIVE_HEALTHY; the caller treats INIT_FAILED as a
    terminal failure and every other status as still-provisioning."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                f"/projects/{ref}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 403:
        return SupabaseApiFailed(reason="forbidden")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code != 200:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        status = str(response.json()["status"])
    except (ValueError, KeyError, TypeError):
        return SupabaseApiFailed(reason="supabase_unreachable")
    return SupabaseProjectStatus(status=status)


@dataclasses.dataclass(frozen=True)
class SupabaseConnectionInfo:
    db_user: str
    db_host: str
    db_port: int
    db_name: str


async def get_connection_info(access_token: str, ref: str) -> SupabaseConnectionInfo | SupabaseApiFailed:
    """GET /v1/projects/{ref}/config/database/pooler — selects the
    session-mode (port 5432) PRIMARY entry, matching the manual guide's
    existing "Session-mode pooler, not transaction mode" requirement.
    Deliberately never reads Supabase's own connection_string/
    connectionString fields (see module docstring) — the caller (browser,
    which already holds db_pass) assembles the final connection string
    itself from this non-secret shape."""
    try:
        async with httpx.AsyncClient(base_url=SUPABASE_API_BASE, timeout=15.0) as client:
            response = await client.get(
                f"/projects/{ref}/config/database/pooler",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        return SupabaseApiFailed(reason="supabase_unreachable")

    if response.status_code == 401:
        return SupabaseApiFailed(reason="unauthorized")
    if response.status_code == 403:
        return SupabaseApiFailed(reason="forbidden")
    if response.status_code == 429:
        return SupabaseApiFailed(reason="rate_limited")
    if response.status_code != 200:
        return SupabaseApiFailed(reason="supabase_unreachable")

    try:
        entries = response.json()
        matched = next(
            e for e in entries
            if e.get("pool_mode") == "session" and e.get("database_type") == "PRIMARY"
        )
        db_user = str(matched["db_user"])
        db_host = str(matched["db_host"])
        db_port = int(matched["db_port"])
        db_name = str(matched["db_name"])
    except (ValueError, KeyError, TypeError, StopIteration):
        return SupabaseApiFailed(reason="pooler_config_unavailable")
    return SupabaseConnectionInfo(db_user=db_user, db_host=db_host, db_port=db_port, db_name=db_name)
