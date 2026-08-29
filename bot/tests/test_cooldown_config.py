"""The DB-backed cooldown override cache: base/cap/factor overrides with a
fail-safe fallback to env-var settings. Mirrors tests/test_provider_override.py's
active-provider-cache tests, but for the cooldown triple."""
from __future__ import annotations

from bot.config import settings
from bot.queue import cooldown_config


def _set_env_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 2.0)


def setup_function():
    cooldown_config.reset_override_cache()


def teardown_function():
    cooldown_config.reset_override_cache()


def test_no_override_falls_back_to_env_defaults(monkeypatch):
    _set_env_defaults(monkeypatch)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_full_override_is_used(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (30.0, 600.0, 1.5)


def test_partial_override_mixes_with_env_defaults(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, None, None)
    assert cooldown_config.effective_config() == (30.0, 3600.0, 2.0)


def test_invalid_factor_falls_back_to_env_defaults_entirely(monkeypatch):
    """A factor < 1 discards the WHOLE override triple, not just the factor --
    a bad factor must not silently pair with a stale overridden base/cap."""
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 0.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_base_above_cap_falls_back_to_env_defaults_entirely(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(700.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_non_positive_base_falls_back_to_env_defaults_entirely(monkeypatch):
    """A base <= 0 makes the cooldown a no-op, silently defeating the churn
    protection. It must discard the WHOLE override triple, not just base --
    pick distinct valid non-defaults for cap/factor so a partial-mix bug
    would be caught."""
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(0.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_negative_base_falls_back_to_env_defaults_entirely(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(-5.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_non_positive_cap_falls_back_to_env_defaults_entirely(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 0.0, 1.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_clearing_the_cache_returns_to_env_defaults(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)
    cooldown_config.reset_override_cache()
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)
