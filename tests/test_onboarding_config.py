"""Tests for onboarding/config.py — public_base_url reads from the real
process environment only (no .env/.env.config file: onboarding/ is a
separate deployed service, not sharing the review engine's config files).
See docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
section 5."""
from __future__ import annotations

from onboarding.config import Settings


def test_public_base_url_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert Settings().public_base_url == ""


def test_public_base_url_reads_from_environment(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://onboarding.example.com")
    assert Settings().public_base_url == "https://onboarding.example.com"
