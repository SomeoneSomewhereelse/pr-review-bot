"""Tests for onboarding/config.py — public_base_url reads from the real
process environment only (no .env/.env.config file: onboarding/ is a
separate deployed service, not sharing the review engine's config files).
See docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
section 5."""

from __future__ import annotations

from onboarding.config import Settings


def test_no_public_base_url_setting_exists():
    """The page derives its base from location.origin; a hand-set env var was
    a second source of truth for the same fact and the two drifted (see
    ISSUES.md). Reintroducing the field would quietly reintroduce the drift."""
    assert "public_base_url" not in Settings.model_fields


def test_database_url_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings().database_url == ""


def test_database_url_strips_whitespace(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "  postgresql://x  \n")
    assert Settings().database_url == "postgresql://x"


def test_session_encryption_key_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSION_ENCRYPTION_KEY", raising=False)
    assert Settings().onboarding_session_encryption_key == ""


def test_session_encryption_key_reads_from_environment_unvalidated(monkeypatch):
    """Format validity (a real Fernet key or not) is deliberately NOT
    checked here -- see config.py's field docstring for why a pydantic-level
    ValidationError would leak this secret's raw value. That check lives in
    main.py's lifespan instead (test_onboarding_main.py)."""
    monkeypatch.setenv("ONBOARDING_SESSION_ENCRYPTION_KEY", "not-a-fernet-key")
    assert Settings().onboarding_session_encryption_key == "not-a-fernet-key"


def test_session_encryption_key_whitespace_only_value_normalizes_to_the_unset_sentinel(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSION_ENCRYPTION_KEY", "   ")
    assert Settings().onboarding_session_encryption_key == ""
