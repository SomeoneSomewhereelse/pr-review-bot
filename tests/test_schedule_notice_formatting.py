from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
