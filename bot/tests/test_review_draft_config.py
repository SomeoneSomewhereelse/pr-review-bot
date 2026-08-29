"""Whether draft PRs get reviewed: a DB override when set, else the
env-configured value. Mirrors tests/test_cooldown_config.py /
tests/test_usage_cap_config.py."""
from __future__ import annotations

import pytest

from bot.config import settings
from bot.queue import review_draft_config


@pytest.fixture(autouse=True)
def _clean_cache():
    review_draft_config.reset_override_cache()
    yield
    review_draft_config.reset_override_cache()


def test_falls_back_to_env_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "review_draft_prs", False)
    assert review_draft_config.effective_review_draft_prs() is False


def test_override_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "review_draft_prs", False)
    review_draft_config.set_override_cache(True)
    assert review_draft_config.effective_review_draft_prs() is True


def test_override_of_false_wins_over_a_true_env_default(monkeypatch):
    monkeypatch.setattr(settings, "review_draft_prs", True)
    review_draft_config.set_override_cache(False)
    assert review_draft_config.effective_review_draft_prs() is False


def test_reset_override_cache_restores_env_fallback(monkeypatch):
    monkeypatch.setattr(settings, "review_draft_prs", False)
    review_draft_config.set_override_cache(True)
    review_draft_config.reset_override_cache()
    assert review_draft_config.effective_review_draft_prs() is False
