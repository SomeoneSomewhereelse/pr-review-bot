"""The operator CLI that sets the DB API-key-index override, per provider.
Uses the shared Postgres test harness -- it writes to the same table the
service reads. Mirrors tests/test_set_provider_script.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import set_api_key

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_the_override():
    assert set_api_key.main(["groq", "2"]) == 0
    assert store.get_key_index_override("groq") == 2


def test_clear_removes_the_override():
    set_api_key.main(["groq", "2"])
    assert set_api_key.main(["groq", "--clear"]) == 0
    assert store.get_key_index_override("groq") is None


def test_providers_track_independent_overrides():
    set_api_key.main(["groq", "2"])
    set_api_key.main(["gemini", "1"])
    assert store.get_key_index_override("groq") == 2
    assert store.get_key_index_override("gemini") == 1


def test_rejects_an_unsupported_provider(capsys):
    assert set_api_key.main(["vertex", "1"]) == 2
    err = capsys.readouterr().err
    assert "vertex" in err
    assert "groq" in err
    assert store.get_key_index_override("groq") is None


def test_rejects_a_negative_index(capsys):
    assert set_api_key.main(["groq", "-1"]) == 2
    assert "index" in capsys.readouterr().err


def test_requires_an_index_or_clear(capsys):
    assert set_api_key.main(["groq"]) == 2
    assert "index" in capsys.readouterr().err


def test_requires_a_provider(capsys):
    assert set_api_key.main([]) == 2


def test_clear_and_index_are_mutually_exclusive(capsys):
    assert set_api_key.main(["groq", "2", "--clear"]) == 2


def test_entry_point_runs_as_a_documented_module_invocation():
    """Mirrors test_set_provider_script.py's identically-motivated test: a
    subprocess run of the documented invocation form must actually work."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_api_key", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sets_the_override_without_a_render_api_key(capsys):
    """No RENDER_API_KEY: verification degrades to a warning and the write
    proceeds -- matches set_provider.py's SKIPPED-on-absent-key convention."""
    assert set_api_key.main(["groq", "2"]) == 0
    assert store.get_key_index_override("groq") == 2
    assert "could not verify against Render" in capsys.readouterr().out


def test_refuses_when_the_slot_is_missing_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_api_key.main(["groq", "2"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_key_index_override("groq") is None
    assert "GROQ_API_KEY_2" in err


def test_force_writes_the_override_despite_a_missing_slot(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_api_key.main(["groq", "2", "--force"])
    err = capsys.readouterr().err
    assert code == 0
    assert store.get_key_index_override("groq") == 2
    assert "GROQ_API_KEY_2" in err
    assert "--force" in err


def test_proceeds_when_the_slot_is_present_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_x"})
            )
        )
        code = set_api_key.main(["groq", "2"])
    assert code == 0
    assert store.get_key_index_override("groq") == 2
    assert "verified present" in capsys.readouterr().out


def test_never_leaks_a_fetched_credential_value(monkeypatch, db_url, capsys):
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
        set_api_key.main(["groq", "2"])
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.out
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.err


def test_proceeds_without_refusal_when_local_database_url_does_not_match_render(
    monkeypatch, capsys
):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": "postgresql://prod-only/db"})
            )
        )
        code = set_api_key.main(["groq", "2"])
    assert code == 0
    assert store.get_key_index_override("groq") == 2
    assert "could not confirm this DATABASE_URL" in capsys.readouterr().out


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom(provider, index):
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(set_api_key, "_verify_render_key_slot", _boom)
    assert set_api_key.main(["groq", "--clear"]) == 0


def test_rejects_an_abbreviated_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        set_api_key.main(["groq", "--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err
