"""Resolves the (env-var-name, value) pair for a provider's currently-active
API-key slot.

Index 0 reads through the Settings singleton, via `base.lower()` -- the same
attribute-name convention scripts/deploy.py's check_provider and
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
"""

from __future__ import annotations

import os

from app.config import settings
from app.providers import registry


def resolve(provider: str, index: int) -> tuple[str, str]:
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return base, getattr(settings, base.lower(), "")
    env_name = f"{base}_{index}"
    return env_name, os.environ.get(env_name, "")
