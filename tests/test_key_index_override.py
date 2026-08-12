"""The DB-backed API-key-index override: a singleton row that lets the
hosted service swap which numbered credential slot is active, per provider,
without a redeploy. Mirrors tests/test_provider_override.py's structure:
store-level tests first, then the process-local cache."""
from __future__ import annotations

import pytest

from app.providers import key_index


@pytest.fixture(autouse=True)
def _clean_cache():
    key_index.reset_override_cache()
    yield
    key_index.reset_override_cache()


def test_active_key_index_falls_back_to_zero():
    assert key_index.active_key_index("groq") == 0


def test_active_key_index_prefers_the_cached_override():
    key_index.set_override_cache({"groq": 2})
    assert key_index.active_key_index("groq") == 2


def test_active_key_index_defensively_floors_a_negative_cached_value():
    """A bad row (hand-edited, or a future bug) must never produce a
    negative index -- there is no such env-var slot."""
    key_index.set_override_cache({"groq": -1})
    assert key_index.active_key_index("groq") == 0


def test_a_provider_missing_from_the_cache_falls_back_to_zero():
    key_index.set_override_cache({"gemini": 3})
    assert key_index.active_key_index("groq") == 0


def test_clearing_the_cache_returns_to_zero():
    key_index.set_override_cache({"groq": 2})
    key_index.reset_override_cache()
    assert key_index.active_key_index("groq") == 0
