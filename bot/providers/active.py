"""The provider actually in force: a DB override when set, else LLM_PROVIDER.

Every read of the active provider goes through active_provider(). Partial
adoption would be a bug -- if only the dispatcher consulted the override, the
factory would still build the env-configured provider, gating on one provider
while calling another.

This module deliberately imports only ``settings``: the DB read lives in the
dispatcher (where the asyncio.to_thread convention applies) and is pushed in
via set_override_cache. That keeps webhook.py from pulling the DB driver in
through this import, and keeps active_provider() non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails or the database is unreachable -- the service
degrades to its configured provider rather than to no provider.
"""

from __future__ import annotations

from bot.config import settings

_override: str | None = None


def active_provider() -> str:
    return _override or settings.llm_provider


def set_override_cache(value: str | None) -> None:
    global _override
    _override = value


def reset_override_cache() -> None:
    set_override_cache(None)
