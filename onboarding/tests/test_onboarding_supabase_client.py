"""Tests for onboarding/supabase_client.py. validate_key() mirrors
render_client.validate_key()'s shape: one cheap read call doubles as both
credential validation and (since Supabase has no separate "who am I"
endpoint) the org list the frame needs next. See
docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md section 3."""

from __future__ import annotations

import json as json_module
import logging

import httpx
import respx

from onboarding import supabase_client

ORGS_URL = "https://api.supabase.com/v1/organizations"
PROJECTS_URL = "https://api.supabase.com/v1/projects"
SENTINEL_PAT = "sbp_SENTINEL_DO_NOT_LOG_9f3a"


async def test_valid_key_returns_orgs():
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
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyValid(
        orgs=[
            supabase_client.SupabaseOrg(slug="org-one", name="Org One"),
            supabase_client.SupabaseOrg(slug="org-two", name="Org Two"),
        ]
    )


async def test_valid_key_with_zero_orgs_is_still_valid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyValid(orgs=[])


async def test_validate_key_sends_bearer_token():
    with respx.mock:
        route = respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
        await supabase_client.validate_key(SENTINEL_PAT)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SENTINEL_PAT}"


async def test_unauthorized_key_is_invalid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.validate_key("bad")
    assert result == supabase_client.SupabaseKeyInvalid(reason="invalid_key")


async def test_forbidden_key_is_invalid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(403))
        result = await supabase_client.validate_key("bad")
    assert result == supabase_client.SupabaseKeyInvalid(reason="invalid_key")


async def test_5xx_is_unreachable_not_invalid():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyInvalid(reason="supabase_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.get(ORGS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyInvalid(reason="supabase_unreachable")


async def test_malformed_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.get(ORGS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await supabase_client.validate_key(SENTINEL_PAT)
    assert result == supabase_client.SupabaseKeyInvalid(reason="supabase_unreachable")


async def test_validate_key_never_logs_the_key(caplog):
    with caplog.at_level(logging.DEBUG):
        with respx.mock:
            respx.get(ORGS_URL).mock(return_value=httpx.Response(401))
            await supabase_client.validate_key(SENTINEL_PAT)
    assert SENTINEL_PAT not in caplog.text


async def test_create_project_returns_ref_and_status():
    with respx.mock:
        respx.post(PROJECTS_URL).mock(
            return_value=httpx.Response(
                201, json={"ref": "abcdefghijklmnopqrst", "status": "INACTIVE"}
            )
        )
        result = await supabase_client.create_project(
            "sentinel-access", "org-one", "pr-review-bot", "sentinelpass123"
        )
    assert result == supabase_client.SupabaseProjectCreated(
        ref="abcdefghijklmnopqrst", status="INACTIVE"
    )


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
    # DEBUG, not the pytest default (WARNING+): a future logger.info/debug
    # leak of the password would otherwise sail past this test uncaught.
    caplog.set_level(logging.DEBUG)
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
                403,
                json={
                    "message": "This organization already has the maximum number of free projects."
                },
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


async def test_create_project_4xx_with_non_dict_json_falls_back_to_unreachable():
    """If a 4xx error body is valid JSON but not a dict (e.g., array or scalar),
    .get("message") raises AttributeError, which must be caught and degrade to
    unreachable, not propagate uncaught."""
    with respx.mock:
        respx.post(PROJECTS_URL).mock(return_value=httpx.Response(403, json=[1, 2, 3]))
        result = await supabase_client.create_project("a", "org-one", "name", "pw")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


PROJECT_STATUS_URL = "https://api.supabase.com/v1/projects/abcdefghijklmnopqrst"
POOLER_URL = "https://api.supabase.com/v1/projects/abcdefghijklmnopqrst/config/database/pooler"

_POOLER_ENTRIES = [
    {
        "identifier": "abcdefghijklmnopqrst",
        "database_type": "PRIMARY",
        "is_using_scram_auth": True,
        "db_user": "postgres.abcdefghijklmnopqrst",
        "db_host": "aws-0-us-east-1.pooler.supabase.com",
        "db_port": 6543,
        "db_name": "postgres",
        "connection_string": "postgresql://masked",
        "connectionString": "postgresql://masked",
        "default_pool_size": None,
        "max_client_conn": None,
        "pool_mode": "transaction",
    },
    {
        "identifier": "abcdefghijklmnopqrst",
        "database_type": "PRIMARY",
        "is_using_scram_auth": True,
        "db_user": "postgres.abcdefghijklmnopqrst",
        "db_host": "aws-0-us-east-1.pooler.supabase.com",
        "db_port": 5432,
        "db_name": "postgres",
        "connection_string": "postgresql://masked",
        "connectionString": "postgresql://masked",
        "default_pool_size": None,
        "max_client_conn": None,
        "pool_mode": "session",
    },
]


async def test_get_project_status_returns_status():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"status": "ACTIVE_HEALTHY", "ref": "x"})
        )
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseProjectStatus(status="ACTIVE_HEALTHY")


async def test_get_project_status_unauthorized():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_get_project_status_unreachable_on_5xx():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_get_project_status_malformed_body_is_unreachable():
    with respx.mock:
        respx.get(PROJECT_STATUS_URL).mock(return_value=httpx.Response(200, json={}))
        result = await supabase_client.get_project_status("a", "abcdefghijklmnopqrst")
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_get_connection_info_selects_the_session_mode_primary_entry():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=_POOLER_ENTRIES))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseConnectionInfo(
        db_user="postgres.abcdefghijklmnopqrst",
        db_host="aws-0-us-east-1.pooler.supabase.com",
        db_port=5432,
        db_name="postgres",
    )


async def test_get_connection_info_never_returns_supabases_own_connection_string_field():
    """Deliberate: whether connection_string embeds the real password or a
    masked placeholder cannot be verified from documentation (spec section
    3 step 9) — the caller assembles the string itself from this shape."""
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=_POOLER_ENTRIES))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert not hasattr(result, "connection_string")
    assert not hasattr(result, "connectionString")


async def test_get_connection_info_falls_back_to_transaction_entry_with_port_forced_to_5432():
    """As of 2026-09-02, Supabase's pooler-config API stopped listing a
    distinct session-mode entry for newer projects -- only "transaction".
    Session and transaction mode share the same pooler host/user, so the
    fallback must reuse the transaction entry's host/user/name but force
    port 5432 rather than trusting its (transaction-mode) port."""
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=[_POOLER_ENTRIES[0]]))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseConnectionInfo(
        db_user="postgres.abcdefghijklmnopqrst",
        db_host="aws-0-us-east-1.pooler.supabase.com",
        db_port=5432,
        db_name="postgres",
    )


async def test_get_connection_info_no_primary_entry_at_all_is_pooler_config_unavailable():
    with respx.mock:
        respx.get(POOLER_URL).mock(
            return_value=httpx.Response(
                200, json=[{**_POOLER_ENTRIES[0], "database_type": "READ_REPLICA"}]
            )
        )
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")


async def test_get_connection_info_empty_array_is_pooler_config_unavailable():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")


async def test_get_connection_info_unauthorized():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(401))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseApiFailed(reason="unauthorized")


async def test_get_connection_info_unreachable_on_5xx():
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(500))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseApiFailed(reason="supabase_unreachable")


async def test_get_connection_info_malformed_entries_with_scalars_is_pooler_config_unavailable():
    """If response.json() returns an array with non-dict elements (e.g. scalars
    or null), iterating and calling .get() on them raises AttributeError, which
    must be caught and degrade gracefully to pooler_config_unavailable."""
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")


async def test_get_connection_info_malformed_entries_with_null_is_pooler_config_unavailable():
    """If response.json() returns an array containing null, calling .get() on
    null raises AttributeError, which must be caught and degrade gracefully."""
    with respx.mock:
        respx.get(POOLER_URL).mock(return_value=httpx.Response(200, json=[None]))
        result = await supabase_client.get_connection_info(
            "a", "abcdefghijklmnopqrst", session_id="s1"
        )
    assert result == supabase_client.SupabaseApiFailed(reason="pooler_config_unavailable")
