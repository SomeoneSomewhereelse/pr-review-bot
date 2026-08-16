"""The unified operator CLI replacing both scripts/set_provider.py and
scripts/set_api_key.py. Uses the shared Postgres test harness -- it writes
to the same table the service reads. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md
section 5 for the full grammar and its mapping to the two scripts this
replaces (which remain untouched and are not modified by this file)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import _override, set_override

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_activates_a_provider_only():
    assert set_override.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") is None


def test_clear_removes_the_provider_override():
    set_override.main(["groq"])
    assert set_override.main(["--clear"]) == 0
    assert store.get_provider_override() is None


def test_sets_index_without_activating():
    assert set_override.main(["groq", "--index", "2", "--no-activate"]) == 0
    assert store.get_key_index_override("groq") == 2
    assert store.get_provider_override() is None


def test_clears_index_without_activating():
    set_override.main(["groq", "--index", "2", "--no-activate"])
    assert set_override.main(["groq", "--clear-index", "--no-activate"]) == 0
    assert store.get_key_index_override("groq") is None


def test_activates_and_sets_index_together():
    assert set_override.main(["groq", "--index", "1"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") == 1


def test_explicit_index_0_is_not_treated_as_unset():
    """--index 0 must take the explicit-index branch (args.index is not
    None), not fall through to the "neither given" branch that reads the
    existing override -- both currently resolve to the same effective
    index, but only if the explicit-0 path is actually taken."""
    set_override.main(["groq", "--index", "2", "--no-activate"])
    assert set_override.main(["groq", "--index", "0"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") == 0


def test_activates_and_clears_index_together():
    set_override.main(["groq", "--index", "1", "--no-activate"])
    assert set_override.main(["groq", "--clear-index"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") is None


def test_providers_track_independent_index_overrides():
    set_override.main(["groq", "--index", "2", "--no-activate"])
    set_override.main(["gemini", "--index", "1", "--no-activate"])
    assert store.get_key_index_override("groq") == 2
    assert store.get_key_index_override("gemini") == 1


def test_rejects_an_unsupported_provider(capsys):
    assert set_override.main(["unknown"]) == 2
    err = capsys.readouterr().err
    assert "unknown" in err
    assert "groq" in err
    assert store.get_provider_override() is None


def test_rejects_a_negative_index(capsys):
    assert set_override.main(["groq", "--index", "-1"]) == 2
    assert "index" in capsys.readouterr().err


def test_requires_a_provider_or_clear(capsys):
    assert set_override.main([]) == 2
    assert "provider" in capsys.readouterr().err


def test_clear_must_be_used_alone_with_a_provider(capsys):
    assert set_override.main(["groq", "--clear"]) == 2
    assert "alone" in capsys.readouterr().err


def test_clear_must_be_used_alone_with_an_index(capsys):
    assert set_override.main(["--clear", "--index", "1"]) == 2
    assert "alone" in capsys.readouterr().err


def test_index_and_clear_index_are_mutually_exclusive(capsys):
    assert set_override.main(["groq", "--index", "1", "--clear-index"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_no_activate_requires_index_or_clear_index(capsys):
    assert set_override.main(["groq", "--no-activate"]) == 2
    assert "no-activate" in capsys.readouterr().err


def test_rejects_an_abbreviated_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        set_override.main(["groq", "--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err


def test_entry_point_runs_as_a_documented_module_invocation():
    """Mirrors the identically-motivated tests in test_set_provider_script.py
    and test_set_api_key_script.py."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_override", "--help"],
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


def test_refuses_when_the_effective_slot_is_missing_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_override.main(["groq", "--index", "2"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert store.get_key_index_override("groq") is None
    assert "GROQ_API_KEY_2" in err


def test_force_writes_despite_a_missing_slot(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_override.main(["groq", "--index", "2", "--force"])
    err = capsys.readouterr().err
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") == 2
    assert "--force" in err


def test_verifies_against_the_currently_configured_index_when_neither_flag_given(
    monkeypatch, db_url, capsys
):
    """The gap-fix: activating a provider that already has a non-zero index
    override must verify THAT index, not always index 0."""
    set_override.main(["groq", "--index", "2", "--no-activate"])
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_x"})
            )
        )
        code = set_override.main(["groq"])
    out = capsys.readouterr().out
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "GROQ_API_KEY_2" in out


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom(provider, index):
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(_override, "verify_render_slot", _boom)
    assert set_override.main(["--clear"]) == 0


def test_clear_index_without_activating_never_verifies(monkeypatch, db_url):
    """Review-round fix: --clear-index --no-activate must never verify
    against Render -- matches old set_api_key.py's --clear, which also
    never checked a credential before clearing one. Mirrors
    test_refuses_when_the_effective_slot_is_missing_on_render's setup
    (the base GROQ_API_KEY is absent from Render's env vars, which WOULD
    refuse the write if verification ran, since --clear-index's effective
    index is always 0) but asserts success -- proving verification was
    skipped entirely, not just that its result was overridden."""
    set_override.main(["groq", "--index", "2", "--no-activate"])
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_override.main(["groq", "--clear-index", "--no-activate"])
    assert code == 0
    assert store.get_key_index_override("groq") is None
    assert store.get_provider_override() is None


def test_clear_index_without_activating_never_calls_the_render_verification(monkeypatch):
    """Same property as the test above, proven the more direct way (like
    test_clear_never_calls_the_render_verification above it): the shared
    verify_render_slot is monkeypatched to blow up if called at all."""

    def _boom(provider, index):
        raise AssertionError("must not verify on --clear-index --no-activate")

    set_override.main(["groq", "--index", "2", "--no-activate"])
    monkeypatch.setattr(_override, "verify_render_slot", _boom)
    assert set_override.main(["groq", "--clear-index", "--no-activate"]) == 0
    assert store.get_key_index_override("groq") is None


def test_clear_index_with_activating_still_verifies(monkeypatch, db_url, capsys):
    """Unlike the --no-activate case above, --clear-index while ALSO
    activating the provider does verify -- against index 0, the slot about
    to become active -- because checking its target credential first is a
    genuine, worthwhile check. The base GROQ_API_KEY is absent from Render's
    env vars here, so this must refuse (exit 2) and leave both overrides
    untouched, same as test_refuses_when_the_effective_slot_is_missing_on_render
    does for --index."""
    set_override.main(["groq", "--index", "2", "--no-activate"])
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_override.main(["groq", "--clear-index"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert store.get_key_index_override("groq") == 2
    assert "GROQ_API_KEY" in err


def test_sets_model_and_activates_provider():
    assert set_override.main(["vertex", "--model", "gemini-2.5-flash"]) == 0
    assert store.get_model_override("vertex") == "gemini-2.5-flash"
    assert store.get_provider_override() == "vertex"


def test_sets_model_without_activating():
    assert set_override.main(
        ["vertex", "--model", "gemini-2.5-flash", "--no-activate"]
    ) == 0
    assert store.get_model_override("vertex") == "gemini-2.5-flash"
    assert store.get_provider_override() is None


def test_clear_model_leaves_other_providers_alone():
    set_override.main(["groq", "--model", "llama-3.3-70b-versatile", "--no-activate"])
    set_override.main(["vertex", "--model", "gemini-2.5-flash", "--no-activate"])
    assert set_override.main(["vertex", "--clear-model", "--no-activate"]) == 0
    assert store.get_model_override("vertex") is None
    assert store.get_model_override("groq") == "llama-3.3-70b-versatile"


def test_model_and_clear_model_are_mutually_exclusive(capsys):
    assert set_override.main(["vertex", "--model", "x", "--clear-model"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_no_activate_alone_still_requires_something_to_do(capsys):
    assert set_override.main(["vertex", "--no-activate"]) == 2
    assert "--no-activate requires" in capsys.readouterr().err


def test_known_model_is_accepted():
    """(provider, model) has a pricing.py rate-table entry -- the write must
    proceed with no --force needed."""
    assert set_override.main(
        ["vertex", "--model", "gemini-2.5-flash", "--no-activate"]
    ) == 0
    assert store.get_model_override("vertex") == "gemini-2.5-flash"


def test_unknown_model_is_refused(capsys):
    """No pricing-table entry for (vertex, this model) -- must refuse (exit 2)
    before ever writing the override, and the refusal must name the models
    this table actually knows for vertex so an operator/agent can see the
    valid options immediately."""
    code = set_override.main(
        ["vertex", "--model", "no-such-model", "--no-activate"]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "no-such-model" in err
    assert "gemini-2.5-flash" in err
    assert store.get_model_override("vertex") is None


def test_force_overrides_the_unknown_model_refusal():
    """--force is the shared escape hatch for both this refusal and the
    Render live-verification refusal above -- it must let an unpriced model
    through when an operator genuinely wants one."""
    assert set_override.main(
        ["vertex", "--model", "no-such-model", "--no-activate", "--force"]
    ) == 0
    assert store.get_model_override("vertex") == "no-such-model"


def test_empty_model_is_refused(capsys):
    """An empty model name is not a model; active_model() would treat it as
    "no override" and silently ignore the write."""
    assert set_override.main(["vertex", "--model", "  ", "--no-activate"]) == 2
    assert "must not be empty" in capsys.readouterr().err


def test_never_leaks_a_fetched_credential_value(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local_differs")
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
        set_override.main(["groq"])
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.out
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.err


def test_list_reports_slots_and_active_state(capsys, monkeypatch):
    from app.config import settings
    from scripts import _override

    monkeypatch.setattr(settings, "groq_api_key", "sentinel-groq")
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    monkeypatch.setattr(
        _override, "local_slot_indices",
        lambda base, env_path=".env": (1,) if base == "GROQ_API_KEY" else (),
    )
    assert set_override.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "groq" in out
    assert "GROQ_API_KEY_1" in out
    assert "llama-3.3-70b-versatile" in out


def test_list_never_prints_a_credential_value(capsys, monkeypatch):
    """The whole point of --list: an agent can answer "is --index 2 valid?"
    without opening .env, and nothing it prints can be a secret."""
    from app.config import settings
    from scripts import _override

    monkeypatch.setattr(settings, "groq_api_key", "SENTINEL-SECRET-VALUE")
    monkeypatch.setattr(
        _override, "local_slot_indices", lambda base, env_path=".env": (1,)
    )
    set_override.main(["--list"])
    captured = capsys.readouterr()
    assert "SENTINEL-SECRET-VALUE" not in captured.out
    assert "SENTINEL-SECRET-VALUE" not in captured.err


def test_list_must_be_used_alone(capsys):
    assert set_override.main(["--list", "groq"]) == 2
    assert "--list must be used alone" in capsys.readouterr().err
