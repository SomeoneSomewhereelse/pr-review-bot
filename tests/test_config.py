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
