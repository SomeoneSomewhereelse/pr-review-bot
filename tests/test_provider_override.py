"""The DB-backed provider override: a singleton row that lets the hosted
service swap providers without a redeploy.

Uses the shared Postgres test harness (``db`` from tests/conftest.py).

Note on ordering: ``test_override_defaults_to_none`` is declared *last*, not
first. This repo runs plain ``pytest`` with no ordering/randomization plugin,
so collection is strictly top-to-bottom -- if that test were declared first
it would always run before any test that sets an override, and could never
catch a regression where the ``db`` fixture stops truncating
``runtime_config`` between tests. Declaring it last means several preceding
tests have already written to the table, so a missing TRUNCATE surfaces as a
real failure on a plain ``pytest tests/test_provider_override.py`` run.
"""
from __future__ import annotations

import psycopg
import pytest

from app.queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_set_then_get_returns_the_override():
    store.set_provider_override("groq", T0)
    assert store.get_provider_override() == "groq"


def test_setting_twice_replaces_rather_than_inserting(db_query):
    store.set_provider_override("groq", T0)
    store.set_provider_override("gemini", T1)
    assert store.get_provider_override() == "gemini"
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_restores_none():
    store.set_provider_override("groq", T0)
    store.set_provider_override(None, T1)
    assert store.get_provider_override() is None


def test_a_second_row_is_rejected(db_exec):
    """The singleton CHECK is what makes 'which row wins' unambiguous."""
    store.set_provider_override("groq", T0)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_exec(
            "INSERT INTO runtime_config (id, provider, updated_at) "
            "VALUES (2, 'gemini', %s)",
            (T1,),
        )


def test_an_empty_provider_string_reads_as_no_override(db_exec):
    """The `or None` collapses '' to None -- a blank row must not be treated
    as an override of the empty-string provider."""
    db_exec(
        "INSERT INTO runtime_config (id, provider, updated_at) VALUES (1, '', %s)",
        (T0,),
    )
    assert store.get_provider_override() is None


def test_override_defaults_to_none():
    assert store.get_provider_override() is None
