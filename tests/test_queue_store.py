"""Ticket store: enqueue/collapse, atomic claim, defer, recover-on-startup.

Uses the shared Postgres test harness (``db``/``db_exec``/``db_query`` from
tests/conftest.py) — a real Postgres, truncated between tests.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"
FUTURE = "2026-01-01T18:00:00+00:00"
PAST = "2026-01-01T06:00:00+00:00"
T_COOL = "2026-01-01T12:05:00+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def _enqueue(repo="owner/repo", pr=1, sha="sha1", provider="groq", now=T0):
    return store.enqueue_or_update(
        repo_full_name=repo, pr_number=pr, head_sha=sha, provider=provider, now=now
    )


def test_enqueue_creates_pending_ticket():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.repo_full_name == "owner/repo"
    assert t.pr_number == 1
    assert t.head_sha == "sha1"
    assert t.provider == "groq"
    assert t.not_before is None


def test_enqueue_same_pr_collapses_and_updates_head_sha():
    tid1 = _enqueue(sha="sha1")
    tid2 = _enqueue(sha="sha2", now=T1)
    assert tid1 == tid2  # one row per (repo, pr)
    assert store.get_ticket(tid1).head_sha == "sha2"


def test_claim_next_due_returns_pending_and_marks_running():
    tid = _enqueue()
    claimed = store.claim_next_due(now=T1)
    assert claimed.id == tid
    assert store.get_ticket(tid).status == "running"


def test_claim_next_due_returns_none_when_empty():
    assert store.claim_next_due(now=T1) is None


def test_claim_is_fifo_by_enqueued_at():
    a = _enqueue(pr=1, now=T0)
    _enqueue(pr=2, now=T1)
    assert store.claim_next_due(now=T1).id == a


def test_deferred_ticket_not_claimed_before_not_before():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    assert store.claim_next_due(now=T1) is None            # not yet due
    assert store.claim_next_due(now=FUTURE).id == tid       # due now


def test_recover_on_startup_resets_running_to_pending():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.recover_on_startup(now=T1)
    assert store.get_ticket(tid).status == "pending"


def test_defer_rate_limited_does_not_increment_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 0


def test_defer_failed_increments_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1


def test_retrying_ticket_is_claimable_once_not_before_passes():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=T1, now=T0)
    claimed = store.claim_next_due(now=T1)
    assert claimed.id == tid
    assert store.get_ticket(tid).status == "running"


def test_mark_failed_sets_status_failed():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T1, error="boom")
    t = store.get_ticket(tid)
    assert t.status == "failed"
    assert t.updated_at == T1


def test_push_during_deferred_rides_out_keeping_not_before():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)   # provider wait
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"       # not reset to pending
    assert t.not_before == FUTURE       # provider clock NOT shortened
    assert t.head_sha == "sha2"         # latest commit recorded


def test_push_during_retrying_rides_out_keeping_not_before_and_attempts():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.defer_failed(tid, not_before=FUTURE, now=T0)  # hard-failure backoff, attempts -> 1
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "retrying"       # not reset to pending
    assert t.not_before == FUTURE       # failure backoff deadline NOT shortened/reset
    assert t.attempts == 1              # not reset by the push
    assert t.head_sha == "sha2"         # latest commit recorded


def test_push_during_running_sets_rereview_flag_and_keeps_running():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "running"
    assert t.rereview_requested == 1
    assert t.head_sha == "sha2"


def test_push_to_done_ticket_within_cooldown_re_arms_deferred(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.finalize_review(
        tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0
    )  # done, last_reviewed_at=T0
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL       # last_reviewed_at(T0) + 300s
    assert t.attempts == 0


def test_push_to_done_ticket_past_cooldown_re_arms_pending(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=FUTURE
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.not_before is None


def test_recover_on_startup_clears_rereview_flag(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)                       # -> running
    db_exec("UPDATE tickets SET rereview_requested = 1 WHERE id = %s", (tid,))
    store.recover_on_startup(now=T1)
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.rereview_requested == 0


def test_new_ticket_has_rereview_and_last_reviewed_defaults():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.rereview_requested == 0
    assert t.last_reviewed_at is None


def test_finalize_review_without_flag_marks_done():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=7, comment_id=99
    )
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.comment_id == 99
    assert t.last_reviewed_at == T1
    assert t.not_before is None
    # non-dirty -> level unchanged (passed value ignored)
    assert store.get_ticket(tid).cooldown_level == 0


def test_finalize_review_none_comment_id_does_not_erase_persisted_id():
    """A future completion path that forgets to pass a real comment_id must not
    silently demote the ticket back to scan-only identity (losing the id)."""
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=0, comment_id=555
    )
    assert store.get_ticket(tid).comment_id == 555

    store.finalize_review(
        tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=0, comment_id=None
    )
    assert store.get_ticket(tid).comment_id == 555   # NOT overwritten to None


def test_finalize_review_with_flag_re_arms_deferred_at_cooldown_and_resets_attempts(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_failed(tid, not_before=T0, now=T0)   # attempts -> 1
    store.claim_next_due(now=T0)          # -> running again
    # Simulate a push during the run setting the dirty flag:
    db_exec("UPDATE tickets SET rereview_requested = 1 WHERE id = %s", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=2)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.attempts == 0
    assert t.rereview_requested == 0
    assert t.last_reviewed_at == T1
    assert t.cooldown_level == 2  # dirty -> stores passed level


def test_finalize_review_dirty_flag_stores_passed_cooldown_level(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)
    db_exec("UPDATE tickets SET rereview_requested = 1 WHERE id = %s", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=3)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.cooldown_level == 3


def test_enqueue_or_update_serializes_under_concurrent_writers():
    """Two threads enqueue the same PR concurrently. With SELECT ... FOR UPDATE
    they serialize (row lock taken up front) rather than interleave: both
    complete without error and the final row is consistent. Serialization
    smoke test — a true race needs threads and is timing-dependent, so this
    asserts the observable invariant (no lost/corrupt write), not a specific
    interleaving."""
    import threading

    tid = _enqueue(pr=50, sha="sha0")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(sha: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.enqueue_or_update(
                repo_full_name="owner/repo", pr_number=50,
                head_sha=sha, provider="groq", now=T1,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in ("shaA", "shaB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []                          # no lock error / no crash
    final = store.get_ticket(tid)
    assert final.head_sha in ("shaA", "shaB")    # a consistent, complete write won
    assert final.status == "pending"


T_400 = "2026-01-01T12:06:40+00:00"   # T0 (12:00:00) + 400s


def _make_done(db_exec, tid, last_reviewed_at, level, now=T0):
    """Directly force a ticket to a completed state (bypasses finalize_review so
    Task 3 tests don't depend on finalize's Task-4 signature)."""
    db_exec(
        "UPDATE tickets SET status='done', last_reviewed_at=%s, cooldown_level=%s, "
        "updated_at=%s WHERE id=%s",
        (last_reviewed_at, level, now, tid),
    )


def test_due_after_cooldown_branches(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    # never reviewed -> pending, level 0
    assert store._due_after_cooldown(None, T1, 0) == ("pending", None, 0)
    # within window (level 0 -> 300s; T0=12:00:00, T1=12:00:01) -> deferred, escalate to 1
    assert store._due_after_cooldown(T0, T1, 0) == ("deferred", T_COOL, 1)
    # elapsed -> pending, reset to 0
    assert store._due_after_cooldown(T0, FUTURE, 0) == ("pending", None, 0)
    # at level 1 the window is 600s; a push 400s after T0 is still within -> deferred, escalate to 2
    status, nb, lvl = store._due_after_cooldown(T0, T_400, 1)
    assert status == "deferred"
    assert nb == "2026-01-01T12:10:00+00:00"   # T0 + 600s
    assert lvl == 2


def test_enqueue_push_within_cooldown_escalates_level(monkeypatch, db_exec):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(sha="sha1")
    _make_done(db_exec, tid, last_reviewed_at=T0, level=1)   # last review T0, already at level 1 (eff=600s)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T_400
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == "2026-01-01T12:10:00+00:00"   # T0 + 600s
    assert t.cooldown_level == 2                          # escalated
    assert t.head_sha == "sha2"


def test_enqueue_push_after_cooldown_resets_level(monkeypatch, db_exec):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(sha="sha1")
    _make_done(db_exec, tid, last_reviewed_at=T0, level=3)   # window eff(3)=2400s (until 12:40)
    store.enqueue_or_update(   # FUTURE = 18:00, well past the window -> quiet -> reset
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=FUTURE
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.not_before is None
    assert t.cooldown_level == 0


def test_effective_cooldown_escalates_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    assert store.effective_cooldown(0) == 300.0     # level 0 == today's flat cooldown
    assert store.effective_cooldown(1) == 600.0
    assert store.effective_cooldown(2) == 1200.0
    assert store.effective_cooldown(3) == 2400.0
    assert store.effective_cooldown(4) == 3600.0    # 300*16=4800 -> capped
    assert store.effective_cooldown(50) == 3600.0   # capped, no 2**50 blowup


def test_effective_cooldown_never_drops_below_base_when_cap_misconfigured(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 100.0)
    assert store.effective_cooldown(0) == 300.0     # base wins over a misconfigured lower cap


def test_next_cooldown_level_increments_and_guards():
    assert store.next_cooldown_level(0) == 1
    assert store.next_cooldown_level(4) == 5
    assert store.next_cooldown_level(30) == 30      # _MAX_COOLDOWN_LEVEL guard


def test_new_ticket_has_cooldown_level_zero():
    tid = _enqueue()
    assert store.get_ticket(tid).cooldown_level == 0


def test_finalize_non_dirty_leaves_nonzero_cooldown_level(db_exec):
    tid = _enqueue()
    store.claim_next_due(now=T0)
    db_exec(
        "UPDATE tickets SET cooldown_level = 3 WHERE id = %s", (tid,)
    )  # rereview_requested stays 0
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=9)
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.cooldown_level == 3   # non-dirty -> ELSE keeps the existing level, ignores the passed 9


def test_new_ticket_has_notice_not_before_none():
    tid = _enqueue()
    assert store.get_ticket(tid).notice_not_before is None


def _seed_deferred_with_review(db_exec, tid, not_before, notice_not_before=None, last_reviewed_at=T0):
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, notice_not_before=%s, "
        "last_reviewed_at=%s WHERE id=%s",
        (not_before, notice_not_before, last_reviewed_at, tid),
    )


def test_tickets_needing_notice_matches_never_notified(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
    result = store.tickets_needing_notice(now=T0)
    assert [t.id for t in result] == [tid]


def test_tickets_needing_notice_matches_stale_marker(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=T_COOL)
    result = store.tickets_needing_notice(now=T0)
    assert [t.id for t in result] == [tid]


def test_tickets_needing_notice_excludes_up_to_date_marker(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=FUTURE)
    assert store.tickets_needing_notice(now=T0) == []


def test_tickets_needing_notice_excludes_no_visible_review(db_exec):
    tid = _enqueue()
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, notice_not_before=NULL "
        "WHERE id=%s",
        (FUTURE, tid),
    )
    assert store.tickets_needing_notice(now=T0) == []


def test_tickets_needing_notice_excludes_already_due_ticket(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=PAST, notice_not_before=None)
    assert store.tickets_needing_notice(now=T0) == []


def test_tickets_needing_notice_excludes_retrying_status(db_exec):
    tid = _enqueue()
    db_exec(
        "UPDATE tickets SET status='retrying', not_before=%s, last_reviewed_at=%s, "
        "notice_not_before=NULL WHERE id=%s",
        (FUTURE, T0, tid),
    )
    assert store.tickets_needing_notice(now=T0) == []


def test_mark_notice_posted_persists_marker(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
    store.mark_notice_posted(tid, FUTURE)
    assert store.get_ticket(tid).notice_not_before == FUTURE
    assert store.tickets_needing_notice(now=T0) == []


def test_clear_notice_resets_marker_to_none(db_exec):
    tid = _enqueue()
    _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=FUTURE)
    store.clear_notice(tid)
    assert store.get_ticket(tid).notice_not_before is None


def test_tickets_needing_notice_respects_batch_cap(monkeypatch, db_exec):
    monkeypatch.setattr(settings, "dispatcher_notice_sweep_batch_size", 2)
    tids = []
    for pr in range(1, 4):  # 3 tickets, cap is 2
        tid = _enqueue(pr=pr, now=T0)
        _seed_deferred_with_review(db_exec, tid, not_before=FUTURE, notice_not_before=None)
        tids.append(tid)

    first_batch = store.tickets_needing_notice(now=T0)
    assert len(first_batch) == 2
    assert [t.id for t in first_batch] == tids[:2]  # oldest-enqueued first

    for t in first_batch:
        store.mark_notice_posted(t.id, FUTURE)

    second_batch = store.tickets_needing_notice(now=T0)
    assert [t.id for t in second_batch] == tids[2:]  # the leftover ticket, picked up next "tick"


def test_schema_columns_match_the_ticket_dataclass(db_query):
    """_row_to_ticket does Ticket(**row), so _SCHEMA and the dataclass must agree
    exactly -- drift is a production TypeError. This also makes the hosted run's
    column check (plan Task 9) a machine-checked invariant rather than a number
    written down in a doc."""
    rows = db_query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tickets'"
    )
    assert {row[0] for row in rows} == set(store.Ticket.__dataclass_fields__)
