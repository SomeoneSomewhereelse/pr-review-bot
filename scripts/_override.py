"""Shared local-value discovery and Render-verification logic for the
provider/key-index override CLI (scripts/set_override.py) and
scripts/deploy.py's numbered-slot sync-env fix.

Extracted from scripts/set_provider.py's _verify_render_credential and
scripts/set_api_key.py's _verify_render_key_slot, which independently
implemented the same "verify against Render, degrade to a warning when it
can't be verified, refuse unless --force" shape. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md.

scripts/set_provider.py and scripts/set_api_key.py are deliberately NOT
refactored to use this module -- both are temporary, slated for deletion
once the presentation this was built for is over (see the design doc's
Non-goals section). Only scripts/set_override.py and scripts/deploy.py's
sync-env fix use it.
"""

from __future__ import annotations

import re

from dotenv import dotenv_values

from app.config import settings
from app.providers import registry
from scripts import _render

_SLOT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def local_slot_indices(base: str, env_path: str = ".env") -> tuple[int, ...]:
    """Ascending indices of every ``{base}_{N}`` slot with a non-empty value.

    THE DEFAULT discovery entry point, and deliberately value-free: it answers
    a names question, so values are discarded INSIDE this function and never
    reach a caller. A caller cannot print, log, or leak what it never
    received. Use local_slot_values() only where the values are genuinely the
    point (--sync-env pushing them).

    N >= 1 only -- index 0 is the base var itself, read through Settings by
    local_value() below. Returns () if env_path doesn't exist (dotenv_values
    degrades gracefully) or nothing matches.
    """
    pattern = _SLOT_RE_CACHE.setdefault(base, re.compile(rf"^{re.escape(base)}_(\d+)$"))
    return tuple(
        sorted(
            int(match.group(1))
            for key, value in dotenv_values(env_path).items()
            if value and (match := pattern.match(key))
        )
    )


def local_slot_values(base: str, env_path: str = ".env") -> dict[str, str]:
    """Every ``{base}_{N}`` key with a non-empty value, WITH its value.

    Value-bearing on purpose and narrow on purpose: scripts/deploy.py's
    _wanted_env() has to push these to Render, and local_value() below has to
    feed one into an in-memory equality check that is never printed -- no
    other caller should call this. Same contract as
    scripts/_render.py::env_vars() -- reduce a returned value to a boolean or
    an equality result immediately; never store it beyond that computation,
    print it, or pass it to anything that might log it. When you only need to
    know WHICH slots exist, call local_slot_indices().

    Reads the file directly (python-dotenv, not os.environ or Settings) because
    Settings can't declare an unbounded family of numbered fields -- mirrors
    app/providers/credentials.py's identical reasoning for the runtime side.
    """
    pattern = _SLOT_RE_CACHE.setdefault(base, re.compile(rf"^{re.escape(base)}_(\d+)$"))
    values = dotenv_values(env_path)
    return {key: value for key, value in values.items() if value and pattern.match(key)}


def local_value(provider: str, index: int) -> str:
    """The local value for (provider, index) -- index 0 via Settings (the
    same attribute-name convention scripts/deploy.py's check_provider and
    _verify_render_credential already use), index >= 1 via the scan above."""
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return getattr(settings, base.lower(), "")
    env_name = registry.slot_env_name(provider, index)
    return local_slot_values(base).get(env_name, "")


def verify_render_slot(provider: str, index: int) -> tuple[bool, str]:
    """(ok_to_proceed, message). Replaces both set_provider.py's
    _verify_render_credential and set_api_key.py's _verify_render_key_slot.

    Differs from both predecessors: attempts an equality-against-the-local-
    value check for ANY index, not just 0 -- a numbered slot routinely has a
    real local counterpart now that Task 1's scan exists, so treating index
    >= 1 as "no local value, ever" (set_api_key.py's old assumption) is no
    longer accurate. Never returns, prints, or logs a fetched Render value --
    only presence/absence and in-memory equality results. See
    docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md
    section 6 for the invariant this maintains.
    """
    env_name = registry.slot_env_name(provider, index)
    if not settings.render_api_key:
        return True, (
            "could not verify against Render (no RENDER_API_KEY); "
            "setting override without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return True, (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); setting override without live verification"
            )
        env_vars = _render.env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return True, (
            f"could not verify against Render ({type(exc).__name__}); "
            "setting override without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return True, (
            "could not confirm this DATABASE_URL is the one the Render service reads "
            "-- skipping live verification"
        )

    live_value = env_vars.get(env_name) or ""
    if not live_value:
        return False, f"{env_name} is missing on the Render service"
    local = local_value(provider, index)
    if not local:
        return True, f"{env_name} present on Render (no local value to compare)"
    if live_value != local:
        return False, f"{env_name} on Render differs from your local .env value"
    return True, f"{env_name} verified on Render (matches local .env)"
