"""Resolves the (env-var-name, value) pair for a provider's currently-active
API-key slot.

Index 0 reads through the Settings singleton, via `base.lower()` -- the same
attribute-name convention bot/scripts/deploy.py's check_provider and
_verify_render_credential already use for the base credential. This keeps
index-0 resolution identical to how every other part of this codebase reads
the base key, and keeps existing tests that
`monkeypatch.setattr(settings, "groq_api_key", ...)` working unchanged.

Index >= 1 has no Settings field -- Settings can't declare an unbounded
family of numbered env vars -- so it reads os.environ directly. A Render env
var never changes within a running process's lifetime (changing one requires
a restart, which re-imports everything), so reading it at resolve-time is
equivalent to reading it at startup; there is no need to enumerate how many
slots exist, only to look up the one that's currently selected.

Deliberately asymmetric with bot/scripts/_override.py's local_value(), which
reads the *local* .env file directly (via python-dotenv) for index >= 1
instead of os.environ: pydantic-settings' env_file=".env" populates Settings
fields but never touches os.environ itself, so a numbered slot present only
in a developer's .env (not yet exported into their shell) resolves here but
not there. On Render the two agree, since real env vars are what populate
both os.environ and Settings. This module answers "what will the running
process actually use" (runtime resolution); bot/scripts/_override.py answers
"what's available to push" (local-machine discovery) -- different questions
by design, not a bug to unify.
"""

from __future__ import annotations

import os

from bot.config import settings
from bot.providers import registry


def resolve(provider: str, index: int) -> tuple[str, str]:
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return base, getattr(settings, base.lower(), "")
    env_name = registry.slot_env_name(provider, index)
    return env_name, os.environ.get(env_name, "")
