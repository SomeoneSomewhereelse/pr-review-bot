"""The re-review cooldown parameters actually in force: a DB override
(base/cap/factor) when set and valid, else the env-configured defaults.

Every read of the effective cooldown config goes through effective_config().
Mirrors app/providers/active.py's provider-override cache exactly, including
the reason for the split: the DB read lives in the dispatcher (where the
asyncio.to_thread convention applies) and is pushed in via set_override_cache,
keeping this module import-light and non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails -- the service degrades to its configured
defaults rather than to no cooldown. An override that reads back invalid
(factor < 1, base > cap, or a non-positive base/cap) is discarded as a WHOLE
triple, never partially applied, so a bad field can never pair with a stale
override in another field.
"""

from __future__ import annotations

from bot.config import settings

_base: float | None = None
_cap: float | None = None
_factor: float | None = None


def effective_config() -> tuple[float, float, float]:
    """(base, cap, factor) -- the DB override where fully valid, else the env defaults."""
    base = _base if _base is not None else settings.dispatcher_rereview_cooldown_seconds
    cap = _cap if _cap is not None else settings.dispatcher_rereview_cooldown_max_seconds
    factor = _factor if _factor is not None else settings.dispatcher_rereview_cooldown_factor
    if factor < 1.0 or base > cap or base <= 0 or cap <= 0:
        return (
            settings.dispatcher_rereview_cooldown_seconds,
            settings.dispatcher_rereview_cooldown_max_seconds,
            settings.dispatcher_rereview_cooldown_factor,
        )
    return base, cap, factor


def set_override_cache(base: float | None, cap: float | None, factor: float | None) -> None:
    global _base, _cap, _factor
    _base, _cap, _factor = base, cap, factor


def reset_override_cache() -> None:
    set_override_cache(None, None, None)
