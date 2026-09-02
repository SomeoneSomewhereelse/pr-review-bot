"""onboarding/session_store.py — the wizard's server-side session store.

Replaces the stateless-relay invariant onboarding/CLAUDE.md used to state:
mobile browsers were found to destroy sessionStorage (and the browsing
context holding it) mid-flow, so wizard progress now lives here instead,
identified by a cookie rather than anything tab-scoped. See
docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md.

Each frame's data is stored as ONE Fernet-encrypted blob per frame (not
field-by-field) inside the `frame_data` JSONB column -- simpler than
classifying which individual fields are secrets per frame, and every value
inside is encrypted either way since the whole container is. Only
get_session()/the router's own display-field allowlist ever decrypts a
frame's contents back out.

Sync functions, run via asyncio.to_thread by callers -- same convention as
bot/queue/store.py, so Postgres network latency never blocks the event
loop. This module never imports from bot/ (onboarding/CLAUDE.md's no-
shared-credential-path rule); it mirrors bot/queue/store.py's pattern, not
its code.
"""
from __future__ import annotations

import dataclasses
import json
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from onboarding.config import settings

SESSION_TTL = timedelta(hours=4)
_POOL_TIMEOUT_SECONDS = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wizard_sessions (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    frame_data JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""

_pool: ConnectionPool | None = None


def _configure(conn) -> None:
    conn.row_factory = dict_row


def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema.
    Idempotent. Fails loudly (RuntimeError, never a bare PoolTimeout) on an
    unreachable database, matching bot/queue/store.py's init_pool()
    convention -- this project's services fail startup loudly rather than
    limping along without their datastore. Never includes
    settings.database_url in the error, which carries the password."""
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
            f"Could not reach the onboarding session database within "
            f"{_POOL_TIMEOUT_SECONDS}s. Check DATABASE_URL is set to a "
            "reachable Postgres connection string."
        ) from exc


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _require_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("session_store.init_pool() has not been called")
    return _pool


def _fernet() -> Fernet:
    return Fernet(settings.onboarding_session_encryption_key.encode("ascii"))


def _encrypt_frame(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode("utf-8")).decode("ascii")


def _decrypt_frame(token: str) -> dict | None:
    """None (never raises) for a token that fails to decrypt or parse -- a
    rotated encryption key or corrupted row must make that one frame look
    not-yet-complete, not crash the caller."""
    try:
        raw = _fernet().decrypt(token.encode("ascii"))
        return json.loads(raw)
    except (InvalidToken, ValueError, TypeError):
        return None


def create_session() -> str:
    """The ONLY function in this module allowed to mint a new session id --
    every other write path (update_frame) requires an existing session and
    fails closed instead of creating one. This is what prevents a dropped
    cookie, a request race, or a stale cookie pointing at an expired row
    from silently forking or resurrecting a session (spec section 3.2).
    Sweeps expired rows before inserting."""
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with _require_pool().connection() as conn:
        conn.execute("DELETE FROM wizard_sessions WHERE expires_at < now()")
        conn.execute(
            "INSERT INTO wizard_sessions (id, created_at, expires_at, frame_data) "
            "VALUES (%s, %s, %s, %s)",
            (session_id, now, now + SESSION_TTL, Jsonb({})),
        )
    return session_id


@dataclasses.dataclass(frozen=True)
class SessionData:
    frames: dict[str, dict]


def get_session(session_id: str) -> SessionData | None:
    """None for a missing OR expired row -- an expired row is deleted here,
    not just skipped, so callers never distinguish the two cases (both mean
    "treat this visitor as fresh"). A frame whose blob fails to decrypt is
    silently omitted from the returned frames rather than raising -- that
    frame reads as not-yet-complete, same as if it were never set."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT frame_data, expires_at FROM wizard_sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < datetime.now(timezone.utc):
            conn.execute("DELETE FROM wizard_sessions WHERE id = %s", (session_id,))
            return None
        frames: dict[str, dict] = {}
        for frame, token in row["frame_data"].items():
            decrypted = _decrypt_frame(token)
            if decrypted is not None:
                frames[frame] = decrypted
        return SessionData(frames=frames)


@dataclasses.dataclass(frozen=True)
class SessionNotFound:
    pass


def update_frame(
    session_id: str, frame: str, data: dict, *, replace: bool = False
) -> SessionNotFound | None:
    """Shallow-merges `data` into the session's existing dict for `frame`
    (creating that frame's entry on its first write), then re-encrypts the
    whole frame as one blob. Requires an existing, non-expired session --
    see create_session()'s docstring for why this must never upsert.

    `replace=True` discards the frame's existing content entirely instead
    of merging -- for the specific endpoints that represent "start this
    frame over" (re-submitting the Render key, starting a new Supabase
    OAuth flow), where leaving old fields around is actively wrong, not
    just stale: e.g. resubmitting a different Render API key must not
    leave a previous `service_id`/`service_url` behind for a service that
    may belong to a different Render account, and starting a fresh
    Supabase OAuth flow must not leave a previous `ref`/`database_url`
    around that GET /api/session could report as "complete" before the
    new flow finishes.

    Not fully race-safe against two concurrent updates to the SAME frame
    (read-then-write, not a single atomic statement) -- acceptable given
    this is a single visitor's own sequential wizard flow, not a resource
    under real concurrent access; only cross-frame writes need the atomicity
    the JSONB `||` merge below provides, which is what protects other
    frames' data from being clobbered by this write.
    """
    existing = get_session(session_id)
    if existing is None:
        return SessionNotFound()
    merged = data if replace else {**existing.frames.get(frame, {}), **data}
    token = _encrypt_frame(merged)
    with _require_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE wizard_sessions SET frame_data = frame_data || %s::jsonb WHERE id = %s",
            (Jsonb({frame: token}), session_id),
        )
        # The session could have been deleted (TTL expiry sweep, or an
        # explicit reset in another tab) in the gap between the read above
        # and this UPDATE -- a zero-row UPDATE silently wrote nothing, and
        # must not be reported as success.
        if cur.rowcount == 0:
            return SessionNotFound()
    return None


def read_frame(session_id: str, frame: str) -> dict | None:
    """Decrypts and returns one frame's stored data, or None if the session
    or that frame's data doesn't exist."""
    session = get_session(session_id)
    if session is None:
        return None
    return session.frames.get(frame)


def delete_session(session_id: str) -> None:
    """No-op if the id doesn't exist."""
    with _require_pool().connection() as conn:
        conn.execute("DELETE FROM wizard_sessions WHERE id = %s", (session_id,))
