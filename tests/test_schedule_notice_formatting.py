from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.formatting import format_schedule_notice
from app.github_app import SCHEDULE_NOTE_END, SCHEDULE_NOTE_START

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_format_schedule_notice_has_markers_and_absolute_utc_time():
    body = format_schedule_notice(NOW)
    assert body.startswith(SCHEDULE_NOTE_START)
    assert body.endswith(SCHEDULE_NOTE_END)
    assert "12:00 UTC" in body
    assert "Re-review scheduled" in body


def test_format_schedule_notice_reflects_the_given_not_before():
    later = NOW + timedelta(hours=2, minutes=30)
    body = format_schedule_notice(later)
    assert "14:30 UTC" in body


def test_format_schedule_notice_normalizes_non_utc_timezone():
    plus_five = timezone(timedelta(hours=5))
    local_time = datetime(2026, 1, 1, 17, 0, 0, tzinfo=plus_five)  # 17:00+05:00 == 12:00 UTC
    body = format_schedule_notice(local_time)
    assert "12:00 UTC" in body
    assert "17:00 UTC" not in body


def test_format_schedule_notice_rejects_naive_datetime():
    naive = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        format_schedule_notice(naive)


def test_usage_cap_schedule_notice_is_distinct_and_still_markered():
    body = format_schedule_notice(NOW, reason="usage_cap")
    assert body.startswith(SCHEDULE_NOTE_START)
    assert body.endswith(SCHEDULE_NOTE_END)
    assert "12:00 UTC" in body
    assert "usage limit" in body.lower()
    assert "not a provider quota issue" in body.lower()


def test_schedule_notice_default_reason_is_byte_identical_to_the_old_output():
    """Every existing call site passes no `reason` -- their output must not
    shift by a single character (design doc §4.1)."""
    assert format_schedule_notice(NOW) == format_schedule_notice(NOW, reason="provider")
    assert format_schedule_notice(NOW) == (
        f"{SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~12:00 UTC\n{SCHEDULE_NOTE_END}"
    )


def test_usage_cap_schedule_notice_still_rejects_a_naive_datetime():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        format_schedule_notice(naive, reason="usage_cap")
