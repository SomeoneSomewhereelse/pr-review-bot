"""scripts/reset_queue.py -- the manual clean-slate reset for the
tickets/reviews tables. Uses the shared Postgres test harness (`db`) since
this script writes to (and, with --yes, truncates) the same tables the
service reads."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from config import settings
from review_queue import store
from scripts import reset_queue

RENDER_SERVICES = "https://api.render.com/v1/services"

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def _enqueue(pr):
    return store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha", provider="groq",
        now=NOW.isoformat(),
    )


def test_dry_run_reports_counts_without_truncating(capsys):
    tid = _enqueue(1)
    _enqueue(2)
    assert reset_queue.main([]) == 0
    out = capsys.readouterr().out
    assert "would remove 2 ticket row(s)" in out
    assert "re-run with --yes" in out
    assert store.get_ticket(tid) is not None  # row still present, not truncated


def test_dry_run_previews_the_render_reachability_status(monkeypatch, capsys):
    """Regression test: the reachability check must run (and print) on the
    dry-run path too, not only inside the --yes branch -- otherwise the dry
    run's stated purpose (preview what a real run would target) is
    inverted, and an operator sees no indication of which database --yes
    would actually hit until it's too late to back out."""
    monkeypatch.setattr(settings, "render_api_key", "")
    assert reset_queue.main([]) == 0
    out = capsys.readouterr().out
    assert "could not verify against Render (no RENDER_API_KEY)" in out


def test_yes_truncates_and_prints_removed_counts(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_api_key", "")
    tid = _enqueue(1)
    assert reset_queue.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "tickets: 1 row(s) removed" in out
    assert "reviews: 0 row(s) removed" in out
    assert store.get_ticket(tid) is None


def test_yes_prints_the_render_reachability_status_before_truncating(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(
                200, json=[{"service": {"id": "srv-1", "name": "pr-review-engine"}}]
            )
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=[{"envVar": {"key": "DATABASE_URL", "value": settings.database_url}}],
            )
        )
        assert reset_queue.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "verified against the live Render service" in out
