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
