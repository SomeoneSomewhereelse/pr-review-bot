from __future__ import annotations

from datetime import datetime, timezone

from app.formatting import format_placeholder
from app.github_app import COMMENT_MARKER

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_short_wait_is_rate_limit_wording_and_has_marker():
    body = format_placeholder(pr_number=42, retry_after=30.0, now=NOW)
    assert COMMENT_MARKER in body
    assert "PR #42" in body
    assert "rate limit" in body.lower()


def test_long_wait_is_daily_quota_wording_with_eta_and_marker():
    # 6 hours -> 18:00 UTC
    body = format_placeholder(pr_number=42, retry_after=6 * 3600, now=NOW)
    assert COMMENT_MARKER in body
    assert "daily" in body.lower()
    assert "18:00 UTC" in body
