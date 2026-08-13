"""Dispatcher step logic: burst (RPM defer + later run) and daily-wall defer.

Tests drive process_next_due(now) directly with an injected clock and stubbed
attempt_review — the infinite run_forever loop is a thin wrapper and is not
unit-tested. Uses the shared Postgres test harness and a cleared blocked_until map.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.providers import active
from app.queue import cooldown_config, dispatcher, store
import app.orchestrator as orchestrator

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(db, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "groq")
    dispatcher.reset_blocked_until()
    active.reset_override_cache()
    yield
    dispatcher.reset_blocked_until()
    active.reset_override_cache()


@pytest.fixture(autouse=True)
def _clean_cooldown_cache():
    cooldown_config.reset_override_cache()
    yield
    cooldown_config.reset_override_cache()


@pytest.fixture(autouse=True)
def _clean_key_index_cache():
    from app.providers import key_index

    key_index.reset_override_cache()
    yield
    key_index.reset_override_cache()


def _enqueue(pr, now=NOW):
    return store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha", provider="groq",
        now=now.isoformat(),
    )


def _stub_comments(monkeypatch):
    posted = []
    monkeypatch.setattr(dispatcher.github_app, "upsert_comment",
                        lambda repo, pr, body, comment_id=None: posted.append(
                            (pr, body, comment_id)
                        ))
    return posted


def _set_comment_id(db_exec, tid, comment_id):
    db_exec("UPDATE tickets SET comment_id = %s WHERE id = %s", (comment_id, tid))


async def test_idle_when_no_tickets(monkeypatch):
    _stub_comments(monkeypatch)
    result = await dispatcher.process_next_due(NOW)
    assert result.action == "idle"


async def test_completed_ticket_runs_and_marks_done(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=1)

    async def fake_attempt(repo, pr, comment_id=None):
        review = type("R", (), {})()
        return orchestrator.ReviewCompleted(review=review)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_rate_limited_ticket_defers_posts_placeholder_and_blocks(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=2)
    _set_comment_id(db_exec, tid, 202)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=30)).isoformat()
    assert posted and posted[0][0] == 2            # placeholder posted on PR 2
    assert posted[0][2] == 202                     # threaded comment_id preserved
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=30)


async def test_blocked_provider_defers_without_calling_attempt(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=3)
    _set_comment_id(db_exec, tid, 303)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert called == []                            # never fired a doomed call
    assert posted and "rate limit" in posted[0][1].lower()
    assert posted[0][2] == 303                     # threaded comment_id preserved


async def test_first_hard_failure_defers_with_backoff_not_terminal(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=5)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("github api exploded")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"          # retryable, NOT terminal
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()  # base backoff


async def test_hard_stop_marks_failed_and_posts_failure_comment(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)  # first failure is terminal
    tid = _enqueue(pr=8)

    async def boom(repo, pr, comment_id=None):
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

    async def rl(repo, pr, comment_id=None):
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

    async def attempt_then_push(repo, pr, comment_id=None):
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
    async def ok(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    result = await dispatcher.process_next_due(NOW + timedelta(seconds=300))
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_dispatcher_escalates_cooldown_on_churn_completion(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(pr=30)
    db_exec("UPDATE tickets SET cooldown_level = 1 WHERE id = %s", (tid,))

    async def attempt_then_push(repo, pr, comment_id=None):
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=30, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=600)).isoformat()   # effective_cooldown(1)
    assert t.cooldown_level == 2                                        # next_cooldown_level(1)
    assert posted == []


async def test_push_during_running_then_deferred_run_does_not_survive_to_next_success(monkeypatch):
    """A push mid-run sets the dirty flag, but if THAT run gets deferred
    (rate-limited here) instead of completing, the flag must not survive to
    the later successful run -- claim_next_due clears it on claim, so the
    flag from the earlier push is considered satisfied by the run that is
    about to happen. Regression test for the stale-flag bug (Finding 1)."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(pr=11)

    async def attempt_then_push_then_rate_limited(repo, pr, comment_id=None):
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

    async def ok(repo, pr, comment_id=None):
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
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    _enqueue(pr=7)  # ticket.provider == "groq" (stale, from _enqueue helper)
    dispatcher._blocked_until["gemini"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
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
    store.finalize_review(
        tid, now=NOW.isoformat(), rereview_not_before=NOW.isoformat(), rereview_cooldown_level=0
    )
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha2",
        provider="groq", now=NOW.isoformat(),
    )
    return tid


async def test_gate_does_not_overwrite_good_review_with_placeholder(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(20, monkeypatch)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    async def fake_attempt(repo, pr, comment_id=None):
        raise AssertionError("attempt_review must not run while blocked")

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # good review preserved; no placeholder posted


async def test_rate_limited_outcome_does_not_overwrite_good_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(21, monkeypatch)

    async def rl(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # no placeholder over the good review


async def test_rate_limited_outcome_then_sweep_posts_schedule_notice(monkeypatch):
    """End-to-end path for the second gap the feature closed: a ticket with a
    visible good review that gets rate-limited on its next attempt must not
    stay fully silent -- the following post_pending_notices sweep should pick
    up exactly the row shape process_next_due's RateLimited path produces and
    post a schedule notice for it."""
    posted = _stub_comments(monkeypatch)
    notices = _stub_append_schedule(monkeypatch)
    tid = _reviewed_then_pushed(23, monkeypatch)

    async def rl(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert posted == []  # no placeholder over the good review

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert notices and notices[0][0] == 23
    assert store.get_ticket(tid).notice_not_before == store.get_ticket(tid).not_before


def _stub_footnotes(monkeypatch):
    appended = []
    monkeypatch.setattr(dispatcher.github_app, "append_review_footnote",
                        lambda repo, pr, footnote, comment_id=None: appended.append(
                            (pr, footnote, comment_id)
                        ))
    return appended


def _stub_clear_schedule(monkeypatch):
    cleared = []
    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice",
                        lambda repo, pr, comment_id=None: cleared.append((pr, comment_id)))
    return cleared


async def test_terminal_failure_appends_footnote_when_good_review_exists(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _reviewed_then_pushed(22, monkeypatch)
    _set_comment_id(db_exec, tid, 2222)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert appended and appended[0][0] == 22   # footnote appended
    assert appended[0][2] == 2222               # threaded comment_id preserved
    assert posted == []                         # good review NOT overwritten


async def test_terminal_failure_overwrites_when_no_good_review(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _enqueue(pr=24)  # fresh: last_reviewed_at is None
    _set_comment_id(db_exec, tid, 2424)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 24        # overwrite via upsert_comment
    assert "could not be completed" in posted[0][1].lower()
    assert posted[0][2] == 2424                  # threaded comment_id preserved
    assert appended == []                        # no footnote when nothing to preserve


async def test_terminal_notice_post_failure_defers_instead_of_stranding(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=25)  # fresh -> overwrite path

    def boom_post(repo, pr, body, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"           # NOT failed (visibility guaranteed first)
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()


async def test_repeated_notice_post_failure_eventually_goes_terminal(monkeypatch):
    """Regression test for the unbounded-retry-loop finding: if the terminal
    notice itself keeps failing to post, forever, the ticket must eventually
    give up and go 'failed' rather than looping in 'retrying' indefinitely.
    """
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_max_notice_post_attempts", 3)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_failure_max_backoff_seconds", 300.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=26)  # fresh -> overwrite path (upsert_comment)

    def boom_post(repo, pr, body, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    now = NOW
    result = None
    for _ in range(20):  # plenty more than the notice-post ceiling
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        result = await dispatcher.process_next_due(now)
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        assert t.status == "retrying"
        # Advance past not_before so the next iteration can claim it again.
        now = datetime.fromisoformat(t.not_before) + timedelta(seconds=1)

    final = store.get_ticket(tid)
    assert final.status == "failed"          # terminal reached, not looping forever
    assert result.action == "failed"


async def test_daily_wall_defers_then_runs_after_reset(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=4)

    async def rate_limited(repo, pr, comment_id=None):
        return orchestrator.ReviewRateLimited(retry_after=6 * 3600)

    monkeypatch.setattr(dispatcher, "attempt_review", rate_limited)
    await dispatcher.process_next_due(NOW)
    assert store.get_ticket(tid).status == "deferred"

    # Before reset: nothing is due.
    assert (await dispatcher.process_next_due(NOW + timedelta(hours=1))).action == "idle"

    # After reset: blocked_until has passed, ticket runs.
    async def ok(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    later = NOW + timedelta(hours=7)
    result = await dispatcher.process_next_due(later)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_completed_review_persists_returned_comment_id(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=60)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=4242)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).comment_id == 4242


async def test_attempt_review_is_called_with_ticket_comment_id(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=61)
    db_exec("UPDATE tickets SET comment_id = 909 WHERE id = %s", (tid,))
    seen = {}

    async def fake_attempt(repo, pr, comment_id=None):
        seen["comment_id"] = comment_id
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=909)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    await dispatcher.process_next_due(NOW)
    assert seen["comment_id"] == 909   # ticket's stored id passed into attempt_review


async def test_sustained_churn_escalates_then_plateaus(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(pr=40)

    async def complete_with_push(repo, pr, comment_id=None):
        # a push lands during every review -> dirty flag -> re-arm each cycle
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=40, head_sha="s",
            provider="groq", now="2026-01-01T00:00:00+00:00",
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", complete_with_push)

    t = NOW
    for secs in (300, 600, 1200, 2400, 3600, 3600):   # levels 0..5, plateau at the 3600 cap
        result = await dispatcher.process_next_due(t)
        assert result.action == "ran"
        tk = store.get_ticket(tid)
        assert tk.status == "deferred"
        assert tk.not_before == (t + timedelta(seconds=secs)).isoformat()
        t = t + timedelta(seconds=secs)   # advance to the next due time


async def test_claim_clears_schedule_notice_when_one_was_pending(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    cleared = _stub_clear_schedule(monkeypatch)
    tid = _enqueue(pr=70)
    _set_comment_id(db_exec, tid, 7070)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
    )

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=7070)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert cleared == [(70, 7070)]
    assert store.get_ticket(tid).notice_not_before is None


async def test_claim_does_not_call_clear_when_no_notice_pending(monkeypatch):
    _stub_comments(monkeypatch)
    cleared = _stub_clear_schedule(monkeypatch)
    _enqueue(pr=71)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert cleared == []


async def test_claim_clear_failure_does_not_block_review_attempt(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=72)
    _set_comment_id(db_exec, tid, 7272)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
    )

    def boom_clear(repo, pr, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice", boom_clear)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=7272)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"                       # review still proceeded
    assert store.get_ticket(tid).notice_not_before == "2026-01-01T11:00:00+00:00"


def _stub_append_schedule(monkeypatch):
    posted = []
    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice",
                        lambda repo, pr, footnote, comment_id=None: posted.append(
                            (pr, footnote, comment_id)
                        ))
    return posted


async def test_post_pending_notices_posts_for_matching_ticket(monkeypatch, db_exec):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=80)
    _set_comment_id(db_exec, tid, 8080)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert posted and posted[0][0] == 80
    assert posted[0][2] == 8080
    assert "13:00 UTC" in posted[0][1]
    assert store.get_ticket(tid).notice_not_before == future.isoformat()


async def test_post_pending_notices_does_not_repost_when_marker_matches(monkeypatch, db_exec):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=81)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), future.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 0
    assert posted == []


async def test_post_pending_notices_per_ticket_failure_does_not_block_others(monkeypatch, db_exec):
    tid1 = _enqueue(pr=82)
    tid2 = _enqueue(pr=83)
    future = NOW + timedelta(hours=1)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid1),
    )
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        (future.isoformat(), NOW.isoformat(), tid2),
    )

    calls = []

    def flaky_append(repo, pr, footnote, comment_id=None):
        calls.append(pr)
        if pr == 82:
            raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice", flaky_append)

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1                                       # only the successful one counted
    assert set(calls) == {82, 83}                            # both attempted
    assert store.get_ticket(tid1).notice_not_before is None  # failed post -> marker not set
    assert store.get_ticket(tid2).notice_not_before == future.isoformat()


async def test_claimed_ticket_runs_against_the_db_override(monkeypatch):
    """The behavioral guarantee: a mid-session override changes which provider
    actually runs, with no restart and no redeploy."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    store.set_provider_override("groq", NOW.isoformat())
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from app.providers.active import active_provider
        seen.append(active_provider())
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    assert seen == ["groq"]


async def test_claim_falls_back_to_env_when_the_override_read_fails(monkeypatch):
    """Fail-safe: an unreachable override must degrade to the configured
    provider, never abort the review, and never keep serving a stale cached
    override from a previous successful refresh."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    # A prior successful refresh cached a DIFFERENT provider. If the failure
    # handler merely logged and left the cache alone, active_provider() would
    # keep returning "groq" forever -- this is what catches that.
    active.set_override_cache("groq")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_provider_override", boom)
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from app.providers.active import active_provider
        seen.append(active_provider())
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    assert seen == ["gemini"]
    assert result.action == "ran"


async def test_claimed_ticket_uses_the_db_key_index_override(monkeypatch):
    """The behavioral guarantee: a mid-session key-index override changes
    which credential slot actually resolves, with no restart and no
    redeploy."""
    _stub_comments(monkeypatch)
    store.set_key_index_override("groq", 2, NOW.isoformat())
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from app.providers import key_index
        seen.append(key_index.active_key_index("groq"))
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    assert seen == [2]


async def test_claim_falls_back_to_index_zero_when_the_key_index_read_fails(monkeypatch):
    """Fail-safe: an unreachable override must degrade to index 0, never
    abort the review, and never keep serving a stale cached override from a
    previous successful refresh."""
    from app.providers import key_index

    _stub_comments(monkeypatch)
    # A prior successful refresh cached a DIFFERENT index. If the failure
    # handler merely logged and left the cache alone, active_key_index()
    # would keep returning 2 forever -- this is what catches that.
    key_index.set_override_cache({"groq": 2})

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_all_key_index_overrides", boom)
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        seen.append(key_index.active_key_index("groq"))
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    assert seen == [0]
    assert result.action == "ran"


async def test_claimed_ticket_uses_the_db_cooldown_override(monkeypatch):
    """The behavioral guarantee: a mid-session cooldown override changes the
    next scheduled re-review, with no restart and no redeploy."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 2.0)
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=NOW.isoformat())

    async def fake_attempt(repo, pr, comment_id=None):
        # A push lands mid-review -> dirty flag -> the cooldown actually gets
        # consulted on the re-arm path (see store.finalize_review).
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=1, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    t = store.get_ticket(1)
    expected = NOW + timedelta(seconds=30.0)  # level 0 -> base override, not env 300s
    assert t.not_before == expected.isoformat()


async def test_claim_falls_back_to_env_cooldown_when_the_override_read_fails(monkeypatch):
    """Fail-safe: an unreachable cooldown override must degrade to the
    configured env defaults, never abort the review, and never keep serving a
    stale cached override from a previous successful refresh."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 2.0)
    # A prior successful refresh cached a DIFFERENT base. If the failure
    # handler merely logged and left the cache alone, effective_cooldown would
    # keep using 30.0 forever -- this is what catches that.
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_cooldown_overrides", boom)

    async def fake_attempt(repo, pr, comment_id=None):
        # A push lands mid-review -> dirty flag -> the cooldown actually gets
        # consulted on the re-arm path (see store.finalize_review).
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=1, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    t = store.get_ticket(1)
    expected = NOW + timedelta(seconds=300.0)  # env default, not the stale 30.0
    assert t.not_before == expected.isoformat()
    assert result.action == "ran"
