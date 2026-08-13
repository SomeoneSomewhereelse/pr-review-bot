"""Direct unit tests for scripts/_render.py, the shared Render API access
module used by scripts/deploy.py and scripts/set_cooldown.py (via
scripts/_override.py, also used by scripts/set_override.py)."""

from __future__ import annotations

import httpx
import respx

from app.config import settings
from scripts import _render

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


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_env_vars_unwraps_the_service_env_list(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"A": "1", "B": "2"}))
        )
        result = _render.env_vars("srv-1")
    assert result == {"A": "1", "B": "2"}
