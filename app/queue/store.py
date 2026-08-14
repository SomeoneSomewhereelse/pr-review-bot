"""Durable ticket store (Postgres via psycopg3) — the queue's source of truth.

One row per (repo, pr): UNIQUE collapses re-triggers so a new push updates the
existing ticket's head_sha instead of stacking a duplicate. A ticket's persisted
``not_before`` prevents an early run after a restart. Timestamps are ISO-8601 UTC
TEXT (lexical comparison == chronological). Functions are synchronous; async
callers wrap them in asyncio.to_thread so Postgres network latency never blocks
the event loop.

Also owns the ``reviews`` table — an insert-only history of completed reviews
(provider, model, timing, tokens, cost, findings) that has no bearing on queue
lifecycle but backs the ``GET /`` / ``GET /api/dashboard`` ops/demo
page (``app/dashboard.py``) via the ``dashboard_*`` read helpers below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from app.config import settings
from app.providers import registry
from app.queue import cooldown_config
from app.specialists.schemas import ReviewResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    head_sha           TEXT,
    status             TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    not_before         TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    comment_id         BIGINT,
    enqueued_at        TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    rereview_requested INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at   TEXT,
    cooldown_level     INTEGER NOT NULL DEFAULT 0,
    notice_not_before  TEXT,
    UNIQUE (repo_full_name, pr_number)
);
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_error TEXT;
CREATE TABLE IF NOT EXISTS runtime_config (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider   TEXT,
    updated_at TEXT NOT NULL
);
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_base_seconds DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_max_seconds  DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_factor       DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS gemini_key_index INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS groq_key_index   INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS vertex_key_index INTEGER;
CREATE TABLE IF NOT EXISTS reviews (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    comment_id         BIGINT,
    created_at         TEXT    NOT NULL,
    total_elapsed_ms   INTEGER NOT NULL,
    total_tokens_in    INTEGER NOT NULL,
    total_tokens_out   INTEGER NOT NULL,
    est_cost_usd       DOUBLE PRECISION NOT NULL,
    results            JSONB   NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_created_at_idx ON reviews (created_at DESC);
"""

_pool: ConnectionPool | None = None

# Explicit rather than relying on psycopg_pool's default (same value), so a test
# can shrink it without waiting 30s for a connection that will never open.
_POOL_TIMEOUT_SECONDS = 30

_FIRST_CONNECT_HELP = (
    "could not open a Postgres connection at startup within {timeout:.0f}s. "
    "On a first deploy this is nearly always one of:\n"
    "  1. the database is still provisioning -- wait until it reports ready, "
    "then deploy again (a failed deploy is not retried automatically);\n"
    "  2. a pooler connection string whose username is missing its project "
    "suffix -- it must look like postgres.<project-ref>, not plain postgres;\n"
    "  3. a password containing characters that must be percent-encoded "
    "(@ # / ?).\n"
    "The driver's own error is logged above as \"error connecting in 'pool-1'\"."
)


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
    last_error: str | None


def _configure(conn) -> None:
    conn.row_factory = dict_row


def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema. Idempotent.

    A PoolTimeout here means the very first connection never succeeded, which on a
    hosted first deploy is nearly always a provisioning or connection-string
    problem rather than a transient blip. Re-raise it as a RuntimeError carrying
    the likely causes: the bare PoolTimeout reads like a hang, and the driver's
    real error is tens of lines further up the log. Startup still fails loudly
    (design spec section 11) -- RuntimeError matches _require_pool()'s convention
    and app/main.py's lifespan already documents it as the fail-loudly path. The
    message never includes settings.database_url, which carries the password.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=4,
            timeout=_POOL_TIMEOUT_SECONDS,
            configure=_configure,
            open=True,
        )
    try:
        with _pool.connection() as conn:
            conn.execute(_SCHEMA)
    except PoolTimeout as exc:
        raise RuntimeError(
            _FIRST_CONNECT_HELP.format(timeout=_POOL_TIMEOUT_SECONDS)
        ) from exc


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _require_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("store.init_pool() has not been called")
    return _pool


def _row_to_ticket(row: dict) -> Ticket:
    return Ticket(**row)


_MAX_COOLDOWN_LEVEL = 30


def effective_cooldown(level: int) -> float:
    """Escalated per-PR cooldown: min(base * factor^min(level, _MAX_COOLDOWN_LEVEL), cap).

    level 0 -> base (identical to a non-escalating cooldown, so normal PRs are
    unaffected). Each consecutive rapid re-review raises the level, geometrically
    lengthening the next wait, capped at the effective cap. base/cap/factor come
    from cooldown_config.effective_config() -- a DB override when set and valid,
    else the env-configured defaults.
    """
    base, cap, factor = cooldown_config.effective_config()
    return max(base, min(base * factor ** min(level, _MAX_COOLDOWN_LEVEL), cap))


def next_cooldown_level(level: int) -> int:
    """Level for the next re-review after a churn re-review (guarded against overflow)."""
    return min(level + 1, _MAX_COOLDOWN_LEVEL)


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


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Enqueue/update a ticket under the per-state re-review policy. The whole
    read-branch-write runs in one transaction; SELECT ... FOR UPDATE locks the
    row so a concurrent writer cannot interleave (Postgres analogue of the old
    SQLite BEGIN IMMEDIATE)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE repo_full_name = %s AND pr_number = %s FOR UPDATE",
            (repo_full_name, pr_number),
        ).fetchone()
        if row is None:
            inserted = conn.execute(
                """
                INSERT INTO tickets
                  (repo_full_name, pr_number, head_sha, status, provider, not_before,
                   attempts, comment_id, enqueued_at, updated_at, rereview_requested,
                   last_reviewed_at, cooldown_level, notice_not_before)
                VALUES (%s, %s, %s, 'pending', %s, NULL, 0, NULL, %s, %s, 0, NULL, 0, NULL)
                ON CONFLICT (repo_full_name, pr_number) DO NOTHING
                RETURNING id
                """,
                (repo_full_name, pr_number, head_sha, provider, now, now),
            ).fetchone()
            if inserted is not None:
                return int(inserted["id"])
            # A concurrent insert won the race; block on its lock, then read it.
            row = conn.execute(
                "SELECT * FROM tickets WHERE repo_full_name = %s AND pr_number = %s FOR UPDATE",
                (repo_full_name, pr_number),
            ).fetchone()

        status = row["status"]
        ticket_id = int(row["id"])
        if status == "running":
            conn.execute(
                "UPDATE tickets SET head_sha = %s, rereview_requested = 1, updated_at = %s "
                "WHERE id = %s",
                (head_sha, now, ticket_id),
            )
        elif status in ("pending", "deferred", "retrying"):
            conn.execute(
                "UPDATE tickets SET head_sha = %s, updated_at = %s WHERE id = %s",
                (head_sha, now, ticket_id),
            )
        else:  # 'done'/'failed' -> re-arm honoring the escalating cooldown
            new_status, not_before, new_level = _due_after_cooldown(
                row["last_reviewed_at"], now, row["cooldown_level"]
            )
            conn.execute(
                "UPDATE tickets SET head_sha = %s, status = %s, not_before = %s, attempts = 0, "
                "rereview_requested = 0, cooldown_level = %s, updated_at = %s WHERE id = %s",
                (head_sha, new_status, not_before, new_level, now, ticket_id),
            )
        return ticket_id


def claim_next_due(now: str) -> Ticket | None:
    """Atomically claim the oldest due ticket via FOR UPDATE SKIP LOCKED."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE tickets SET status = 'running', updated_at = %s, rereview_requested = 0
            WHERE id = (
                SELECT id FROM tickets
                WHERE status = 'pending'
                   OR (status IN ('deferred','retrying') AND not_before IS NOT NULL
                       AND not_before <= %s)
                ORDER BY enqueued_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (now, now),
        ).fetchone()
        return _row_to_ticket(row) if row else None


def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    """Per-provider rate-limit deferral. Does NOT count toward the hard stop."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = %s, "
            "updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    """Per-ticket hard-failure backoff. Sets status='retrying' (distinct from a
    cooldown/rate-limit 'deferred' wait, so a schedule notice never posts on a
    ticket that's actually silently retrying after an error) and increments
    attempts (drives backoff + hard stop)."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'retrying', not_before = %s, "
            "attempts = attempts + 1, updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


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
    with _require_pool().connection() as conn:
        conn.execute(
            """
            UPDATE tickets SET
              last_reviewed_at   = %(now)s,
              comment_id         = COALESCE(%(comment_id)s, comment_id),
              status             = CASE WHEN rereview_requested = 1 THEN 'deferred' ELSE 'done' END,
              not_before         = CASE WHEN rereview_requested = 1 THEN %(rnb)s ELSE NULL END,
              attempts           = CASE WHEN rereview_requested = 1 THEN 0 ELSE attempts END,
              cooldown_level     = CASE WHEN rereview_requested = 1
                                        THEN %(new_level)s ELSE cooldown_level END,
              rereview_requested = 0,
              updated_at         = %(now)s
            WHERE id = %(id)s
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
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'failed', last_error = %s, updated_at = %s WHERE id = %s",
            (error, now, ticket_id),
        )


def record_review(
    repo_full_name: str,
    pr_number: int,
    review: ReviewResult,
    comment_id: int | None,
    now: str,
) -> None:
    """Persist a completed review for the dashboard (insert-only).

    Callers must never let a failure here affect the review itself — the PR
    comment is already posted by the time this is called.
    """
    results = Jsonb([r.model_dump() for r in review.results])
    with _require_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO reviews
              (repo_full_name, pr_number, provider, model, comment_id, created_at,
               total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                repo_full_name, pr_number, review.provider, review.model, comment_id, now,
                review.total_elapsed_ms, review.total_tokens_in, review.total_tokens_out,
                review.est_cost_usd, results,
            ),
        )


_TICKET_STATUSES = ("pending", "running", "deferred", "retrying", "done", "failed")


def dashboard_stats() -> dict:
    """Aggregate totals across all recorded reviews (count, cost, avg elapsed)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(est_cost_usd), 0) AS cost, "
            "COALESCE(AVG(total_elapsed_ms), 0) AS avg_ms FROM reviews"
        ).fetchone()
    return {
        "total_reviews": int(row["n"]),
        "total_cost_usd": round(float(row["cost"]), 4),
        "avg_elapsed_ms": int(row["avg_ms"]),
    }


def dashboard_queue_counts() -> dict[str, int]:
    """Ticket counts per status, zero-filled for every known status."""
    counts = {status: 0 for status in _TICKET_STATUSES}
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
        ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts


def dashboard_reviews(limit: int = 50) -> list[dict]:
    """Most recent completed reviews, newest first, with a derived comment_url."""
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT repo_full_name, pr_number, provider, model, comment_id, created_at, "
            "total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results "
            "FROM reviews ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    reviews = []
    for row in rows:
        comment_url = None
        if row["comment_id"] is not None:
            comment_url = (
                f"https://github.com/{row['repo_full_name']}/pull/"
                f"{row['pr_number']}#issuecomment-{row['comment_id']}"
            )
        reviews.append({
            "repo": row["repo_full_name"],
            "pr_number": row["pr_number"],
            "provider": row["provider"],
            "model": row["model"],
            "created_at": row["created_at"],
            "elapsed_ms": row["total_elapsed_ms"],
            "tokens_in": row["total_tokens_in"],
            "tokens_out": row["total_tokens_out"],
            "est_cost_usd": row["est_cost_usd"],
            "comment_url": comment_url,
            "specialists": row["results"],
        })
    return reviews


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
    with _require_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'deferred'
              AND not_before IS NOT NULL
              AND not_before > %s
              AND last_reviewed_at IS NOT NULL
              AND (notice_not_before IS NULL OR notice_not_before != not_before)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT %s
            """,
            (now, settings.dispatcher_notice_sweep_batch_size),
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    """Record that a notice reflecting not_before was just posted. A single
    independent UPDATE -- not inside enqueue_or_update's or finalize_review's
    transactions, same pattern as mark_failed."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = %s WHERE id = %s", (not_before, ticket_id)
        )


def clear_notice(ticket_id: int) -> None:
    """Clear the notice marker after the dispatcher has stripped the schedule
    footnote from GitHub (called right after a ticket is claimed)."""
    with _require_pool().connection() as conn:
        conn.execute("UPDATE tickets SET notice_not_before = NULL WHERE id = %s", (ticket_id,))


def recover_on_startup(now: str) -> None:
    """Reset any ticket interrupted mid-review (crash) back to pending, clearing the
    dirty flag (the fresh pending review already covers the latest commit)."""
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', rereview_requested = 0, updated_at = %s "
            "WHERE status = 'running'",
            (now,),
        )


def get_ticket(ticket_id: int) -> Ticket | None:
    with _require_pool().connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None


def get_provider_override() -> str | None:
    """The provider override in force, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get("provider") or None


def set_provider_override(provider: str | None, now: str) -> None:
    """Set the override, or clear it with provider=None.

    Upserts the singleton row: CHECK (id = 1) makes a second row impossible, so
    there is never ambiguity about which row wins.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config (id, provider, updated_at) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET provider = EXCLUDED.provider, "
            "updated_at = EXCLUDED.updated_at",
            (provider, now),
        )


def get_cooldown_overrides() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) overrides in force, or (None, None, None) when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT cooldown_base_seconds, cooldown_max_seconds, cooldown_factor "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    if row is None:
        return (None, None, None)
    return (row["cooldown_base_seconds"], row["cooldown_max_seconds"], row["cooldown_factor"])


def set_cooldown_override(
    base: float | None, cap: float | None, factor: float | None, now: str
) -> None:
    """Set the (base, cap, factor) override triple, or clear a field with None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. Writes exactly the three values it's given; a
    caller wanting to change only one field is responsible for reading the
    current triple first (see scripts/set_cooldown.py).
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config "
            "(id, cooldown_base_seconds, cooldown_max_seconds, cooldown_factor, updated_at) "
            "VALUES (1, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "cooldown_base_seconds = EXCLUDED.cooldown_base_seconds, "
            "cooldown_max_seconds = EXCLUDED.cooldown_max_seconds, "
            "cooldown_factor = EXCLUDED.cooldown_factor, "
            "updated_at = EXCLUDED.updated_at",
            (base, cap, factor, now),
        )


def get_key_index_override(provider: str) -> int | None:
    """The API-key-slot index override for `provider`, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get(column)


def set_key_index_override(provider: str, index: int | None, now: str) -> None:
    """Set the override for `provider`, or clear it with index=None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. `column` is looked up through
    registry.KEY_INDEX_COLUMNS -- a hardcoded whitelist of exactly three
    names -- and never built from `provider` directly; psycopg parameterizes
    values but not column identifiers, so this lookup IS the injection
    guard, not an optimization.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with _require_pool().connection() as conn:
        conn.execute(
            f"INSERT INTO runtime_config (id, {column}, updated_at) VALUES (1, %s, %s) "
            f"ON CONFLICT (id) DO UPDATE SET {column} = EXCLUDED.{column}, "
            "updated_at = EXCLUDED.updated_at",
            (index, now),
        )


def get_all_key_index_overrides() -> dict[str, int]:
    """{provider: index} for every provider with a non-null override.

    One query reading all three columns -- the dispatcher calls this once
    per claimed ticket, not once per provider.
    """
    columns = registry.KEY_INDEX_COLUMNS
    select = ", ".join(columns.values())
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {select} FROM runtime_config WHERE id = 1").fetchone()
    if row is None:
        return {}
    return {
        provider: row[column] for provider, column in columns.items() if row[column] is not None
    }
