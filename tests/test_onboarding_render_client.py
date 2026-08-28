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


async def test_malformed_200_body_is_unreachable_not_a_crash():
    """A non-JSON or unexpectedly-shaped 200 body must not let a traceback
    (which could carry request/response context) escape validate_key."""
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="render_unreachable")


async def test_malformed_200_body_wrong_shape_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(200, json={"not": "a list"}))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="render_unreachable")


async def test_sends_the_key_as_a_bearer_token():
    with respx.mock:
        route = respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(
                200, json=[{"owner": {"name": "Ada Lovelace"}, "cursor": "x"}]
            )
        )
        await render_client.validate_key(SENTINEL_KEY)
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {SENTINEL_KEY}"


CREATE_URL = f"{render_client.RENDER_API_BASE}/services"


def _service_created_response(service_id="srv-abc123", slug="pr-review-bot-a1b2c3d4"):
    return httpx.Response(
        201,
        json={
            "deployId": "dep-initial",
            "service": {
                "id": service_id,
                "name": slug,
                "slug": slug,
                "ownerId": "usr-1",
                "type": "web_service",
                "autoDeploy": "yes",
            },
        },
    )


async def test_create_service_returns_id_and_slug_derived_url():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(return_value=_service_created_response())
        result = await render_client.create_service(
            SENTINEL_KEY, "https://github.com/x/y", "my-name"
        )
    assert result == render_client.RenderServiceCreated(
        service_id="srv-abc123", service_url="https://pr-review-bot-a1b2c3d4.onrender.com"
    )


async def test_create_service_ignores_submitted_name_uses_response_slug():
    # Render may normalize the submitted name -- the returned slug differs
    # from what was submitted, and the URL must follow the slug.
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(
            return_value=_service_created_response(slug="my-name-normalized")
        )
        result = await render_client.create_service(
            SENTINEL_KEY, "https://github.com/x/y", "My Name!"
        )
    assert result.service_url == "https://my-name-normalized.onrender.com"


async def test_create_service_invalid_owners_call_is_invalid_key():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="invalid_key")


async def test_create_service_401_is_invalid_key():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="invalid_key")


async def test_create_service_5xx_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(return_value=httpx.Response(503))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="render_unreachable")


async def test_create_service_name_collision_is_request_rejected_with_message():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(
            return_value=httpx.Response(
                422, json={"message": "a service with that name already exists"}
            )
        )
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "taken")
    assert result == render_client.RenderServiceCreationFailed(
        reason="request_rejected", message="a service with that name already exists"
    )


async def test_create_service_timeout_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="render_unreachable")


async def test_create_service_owners_5xx_is_unreachable_not_invalid_key():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(500))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="render_unreachable")


async def test_create_service_owners_malformed_200_body_is_unreachable_not_invalid_key():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="render_unreachable")


ENV_VAR_URL_A = f"{render_client.RENDER_API_BASE}/services/srv-1/env-vars/A_KEY"
ENV_VAR_URL_B = f"{render_client.RENDER_API_BASE}/services/srv-1/env-vars/B_KEY"


async def test_push_env_vars_all_succeed():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(return_value=httpx.Response(200))
        respx.put(ENV_VAR_URL_B).mock(return_value=httpx.Response(200))
        result = await render_client.push_env_vars(
            SENTINEL_KEY, "srv-1", {"A_KEY": "a-value", "B_KEY": "b-value"}
        )
    assert result == render_client.RenderEnvVarsPushed(pushed=["A_KEY", "B_KEY"])


async def test_push_env_vars_stops_at_first_failure_reports_partial():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(return_value=httpx.Response(200))
        respx.put(ENV_VAR_URL_B).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.push_env_vars(
            SENTINEL_KEY, "srv-1", {"A_KEY": "a-value", "B_KEY": "b-value"}
        )
    assert result == render_client.RenderEnvVarsPushFailed(reason="invalid_key", pushed=["A_KEY"])


async def test_push_env_vars_404_is_service_not_found():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(return_value=httpx.Response(404))
        result = await render_client.push_env_vars(SENTINEL_KEY, "srv-1", {"A_KEY": "a-value"})
    assert result == render_client.RenderEnvVarsPushFailed(reason="service_not_found", pushed=[])


async def test_push_env_vars_timeout_is_unreachable():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await render_client.push_env_vars(SENTINEL_KEY, "srv-1", {"A_KEY": "a-value"})
    assert result == render_client.RenderEnvVarsPushFailed(reason="render_unreachable", pushed=[])


DEPLOYS_URL = f"{render_client.RENDER_API_BASE}/services/srv-1/deploys"
DEPLOY_STATUS_URL = f"{render_client.RENDER_API_BASE}/services/srv-1/deploys/dep-1"


async def test_trigger_deploy_returns_deploy_id():
    with respx.mock:
        respx.post(DEPLOYS_URL).mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        result = await render_client.trigger_deploy(SENTINEL_KEY, "srv-1")
    assert result == render_client.RenderDeployTriggered(deploy_id="dep-1")


async def test_trigger_deploy_401_is_invalid_key():
    with respx.mock:
        respx.post(DEPLOYS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.trigger_deploy(SENTINEL_KEY, "srv-1")
    assert result == render_client.RenderDeployTriggerFailed(reason="invalid_key")


async def test_trigger_deploy_404_is_service_not_found():
    with respx.mock:
        respx.post(DEPLOYS_URL).mock(return_value=httpx.Response(404))
        result = await render_client.trigger_deploy(SENTINEL_KEY, "srv-1")
    assert result == render_client.RenderDeployTriggerFailed(reason="service_not_found")


async def test_poll_deploy_status_live():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "live"}})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="live")


async def test_poll_deploy_status_in_flight_bucket():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(
                200, json={"deploy": {"id": "dep-1", "status": "build_in_progress"}}
            )
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="in_progress")


async def test_poll_deploy_status_failed_bucket():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(
                200, json={"deploy": {"id": "dep-1", "status": "build_failed"}}
            )
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="failed")


async def test_poll_deploy_status_canceled_is_not_failed():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "canceled"}})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="canceled")


async def test_poll_deploy_status_401_is_invalid_key():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(401, json={"message": "nope"})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatusFailed(reason="invalid_key")


async def test_poll_deploy_status_404_is_service_not_found():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(return_value=httpx.Response(404))
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatusFailed(reason="service_not_found")


async def test_poll_deploy_status_5xx_is_unreachable():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(return_value=httpx.Response(503))
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatusFailed(reason="render_unreachable")
