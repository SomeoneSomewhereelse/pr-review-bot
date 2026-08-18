"""doctor is read-only, degrades instead of erroring, and never leaks a value."""
from __future__ import annotations

import json

import pytest

from app.config import settings
from scripts import deploy, doctor

SENTINEL = "SENTINEL-2b6d40af91ce7385-DO-NOT-LEAK"


@pytest.fixture
def bare(monkeypatch):
    """A freshly-cloned checkout: nothing configured at all."""
    for field in (
        "github_app_id", "github_app_private_key", "github_webhook_secret",
        "database_url", "llm_provider", "groq_api_key", "gemini_api_key",
        "gcp_service_account_key", "public_base_url", "render_api_key",
        "uptimerobot_api_key",
    ):
        monkeypatch.setattr(settings, field, type(getattr(settings, field))(), raising=False)


def test_llm_provider_row_does_not_depend_on_a_database(bare, monkeypatch):
    """Step 4 must clear on local config alone. Gating it on
    deploy.check_provider (which SKIPs with no DATABASE_URL) would strand an
    operator who has a provider configured but no database yet."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x", raising=False)
    assert doctor.check_llm_provider().status == "PASS"
    state, _results = doctor.build_state("local", "")
    assert state.llm_ready is True


def test_a_bare_checkout_reports_step_two_not_a_crash(bare, monkeypatch):
    """Render not existing yet is the NORMAL state at the start, not a failure.

    Prereqs must PASS deterministically regardless of what tools happen to be
    installed on the machine running the suite -- cloudflared in particular
    is an unusual tool that a bare fixture alone does not guarantee, so PATH
    is monkeypatched the same way tests/test_prereqs.py does.
    """
    monkeypatch.setattr(doctor._prereqs.shutil, "which", lambda name: "/usr/bin/" + name)
    track = doctor.resolve_track(None)
    assert track == "local"
    state, results = doctor.build_state(track, "")
    step = doctor.current_step(track, state)
    assert step is not None
    assert step.number == 2, "prereqs pass in this environment; the App comes next"
    assert results, "a table is the deliverable even with nothing configured"


def test_no_check_raises_even_with_nothing_configured(bare):
    """Every row must render. _safe guarantees it; this pins that doctor uses it."""
    _state, results = doctor.build_state("hosted", "")
    for result in results:
        assert result.status in ("PASS", "WARN", "FAIL", "SKIPPED")


def test_missing_operator_keys_skip_rather_than_fail(bare):
    _state, results = doctor.build_state("hosted", "https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["render-service"].status == "SKIPPED"
    assert "RENDER_API_KEY" in by_name["render-service"].detail


def test_local_track_reports_the_tunnel_and_hosted_does_not(bare):
    local_names = {r.name for r in doctor.build_state("local", "")[1]}
    hosted_names = {r.name for r in doctor.build_state("hosted", "")[1]}
    assert "tunnel" in local_names
    assert "tunnel" not in hosted_names
    assert "uptime-pinger" in hosted_names
    assert "uptime-pinger" not in local_names


def test_prereqs_row_names_the_install_hint_when_a_tool_is_missing(bare, monkeypatch):
    monkeypatch.setattr(doctor._prereqs.shutil, "which", lambda name: None)
    result = doctor.check_prereqs("local")
    assert result.status == "FAIL"
    assert "git-scm.com" in result.detail, "the hint's URL must reach the operator"


def test_local_config_row_reports_names_never_values(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SENTINEL, raising=False)
    result = doctor.check_local_config()
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert SENTINEL not in result.detail


def test_json_output_is_machine_readable_and_leak_free(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SENTINEL, raising=False)
    state, results = doctor.build_state("local", "")
    payload = doctor.as_json("local", doctor.current_step("local", state), results)
    assert SENTINEL not in payload
    parsed = json.loads(payload)
    assert parsed["track"] == "local"
    assert parsed["step"]["number"] >= 1
    assert parsed["step"]["command"]
    assert all({"name", "status", "detail"} <= set(c) for c in parsed["checks"])


def test_render_includes_the_you_are_here_line(bare):
    state, results = doctor.build_state("local", "")
    text = doctor.render("local", doctor.current_step("local", state), results)
    assert "step" in text.lower()
    assert "of 8" in text


def test_render_reports_completion_when_every_step_is_satisfied():
    text = doctor.render("local", None, [deploy.CheckResult("config", "PASS")])
    assert "complete" in text.lower()


def test_render_completion_message_omits_the_caveat_when_nothing_fails():
    text = doctor.render("local", None, [deploy.CheckResult("config", "PASS")])
    assert "FAIL" not in text


def test_render_completion_message_notes_any_remaining_fail_row():
    """Step 8 being structurally unreachable used to let 'setup complete'
    print alongside a table with a FAIL row still showing -- self-
    contradictory. render() must call that out when it happens."""
    results = [
        deploy.CheckResult("config", "PASS"),
        deploy.CheckResult("uptime-pinger", "FAIL", "wrong URL"),
    ]
    text = doctor.render("hosted", None, results)
    assert "complete" in text.lower()
    assert "FAIL" in text
    assert "uptime-pinger" in text


def test_hosted_keepalive_is_satisfied_when_uptime_pinger_is_skipped(bare, monkeypatch):
    """uptime-pinger SKIPs without a local UPTIMEROBOT_API_KEY -- that must
    not strand an operator on step 8 forever."""
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    state, results = doctor.build_state("hosted", "https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["uptime-pinger"].status == "SKIPPED"
    assert state.keepalive is True


def test_hosted_keepalive_is_false_when_uptime_pinger_actively_fails(bare, monkeypatch):
    """An active FAIL (the monitor exists but is misconfigured) is the one
    case that must still block step 8."""
    monkeypatch.setattr(deploy, "check_uptime_pinger", lambda base: deploy.CheckResult(
        "uptime-pinger", "FAIL", "points at the wrong URL"
    ))
    state, results = doctor.build_state("hosted", "https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["uptime-pinger"].status == "FAIL"
    assert state.keepalive is False


def test_hosted_step_eight_is_reachable_when_public_url_clears_but_pinger_fails():
    """The bug this guards: step 6 and step 8 used to share one boolean, so
    the moment public_url/health cleared, step 8 cleared with it -- making it
    structurally unreachable as the reported current_step."""
    state = doctor.State(
        prereqs=True, app_credentials=True, app_installed=True, llm_ready=True,
        database=True, public_url=True, webhook=True, keepalive=False,
    )
    step = doctor.current_step("hosted", state)
    assert step is not None
    assert step.number == 8


def test_main_runs_the_full_wiring_and_prints_plain_text(capsys):
    """Exercises main()'s own wiring end-to-end -- resolve_track,
    deploy.resolve_base_url, build_state, current_step, render -- which no
    other test drives together. Runs against whatever local state exists;
    the point is the wiring succeeding and returning 0, not a specific
    check outcome."""
    result = doctor.main([])
    assert result == 0
    out = capsys.readouterr().out
    assert "step" in out.lower() or "complete" in out.lower()


def test_main_json_variant_prints_well_formed_json(capsys):
    result = doctor.main(["--json"])
    assert result == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "track" in parsed
    assert "checks" in parsed


def test_main_rejects_an_unknown_track(capsys):
    """argparse's choices= exits 2 itself, the same shape
    tests/test_deploy_script.py::test_main_rejects_an_unknown_flag asserts."""
    with pytest.raises(SystemExit) as exc:
        doctor.main(["--track", "nonsense"])
    assert exc.value.code == 2
    assert "nonsense" in capsys.readouterr().err


def test_doctor_never_calls_the_mutating_webhook_setter():
    """check_installation_and_webhook PATCHes the App's hook URL when wrong.
    doctor is read-only, so it must use get_webhook_url() instead."""
    import inspect

    source = inspect.getsource(doctor)
    assert "set_webhook_url" not in source
    assert "check_installation_and_webhook" not in source
