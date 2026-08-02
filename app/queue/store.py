"""Durable ticket store (stdlib sqlite3) — the queue's source of truth.

One row per (repo, pr): UNIQUE collapses re-triggers so a new push updates the
existing ticket's head_sha instead of stacking a duplicate review. A ticket's
persisted ``not_before`` is what actually prevents an early run after a restart
(the dispatcher's in-memory blocked_until is only a soft optimization).

Times are ISO-8601 UTC strings, passed in by the caller so tests are
deterministic. sqlite writes are sub-millisecond at demo scale; the dispatcher
and webhook call these directly (documented acceptable, single-instance).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    repo_full_name  TEXT    NOT NULL,
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT,
    status          TEXT    NOT NULL,
    provider        TEXT    NOT NULL,
    not_before      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    comment_id      INTEGER,
    enqueued_at     TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    rereview_requested INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TEXT,
    cooldown_level  INTEGER NOT NULL DEFAULT 0,
    notice_not_before TEXT,
    UNIQUE(repo_full_name, pr_number)
);
"""


@dataclass
class Ticket:
    id: int
    repo_full_name: str
    pr_number: int
    head_sha: str | None
    status: str
    provider: str
    not_before: str | None
    attempts: int
    comment_id: int | None
    enqueued_at: str
    updated_at: str
    rereview_requested: int
    last_reviewed_at: str | None
    cooldown_level: int
    notice_not_before: str | None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.queue_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema, if missing (idempotent)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    if "rereview_requested" not in existing:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN rereview_requested INTEGER NOT NULL DEFAULT 0"
        )
    if "last_reviewed_at" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_reviewed_at TEXT")
    if "cooldown_level" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN cooldown_level INTEGER NOT NULL DEFAULT 0")
    if "notice_not_before" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN notice_not_before TEXT")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(**{k: row[k] for k in row.keys()})


_MAX_COOLDOWN_LEVEL = 30


def effective_cooldown(level: int) -> float:
    """Escalated per-PR cooldown: min(base * 2^min(level, _MAX_COOLDOWN_LEVEL), cap).

    level 0 -> base (identical to a non-escalating cooldown, so normal PRs are
    unaffected). Each consecutive rapid re-review raises the level, geometrically
    lengthening the next wait, capped at dispatcher_rereview_cooldown_max_seconds.
    """
    base = settings.dispatcher_rereview_cooldown_seconds
    cap = settings.dispatcher_rereview_cooldown_max_seconds
    return max(base, min(base * 2 ** min(level, _MAX_COOLDOWN_LEVEL), cap))


def next_cooldown_level(level: int) -> int:
    """Level for the next re-review after a churn re-review (guarded against overflow)."""
    return min(level + 1, _MAX_COOLDOWN_LEVEL)


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Enqueue a review ticket, applying the per-state re-review policy.

    - no row        -> insert 'pending'
    - 'pending'     -> update head_sha, stay pending (first review not yet run)
    - 'deferred'/'retrying' -> ride out: update head_sha only; keep
                       status/not_before (a push cannot shorten a
                       provider/cooldown wait or a failure backoff)
    - 'running'     -> update head_sha + set rereview_requested (dirty flag)
    - 'done'/'failed' -> re-arm via cooldown helper; escalate cooldown_level
                       on churn, reset to 0 when cooldown elapsed; always
                       reset attempts to 0
    """
    # Atomic against claim_next_due/finalize_review even off the event loop:
    # BEGIN IMMEDIATE takes the write lock up front, so no concurrent writer can
    # interleave between this SELECT and its UPDATE. Invariants that keep this
    # deadlock-free (see the design doc's Finding 3): the body opens no second
    # connection and calls no other store function (_due_after_cooldown is pure),
    # and the write lock is always released via commit/rollback + close in finally.
    conn = _connect()
    conn.isolation_level = None  # manual transaction control (issue our own BEGIN)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM tickets WHERE repo_full_name = ? AND pr_number = ?",
                (repo_full_name, pr_number),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO tickets
                      (repo_full_name, pr_number, head_sha, status, provider,
                       not_before, attempts, comment_id, enqueued_at, updated_at,
                       rereview_requested, last_reviewed_at)
                    VALUES (?, ?, ?, 'pending', ?, NULL, 0, NULL, ?, ?, 0, NULL)
                    ON CONFLICT(repo_full_name, pr_number) DO NOTHING
                    """,
                    (repo_full_name, pr_number, head_sha, provider, now, now),
                )
                row = conn.execute(
                    "SELECT id FROM tickets WHERE repo_full_name = ? AND pr_number = ?",
                    (repo_full_name, pr_number),
                ).fetchone()
                ticket_id = int(row["id"])
            else:
                status = row["status"]
                ticket_id = int(row["id"])
                if status == "running":
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, rereview_requested = 1, "
                        "updated_at = ? WHERE id = ?",
                        (head_sha, now, ticket_id),
                    )
                elif status in ("pending", "deferred", "retrying"):
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, updated_at = ? WHERE id = ?",
                        (head_sha, now, ticket_id),
                    )
                else:  # 'done'/'failed' -> re-arm honoring the escalating cooldown
                    new_status, not_before, new_level = _due_after_cooldown(
                        row["last_reviewed_at"], now, row["cooldown_level"]
                    )
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, status = ?, not_before = ?, "
                        "attempts = 0, rereview_requested = 0, cooldown_level = ?, "
                        "updated_at = ? WHERE id = ?",
                        (head_sha, new_status, not_before, new_level, now, ticket_id),
                    )
            conn.execute("COMMIT")
            return ticket_id
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def claim_next_due(now: str) -> Ticket | None:
    """Claim the oldest due ticket (pending, or deferred/retrying whose not_before passed).

    Atomic: the UPDATE-to-running only succeeds if the row is still claimable,
    so a second concurrent claim of the same row is impossible.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'pending'
               OR (status IN ('deferred', 'retrying') AND not_before IS NOT NULL
                   AND not_before <= ?)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE tickets SET status = 'running', updated_at = ?, rereview_requested = 0 "
            "WHERE id = ? AND status IN ('pending', 'deferred', 'retrying')",
            (now, row["id"]),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute("SELECT * FROM tickets WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_ticket(claimed)


def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    """Per-provider rate-limit deferral. Does NOT count toward the hard stop."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = ?, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )


def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    """Per-ticket hard-failure backoff. Sets status='retrying' (distinct from a
    cooldown/rate-limit 'deferred' wait, so a schedule notice never posts on a
    ticket that's actually silently retrying after an error) and increments
    attempts (drives backoff + hard stop)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'retrying', not_before = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )


def _due_after_cooldown(
    last_reviewed_at: str | None, now: str, level: int
) -> tuple[str, str | None, int]:
    """Re-arm state + next escalation level, honoring the escalating cooldown.

    Churn (within effective_cooldown(level) of last completed review):
      → ('deferred', due, next_cooldown_level(level)).
    Quiet or never-reviewed:
      → ('pending', None, 0); escalation resets.
    """
    if last_reviewed_at is None:
        return ("pending", None, 0)
    due = datetime.fromisoformat(last_reviewed_at) + timedelta(seconds=effective_cooldown(level))
    if datetime.fromisoformat(now) < due:
        return ("deferred", due.isoformat(), next_cooldown_level(level))
    return ("pending", None, 0)


def finalize_review(
    ticket_id: int,
    now: str,
    rereview_not_before: str,
    rereview_cooldown_level: int,
    comment_id: int | None = None,
) -> None:
    """Finalize a completed review, resolving the dirty flag in one statement.

    Always records last_reviewed_at + comment_id. If a push set rereview_requested
    during the run, re-arm to 'deferred' at rereview_not_before with a fresh
    attempts budget and store the escalated rereview_cooldown_level; otherwise mark
    'done' and leave the level unchanged (latent — the next push resolves it).
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE tickets SET
              last_reviewed_at   = :now,
              comment_id         = COALESCE(:comment_id, comment_id),
              status             = CASE WHEN rereview_requested = 1 THEN 'deferred' ELSE 'done' END,
              not_before         = CASE WHEN rereview_requested = 1 THEN :rnb ELSE NULL END,
              attempts           = CASE WHEN rereview_requested = 1 THEN 0 ELSE attempts END,
              cooldown_level     = CASE WHEN rereview_requested = 1 THEN :new_level ELSE cooldown_level END,
              rereview_requested = 0,
              updated_at         = :now
            WHERE id = :id
            """,
            {
                "now": now,
                "comment_id": comment_id,
                "rnb": rereview_not_before,
                "new_level": rereview_cooldown_level,
                "id": ticket_id,
            },
        )


def mark_failed(ticket_id: int, now: str, error: str | None = None) -> None:
    """Mark a ticket as failed after a non-rate-limit exception from attempt_review.

    A push to a 'failed' (or 'done') ticket is handled by
    ``enqueue_or_update``'s terminal-state branch: it calls
    ``_due_after_cooldown`` and re-arms the ticket to 'pending' (cooldown
    elapsed, or no prior successful review) or 'deferred' (still cooling down
    from the last completed review), escalating/resetting ``cooldown_level``
    per the escalation policy and resetting ``attempts`` to 0 either way.
    ``error`` is accepted for future use (e.g. logging/inspection) but is not
    persisted in a column today — the schema has no error column.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'failed', updated_at = ? WHERE id = ?",
            (now, ticket_id),
        )


def tickets_needing_notice(now: str) -> list[Ticket]:
    """Deferred (schedule-wait, never retry-backoff since 'retrying' is a
    distinct status) tickets with a visible prior review whose schedule has
    changed since the last notice was posted (or none was posted yet).
    Excludes a ticket whose not_before has already passed -- it is about to
    be claimed for a real review, so a "scheduled" note for a time that's
    already gone would be wrong. Capped at dispatcher_notice_sweep_batch_size
    per call so a mass re-arm can't stall process_next_due for a whole
    dispatcher tick; any ticket past the cap keeps its stale marker and is
    picked up by the next call (self-healing, no new state)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'deferred'
              AND not_before IS NOT NULL
              AND not_before > ?
              AND last_reviewed_at IS NOT NULL
              AND (notice_not_before IS NULL OR notice_not_before != not_before)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT ?
            """,
            (now, settings.dispatcher_notice_sweep_batch_size),
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    """Record that a notice reflecting not_before was just posted. A single
    independent UPDATE -- not inside enqueue_or_update's or finalize_review's
    transactions, same pattern as mark_failed."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = ? WHERE id = ?",
            (not_before, ticket_id),
        )


def clear_notice(ticket_id: int) -> None:
    """Clear the notice marker after the dispatcher has stripped the schedule
    footnote from GitHub (called right after a ticket is claimed)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = NULL WHERE id = ?",
            (ticket_id,),
        )


def recover_on_startup(now: str) -> None:
    """Reset any ticket interrupted mid-review (crash) back to pending, clearing the
    dirty flag (the fresh pending review already covers the latest commit)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', rereview_requested = 0, updated_at = ? "
            "WHERE status = 'running'",
            (now,),
        )


def get_ticket(ticket_id: int) -> Ticket | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None
