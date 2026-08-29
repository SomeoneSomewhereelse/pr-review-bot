"""Per-key daily usage cap: the pure usage-day bucket helper and the
`reviews`-derived usage query that the dispatcher's pre-flight cap check
reads (design doc 2026-08-15-key-usage-cap-design.md §3).

usage_bucket_start is pure and needs no database; the get_key_usage tests
below use the shared Postgres test harness (tests/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from bot.queue import store
from bot.specialists.schemas import ReviewResult, SpecialistResult


@pytest.fixture
def _temp_db(db):
    """Opt-in: only the get_key_usage tests below touch the database; the
    usage_bucket_start tests above are pure and must stay DB-free."""
    yield


def _review(provider: str, tokens_in: int, tokens_out: int, cost: float) -> ReviewResult:
    return ReviewResult(
        pr_number=1,
        provider=provider,
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=1)],
        total_elapsed_ms=1,
        total_tokens_in=tokens_in,
        total_tokens_out=tokens_out,
        est_cost_usd=cost,
    )


def _record(provider="groq", key_index=0, created_at="2026-01-01T12:00:00+00:00",
            tokens_in=100, tokens_out=50, cost=0.01):
    review = _review(provider, tokens_in, tokens_out, cost)
    store.record_review("owner/repo", 1, review, None, created_at, key_index)


SINCE = "2026-01-01T04:00:00+00:00"


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


def test_get_key_usage_on_empty_table_is_zero(_temp_db):
    assert store.get_key_usage("groq", 0, SINCE) == 0


def test_get_key_usage_sums_tokens_for_the_matching_slot(_temp_db):
    _record(tokens_in=100, tokens_out=50, cost=0.01)
    _record(tokens_in=200, tokens_out=25, cost=0.02)
    assert store.get_key_usage("groq", 0, SINCE) == 375  # (100+50) + (200+25)


def test_get_key_usage_counts_tokens_from_an_unpriced_review(_temp_db):
    """A review on an unpriced model persists NULL est_cost_usd (design spec
    2026-08-18 section 6a) but real token counts. The cap must still see
    those tokens: a cost-based total would silently under-count here, because
    SQL SUM skips NULLs -- which is exactly why the cost half of this
    function's return value was removed rather than merely left unused."""
    _record(tokens_in=100, tokens_out=50, cost=None)
    assert store.get_key_usage("groq", 0, SINCE) == 150


def test_get_key_usage_ignores_other_key_slots(_temp_db):
    _record(key_index=0, tokens_in=100, tokens_out=0, cost=0.01)
    _record(key_index=1, tokens_in=900, tokens_out=0, cost=0.09)
    assert store.get_key_usage("groq", 0, SINCE) == 100
    assert store.get_key_usage("groq", 1, SINCE) == 900


def test_get_key_usage_ignores_other_providers(_temp_db):
    _record(provider="groq", tokens_in=100, tokens_out=0, cost=0.01)
    _record(provider="vertex", tokens_in=900, tokens_out=0, cost=0.09)
    assert store.get_key_usage("groq", 0, SINCE) == 100


def test_get_key_usage_treats_a_null_key_index_as_slot_zero(_temp_db, db_exec):
    """Rows written before the key_index column existed are NULL; they must
    count toward slot 0, the same nullable-means-index-0 convention the
    runtime_config override columns already use (design doc §3)."""
    db_exec(
        "INSERT INTO reviews (repo_full_name, pr_number, provider, model, comment_id, "
        "created_at, total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, "
        "results) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb)",
        ("owner/repo", 7, "groq", "m", None, "2026-01-01T12:00:00+00:00", 1, 40, 10, 0.005),
    )
    assert store.get_key_usage("groq", 0, SINCE) == 50


def test_get_key_usage_excludes_rows_before_the_window(_temp_db):
    _record(created_at="2026-01-01T03:59:59+00:00", tokens_in=900, tokens_out=0, cost=0.09)
    _record(created_at="2026-01-01T12:00:00+00:00", tokens_in=100, tokens_out=0, cost=0.01)
    assert store.get_key_usage("groq", 0, SINCE) == 100


def test_get_key_usage_includes_a_row_exactly_at_since(_temp_db):
    """`since` is inclusive -- it is the bucket-start instant itself, which
    belongs to the new window (see usage_bucket_start)."""
    _record(created_at=SINCE, tokens_in=100, tokens_out=0, cost=0.01)
    assert store.get_key_usage("groq", 0, SINCE) == 100
