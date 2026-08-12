"""Settings validation: catches a misconfigured .env at startup rather than
silently reverting to unbounded/disabled behavior at runtime.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


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
