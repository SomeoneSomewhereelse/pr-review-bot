"""Deterministic tests for scripts/deploy.py.

Two mocking harnesses are in play and they are not interchangeable:
`respx` intercepts `httpx` (Render, UptimeRobot, /healthz), while GitHub calls
go through PyGithub/`requests` and are therefore monkeypatched at the
`github_app` function boundary rather than at the HTTP layer. See
tests/test_github_app.py for why respx cannot see PyGithub traffic.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from scripts import deploy

BASE = "https://x.onrender.com"
HEALTH = f"{BASE}/healthz"


@pytest.fixture(autouse=True)
def _no_real_provider_credentials(monkeypatch):
    """deploy.py's tests never need a real provider key, and _wanted_env now
    reads every provider's credential -- so without this, a developer's .env
    flows into mocked request bodies and out through any respx match failure."""
    for name in ("gemini_api_key", "groq_api_key", "github_models_token"):
        monkeypatch.setattr(settings, name, "")


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
    assert lines[-1] == (
        "1 failed, 1 skipped -- see README.md#deploying-to-production-render--supabase"
    )


def test_render_report_indents_continuation_lines():
    """A detail may wrap only to enumerate observed values; the continuation
    must align under the detail column, not the name column."""
    report = deploy.render_report(
        [
            deploy.CheckResult(
                "uptime-pinger", "FAIL", "no monitor matches /healthz\nfound: /healthz,"
            )
        ]
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


def test_providers_table_covers_every_supported_provider():
    """One table, read by check_config, --sync-env and set_provider.py, so a
    provider cannot be known to one consumer and unknown to another."""
    assert set(deploy._PROVIDERS) == {"gemini", "groq", "github_models"}
    for credential, model_var in deploy._PROVIDERS.values():
        assert credential and model_var


def test_check_config_fails_on_an_unrecognized_provider(complete_config, monkeypatch):
    """An unrecognized value used to contribute no requirement and pass with
    nothing verified -- which after the vertex retirement includes 'vertex'."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "vertex" in result.detail
    assert "gemini" in result.detail


def test_check_config_reports_a_bad_provider_alongside_other_missing_keys(
    complete_config, monkeypatch
):
    """An unsupported provider must not mask problems already collected --
    one run surfaces every problem, per this module's own contract."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    detail = deploy.check_config().detail
    assert "GITHUB_WEBHOOK_SECRET" in detail
    assert "vertex" in detail


def test_check_config_requires_the_gemini_key_when_gemini_selected(
    complete_config, monkeypatch
):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GEMINI_API_KEY" in result.detail


def test_check_config_never_prints_a_secret_value(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET_VALUE")
    result = deploy.check_config()
    assert "gsk_SUPER_SECRET_VALUE" not in result.detail


@pytest.fixture
def unreadable_pem(complete_config):
    """chmod 000 -- root reads anything, so this cannot be tested as root."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; cannot test an unreadable PEM")
    complete_config.chmod(0o000)
    yield complete_config
    complete_config.chmod(0o600)


def test_check_config_fails_on_an_unreadable_pem(unreadable_pem):
    """is_file() said yes while read_bytes() raised, so config passed and the
    failure surfaced later as a traceback."""
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "unreadable" in result.detail


def test_check_config_distinguishes_unreadable_from_missing(unreadable_pem):
    """Different problems need different actions: fix permissions vs create a
    key. Reporting both as 'missing' sends the operator to the wrong fix."""
    detail = deploy.check_config().detail
    assert "GITHUB_APP_PRIVATE_KEY_B64 or _PATH" not in detail


def test_check_config_reports_a_missing_pem_as_missing(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nope/absent.pem")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY_B64 or _PATH" in result.detail


def test_check_config_uses_b64_without_touching_the_filesystem(
    complete_config, monkeypatch
):
    monkeypatch.setattr(settings, "github_app_private_key_b64", "Zm9v")
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nope/absent.pem")
    assert deploy.check_config().status == "PASS"


@pytest.fixture
def github_seam(monkeypatch):
    """Monkeypatch the github_app boundary and record webhook writes.

    The check's job is the decision logic (read -> compare -> conditionally
    write); github_app's own HTTP behavior is covered in tests/test_github_app.py
    with the requests-level fake_transport harness, which respx cannot replace.
    """
    from app import github_app

    state = {"installation_id": 424242, "current_url": "", "written": []}

    monkeypatch.setattr(
        github_app, "discover_installation_id", lambda repo: state["installation_id"]
    )
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


def test_failed_webhook_write_fails_with_the_status(github_seam, monkeypatch):
    """A failing PATCH must render an actionable, status-bearing FAIL like the
    read path above it -- not fall through to the generic _safe() catch-all."""
    from github import GithubException

    from app import github_app

    github_seam["current_url"] = "https://old.example/webhook"

    def _raise(url):
        raise GithubException(502, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "set_webhook_url", _raise)
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "502" in result.detail
    assert "webhook write failed" in result.detail


def test_installation_lookup_non_404_reports_the_underlying_status(github_seam, monkeypatch):
    """A 401 (bad key) and a 502 (GitHub degraded) must render differently --
    the generic RuntimeError message alone collapses both to the same string."""
    from github import GithubException

    from app import github_app

    def _raise(repo):
        try:
            raise GithubException(401, {"message": "bad credentials"}, None)
        except GithubException as exc:
            raise RuntimeError("installation lookup failed") from exc

    monkeypatch.setattr(github_app, "discover_installation_id", _raise)
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "401" in result.detail
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


def _deploys_response(deploy_obj):
    return httpx.Response(200, json=[{"deploy": deploy_obj}])


@respx.mock
def test_render_service_reports_the_live_commit(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cdaffffffff"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "4e39cda" in result.detail


@respx.mock
def test_render_service_fails_when_local_head_is_not_deployed(monkeypatch):
    """The never-push operator's trap: changes that never reached the build."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("1b10b18", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "4e39cda" in result.detail and "1b10b18" in result.detail


@respx.mock
def test_render_service_fails_on_a_dirty_working_tree(monkeypatch):
    """Uncommitted changes can be in no build, by construction."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", True))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "dirty" in result.detail


@respx.mock
def test_render_service_reports_an_image_without_claiming_verification(monkeypatch):
    """No local comparison is possible, and the row must not imply one."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "image": {"ref": "ghcr.io/you/pr-review:v3"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "ghcr.io/you/pr-review:v3" in result.detail
    assert "no local comparison" in result.detail


@respx.mock
def test_render_service_degrades_when_render_reports_no_artifact(monkeypatch):
    """Assumption 4 is unverified: a missing field must never produce a FAIL."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response({"id": "dep-abc", "status": "live"})
    )
    assert deploy.check_render_service().status == "PASS"


@respx.mock
def test_render_service_skips_the_comparison_outside_a_git_repo(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: None)
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "no git" in result.detail


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
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH, "status": 2, "interval": 300})
            )
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
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH, "status": 0, "interval": 300})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "paused" in result.detail


def test_uptime_pinger_fails_when_the_interval_lets_the_instance_sleep(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH, "status": 2, "interval": 1800})
            )
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
    """Replace all eight checks with constant results, in report order."""
    names = [
        "config", "github-app", "health", "database", "provider",
        "provider-live", "render-service", "uptime-pinger",
    ]
    fns = [
        "check_config",
        "check_installation_and_webhook",
        "check_health_endpoint",
        "check_database",
        "check_provider",
        "check_provider_live",
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
    _stub_all_checks(monkeypatch, ["PASS"] * 5 + ["SKIPPED"] * 3)
    assert deploy.main([]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_returns_one_when_any_check_fails(runnable, monkeypatch, capsys):
    _stub_all_checks(
        monkeypatch,
        ["PASS", "FAIL", "PASS", "PASS", "PASS", "PASS", "SKIPPED", "SKIPPED"],
    )
    assert deploy.main([]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_main_with_sync_env_falls_through_to_the_checklist_on_success(
    runnable, monkeypatch, capsys
):
    """Spec section 8 step 7: a successful sync must not skip the post-sync
    checklist -- it is the thing that proves the sync actually took."""
    _stub_all_checks(monkeypatch, ["PASS"] * 5 + ["SKIPPED"] * 3)
    monkeypatch.setattr(deploy, "sync_env", lambda: 0)
    assert deploy.main(["--sync-env"]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_with_sync_env_returns_early_without_the_checklist_on_failure(
    runnable, monkeypatch, capsys
):
    """A non-zero sync_env() must short-circuit main() before run_checks/
    render_report ever run -- printing the table after a failed sync would
    misleadingly suggest the sync itself is fine."""
    _stub_all_checks(monkeypatch, ["PASS"] * 5 + ["SKIPPED"] * 3)
    monkeypatch.setattr(deploy, "sync_env", lambda: 2)
    assert deploy.main(["--sync-env"]) == 2
    assert "all checks passed" not in capsys.readouterr().out


def test_main_returns_two_without_a_target_repo(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    assert deploy.main([]) == 2


def test_main_returns_two_without_a_base_url(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.main([]) == 2


def test_run_checks_reports_all_eight_in_order(runnable, monkeypatch):
    _stub_all_checks(monkeypatch, ["PASS"] * 8)
    results = deploy.run_checks("owner/repo", BASE)
    assert [r.name for r in results] == [
        "config", "github-app", "health", "database", "provider",
        "provider-live", "render-service", "uptime-pinger",
    ]


def test_an_exploding_check_becomes_a_fail_and_does_not_abort_the_run(runnable, monkeypatch):
    """A complete table is the deliverable; one broken check must not deprive
    the operator of the other seven diagnoses (spec section 7.3)."""
    def _boom():
        raise ValueError("unexpected")

    _stub_all_checks(monkeypatch, ["PASS"] * 8)
    monkeypatch.setattr(deploy, "check_database", _boom)
    results = deploy.run_checks("owner/repo", BASE)
    assert len(results) == 8
    database = next(r for r in results if r.name == "database")
    assert database.status == "FAIL"
    assert "ValueError" in database.detail


@pytest.fixture
def gemini_only_config(complete_config, monkeypatch):
    """A first-time user's .env: LLM_PROVIDER at its 'gemini' default, with the
    other providers' keys listed but empty."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "github_models_token", "")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    # database_url is a dummy, unreachable host -- stub psycopg.connect so the
    # masking guard's "no override" outcome is deterministic rather than an
    # accident of DNS failure (tests must never open a real DB connection).
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
    return None


def test_wanted_env_pushes_the_selected_providers_credential_and_model(
    gemini_only_config, monkeypatch
):
    monkeypatch.setattr(settings, "llm_model", "gemini-flash-latest")
    wanted = deploy._wanted_env()
    assert wanted["GEMINI_API_KEY"] == "gk_x"
    assert wanted["LLM_MODEL"] == "gemini-flash-latest"
    assert wanted["LLM_PROVIDER"] == "gemini"


def test_wanted_env_omits_unset_credentials_of_other_providers(gemini_only_config):
    """A Groq-only or Gemini-only .env must never be asked for another
    provider's key -- the whole point of opt-in provider config."""
    wanted = deploy._wanted_env()
    assert "GROQ_API_KEY" not in wanted
    assert "GITHUB_MODELS_TOKEN" not in wanted


def test_wanted_env_includes_other_credentials_that_are_set(
    gemini_only_config, monkeypatch
):
    """Pushed when locally filled, so a later dashboard-side switch works."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    assert deploy._wanted_env()["GROQ_API_KEY"] == "gsk_x"


def test_sync_env_does_not_demand_other_providers_keys(
    gemini_only_config, monkeypatch, capsys
):
    """The regression this task exists for: the default config could not sync
    at all, and the error named two providers the user never chose."""
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: None)
    code = deploy.sync_env()
    err = capsys.readouterr().err
    assert "GROQ_API_KEY" not in err
    assert "GITHUB_MODELS_TOKEN" not in err
    assert code == 1          # got past the guards, failed on the missing service


def test_sync_env_refuses_when_the_selected_credential_is_empty(
    gemini_only_config, monkeypatch, capsys
):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    called = []
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    assert called == []       # refused before any HTTP


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
    # database_url is a dummy, unreachable host -- stub psycopg.connect so the
    # masking guard's "no override" outcome is deterministic rather than an
    # accident of DNS failure (tests must never open a real DB connection).
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_render_env_vars_unwraps_the_service_env_list(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"A": "1", "B": "2"}))
        )
        result = deploy._render_env_vars("srv-1")
    assert result == {"A": "1", "B": "2"}


def test_sync_env_requires_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.sync_env() == 2


def test_sync_env_exits_2_on_an_unreadable_pem_without_a_traceback(
    unreadable_pem, monkeypatch, capsys
):
    """The parked residual: _wanted_env's OSError sat outside sync_env's try."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    called = []
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    assert "GITHUB_APP_PRIVATE_KEY_B64" in capsys.readouterr().err
    assert called == []


def test_sync_env_refuses_to_push_an_empty_value(sync_ready, monkeypatch, capsys):
    """A blank .env entry must never overwrite a working remote secret, and the
    guard must fire before any request is issued."""
    monkeypatch.setattr(settings, "groq_api_key", "")
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list())
        )
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_sync_env_refuses_gemini_provider_with_no_synced_gemini_key(
    sync_ready, monkeypatch, capsys
):
    """settings.llm_provider defaults to 'gemini'; if the selected provider's
    own credential is empty locally, syncing would push a service that boots
    and answers /healthz while failing every real review, with every
    checklist check reporting green. The guard must fire before any request."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list())
        )
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_sync_env_reports_a_partial_push_as_exit_one_not_could_not_run(
    sync_ready, capsys
):
    """Once one PUT has actually landed on the service, a later failure is a
    PARTIAL push, not a failure to start -- it must exit 1 and name what was
    already pushed, never exit 2's 'could not run at all', which would leave
    an operator thinking the half-configured service is untouched."""
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        wanted = deploy._wanted_env()
        current = dict.fromkeys(wanted, "stale")
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(current))
        )
        first_key, second_key = list(wanted)[:2]
        respx.put(f"{RENDER_SERVICES}/srv-1/env-vars/{first_key}").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.put(f"{RENDER_SERVICES}/srv-1/env-vars/{second_key}").mock(
            side_effect=httpx.ConnectError("boom")
        )
        code = deploy.sync_env()
    err = capsys.readouterr().err
    assert code == 1
    assert first_key in err            # names what was actually pushed
    assert second_key not in err       # never claims the failed one landed
    assert "partial" in err.lower()


def test_sync_env_pushes_only_changed_keys_via_the_single_key_endpoint(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        wanted = deploy._wanted_env()
        current = dict.fromkeys(wanted, "stale")
        current["GITHUB_TARGET_REPO"] = wanted["GITHUB_TARGET_REPO"]       # already correct
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(current))
        )
        bulk = respx.put(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json={})
        )
        single = respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])  # nothing in flight
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1", "status": "created"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "live"}})
        )
        assert deploy.sync_env() == 0
        assert not bulk.called          # the bulk PUT would delete DATABASE_URL
        # All but GITHUB_TARGET_REPO differ.
        assert single.call_count == len(wanted) - 1


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
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])  # nothing in flight
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "build_failed"}})
        )
        assert deploy.sync_env() == 1


def test_canceled_is_not_treated_as_a_build_failure():
    """Cancellation is what a superseding deploy looks like, not a failure."""
    assert "canceled" not in deploy._DEPLOY_FAILED_STATUSES
    assert "build_failed" in deploy._DEPLOY_FAILED_STATUSES


def test_deploy_timeout_allows_for_a_cold_docker_build():
    assert deploy._DEPLOY_TIMEOUT_SECONDS >= 900


@respx.mock
def test_trigger_and_wait_reports_a_superseded_deploy_distinctly(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    respx.post("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=httpx.Response(201, json={"id": "dep-1"})
    )
    respx.get("https://api.render.com/v1/services/svc-1/deploys/dep-1").mock(
        return_value=httpx.Response(200, json={"status": "canceled"})
    )
    code = deploy._trigger_and_wait("svc-1")
    err = capsys.readouterr().err
    assert code == 1
    assert "superseded" in err
    assert "env vars" in err          # says what did happen, not just what failed


@respx.mock
def test_sync_env_waits_for_an_in_flight_deploy_before_triggering(monkeypatch):
    """Triggering on top of a running build stacks two; waiting guarantees the
    pushed values are in the live container."""
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    statuses = iter([
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "live"}}],
    ])
    route = respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        side_effect=lambda request: httpx.Response(200, json=next(statuses))
    )
    deploy._wait_for_in_flight("svc-1")
    assert route.call_count == 2


@respx.mock
def test_wait_for_in_flight_prints_periodic_progress_during_a_long_wait(monkeypatch, capsys):
    """The initial announcement and the timeout message are covered elsewhere;
    this covers the periodic line in between so a long real wait is never
    silent for more than _IN_FLIGHT_PROGRESS_EVERY polls."""
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    monkeypatch.setattr(deploy, "_IN_FLIGHT_PROGRESS_EVERY", 2)
    statuses = iter([
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "live"}}],
    ])
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        side_effect=lambda request: httpx.Response(200, json=next(statuses))
    )
    assert deploy._wait_for_in_flight("svc-1") is True
    out = capsys.readouterr().out
    assert out.count("still waiting for in-flight deploy") >= 1


def test_sync_env_triggers_a_fresh_deploy_after_the_in_flight_one_settles(sync_ready):
    """The real property under review: observe an in-flight deploy, wait for it
    to settle, then issue a BRAND-NEW trigger -- never adopt the one observed.
    If the code adopted dep-old instead, it would poll GET .../deploys/dep-old,
    which is not mocked here, and sync_env would return 2, not 0."""
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        in_flight = iter([
            [{"deploy": {"id": "dep-old", "status": "build_in_progress"}}],
            [{"deploy": {"id": "dep-old", "status": "live"}}],
        ])
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            side_effect=lambda request: httpx.Response(200, json=next(in_flight))
        )
        new_deploy = {"deploy": {"id": "dep-new", "status": "created"}}
        trigger = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json=new_deploy)
        )
        poll_new = respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-new").mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-new", "status": "live"}})
        )
        assert deploy.sync_env() == 0
        assert trigger.called
        assert poll_new.called          # polled the NEW id, not the observed dep-old


def test_sync_env_times_out_waiting_for_in_flight_and_refuses_to_trigger(
    sync_ready, monkeypatch, capsys
):
    """The regression this fix round exists for: a timed-out wait must not
    silently fall through into triggering a second deploy on top of one still
    building. The assertion on the POST route's call_count is the one that
    would have caught it."""
    monkeypatch.setattr(deploy, "_DEPLOY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        stuck = [{"deploy": {"id": "dep-stuck", "status": "build_in_progress"}}]
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=stuck)
        )
        trigger = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-new"}})
        )
        code = deploy.sync_env()
    err = capsys.readouterr().err
    assert code == 1
    assert "timed out" in err
    assert "in-flight" in err
    assert trigger.call_count == 0


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
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=[])  # nothing in flight
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


def test_wanted_env_is_always_a_superset_of_the_always_synced_names():
    """_ALWAYS_SYNCED is what the docs test validates against, so _wanted_env()
    must always include it regardless of the selected provider -- otherwise the
    docs test would silently stop covering vars that are actually pushed. Unlike
    the old fixed eight-name set, exact equality no longer holds: _wanted_env()
    also carries LLM_PROVIDER, the selected provider's credential and model var,
    and any other provider's credential that happens to be set locally."""
    assert set(deploy._ALWAYS_SYNCED) <= set(deploy._wanted_env())


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_env_var_names_match_the_docs():
    """Every name --sync-env can push must be documented, or an operator has no
    way to know what the service needs."""
    readme = (_REPO_ROOT / "README.md").read_text()
    setup = (_REPO_ROOT / "SETUP.md").read_text()
    names = set(deploy._ALWAYS_SYNCED) | {"LLM_PROVIDER"}
    for credential, model_var in deploy._PROVIDERS.values():
        names.add(credential)
        names.add(model_var)
    for name in sorted(names):
        assert name in readme, f"{name} missing from README.md"
        assert name in setup, f"{name} missing from SETUP.md"


def test_exit_codes_are_documented():
    """Spec section 7.2 lists three causes for exit 2; the docs must carry
    them, or the contract exists only in the code."""
    for doc in ("README.md", "SETUP.md"):
        text = (_REPO_ROOT / doc).read_text()
        assert "exit 0" in text and "exit 1" in text and "exit 2" in text


def test_main_rejects_an_unknown_flag(monkeypatch, capsys):
    """A typo must not silently degrade to a checks-only run that reports
    success for a sync that never happened."""
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--sync-en"])
    assert exc.value.code == 2
    assert "--sync-en" in capsys.readouterr().err


def test_main_supports_help(capsys):
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--help"])
    assert exc.value.code == 0
    assert "--sync-env" in capsys.readouterr().out


def test_operator_api_keys_are_blank_by_default():
    """A test that forgets to monkeypatch these must hit no live API. Task 2
    overwrote a live Render env var because this guard did not exist."""
    assert settings.render_api_key == ""
    assert settings.uptimerobot_api_key == ""


def test_check_render_service_skips_rather_than_calling_out(monkeypatch):
    """With the keys quarantined, the Render check degrades to SKIPPED --
    it can never reach api.render.com from a default test run."""
    result = deploy.check_render_service()
    assert result.status == "SKIPPED"


class _FakeCursor:
    """Stands in for the cursor psycopg.Connection.execute() returns."""

    def __init__(self, outcome):
        self._outcome = outcome

    def fetchone(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _FakeConn:
    """A context-manager connection whose .execute().fetchone() is scripted --
    _resolved_provider() reads via a raw psycopg.connect(), not the store's
    pool, so the seam to fake is psycopg.connect itself."""

    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self, *_args, **_kwargs):
        return _FakeCursor(self._outcome)

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


@pytest.fixture
def override_seam(complete_config, monkeypatch):
    """check_provider (and sync_env's masking guard) resolve the override via
    a raw, short-timeout psycopg.connect() -- mirroring why check_database
    avoids store.init_pool()'s 30s pool timeout in a one-shot CLI. This fakes
    that connection so tests stay offline; call the fixture with the row
    fetchone() should return (a 1-tuple, or None for no row), or with an
    exception instance to simulate a query failure (e.g. a missing table).

    complete_config does not set a DATABASE_URL, so this also supplies one --
    without it check_provider SKIPs before ever reaching psycopg.connect."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")

    def _set(outcome):
        monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(outcome))

    _set(None)
    return _set


def test_check_provider_reports_the_env_value_when_no_override(override_seam):
    override_seam(None)
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert "groq" in result.detail
    assert "env" in result.detail


def test_check_provider_reports_a_satisfied_override(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    override_seam(("groq",))
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert "groq" in result.detail
    assert "override" in result.detail


def test_check_provider_fails_when_the_overrides_credential_is_missing(
    override_seam, monkeypatch
):
    """Green rows on a service failing every review is the exact failure this
    check exists to prevent."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "")
    override_seam(("groq",))
    result = deploy.check_provider()
    assert result.status == "FAIL"
    assert "GROQ_API_KEY" in result.detail


def test_check_provider_treats_an_empty_string_override_as_no_override(override_seam):
    """store.get_provider_override() collapses '' to None
    (tests/test_provider_override.py::test_an_empty_provider_string_reads_as_no_override
    pins this). The raw read here must match, or the CLI and the dispatcher
    would disagree about whether an override is active."""
    override_seam(("",))
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert "groq" in result.detail          # complete_config's env value, not ""
    assert "env" in result.detail
    assert "override" not in result.detail


def test_check_provider_skips_when_the_override_read_raises(override_seam):
    """A database the app has never booted against has no runtime_config
    table yet -- the query raises. That is `database`'s row to report, not
    provider's, so this must SKIP rather than blow up."""
    override_seam(RuntimeError("relation \"runtime_config\" does not exist"))
    result = deploy.check_provider()
    assert result.status == "SKIPPED"


def test_check_provider_skips_without_a_database_url(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.check_provider().status == "SKIPPED"


def test_resolved_provider_or_env_falls_back_without_a_database_url(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    assert deploy._resolved_provider_or_env() == ("groq", None)


def test_resolved_provider_or_env_resolves_the_override_when_database_url_is_set(
    override_seam,
):
    override_seam(("gemini",))
    assert deploy._resolved_provider_or_env() == ("gemini", "gemini")


def test_resolved_provider_or_env_propagates_a_db_error(override_seam):
    override_seam(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        deploy._resolved_provider_or_env()


def test_provider_live_skips_without_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.check_provider_live().status == "SKIPPED"


def test_provider_live_skips_when_the_override_read_raises(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    override_seam(RuntimeError("boom"))
    assert deploy.check_provider_live().status == "SKIPPED"


def test_provider_live_passes_for_the_plain_env_provider_without_a_database_url(
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_provider_live()
    assert result.status == "PASS"
    assert "groq" in result.detail
    assert "no DATABASE_URL to check for an override" in result.detail


def test_provider_live_fails_when_the_overrides_credential_is_missing_on_render(
    override_seam, monkeypatch
):
    """The exact failure hit live during the demo rehearsal: `provider` PASSes
    locally while `provider-live` catches that Render was never given the key."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")  # present locally
    override_seam(("gemini",))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_provider_live()
    assert result.status == "FAIL"
    assert "GEMINI_API_KEY" in result.detail
    assert "not present" in result.detail


def test_provider_live_never_leaks_a_fetched_value(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    override_seam(None)
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"GROQ_API_KEY": "gsk_SUPER_SECRET"})
            )
        )
        result = deploy.check_provider_live()
    assert "gsk_SUPER_SECRET" not in result.detail
    # DATABASE_URL was set and read (no override active) -- distinct from the
    # no-DATABASE_URL fallback case, which says so explicitly.
    assert result.detail.endswith("(env) -- GROQ_API_KEY present on Render")


def test_run_checks_includes_the_provider_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider_live",
                        lambda: deploy.CheckResult("provider-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn, lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider-live" in names
    assert names.index("provider-live") > names.index("provider")


def test_run_checks_includes_the_provider_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider",
                        lambda: deploy.CheckResult("provider", "PASS", ""))
    # fn -> the row name it reports as, mirroring _stub_all_checks above.
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider_live", "provider-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn,
                            lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider" in names
    assert names.index("provider") > names.index("database")


def test_sync_env_refuses_when_an_override_would_mask_the_push(
    complete_config, monkeypatch, capsys
):
    """--sync-env would otherwise report a provider change that silently does
    nothing, because the override wins at runtime."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    # Same seam check_provider reads through -- no pool, no real connection.
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: ("groq", "groq"))
    called = []
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    err = capsys.readouterr().err
    assert "groq" in err and "set_provider" in err
    assert called == []
