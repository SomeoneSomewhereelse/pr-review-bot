"""The DB-backed API-key-index override: a singleton row that lets the
hosted service swap which numbered credential slot is active, per provider,
without a redeploy. Mirrors tests/test_provider_override.py's structure:
store-level tests first, then the process-local cache."""
from __future__ import annotations

import pytest

from providers import key_index
from review_queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_set_then_get_returns_the_override():
    store.set_key_index_override("groq", 2, T0)
    assert store.get_key_index_override("groq") == 2


def test_setting_twice_replaces_rather_than_inserting(db_query):
    store.set_key_index_override("groq", 1, T0)
    store.set_key_index_override("groq", 2, T1)
    assert store.get_key_index_override("groq") == 2
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_restores_none():
    store.set_key_index_override("groq", 2, T0)
    store.set_key_index_override("groq", None, T1)
    assert store.get_key_index_override("groq") is None


def test_providers_track_independent_indices():
    store.set_key_index_override("groq", 2, T0)
    store.set_key_index_override("gemini", 1, T0)
    assert store.get_key_index_override("groq") == 2
    assert store.get_key_index_override("gemini") == 1


def test_get_all_key_index_overrides_returns_only_the_set_ones():
    store.set_key_index_override("groq", 2, T0)
    assert store.get_all_key_index_overrides() == {"groq": 2}


def test_get_all_key_index_overrides_is_empty_when_no_row_exists():
    assert store.get_all_key_index_overrides() == {}


def test_override_defaults_to_none():
    assert store.get_key_index_override("groq") is None


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


def test_vertex_round_trips_through_the_generic_key_index_machinery():
    """vertex needs no new store functions -- it is a third entry under the
    already-generic KEY_INDEX_COLUMNS, including the migration that adds
    vertex_key_index to a runtime_config table that already existed."""
    store.set_key_index_override("vertex", 3, T0)
    assert store.get_key_index_override("vertex") == 3
    assert store.get_all_key_index_overrides() == {"vertex": 3}
    store.set_key_index_override("vertex", None, T1)
    assert store.get_key_index_override("vertex") is None


def test_vertex_index_is_independent_of_the_other_providers():
    store.set_key_index_override("vertex", 2, T0)
    store.set_key_index_override("groq", 1, T0)
    assert store.get_key_index_override("vertex") == 2
    assert store.get_key_index_override("groq") == 1
    assert store.get_key_index_override("gemini") is None
