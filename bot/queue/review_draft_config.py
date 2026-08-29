"""Whether draft PRs get reviewed: a DB override when set, else the
env-configured Settings.review_draft_prs.

Every read of the effective flag goes through effective_review_draft_prs().
Mirrors app/queue/cooldown_config.py / usage_cap_config.py exactly, including
the reason for the split: the DB read lives in the dispatcher (where the
asyncio.to_thread convention applies) and is pushed in via set_override_cache,
keeping this module import-light and non-blocking.

Fail-safe by construction: the cache starts empty (None = no override), so
before the first refresh -- and whenever a refresh fails -- the service
degrades to its configured default. Unlike the cooldown/usage-cap overrides,
a bool has no invalid state to discard: None means unset, True/False are both
valid overrides.
"""

from __future__ import annotations

from bot.config import settings

_override: bool | None = None


def effective_review_draft_prs() -> bool:
    """The DB override when set, else the env-configured default."""
    return _override if _override is not None else settings.review_draft_prs


def set_override_cache(value: bool | None) -> None:
    global _override
    _override = value


def reset_override_cache() -> None:
    set_override_cache(None)
