"""Settings validation: catches a misconfigured .env at startup rather than
silently reverting to unbounded/disabled behavior at runtime.
"""
from __future__ import annotations

import re
from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import OPERATIONAL_KEYS, Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z_0-9]+)=")


def _key_names(path: Path) -> set[str]:
    """Env-var NAMES only -- values are discarded before returning.

    This function is the reason a test may look at .env at all: it can only
    ever produce names, so no assertion built on it can print a secret. See
    CLAUDE.md's "Secret handling" section.
    """
    if not path.is_file():
        return set()
    return {
        match.group(1)
        for line in path.read_text().splitlines()
        if (match := _KEY_RE.match(line))
    }


def test_notice_sweep_batch_size_rejects_non_positive_values():
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(dispatcher_notice_sweep_batch_size=bad)


def test_notice_sweep_batch_size_accepts_positive_values():
    assert Settings(dispatcher_notice_sweep_batch_size=1).dispatcher_notice_sweep_batch_size == 1


def test_cooldown_factor_rejects_below_one():
    with pytest.raises(ValidationError):
        Settings(dispatcher_rereview_cooldown_factor=0.5)


def test_cooldown_factor_accepts_exactly_one():
    settings = Settings(dispatcher_rereview_cooldown_factor=1.0)
    assert settings.dispatcher_rereview_cooldown_factor == 1.0


def test_cooldown_factor_defaults_to_two():
    assert Settings().dispatcher_rereview_cooldown_factor == 2.0


def test_vertex_settings_default_to_derive_everything_from_the_key(monkeypatch):
    """GCP_PROJECT is an OPTIONAL override: unset means "use the project_id
    embedded in the service-account key itself" (design doc §2).

    _env_file=None plus delenv because these defaults must be asserted against
    the code, not against whatever this working copy's .env or the developer's
    exported shell happens to say."""
    for name in ("GCP_PROJECT", "GCP_LOCATION", "GCP_SERVICE_ACCOUNT_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.gcp_project == ""
    assert settings.gcp_location == "us-central1"
    assert settings.gcp_service_account_key == ""


def test_key_usage_caps_default_to_off(monkeypatch):
    """The cap defaults to None so an existing deployment that sets no env var
    sees no behavior change (design doc §2.1). _env_file=None plus delenv
    because these defaults must be asserted against the code, not against
    whatever this working copy's .env happens to say."""
    for name in (
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_RESET_TIME_UTC",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.key_usage_token_cap is None
    assert settings.key_usage_reset_time_utc == time(4, 0)


def test_key_usage_reset_time_parses_hh_mm(monkeypatch):
    """Arbitrary wall-clock granularity, not whole hours only -- a demo run
    sets the reset a couple of minutes out rather than waiting for the next
    hour boundary (design doc §2.1)."""
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "04:07")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(4, 7)


def test_key_usage_reset_time_parses_hh_mm_ss(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "23:59:30")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(23, 59, 30)


def test_key_usage_caps_parse_from_env(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_TOKEN_CAP", "20000")
    settings = Settings(_env_file=None)
    assert settings.key_usage_token_cap == 20000


def test_key_usage_reset_time_rejects_garbage(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "not-a-time")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_key_usage_caps_reject_non_positive_values():
    """0 (or negative) would make the dispatcher's `tokens >= 0` comparison
    unconditionally true -- every ticket deferred forever, and the deferral is
    STICKY (fixing the env var doesn't release already-deferred tickets). The
    cap must reject non-positive values at startup."""
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(key_usage_token_cap=bad)


def test_key_usage_caps_accept_positive_values():
    settings = Settings(key_usage_token_cap=1)
    assert settings.key_usage_token_cap == 1


def test_env_config_wins_over_env(tmp_path):
    """.env.config is the designated home for operational config, so it must
    win if a key somehow appears in both files."""
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=from_secrets_file\n")
    config = tmp_path / ".env.config"
    config.write_text("LLM_PROVIDER=from_config_file\n")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.llm_provider == "from_config_file"


def test_both_files_merge(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=sentinel-key\n")
    config = tmp_path / ".env.config"
    config.write_text("LLM_PROVIDER=groq\n")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.gemini_api_key == "sentinel-key"
    assert settings.llm_provider == "groq"


def test_process_env_beats_both_files(tmp_path, monkeypatch):
    """This is what makes Render unaffected by the split: neither file exists
    in the container, and injected env vars outrank both anyway."""
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=from_secrets_file\n")
    config = tmp_path / ".env.config"
    config.write_text("LLM_PROVIDER=from_config_file\n")
    monkeypatch.setenv("LLM_PROVIDER", "from_process_env")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.llm_provider == "from_process_env"


def test_every_operational_key_is_a_real_settings_field():
    """A typo in the allowlist would classify a key that cannot be read,
    silently exempting a real key from the placement guard below."""
    fields = set(Settings.model_fields)
    unknown = {key for key in OPERATIONAL_KEYS if key.lower() not in fields}
    assert not unknown, f"OPERATIONAL_KEYS names no such Settings field: {sorted(unknown)}"


def test_no_operational_key_lives_in_the_secrets_file():
    """Operational config must not sit in .env, because an agent may never open
    .env -- which is the entire point of the split. Reports NAMES only."""
    misplaced = _key_names(_REPO_ROOT / ".env") & OPERATIONAL_KEYS
    assert not misplaced, (
        f"move these keys from .env to .env.config: {sorted(misplaced)}"
    )


def test_no_unlisted_key_lives_in_the_config_file():
    """Secret-by-default: anything not on the allowlist must stay in .env."""
    intruders = _key_names(_REPO_ROOT / ".env.config") - OPERATIONAL_KEYS
    assert not intruders, (
        f"these keys are not on OPERATIONAL_KEYS and must live in .env: {sorted(intruders)}"
    )


_RETIRED_CREDENTIAL_KEYS = frozenset(
    {
        "GITHUB_APP_PRIVATE_KEY_B64",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GCP_SERVICE_ACCOUNT_KEY_B64",
        "GCP_SERVICE_ACCOUNT_KEY_PATH",
    }
)
_RETIRED_NUMBERED_RE = re.compile(
    r"^(GCP_SERVICE_ACCOUNT_KEY_B64|GCP_SERVICE_ACCOUNT_KEY_PATH)_\d+$"
)


def test_no_legacy_credential_var_lives_in_the_secrets_file():
    """Migration checklist for the verbatim-only credential convention
    (docs/superpowers/specs/2026-08-16-credential-convention-design.md):
    these four names, and vertex's numbered _B64_n/_PATH_n siblings, are
    retired and no Settings field reads them anymore. Reports NAMES only --
    see CLAUDE.md's "Secret handling" section."""
    names = _key_names(_REPO_ROOT / ".env")
    legacy = {
        name
        for name in names
        if name in _RETIRED_CREDENTIAL_KEYS or _RETIRED_NUMBERED_RE.match(name)
    }
    assert not legacy, (
        f"retired credential var(s) still in .env, no longer read: {sorted(legacy)} -- "
        "rename GITHUB_APP_PRIVATE_KEY_B64 to GITHUB_APP_PRIVATE_KEY, "
        "GCP_SERVICE_ACCOUNT_KEY_B64[_n] to GCP_SERVICE_ACCOUNT_KEY[_n] (base64-encode any "
        "local key file first with scripts/encode_credential.py), and remove the _PATH "
        "variants entirely"
    )


def test_vertex_model_defaults_to_the_confirmed_working_vertex_model(monkeypatch):
    """gemini-flash-latest 404s on Vertex; gemini-2.5-flash is the value this
    project confirmed live (ISSUES.md). A non-empty default also keeps
    --sync-env's empty-value guard from ever tripping on it."""
    monkeypatch.delenv("VERTEX_MODEL", raising=False)
    assert Settings(_env_file=None).vertex_model == "gemini-2.5-flash"


def test_target_repos_splits_comma_separated_list():
    settings = Settings(github_target_repo="org/repo-a,org/repo-b", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo-a", "org/repo-b"})


def test_target_repos_strips_whitespace_around_entries():
    settings = Settings(github_target_repo=" org/repo-a , org/repo-b ", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo-a", "org/repo-b"})


def test_target_repos_empty_string_means_no_restriction():
    settings = Settings(github_target_repo="", _env_file=None)
    assert settings.target_repos() == frozenset()


def test_target_repos_single_value_has_no_comma():
    settings = Settings(github_target_repo="org/repo", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo"})


def test_cost_cap_is_gone_entirely():
    """A dollar cap built on unverified rates is a safety control that can fail
    open -- worse than no cap (design spec 2026-08-18 section 6c)."""
    assert "KEY_USAGE_COST_CAP_USD" not in OPERATIONAL_KEYS
    assert not hasattr(Settings(_env_file=None), "key_usage_cost_cap_usd")


def test_llm_provider_has_no_implicit_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings(_env_file=None).llm_provider == ""


def test_importing_config_with_llm_provider_unset_does_not_raise(monkeypatch):
    """A pydantic *required* field would raise at import, because
    app/config.py builds Settings() at module scope -- which would break
    pytest and scripts/doctor.py before either could report the problem
    (design spec 2026-08-18 section 6e). This pins that trap shut.

    reload() rebinds app.config's module-level `settings` to a brand-new
    Settings() instance -- every OTHER already-imported module's own
    `from app.config import settings` still points at the pre-reload
    singleton, so leaving the swap in place desyncs the two for the rest of
    the process (a later test doing a fresh in-function `from app.config
    import settings` would silently pick up the orphaned new instance while
    e.g. app/providers/credentials.py keeps reading the old one -- confirmed
    by 7 unrelated full-suite failures before this restore was added). The
    try/finally restores the original singleton so reload is exercised (and
    a genuine raise there still fails this test) without leaking a second,
    diverging Settings object into the rest of the suite.
    """
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    import importlib
    import app.config
    original_settings = app.config.settings
    try:
        importlib.reload(app.config)  # must not raise
    finally:
        app.config.settings = original_settings
