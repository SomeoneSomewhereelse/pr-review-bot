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

_SLOT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def local_numbered_slots(base: str, env_path: str = ".env") -> dict[str, str]:
    """Every ``{base}_{N}`` key with a non-empty value in the local env file.

    N >= 1 only -- index 0 is the base var itself, read through Settings by
    local_value() below, never through this scan. Reads the file directly
    (python-dotenv, not os.environ or Settings) because Settings can't
    declare an unbounded family of numbered fields -- mirrors
    app/providers/credentials.py's identical reasoning for the runtime side.
    Returns {} if env_path doesn't exist (dotenv_values degrades gracefully)
    or nothing matches.
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
    env_name = f"{base}_{index}"
    return local_numbered_slots(base).get(env_name, "")
