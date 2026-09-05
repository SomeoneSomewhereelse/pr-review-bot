"""Direct unit coverage for bot/scripts/_override.py -- the shared local-value
discovery and Render-verification logic behind bot/scripts/set_override.py and
bot/scripts/deploy.py's numbered-slot sync-env fix. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md."""
from __future__ import annotations

import httpx
import pytest
import respx

from bot.config import settings
from bot.scripts import _override


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_local_slot_values_finds_matching_keys(tmp_path, local_slot_discovery_allowed):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY_1=gsk_one\nGROQ_API_KEY_2=gsk_two\nOTHER_VAR=x\n")
    slots = _override.local_slot_values("GROQ_API_KEY", env_path=str(env_file))
    assert slots == {"GROQ_API_KEY_1": "gsk_one", "GROQ_API_KEY_2": "gsk_two"}


def test_local_slot_values_ignores_empty_values(tmp_path, local_slot_discovery_allowed):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY_1=\nGROQ_API_KEY_2=gsk_two\n")
    slots = _override.local_slot_values("GROQ_API_KEY", env_path=str(env_file))
    assert slots == {"GROQ_API_KEY_2": "gsk_two"}


def test_local_slot_values_returns_empty_for_a_missing_file(
    tmp_path, local_slot_discovery_allowed
):
    missing = tmp_path / "does-not-exist.env"
    assert _override.local_slot_values("GROQ_API_KEY", env_path=str(missing)) == {}


def test_local_slot_values_does_not_match_a_different_base(
    tmp_path, local_slot_discovery_allowed
):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY_1=gk_one\n")
    assert _override.local_slot_values("GROQ_API_KEY", env_path=str(env_file)) == {}


def test_local_value_index_0_reads_through_settings(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_base")
    assert _override.local_value("groq", 0) == "gsk_base"


def test_local_value_index_n_reads_the_scan(monkeypatch):
    monkeypatch.setattr(
        _override, "local_slot_values",
        lambda base, env_path=".env": {"GROQ_API_KEY_2": "gsk_two"},
    )
    assert _override.local_value("groq", 2) == "gsk_two"


def test_local_value_index_n_returns_empty_when_unprovisioned(monkeypatch):
    monkeypatch.setattr(_override, "local_slot_values", lambda base, env_path=".env": {})
    assert _override.local_value("groq", 3) == ""


RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_verify_render_slot_degrades_without_a_render_api_key():
    ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "could not verify against Render" in message


def test_verify_render_slot_degrades_when_no_service_matches(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "no service named" in message


def test_verify_render_slot_skips_when_database_url_does_not_match(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": "postgresql://prod-only/db"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "could not confirm this DATABASE_URL" in message


def test_verify_render_slot_refuses_when_missing_on_render(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", db_url)
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        ok, message = _override.verify_render_slot("groq", 2)
    assert ok is False
    assert "GROQ_API_KEY_2" in message


def test_verify_render_slot_refuses_when_local_value_differs(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", db_url)
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_remote"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is False
    assert "differs" in message
    assert "gsk_local" not in message
    assert "gsk_remote" not in message


def test_verify_render_slot_passes_when_local_value_matches(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", db_url)
    monkeypatch.setattr(settings, "groq_api_key", "gsk_match")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_match"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "verified" in message


def test_verify_render_slot_passes_with_no_local_value_to_compare(monkeypatch, db_url):
    """The numbered-slot case set_api_key.py used to always be in -- no local
    counterpart at all, but Render has a real value."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", db_url)
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_remote"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 2)
    assert ok is True
    assert "no local value to compare" in message


def test_verify_render_slot_never_leaks_a_fetched_value(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_SUPER_SECRET_REMOTE"}
                ),
            )
        )
        _, message = _override.verify_render_slot("groq", 2)
    assert "gsk_SUPER_SECRET_REMOTE" not in message


def test_local_slot_indices_returns_indices_not_values(tmp_path, local_slot_discovery_allowed):
    """Discovery answers a NAMES question, so it must not hand back values --
    a caller cannot leak what it never received."""
    from bot.scripts import _override

    env = tmp_path / ".env"
    env.write_text(
        "GROQ_API_KEY=sentinel-zero\n"
        "GROQ_API_KEY_1=sentinel-one\n"
        "GROQ_API_KEY_2=sentinel-two\n"
    )
    indices = _override.local_slot_indices("GROQ_API_KEY", env_path=str(env))
    assert indices == (1, 2)
    assert all(isinstance(i, int) for i in indices)


def test_local_slot_indices_skips_empty_slots(tmp_path, local_slot_discovery_allowed):
    from bot.scripts import _override

    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY_1=sentinel-one\nGROQ_API_KEY_2=\n")
    assert _override.local_slot_indices("GROQ_API_KEY", env_path=str(env)) == (1,)


def test_local_slot_indices_is_empty_for_a_missing_file(local_slot_discovery_allowed):
    from bot.scripts import _override

    assert _override.local_slot_indices("GROQ_API_KEY", env_path="no-such-file") == ()


def test_local_slot_values_still_carries_values_for_sync_env(
    tmp_path, local_slot_discovery_allowed
):
    """The value-bearing variant survives, narrowly: --sync-env genuinely has
    to push the values."""
    from bot.scripts import _override

    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY_1=sentinel-one\n")
    assert _override.local_slot_values("GROQ_API_KEY", env_path=str(env)) == {
        "GROQ_API_KEY_1": "sentinel-one"
    }
