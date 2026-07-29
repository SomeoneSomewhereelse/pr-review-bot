"""Ticket store: enqueue/collapse, atomic claim, defer, recover-on-startup.

Uses a temp DB path via monkeypatching settings.queue_db_path (matching the
codebase's settings-monkeypatch convention). No network, no real time.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.queue import store


def _column_names(db_path: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"
FUTURE = "2026-01-01T18:00:00+00:00"
PAST = "2026-01-01T06:00:00+00:00"
T_COOL = "2026-01-01T12:05:00+00:00"


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "queue_db_path", str(tmp_path / "queue.db"))
    store.init_db()
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


def test_mark_done_sets_status_and_comment_id():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_done(tid, now=T1, comment_id=555)
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.comment_id == 555


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
    assert t.status == "deferred"
    assert t.attempts == 1


def test_mark_failed_sets_status_failed():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T1, error="boom")
    t = store.get_ticket(tid)
    assert t.status == "failed"
    assert t.updated_at == T1


def test_mark_failed_ticket_is_re_armed_to_pending_by_a_fresh_push():
    """A 'failed' ticket is NOT special-cased by enqueue_or_update's CASE
    logic (which only protects 'running'), so a subsequent push to that PR
    re-arms it to pending, unlike a stuck 'running' ticket would."""
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T0, error="boom")
    assert store.get_ticket(tid).status == "failed"

    tid2 = _enqueue(sha="sha2", now=T1)
    assert tid2 == tid
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.head_sha == "sha2"


def test_new_ticket_has_rereview_and_last_reviewed_defaults():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.rereview_requested == 0
    assert t.last_reviewed_at is None


def test_init_db_migrates_a_pre_existing_table_missing_new_columns(tmp_path, monkeypatch):
    import sqlite3

    db = str(tmp_path / "old.db")
    monkeypatch.setattr(settings, "queue_db_path", db)
    # Create an OLD-shape table without the two new columns.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY, repo_full_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL, head_sha TEXT, status TEXT NOT NULL,
                provider TEXT NOT NULL, not_before TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, comment_id INTEGER,
                enqueued_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(repo_full_name, pr_number)
            )
            """
        )
    store.init_db()  # must add the missing columns, not crash
    cols = _column_names(db)
    assert "rereview_requested" in cols
    assert "last_reviewed_at" in cols


def test_finalize_review_without_flag_marks_done():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, comment_id=99)
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.comment_id == 99
    assert t.last_reviewed_at == T1
    assert t.not_before is None


def test_finalize_review_with_flag_re_arms_deferred_at_cooldown_and_resets_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_failed(tid, not_before=T0, now=T0)   # attempts -> 1
    store.claim_next_due(now=T0)          # -> running again
    # Simulate a push during the run setting the dirty flag:
    import sqlite3
    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET rereview_requested = 1 WHERE id = ?", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.attempts == 0
    assert t.rereview_requested == 0
    assert t.last_reviewed_at == T1


def test_due_after_cooldown_branches():
    assert store._due_after_cooldown(None, T1, 300.0) == ("pending", None)
    # last review at T0 (12:00:00), cooldown 300s -> due at 12:05:00
    status, nb = store._due_after_cooldown(T0, T1, 300.0)   # T1 is 12:00:01, still cooling
    assert status == "deferred"
    assert nb == T_COOL
    assert store._due_after_cooldown(T0, FUTURE, 300.0) == ("pending", None)  # long past cooldown
