"""The API-key-slot index actually in force per provider: a DB override when
set, else index 0 (the base, unsuffixed env var).

Every read of the active index goes through active_key_index(). Mirrors
providers/active.py's provider-override cache exactly, generalized from
one provider to a per-provider dict: each provider tracks its own slot
independently, so switching *which provider* is active never disturbs the
key slot chosen for the other two.

This module deliberately imports nothing DB-related: the DB read lives in
the dispatcher (where the asyncio.to_thread convention applies) and is
pushed in via set_override_cache, keeping this module import-light and
non-blocking.

Fail-safe by construction: the cache starts empty, so before the first
refresh -- and whenever a refresh fails -- every provider degrades to index
0 rather than to a crash or a stale value. A negative cached value (hand-
edited row, or a future bug) is also defensively treated as "no override"
rather than propagated -- there is no such env-var slot.
"""

from __future__ import annotations

_overrides: dict[str, int] = {}


def active_key_index(provider: str) -> int:
    value = _overrides.get(provider)
    return value if value is not None and value >= 0 else 0


def set_override_cache(overrides: dict[str, int]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
