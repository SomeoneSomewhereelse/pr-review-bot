"""Pure exponential-backoff math for hard-failure retries (jitter injected)."""
from __future__ import annotations

import pytest

from app.config import settings
from app.queue import dispatcher


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_failure_max_backoff_seconds", 300.0)


def test_first_attempt_is_base():
    assert dispatcher.compute_backoff(1, jitter=0.0) == 2.0


def test_backoff_doubles_each_attempt():
    assert dispatcher.compute_backoff(2, jitter=0.0) == 4.0
    assert dispatcher.compute_backoff(3, jitter=0.0) == 8.0
    assert dispatcher.compute_backoff(4, jitter=0.0) == 16.0


def test_backoff_is_capped():
    # 2 * 2**19 would be ~1M; capped at 300.
    assert dispatcher.compute_backoff(20, jitter=0.0) == 300.0


def test_jitter_is_added_on_top():
    assert dispatcher.compute_backoff(1, jitter=5.0) == 7.0


def test_jitter_seam_returns_zero_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_backoff_jitter_seconds", 0.0)
    assert dispatcher._jitter() == 0.0
