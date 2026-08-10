"""The operator CLI that sets the DB provider override. Uses the shared
Postgres test harness -- it writes to the same table the service reads."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import set_provider

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_the_override():
    assert set_provider.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"


def test_clear_removes_the_override():
    set_provider.main(["groq"])
    assert set_provider.main(["--clear"]) == 0
    assert store.get_provider_override() is None


def test_rejects_an_unsupported_provider(capsys):
    """It can only validate the name -- it runs locally and cannot know
    whether the credential exists on the service."""
    assert set_provider.main(["vertex"]) == 2
    err = capsys.readouterr().err
    assert "vertex" in err
    assert "groq" in err
    assert store.get_provider_override() is None


def test_requires_a_provider_or_clear(capsys):
    assert set_provider.main([]) == 2
    assert "provider" in capsys.readouterr().err


def test_entry_point_runs_as_a_documented_module_invocation():
    """`uv run python scripts/set_provider.py` (the form the docs used to
    show) raises ModuleNotFoundError: No module named 'app' before main() ever
    runs, because `python scripts/x.py` puts scripts/ on sys.path[0] rather
    than the repo root, and nothing installs `app` into the venv. Every other
    test in this module calls main() in-process, which is exactly why that
    was never caught. --help needs no database and exits before any DB code
    runs, so this does not depend on the `db` fixture actually doing anything."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_provider", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_rejects_an_abbreviated_flag(capsys):
    """allow_abbrev=False: a truncated --clear must not be silently accepted.
    scripts/deploy.py carries the same guard after an argparse abbreviation
    (--sync-en matched to --sync-env) triggered a real production incident."""
    with pytest.raises(SystemExit) as exc:
        set_provider.main(["--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err


RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sets_the_override_without_a_render_api_key(capsys):
    """No RENDER_API_KEY: verification degrades to a warning and the write
    proceeds -- matches this CLI's SKIPPED-on-absent-key convention."""
    assert set_provider.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"
    assert "could not verify against Render" in capsys.readouterr().out


def test_degrades_to_a_warning_when_no_service_matches_the_configured_name(
    monkeypatch, capsys
):
    """render_service_name doesn't match anything Render returns --
    _find_render_service_id() degrades to None. Like the missing-key case,
    this must warn and still let the write proceed, never block it."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        code = set_provider.main(["groq"])
    out = capsys.readouterr().out
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "no service named" in out


def test_refuses_when_the_credential_is_missing_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_provider.main(["groq"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert "GROQ_API_KEY" in err


def test_force_writes_the_override_despite_a_missing_credential(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_provider.main(["groq", "--force"])
    err = capsys.readouterr().err
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "GROQ_API_KEY" in err
    assert "--force" in err


def test_refuses_when_the_credential_differs_from_local_env(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_remote"})
            )
        )
        code = set_provider.main(["groq"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert "GROQ_API_KEY" in err
    assert "differs" in err


def test_proceeds_when_the_credential_matches_local_env(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_match")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_match"})
            )
        )
        code = set_provider.main(["groq"])
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "verified" in capsys.readouterr().out


def test_never_leaks_a_fetched_credential_value(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_SUPER_SECRET_REMOTE"}
                ),
            )
        )
        set_provider.main(["groq"])
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.out
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.err


def test_proceeds_without_refusal_when_local_database_url_does_not_match_render(
    monkeypatch, capsys
):
    """The closed edge case: RENDER_API_KEY set globally but DATABASE_URL
    points at a local/test database -- this write cannot affect production,
    so it must not be refused no matter what Render's credentials look like."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "")  # would refuse if compared
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": "postgresql://prod-only/db"})
            )
        )
        code = set_provider.main(["groq"])
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "could not confirm this DATABASE_URL" in capsys.readouterr().out


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom(provider):
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(set_provider, "_verify_render_credential", _boom)
    assert set_provider.main(["--clear"]) == 0


def test_rejects_an_abbreviated_force_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        set_provider.main(["groq", "--for"])
    assert exc.value.code == 2
    assert "--for" in capsys.readouterr().err
