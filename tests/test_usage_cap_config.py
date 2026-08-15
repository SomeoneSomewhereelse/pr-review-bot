"""The usage caps actually in force: a DB override when set and valid, else the
env-configured values. Mirrors tests/test_cooldown_config.py."""
from __future__ import annotations

from datetime import time

import pytest

from app.config import settings
from app.queue import usage_cap_config


@pytest.fixture(autouse=True)
def _clean_cache():
    usage_cap_config.reset_override_cache()
    yield
    usage_cap_config.reset_override_cache()


def test_falls_back_to_env_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    assert usage_cap_config.effective_caps() == (5000, 0.25, time(4, 0))


def test_override_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(20000, 0.5, "06:30")
    assert usage_cap_config.effective_caps() == (20000, 0.5, time(6, 30))


def test_reset_time_accepts_seconds():
    usage_cap_config.set_override_cache(None, None, "23:59:30")
    assert usage_cap_config.effective_caps()[2] == time(23, 59, 30)


def test_an_unparseable_reset_time_discards_the_whole_trio(monkeypatch):
    """All-or-nothing, exactly like cooldown_config: a bad field must never
    pair with a stale override in another field."""
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(20000, 0.5, "not-a-time")
    assert usage_cap_config.effective_caps() == (5000, 0.25, time(4, 0))


def test_a_non_positive_cap_discards_the_whole_trio(monkeypatch):
    """A 0 cap makes the dispatcher's `tokens >= cap` comparison
    unconditionally true -- every ticket deferred forever, and STICKILY, since
    not_before is already a real future timestamp by then."""
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(0, 0.5, "06:30")
    assert usage_cap_config.effective_caps() == (5000, 0.25, time(4, 0))


def test_a_partial_override_fills_the_rest_from_env(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(None, 0.75, None)
    assert usage_cap_config.effective_caps() == (5000, 0.75, time(4, 0))
