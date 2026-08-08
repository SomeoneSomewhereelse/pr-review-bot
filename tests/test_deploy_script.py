"""Deterministic tests for scripts/deploy.py.

Two mocking harnesses are in play and they are not interchangeable:
`respx` intercepts `httpx` (Render, UptimeRobot, /healthz), while GitHub calls
go through PyGithub/`requests` and are therefore monkeypatched at the
`github_app` function boundary rather than at the HTTP layer. See
tests/test_github_app.py for why respx cannot see PyGithub traffic.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from scripts import deploy

BASE = "https://x.onrender.com"
HEALTH = f"{BASE}/healthz"


def test_resolve_base_url_prefers_settings_and_strips_trailing_slash(monkeypatch):
    """A trailing slash would make check_uptime_pinger's exact-URL comparison
    fail against a correctly configured monitor (spec section 7.1)."""
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com/")
    assert deploy.resolve_base_url() == "https://x.onrender.com"


def test_resolve_base_url_falls_back_to_render_external_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://y.onrender.com/")
    assert deploy.resolve_base_url() == "https://y.onrender.com"


def test_resolve_base_url_empty_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.resolve_base_url() == ""


def test_render_report_aligns_columns_and_summarizes():
    report = deploy.render_report(
        [
            deploy.CheckResult("config", "PASS", ""),
            deploy.CheckResult("health", "FAIL", "HEAD /healthz -> 405 (GET ok)"),
            deploy.CheckResult("database", "SKIPPED", "set DATABASE_URL"),
        ]
    )
    lines = report.split("\n")
    # Status starts at the same column on every row.
    status_columns = {line.index(s) for line, s in zip(lines[:3], ["PASS", "FAIL", "SKIPPED"])}
    assert len(status_columns) == 1
    assert lines[-1] == "1 failed, 1 skipped -- see README.md#deploying-to-production"


def test_render_report_indents_continuation_lines():
    """A detail may wrap only to enumerate observed values; the continuation
    must align under the detail column, not the name column."""
    report = deploy.render_report(
        [deploy.CheckResult("uptime-pinger", "FAIL", "no monitor matches /healthz\nfound: /healthz,")]
    )
    first, second = report.split("\n")[:2]
    assert second.startswith(" ")
    assert second.index("found:") == first.index("no monitor")


def test_render_report_summary_when_everything_passes():
    report = deploy.render_report([deploy.CheckResult("config", "PASS", "")])
    assert report.split("\n")[-1] == "all checks passed"


@pytest.fixture
def complete_config(monkeypatch, tmp_path):
    """Every value check_config requires, present and valid."""
    pem = tmp_path / "key.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key_b64", "")
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem))
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    return pem


def test_check_config_passes_when_everything_is_present(complete_config):
    assert deploy.check_config().status == "PASS"


def test_check_config_accepts_base64_key_without_a_pem_file(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nonexistent.pem")
    monkeypatch.setattr(settings, "github_app_private_key_b64", "aGVsbG8=")
    assert deploy.check_config().status == "PASS"


def test_check_config_fails_when_the_pem_path_does_not_exist(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nonexistent.pem")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail


def test_check_config_names_every_missing_key_at_once(complete_config, monkeypatch):
    """One run should surface all of them, not the first alphabetically."""
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "github_target_repo", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert "GITHUB_TARGET_REPO" in result.detail


def test_check_config_requires_the_key_for_the_selected_provider(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "github_models")
    monkeypatch.setattr(settings, "github_models_token", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_MODELS_TOKEN" in result.detail


def test_check_config_ignores_provider_keys_for_other_providers(complete_config, monkeypatch):
    """groq is selected, so a missing GITHUB_MODELS_TOKEN is irrelevant."""
    monkeypatch.setattr(settings, "github_models_token", "")
    assert deploy.check_config().status == "PASS"


def test_check_config_never_prints_a_secret_value(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET_VALUE")
    result = deploy.check_config()
    assert "gsk_SUPER_SECRET_VALUE" not in result.detail


@pytest.fixture
def github_seam(monkeypatch):
    """Monkeypatch the github_app boundary and record webhook writes.

    The check's job is the decision logic (read -> compare -> conditionally
    write); github_app's own HTTP behavior is covered in tests/test_github_app.py
    with the requests-level fake_transport harness, which respx cannot replace.
    """
    from app import github_app

    state = {"installation_id": 424242, "current_url": "", "written": []}

    monkeypatch.setattr(github_app, "discover_installation_id", lambda repo: state["installation_id"])
    monkeypatch.setattr(github_app, "get_webhook_url", lambda: state["current_url"])
    monkeypatch.setattr(github_app, "set_webhook_url", lambda url: state["written"].append(url))
    return state


def test_webhook_already_correct_passes_without_writing(github_seam):
    github_seam["current_url"] = "https://x.onrender.com/webhook"
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "PASS"
    assert "already correct" in result.detail
    assert github_seam["written"] == []          # no PATCH issued


def test_webhook_mismatch_is_updated(github_seam):
    github_seam["current_url"] = "https://old.example/webhook"
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "PASS"
    assert github_seam["written"] == ["https://x.onrender.com/webhook"]
    assert "https://old.example/webhook" in result.detail


def test_webhook_absent_is_set_on_first_deploy(github_seam):
    github_seam["current_url"] = ""
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "PASS"
    assert github_seam["written"] == ["https://x.onrender.com/webhook"]


def test_app_not_installed_fails_with_an_actionable_detail(github_seam, monkeypatch):
    from app import github_app

    def _raise(repo):
        raise github_app.AppNotInstalledError("not installed")

    monkeypatch.setattr(github_app, "discover_installation_id", _raise)
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "install" in result.detail.lower()
    assert github_seam["written"] == []


def test_failed_webhook_read_does_not_write(github_seam, monkeypatch):
    """Writing blind after a failed read is how a correct URL gets clobbered."""
    from github import GithubException

    from app import github_app

    def _raise():
        raise GithubException(500, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "get_webhook_url", _raise)
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "500" in result.detail
    assert github_seam["written"] == []


def test_health_passes_when_get_and_head_both_return_200():
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "PASS"


def test_health_fails_when_head_is_405_even_though_get_is_200():
    """The exact regression that silently broke keep-warm for 71 minutes."""
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(405))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
    assert "HEAD" in result.detail


def test_health_fails_when_get_is_not_200():
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(503))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
    assert "503" in result.detail


def test_health_fails_on_a_transport_error():
    with respx.mock:
        respx.get(HEALTH).mock(side_effect=httpx.ConnectError("refused"))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"


DEAD_DB_URL = "postgresql://u:pw@127.0.0.1:1/postgres?connect_timeout=1"


def test_database_skips_with_a_hint_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    result = deploy.check_database()
    assert result.status == "SKIPPED"
    assert "DATABASE_URL" in result.detail


def test_database_fails_fast_when_unreachable(monkeypatch):
    """127.0.0.1:1 refuses immediately, so this costs about a second."""
    monkeypatch.setattr(settings, "database_url", DEAD_DB_URL)
    result = deploy.check_database()
    assert result.status == "FAIL"
    assert "connect" in result.detail.lower()


def test_database_passes_against_the_provisioned_test_database(db, db_url, monkeypatch):
    """The `db` fixture opens the pool and creates the schema, so `tickets` exists."""
    monkeypatch.setattr(settings, "database_url", db_url)
    result = deploy.check_database()
    assert result.status == "PASS"
    assert "tickets present" in result.detail


def test_database_distinguishes_reachable_but_unprovisioned(db, db_url, db_exec, monkeypatch):
    """A DATABASE_URL pointing at the wrong Supabase project answers SELECT 1
    but has no tickets table -- a setup mistake a bare SELECT 1 calls success."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS tickets")
    result = deploy.check_database()
    assert result.status == "FAIL"
    assert "tickets" in result.detail


SENTINEL_PASSWORD = "sentinel-pw-must-not-appear"


def test_check_database_failure_never_leaks_the_connection_string(monkeypatch):
    """CLAUDE.md: no secret is ever logged. database_url carries the password,
    so the failure detail must describe the failure shape -- exception type and
    the non-secret hostname -- and never interpolate the URL."""
    monkeypatch.setattr(
        settings,
        "database_url",
        f"postgresql://someuser:{SENTINEL_PASSWORD}@127.0.0.1:1/postgres?connect_timeout=1",
    )
    result = deploy.check_database()
    assert result.status == "FAIL"
    rendered = result.detail + repr(result) + deploy.render_report([result])
    assert SENTINEL_PASSWORD not in rendered


RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(name="pr-review-engine", service_id="srv-1"):
    return [{"service": {"id": service_id, "name": name}}]


def _deploy_list(status):
    return [{"deploy": {"id": "dep-1", "status": status}}]


def test_render_service_skips_with_a_hint_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    result = deploy.check_render_service()
    assert result.status == "SKIPPED"
    assert "RENDER_API_KEY" in result.detail


def test_render_service_passes_when_latest_deploy_is_live(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=_deploy_list("live"))
        )
        result = deploy.check_render_service()
    assert result.status == "PASS"


def test_render_service_fails_and_names_a_non_live_status(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=_deploy_list("build_failed"))
        )
        result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "build_failed" in result.detail


def test_render_service_fails_when_the_configured_name_is_absent(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list(name="something-else"))
        )
        result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "pr-review-engine" in result.detail


def test_render_service_never_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_SUPER_SECRET")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(401, json={}))
        result = deploy.check_render_service()
    assert "rnd_SUPER_SECRET" not in result.detail


UPTIMEROBOT = "https://api.uptimerobot.com/v2/getMonitors"


def _monitors(*monitors):
    return {"stat": "ok", "monitors": list(monitors)}


def test_uptime_pinger_skips_with_a_hint_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "")
    result = deploy.check_uptime_pinger(BASE)
    assert result.status == "SKIPPED"
    assert "UPTIMEROBOT_API_KEY" in result.detail


def test_uptime_pinger_passes_for_an_active_five_minute_monitor(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(200, json=_monitors({"url": HEALTH, "status": 2, "interval": 300}))
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "PASS"


def test_uptime_pinger_fails_on_a_near_miss_url(monkeypatch):
    """The real outage: a trailing comma, firing on schedule, 404ing every time."""
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH + ",", "status": 2, "interval": 300})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert HEALTH + "," in result.detail       # the near-miss is visible on sight


def test_uptime_pinger_fails_when_paused(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(200, json=_monitors({"url": HEALTH, "status": 0, "interval": 300}))
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "paused" in result.detail


def test_uptime_pinger_fails_when_the_interval_lets_the_instance_sleep(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(200, json=_monitors({"url": HEALTH, "status": 2, "interval": 1800}))
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "1800" in result.detail


def test_uptime_pinger_never_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_SUPER_SECRET")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(return_value=httpx.Response(500, json={}))
        result = deploy.check_uptime_pinger(BASE)
    assert "u_SUPER_SECRET" not in result.detail


def _stub_all_checks(monkeypatch, statuses):
    """Replace all six checks with constant results, in report order."""
    names = ["config", "github-app", "health", "database", "render-service", "uptime-pinger"]
    fns = [
        "check_config",
        "check_installation_and_webhook",
        "check_health_endpoint",
        "check_database",
        "check_render_service",
        "check_uptime_pinger",
    ]
    for fn, name, status in zip(fns, names, statuses):
        monkeypatch.setattr(
            deploy, fn, (lambda n, s: lambda *args: deploy.CheckResult(n, s, ""))(name, status)
        )


@pytest.fixture
def runnable(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", BASE)


def test_main_returns_zero_when_all_pass_or_skip(runnable, monkeypatch, capsys):
    _stub_all_checks(monkeypatch, ["PASS"] * 4 + ["SKIPPED"] * 2)
    assert deploy.main([]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_returns_one_when_any_check_fails(runnable, monkeypatch, capsys):
    _stub_all_checks(monkeypatch, ["PASS", "FAIL", "PASS", "PASS", "SKIPPED", "SKIPPED"])
    assert deploy.main([]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_main_returns_two_without_a_target_repo(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    assert deploy.main([]) == 2


def test_main_returns_two_without_a_base_url(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.main([]) == 2


def test_run_checks_reports_all_six_in_order(runnable, monkeypatch):
    _stub_all_checks(monkeypatch, ["PASS"] * 6)
    results = deploy.run_checks("owner/repo", BASE)
    assert [r.name for r in results] == [
        "config", "github-app", "health", "database", "render-service", "uptime-pinger"
    ]


def test_an_exploding_check_becomes_a_fail_and_does_not_abort_the_run(runnable, monkeypatch):
    """A complete table is the deliverable; one broken check must not deprive
    the operator of the other five diagnoses (spec section 7.3)."""
    def _boom():
        raise ValueError("unexpected")

    _stub_all_checks(monkeypatch, ["PASS"] * 6)
    monkeypatch.setattr(deploy, "check_database", _boom)
    results = deploy.run_checks("owner/repo", BASE)
    assert len(results) == 6
    database = next(r for r in results if r.name == "database")
    assert database.status == "FAIL"
    assert "ValueError" in database.detail


@pytest.fixture
def sync_ready(monkeypatch, tmp_path):
    """Every value _wanted_env() reads, non-empty, plus a Render key."""
    pem = tmp_path / "key.pem"
    pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h:5432/postgres")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key_b64", "")
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem))
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(settings, "github_models_token", "ghp_x")
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sync_env_requires_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.sync_env() == 2


def test_sync_env_refuses_to_push_an_empty_value(sync_ready, monkeypatch, capsys):
    """A blank .env entry must never overwrite a working remote secret, and the
    guard must fire before any request is issued."""
    monkeypatch.setattr(settings, "groq_api_key", "")
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_sync_env_pushes_only_changed_keys_via_the_single_key_endpoint(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        current = dict.fromkeys(deploy._SYNCED_ENV_VARS, "stale")
        current["GITHUB_TARGET_REPO"] = "owner/repo"       # already correct
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(current))
        )
        bulk = respx.put(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json={})
        )
        single = respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1", "status": "created"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "live"}})
        )
        assert deploy.sync_env() == 0
        assert not bulk.called          # the bulk PUT would delete DATABASE_URL
        # Seven of eight differ; GITHUB_TARGET_REPO already matched.
        assert single.call_count == len(deploy._SYNCED_ENV_VARS) - 1


def test_sync_env_skips_the_deploy_when_nothing_changed(sync_ready, capsys):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(deploy._wanted_env()))
        )
        triggered = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={})
        )
        assert deploy.sync_env() == 0
        assert not triggered.called
    assert "already in sync" in capsys.readouterr().out


def test_sync_env_fails_when_the_deploy_fails(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "build_failed"}})
        )
        assert deploy.sync_env() == 1


def test_sync_env_never_prints_a_secret_value(sync_ready, monkeypatch, capsys):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "live"}})
        )
        deploy.sync_env()
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET" not in captured.out + captured.err
