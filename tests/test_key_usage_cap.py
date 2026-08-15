"""Per-key daily usage cap: the pure usage-day bucket helper and the
`reviews`-derived usage query that the dispatcher's pre-flight cap check
reads (design doc 2026-08-15-key-usage-cap-design.md §3).

usage_bucket_start is pure and needs no database; the get_key_usage tests
below use the shared Postgres test harness (tests/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from app.queue import store


def _utc(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


RESET = time(4, 0)


def test_bucket_start_is_today_when_now_is_after_the_reset():
    assert store.usage_bucket_start(_utc(2026, 1, 1, 12, 0), RESET) == _utc(2026, 1, 1, 4, 0)


def test_bucket_start_is_yesterday_when_now_is_before_the_reset():
    assert store.usage_bucket_start(_utc(2026, 1, 1, 3, 59), RESET) == _utc(2025, 12, 31, 4, 0)


def test_bucket_start_at_the_exact_reset_instant_is_today():
    """The reset instant itself starts the NEW window (>=, not >), so a review
    landing exactly on the boundary is accounted to the fresh day."""
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 0), RESET) == _utc(2026, 1, 1, 4, 0)


def test_bucket_start_respects_minute_granularity():
    reset = time(4, 30)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 29), reset) == _utc(2025, 12, 31, 4, 30)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 31), reset) == _utc(2026, 1, 1, 4, 30)


def test_bucket_start_respects_second_granularity():
    reset = time(4, 30, 15)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 30, 14), reset) == _utc(
        2025, 12, 31, 4, 30, 15
    )
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 30, 16), reset) == _utc(
        2026, 1, 1, 4, 30, 15
    )


def test_bucket_start_with_midnight_reset_is_calendar_day():
    midnight = time(0, 0)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 0, 0), midnight) == _utc(2026, 1, 1, 0, 0)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 23, 59), midnight) == _utc(2026, 1, 1, 0, 0)


def test_bucket_start_crosses_a_month_boundary_backwards():
    assert store.usage_bucket_start(_utc(2026, 3, 1, 1, 0), RESET) == _utc(2026, 2, 28, 4, 0)
