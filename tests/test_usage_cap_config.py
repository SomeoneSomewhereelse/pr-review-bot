"""The usage cap actually in force: a DB override when set and valid, else the
env-configured value. Mirrors tests/test_cooldown_config.py."""
from __future__ import annotations

from datetime import time

import pytest

from bot.config import settings
from bot.queue import usage_cap_config


@pytest.fixture(autouse=True)
def _clean_cache():
    usage_cap_config.reset_override_cache()
    yield
    usage_cap_config.reset_override_cache()


def test_falls_back_to_env_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    assert usage_cap_config.effective_caps() == (5000, time(4, 0))


def test_override_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(20000, "06:30")
    assert usage_cap_config.effective_caps() == (20000, time(6, 30))


def test_reset_time_accepts_seconds():
    usage_cap_config.set_override_cache(None, "23:59:30")
    assert usage_cap_config.effective_caps()[1] == time(23, 59, 30)


def test_an_unparseable_reset_time_discards_the_whole_pair(monkeypatch):
    """All-or-nothing, exactly like cooldown_config: a bad field must never
    pair with a stale override in the other field."""
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(20000, "not-a-time")
    assert usage_cap_config.effective_caps() == (5000, time(4, 0))


def test_a_non_positive_cap_discards_the_whole_pair(monkeypatch):
    """A 0 cap makes the dispatcher's `tokens >= cap` comparison
    unconditionally true -- every ticket deferred forever, and STICKILY, since
    not_before is already a real future timestamp by then."""
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(0, "06:30")
    assert usage_cap_config.effective_caps() == (5000, time(4, 0))


def test_a_partial_override_fills_the_rest_from_env(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(None, "06:30")
    assert usage_cap_config.effective_caps() == (5000, time(6, 30))


def test_effective_caps_returns_a_token_cap_and_a_reset_time(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 20_000)
    tokens, reset = usage_cap_config.effective_caps()
    assert tokens == 20_000
    assert reset == settings.key_usage_reset_time_utc
