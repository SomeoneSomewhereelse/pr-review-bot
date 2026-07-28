"""Dispatcher step logic: burst (RPM defer + later run) and daily-wall defer.

Tests drive process_next_due(now) directly with an injected clock and stubbed
attempt_review — the infinite run_forever loop is a thin wrapper and is not
unit-tested. Uses a temp DB and a cleared blocked_until map.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.queue import dispatcher, store
import app.orchestrator as orchestrator

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "queue_db_path", str(tmp_path / "queue.db"))
    monkeypatch.setattr(settings, "llm_provider", "groq")
    store.init_db()
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


def _enqueue(pr, now=NOW):
    return store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha", provider="groq",
        now=now.isoformat(),
    )


def _stub_comments(monkeypatch):
    posted = []
    monkeypatch.setattr(dispatcher.github_app, "upsert_comment",
                        lambda repo, pr, body: posted.append((pr, body)))
    return posted


async def test_idle_when_no_tickets(monkeypatch):
    _stub_comments(monkeypatch)
    result = await dispatcher.process_next_due(NOW)
    assert result.action == "idle"


async def test_completed_ticket_runs_and_marks_done(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=1)

    async def fake_attempt(repo, pr):
        review = type("R", (), {})()
        return orchestrator.ReviewCompleted(review=review)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_rate_limited_ticket_defers_posts_placeholder_and_blocks(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=2)

    async def fake_attempt(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=30)).isoformat()
    assert posted and posted[0][0] == 2            # placeholder posted on PR 2
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=30)


async def test_blocked_provider_defers_without_calling_attempt(monkeypatch):
    posted = _stub_comments(monkeypatch)
    _enqueue(pr=3)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert called == []                            # never fired a doomed call
    assert posted and "rate limit" in posted[0][1].lower()


async def test_daily_wall_defers_then_runs_after_reset(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=4)

    async def rate_limited(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=6 * 3600)

    monkeypatch.setattr(dispatcher, "attempt_review", rate_limited)
    await dispatcher.process_next_due(NOW)
    assert store.get_ticket(tid).status == "deferred"

    # Before reset: nothing is due.
    assert (await dispatcher.process_next_due(NOW + timedelta(hours=1))).action == "idle"

    # After reset: blocked_until has passed, ticket runs.
    async def ok(repo, pr):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    later = NOW + timedelta(hours=7)
    result = await dispatcher.process_next_due(later)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"
