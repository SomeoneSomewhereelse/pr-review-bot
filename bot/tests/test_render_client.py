"""Direct unit tests for bot/render_client.py, the shared Render API access
module used by bot/scripts/deploy.py, bot/scripts/set_override.py (via
bot/scripts/_override.py), bot/scripts/reset_queue.py, and
dashboard/environment.py."""

from __future__ import annotations

import httpx
import respx

from bot import render_client as _render
from bot.config import settings

RENDER_SERVICES = "https://api.render.com/v1/services"


def test_find_service_id_returns_the_matching_service(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(
                200,
                json=[{"service": {"id": "srv-1", "name": "pr-review-engine"}}],
            )
        )
        assert _render.find_service_id() == "srv-1"


def test_find_service_id_returns_none_when_no_service_matches(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(
                200,
                json=[{"service": {"id": "srv-1", "name": "pr-review-engine"}}],
            )
        )
        assert _render.find_service_id() is None


def test_unwrap_returns_the_inner_dict_when_wrapped():
    assert _render.unwrap({"service": {"id": "srv-1"}}, "service") == {"id": "srv-1"}


def test_unwrap_returns_the_item_itself_when_bare():
    assert _render.unwrap({"id": "srv-1"}, "service") == {"id": "srv-1"}


def _env_var_list(values: dict, cursor_prefix: str = "c"):
    return [
        {"envVar": {"key": k, "value": v}, "cursor": f"{cursor_prefix}{i}"}
        for i, (k, v) in enumerate(values.items())
    ]


def test_env_vars_unwraps_the_service_env_list(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"A": "1", "B": "2"}))
        )
        result = _render.env_vars("srv-1")
    assert result == {"A": "1", "B": "2"}


def test_env_vars_follows_the_cursor_across_a_full_page(monkeypatch):
    """A service with more vars than one page must not silently drop the
    rest -- this is the bug that made DATABASE_URL and GCP_SERVICE_ACCOUNT_KEY
    invisible to every check built on env_vars() once the live service grew
    past Render's per-page limit."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    full_page = {f"KEY_{i}": str(i) for i in range(_render._ENV_VARS_PAGE_LIMIT)}
    second_page = {"DATABASE_URL": "postgres://...", "GCP_SERVICE_ACCOUNT_KEY": "ey..."}
    with respx.mock:
        route = respx.get(f"{RENDER_SERVICES}/srv-1/env-vars")
        route.side_effect = [
            httpx.Response(200, json=_env_var_list(full_page, cursor_prefix="p1-")),
            httpx.Response(200, json=_env_var_list(second_page, cursor_prefix="p2-")),
        ]
        result = _render.env_vars("srv-1")
    assert result == {**full_page, **second_page}
    assert route.call_count == 2
    second_call_params = dict(route.calls[1].request.url.params)
    assert second_call_params["cursor"] == f"p1-{_render._ENV_VARS_PAGE_LIMIT - 1}"


def test_env_vars_stops_after_a_short_page(monkeypatch):
    """A page shorter than the limit is the last page -- no further request."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        route = respx.get(f"{RENDER_SERVICES}/srv-1/env-vars")
        route.mock(return_value=httpx.Response(200, json=_env_var_list({"A": "1"})))
        result = _render.env_vars("srv-1")
    assert result == {"A": "1"}
    assert route.call_count == 1
