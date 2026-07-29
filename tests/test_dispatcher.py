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


async def test_first_hard_failure_defers_with_backoff_not_terminal(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=5)

    async def boom(repo, pr):
        raise RuntimeError("github api exploded")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"          # retryable, NOT terminal
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()  # base backoff


async def test_hard_stop_marks_failed_and_posts_failure_comment(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)  # first failure is terminal
    tid = _enqueue(pr=8)

    async def boom(repo, pr):
        raise RuntimeError("still broken")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 8
    assert "could not be completed" in posted[0][1].lower()


async def test_rate_limited_zero_retry_after_is_floored(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_min_retry_after_seconds", 1.0)
    tid = _enqueue(pr=9)

    async def rl(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=0.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    await dispatcher.process_next_due(NOW)
    t = store.get_ticket(tid)
    assert t.not_before == (NOW + timedelta(seconds=1)).isoformat()   # floored, not now+0
    assert t.attempts == 0                                            # RL not counted
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=1)


async def test_push_during_running_triggers_one_cooldown_re_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(pr=10)

    async def attempt_then_push(repo, pr):
        # A push lands mid-review -> dirty flag on the running ticket.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=10, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    t = store.get_ticket(tid)
    assert t.status == "deferred"                                     # re-armed, not done
    assert t.not_before == (NOW + timedelta(seconds=300)).isoformat()  # at cooldown

    # During the cooldown wait: nothing due, and NO placeholder churn.
    posted.clear()
    assert (await dispatcher.process_next_due(NOW + timedelta(seconds=60))).action == "idle"
    assert posted == []

    # After cooldown: the re-review runs.
    async def ok(repo, pr):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    result = await dispatcher.process_next_due(NOW + timedelta(seconds=300))
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_push_during_running_then_deferred_run_does_not_survive_to_next_success(monkeypatch):
    """A push mid-run sets the dirty flag, but if THAT run gets deferred
    (rate-limited here) instead of completing, the flag must not survive to
    the later successful run -- claim_next_due clears it on claim, so the
    flag from the earlier push is considered satisfied by the run that is
    about to happen. Regression test for the stale-flag bug (Finding 1)."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(pr=11)

    async def attempt_then_push_then_rate_limited(repo, pr):
        # A push lands mid-review -> dirty flag set on the running ticket.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=11, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        # But THIS attempt itself gets rate-limited (deferred, not completed).
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push_then_rate_limited)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=30)).isoformat()

    # The next claim (once due) must clear the stale flag at claim time, so
    # a successful run does not spuriously re-arm for an extra re-review.
    posted.clear()

    async def ok(repo, pr):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    result = await dispatcher.process_next_due(NOW + timedelta(seconds=30))
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"   # NOT "deferred" for a bogus re-review


async def test_blocked_gate_uses_current_settings_provider_not_stale_ticket_provider(monkeypatch):
    """The ticket was enqueued under provider 'groq' (see _enqueue), but the
    _blocked_until gate must key off settings.llm_provider (the CURRENT
    provider actually used by attempt_review), which here we set to a
    different name to simulate LLM_PROVIDER having changed with a ticket
    still in flight."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "github_models")
    _enqueue(pr=7)  # ticket.provider == "groq" (stale, from _enqueue helper)
    dispatcher._blocked_until["github_models"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert called == []  # gated on the current provider's block, not the stale one
    assert posted and "rate limit" in posted[0][1].lower()


def _reviewed_then_pushed(pr, monkeypatch):
    """A ticket that HAS a completed review (last_reviewed_at set) and a pending
    re-review queued by a later push (cooldown 0 -> immediately claimable)."""
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 0.0)
    tid = _enqueue(pr=pr)
    store.claim_next_due(NOW.isoformat())
    store.finalize_review(tid, now=NOW.isoformat(), rereview_not_before=NOW.isoformat())
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha2",
        provider="groq", now=NOW.isoformat(),
    )
    return tid


async def test_gate_does_not_overwrite_good_review_with_placeholder(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(20, monkeypatch)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    async def fake_attempt(repo, pr):
        raise AssertionError("attempt_review must not run while blocked")

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # good review preserved; no placeholder posted


async def test_rate_limited_outcome_does_not_overwrite_good_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(21, monkeypatch)

    async def rl(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # no placeholder over the good review


def _stub_footnotes(monkeypatch):
    appended = []
    monkeypatch.setattr(dispatcher.github_app, "append_review_footnote",
                        lambda repo, pr, footnote: appended.append((pr, footnote)))
    return appended


async def test_terminal_failure_appends_footnote_when_good_review_exists(monkeypatch):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _reviewed_then_pushed(22, monkeypatch)

    async def boom(repo, pr):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert appended and appended[0][0] == 22   # footnote appended
    assert posted == []                         # good review NOT overwritten


async def test_terminal_failure_overwrites_when_no_good_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _enqueue(pr=24)  # fresh: last_reviewed_at is None

    async def boom(repo, pr):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 24        # overwrite via upsert_comment
    assert "could not be completed" in posted[0][1].lower()
    assert appended == []                        # no footnote when nothing to preserve


async def test_terminal_notice_post_failure_defers_instead_of_stranding(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=25)  # fresh -> overwrite path

    def boom_post(repo, pr, body):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"           # NOT failed (visibility guaranteed first)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()


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
