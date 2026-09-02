"""Postgres test harness for onboarding/session_store.py -- reuses the
shared `db_url` fixture from the repo-root conftest.py (that file's own
`db` fixture is bot/-specific, truncating bot's queue tables; this is
onboarding/'s own counterpart, truncating wizard_sessions instead)."""
from __future__ import annotations

import pytest

from onboarding import session_store
from onboarding.config import settings


@pytest.fixture
def onboarding_db(db_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", db_url)
    session_store.init_pool()
    with session_store._require_pool().connection() as conn:
        conn.execute("TRUNCATE wizard_sessions")
    yield


@pytest.fixture(scope="session", autouse=True)
def _close_session_store_pool_at_session_end():
    yield
    session_store.close_pool()
