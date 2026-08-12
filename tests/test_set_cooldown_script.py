"""The operator CLI that sets the DB cooldown override. Uses the shared
Postgres test harness -- it writes to the same table the service reads.
Mirrors tests/test_set_provider_script.py's shape, minus the credential
checks: there is no secret at stake here, only "does this write reach the
database production actually reads" -- which degrades to a warning, never a
refusal, so there is no --force flag."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import set_cooldown

_REPO_ROOT = Path(__file__).resolve().parent.parent

RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_base_only_leaves_others_untouched():
    assert set_cooldown.main(["--base", "30"]) == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)


def test_sets_all_three():
    assert set_cooldown.main(["--base", "30", "--cap", "600", "--factor", "1.5"]) == 0
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)


def test_a_second_call_with_one_flag_preserves_the_others_read_modify_write():
    set_cooldown.main(["--base", "30", "--cap", "600", "--factor", "1.5"])
    assert set_cooldown.main(["--factor", "3.0"]) == 0
    assert store.get_cooldown_overrides() == (30.0, 600.0, 3.0)


def test_clear_resets_all_three():
    set_cooldown.main(["--base", "30", "--cap", "600", "--factor", "1.5"])
    assert set_cooldown.main(["--clear"]) == 0
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_a_factor_below_one(capsys):
    assert set_cooldown.main(["--factor", "0.5"]) == 2
    assert "factor" in capsys.readouterr().err.lower()
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_a_non_positive_base(capsys):
    assert set_cooldown.main(["--base", "-5"]) == 2
    assert "base" in capsys.readouterr().err.lower()
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_a_zero_base(capsys):
    assert set_cooldown.main(["--base", "0"]) == 2
    assert "base" in capsys.readouterr().err.lower()
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_a_non_positive_cap(capsys):
    assert set_cooldown.main(["--cap", "-1"]) == 2
    assert "cap" in capsys.readouterr().err.lower()
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_clear_combined_with_base(capsys):
    assert set_cooldown.main(["--clear", "--base", "30"]) == 2
    err = capsys.readouterr().err.lower()
    assert "clear" in err
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_clear_combined_with_cap(capsys):
    assert set_cooldown.main(["--clear", "--cap", "600"]) == 2
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_clear_combined_with_factor(capsys):
    assert set_cooldown.main(["--clear", "--factor", "1.5"]) == 2
    assert store.get_cooldown_overrides() == (None, None, None)


def test_refuses_a_write_that_would_resolve_to_an_inert_override(capsys):
    """A lone --cap below the env-configured base resolves (once merged with
    env defaults) to base=300 > cap=20 -- effective_config() would discard
    the whole triple, so the write must be refused rather than silently
    doing nothing while reporting success."""
    assert set_cooldown.main(["--cap", "20"]) == 2
    err = capsys.readouterr().err.lower()
    assert "300" in err or "base" in err
    assert store.get_cooldown_overrides() == (None, None, None)


def test_allows_a_cap_write_that_stays_above_the_resolved_base():
    assert set_cooldown.main(["--cap", "600"]) == 0
    assert store.get_cooldown_overrides() == (None, 600.0, None)


def test_requires_at_least_one_flag_or_clear(capsys):
    assert set_cooldown.main([]) == 2
    assert capsys.readouterr().err


def test_rejects_an_abbreviated_flag(capsys):
    """allow_abbrev=False, matching scripts/set_provider.py's guard."""
    with pytest.raises(SystemExit) as exc:
        set_cooldown.main(["--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err


def test_entry_point_runs_as_a_documented_module_invocation():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_cooldown", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_sets_the_override_without_a_render_api_key(capsys):
    assert set_cooldown.main(["--base", "30"]) == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)
    assert "could not verify against Render" in capsys.readouterr().out


def test_degrades_to_a_warning_when_no_service_matches_the_configured_name(
    monkeypatch, capsys
):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        code = set_cooldown.main(["--base", "30"])
    out = capsys.readouterr().out
    assert code == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)
    assert "no service named" in out


def test_warns_but_proceeds_when_local_database_url_does_not_match_render(
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
        code = set_cooldown.main(["--base", "30"])
    assert code == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)
    assert "could not confirm this DATABASE_URL" in capsys.readouterr().out


def test_confirms_when_database_url_matches_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_cooldown.main(["--base", "30"])
    assert code == 0
    assert "verified" in capsys.readouterr().out.lower()


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom():
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(set_cooldown, "_verify_render_reachability", _boom)
    assert set_cooldown.main(["--clear"]) == 0
