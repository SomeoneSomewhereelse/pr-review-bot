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


def test_format_failure_has_marker_pr_and_attempts_no_error_text():
    from app.formatting import format_failure

    body = format_failure(pr_number=42, attempts=5)
    assert COMMENT_MARKER in body
    assert "PR #42" in body
    assert "5" in body                       # attempt count surfaced
    assert "traceback" not in body.lower()   # no raw error/exception text


def test_format_failure_singular_grammar():
    from app.formatting import format_failure

    body = format_failure(pr_number=1, attempts=1)
    assert "1 attempt" in body
    assert "1 attempts" not in body


def test_format_failure_footnote_submarkers_and_grammar():
    from app.formatting import format_failure_footnote
    from app.github_app import FAIL_NOTE_END, FAIL_NOTE_START

    body = format_failure_footnote(attempts=3)
    assert FAIL_NOTE_START in body and FAIL_NOTE_END in body
    assert "3 attempts" in body
    assert "traceback" not in body.lower()  # no raw error text

    single = format_failure_footnote(attempts=1)
    assert "1 attempt" in single and "1 attempts" not in single
