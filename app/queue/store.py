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


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(**{k: row[k] for k in row.keys()})


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Insert a pending ticket, or collapse onto the existing one for this PR.

    On conflict: update head_sha and re-arm to 'pending' (clearing not_before)
    UNLESS the ticket is currently 'running' — a running review is left to
    finish; the newer head_sha is still recorded.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tickets
              (repo_full_name, pr_number, head_sha, status, provider,
               not_before, attempts, comment_id, enqueued_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, NULL, 0, NULL, ?, ?)
            ON CONFLICT(repo_full_name, pr_number) DO UPDATE SET
              head_sha   = excluded.head_sha,
              status     = CASE WHEN tickets.status = 'running'
                                THEN 'running' ELSE 'pending' END,
              not_before = CASE WHEN tickets.status = 'running'
                                THEN tickets.not_before ELSE NULL END,
              updated_at = excluded.updated_at
            """,
            (repo_full_name, pr_number, head_sha, provider, now, now),
        )
        row = conn.execute(
            "SELECT id FROM tickets WHERE repo_full_name = ? AND pr_number = ?",
            (repo_full_name, pr_number),
        ).fetchone()
        return int(row["id"])


def claim_next_due(now: str) -> Ticket | None:
    """Claim the oldest due ticket (pending, or deferred whose not_before passed).

    Atomic: the UPDATE-to-running only succeeds if the row is still claimable,
    so a second concurrent claim of the same row is impossible.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'pending'
               OR (status = 'deferred' AND not_before IS NOT NULL AND not_before <= ?)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE tickets SET status = 'running', updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'deferred')",
            (now, row["id"]),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute("SELECT * FROM tickets WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_ticket(claimed)


def defer(ticket_id: int, not_before: str, now: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )


def mark_done(ticket_id: int, now: str, comment_id: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'done', comment_id = ?, updated_at = ? WHERE id = ?",
            (comment_id, now, ticket_id),
        )


def mark_failed(ticket_id: int, now: str, error: str | None = None) -> None:
    """Mark a ticket as failed after a non-rate-limit exception from attempt_review.

    Unlike a stuck 'running' ticket, 'failed' is NOT special-cased by
    ``enqueue_or_update``'s CASE logic (which only protects 'running'), so a
    fresh push to a failed PR re-arms it to 'pending' normally. ``error`` is
    accepted for future use (e.g. logging/inspection) but is not persisted in
    a column today — the schema has no error column.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'failed', updated_at = ? WHERE id = ?",
            (now, ticket_id),
        )


def recover_on_startup(now: str) -> None:
    """Reset any ticket interrupted mid-review (crash) back to pending."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (now,),
        )


def get_ticket(ticket_id: int) -> Ticket | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None
