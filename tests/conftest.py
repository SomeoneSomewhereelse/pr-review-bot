"""Shared Postgres test harness. Uses DATABASE_URL if the environment already
provides one (CI's `services: postgres`); otherwise spins a throwaway Postgres
via testcontainers (local dev — Docker required). Never touches Supabase."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from app.config import settings
from app.queue import store

# Hosts treated as "local/CI Postgres, safe for tests to TRUNCATE". Anything
# else (e.g. a Supabase pooler hostname) is refused unless the operator
# explicitly opts in via ALLOW_REMOTE_TEST_DB=1 -- this guard exists solely so
# an accidentally-exported DATABASE_URL pointing at a real Supabase database
# can never get truncated by a test run.
_LOCAL_TEST_DB_HOSTS = {"localhost", "127.0.0.1"}


def _looks_like_local_test_db(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in _LOCAL_TEST_DB_HOSTS or host.endswith(".internal")


@pytest.fixture(scope="session")
def db_url() -> str:
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        if not _looks_like_local_test_db(env_url) and not os.environ.get(
            "ALLOW_REMOTE_TEST_DB"
        ):
            raise AssertionError(
                "DATABASE_URL does not look like a local/CI Postgres (host must be "
                "'localhost', '127.0.0.1', or end in '.internal'). Refusing to run "
                "destructive tests (TRUNCATE) against it -- this guard protects a real "
                "database (e.g. Supabase) from being wiped by a test run. If this really "
                "is an intentional, disposable local/CI Postgres on an unusual "
                "hostname, set ALLOW_REMOTE_TEST_DB=1 to bypass."
            )
        yield env_url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        # psycopg (not psycopg2) driver URL
        yield pg.get_connection_url(driver="psycopg")


@pytest.fixture
def db(db_url, monkeypatch):
    """Point the store at the test Postgres, ensure schema, and truncate between
    tests. Opt-in (DB-touching test modules request it via an autouse wrapper)."""
    monkeypatch.setattr(settings, "database_url", db_url)
    store.close_pool()
    store.init_pool()
    with store._require_pool().connection() as conn:
        conn.execute("TRUNCATE tickets RESTART IDENTITY")
    yield
    store.close_pool()


@pytest.fixture
def db_exec(db_url):
    """Run a raw statement against the test DB (replaces test-side sqlite3.connect)."""
    import psycopg

    def _exec(sql: str, params: tuple = ()):
        with psycopg.connect(db_url) as conn:
            conn.execute(sql, params)
            conn.commit()

    return _exec


@pytest.fixture
def db_query(db_url):
    """Run a raw query and return the rows (list of tuples)."""
    import psycopg

    def _query(sql: str, params: tuple = ()):
        with psycopg.connect(db_url) as conn:
            return conn.execute(sql, params).fetchall()

    return _query
