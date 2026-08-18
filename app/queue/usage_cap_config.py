"""The per-key daily usage cap actually in force: a DB override (token cap /
reset time) when set and valid, else the env-configured defaults.

Every read of the effective cap goes through effective_caps(). Mirrors
app/queue/cooldown_config.py exactly, including the reason for the split: the
DB read lives in the dispatcher (where the asyncio.to_thread convention
applies) and is pushed in via set_override_cache, keeping this module
import-light and non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails -- the service degrades to its configured
defaults. An override that reads back invalid (a non-positive cap, or a reset
time that will not parse) is discarded as a WHOLE PAIR, never partially
applied, so a bad field can never pair with a stale override in the other
field.

Why a non-positive cap is treated as invalid rather than clamped: the
dispatcher's gate is `tokens >= cap`, which a 0 cap makes unconditionally true
-- every ticket deferred forever. That deferral is STICKY, because a ticket's
not_before is already set to a real future timestamp by the time it happens, so
correcting the override afterwards does not release already-deferred tickets.
"""

from __future__ import annotations

from datetime import time

from app.config import settings

_tokens: int | None = None
_reset: str | None = None


def _env_caps() -> tuple[int | None, time]:
    return (
        settings.key_usage_token_cap,
        settings.key_usage_reset_time_utc,
    )


def effective_caps() -> tuple[int | None, time]:
    """(token cap, reset time) -- the DB override where fully valid, else the
    env defaults. A None cap means the cap is not enforced."""
    tokens = _tokens if _tokens is not None else settings.key_usage_token_cap
    if _reset is not None:
        try:
            reset = time.fromisoformat(_reset)
        except ValueError:
            return _env_caps()
    else:
        reset = settings.key_usage_reset_time_utc
    if tokens is not None and tokens <= 0:
        return _env_caps()
    return tokens, reset


def set_override_cache(tokens: int | None, reset: str | None) -> None:
    """`reset` is the raw "HH:MM"/"HH:MM:SS" text as stored; parsing (and
    rejecting garbage) happens in effective_caps, so a malformed value degrades
    the whole pair at read time rather than raising inside a refresh."""
    global _tokens, _reset
    _tokens, _reset = tokens, reset


def reset_override_cache() -> None:
    set_override_cache(None, None)
