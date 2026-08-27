"""Tests for onboarding/config.py — public_base_url reads from the real
process environment only (no .env/.env.config file: onboarding/ is a
separate deployed service, not sharing the review engine's config files).
See docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
section 5."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from onboarding.config import Settings


def test_public_base_url_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert Settings().public_base_url == ""


def test_public_base_url_reads_from_environment(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://onboarding.example.com")
    assert Settings().public_base_url == "https://onboarding.example.com"


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


def test_whitespace_only_value_normalizes_to_the_unset_sentinel(monkeypatch):
    """onboarding/main.py's lifespan refuses to boot on a falsy value; a
    whitespace-only string would otherwise sail past that check and leave the
    service running with an unusable base URL."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "   ")
    assert Settings().public_base_url == ""


def test_surrounding_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "  https://onboarding.example.com  ")
    assert Settings().public_base_url == "https://onboarding.example.com"


def test_trailing_slash_is_stripped(monkeypatch):
    """A trailing slash would make index.html's buildManifest() emit
    `https://host//?gh_step=manifest`, and Starlette does not route `//` to
    `/` -- GitHub's redirect back would 404 *after* the visitor has already
    created a real App, whose one-time credentials are then unrecoverable.
    Same reason scripts/create_github_app.py, scripts/deploy.py and
    scripts/doctor.py all rstrip('/') their own base URL."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://onboarding.example.com/")
    assert Settings().public_base_url == "https://onboarding.example.com"


@pytest.mark.parametrize(
    "value",
    [
        'https://onboarding.example.com"',          # breaks out of the JS string
        'https://onboarding.example.com/</script>',  # breaks out of the <script> tag
        "https://onboarding.example.com<",
        "https://onboarding.example.com>",
        "https://onboarding.example.com/x y",        # embedded whitespace
        "https://onboarding.example.com?a=b",        # query string
        "https://onboarding.example.com#frag",       # fragment
        "onboarding.example.com",                    # no scheme
        "ftp://onboarding.example.com",              # wrong scheme
        "javascript:alert(1)",
        "https://",                                  # no host
        "///evil.example.com",
    ],
)
def test_malformed_public_base_url_is_rejected(monkeypatch, value):
    """The value is substituted raw into a <script> block on a page that
    holds a GitHub App private key in sessionStorage (onboarding/router.py's
    index()), so a quote or angle bracket in it is a script-injection vector,
    not a cosmetic problem."""
    monkeypatch.setenv("PUBLIC_BASE_URL", value)
    with pytest.raises(ValidationError):
        Settings()


def test_rejection_message_is_actionable(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "not-a-url")
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        Settings()


def test_supabase_oauth_client_id_whitespace_only_value_normalizes_to_the_unset_sentinel(monkeypatch):
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
        '66666666-6666-4666-8666-666666666666"',      # breaks out of the JS string
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


def test_supabase_oauth_client_secret_whitespace_only_value_normalizes_to_the_unset_sentinel(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_SECRET", "   ")
    assert Settings().supabase_oauth_client_secret == ""


def test_supabase_oauth_client_secret_surrounding_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CLIENT_SECRET", "  sb_secret_sentinel\n")
    assert Settings().supabase_oauth_client_secret == "sb_secret_sentinel"
