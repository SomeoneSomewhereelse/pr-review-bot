"""Tests for onboarding/config.py — public_base_url reads from the real
process environment only (no .env/.env.config file: onboarding/ is a
separate deployed service, not sharing the review engine's config files).
See docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
section 5."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from onboarding.config import Settings


def test_no_public_base_url_setting_exists():
    """The page derives its base from location.origin; a hand-set env var was
    a second source of truth for the same fact and the two drifted (see
    ISSUES.md). Reintroducing the field would quietly reintroduce the drift."""
    assert "public_base_url" not in Settings.model_fields


def test_supabase_oauth_client_id_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("SUPABASE_OAUTH_CLIENT_ID", raising=False)
    assert Settings().supabase_oauth_client_id == ""


def test_supabase_oauth_client_id_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_ID", "66666666-6666-4666-8666-666666666666")
    assert Settings().supabase_oauth_client_id == "66666666-6666-4666-8666-666666666666"


def test_supabase_oauth_client_secret_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("SUPABASE_OAUTH_CLIENT_SECRET", raising=False)
    assert Settings().supabase_oauth_client_secret == ""


def test_supabase_oauth_client_secret_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_SECRET", "sb_secret_sentinel")
    assert Settings().supabase_oauth_client_secret == "sb_secret_sentinel"


def test_supabase_oauth_client_id_whitespace_only_value_normalizes_to_the_unset_sentinel(
    monkeypatch,
):
    """Same footgun as public_base_url: a pasted value with only whitespace
    would otherwise sail past the lifespan's falsy-value presence check."""
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_ID", "   ")
    assert Settings().supabase_oauth_client_id == ""


def test_supabase_oauth_client_id_surrounding_whitespace_is_stripped(monkeypatch):
    """A trailing newline from a copy-pasted dashboard value is exactly the
    plausible real-world case this guards against."""
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_ID", "  66666666-6666-4666-8666-666666666666\n")
    assert Settings().supabase_oauth_client_id == "66666666-6666-4666-8666-666666666666"


@pytest.mark.parametrize(
    "value",
    [
        '66666666-6666-4666-8666-666666666666"',  # breaks out of the JS string
        "66666666-6666-4666-8666-666666666666</script>",  # breaks out of the <script> tag
        "66666666-6666-4666-8666-666666666666<",
        "66666666-6666-4666-8666-666666666666>",
        "66666666-6666-4666-8666-666666666666\\",
        "'66666666-6666-4666-8666-666666666666'",
    ],
)
def test_malformed_supabase_oauth_client_id_is_rejected(monkeypatch, value):
    """client_id is templated raw into a <script> block the same way
    public_base_url is (onboarding/router.py's index()), so it needs the
    same injection-character rejection."""
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_ID", value)
    with pytest.raises(ValidationError, match="SUPABASE_OAUTH_CLIENT_ID"):
        Settings()


def test_supabase_oauth_client_secret_whitespace_only_value_normalizes_to_the_unset_sentinel(
    monkeypatch,
):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_SECRET", "   ")
    assert Settings().supabase_oauth_client_secret == ""


def test_supabase_oauth_client_secret_surrounding_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_SECRET", "  sb_secret_sentinel\n")
    assert Settings().supabase_oauth_client_secret == "sb_secret_sentinel"
