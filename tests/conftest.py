"""Shared Postgres test harness. Uses DATABASE_URL if the environment already
provides one (CI's `services: postgres`); otherwise spins a throwaway Postgres
via testcontainers (local dev — Docker required). Never touches Supabase."""
from __future__ import annotations

import os

import pytest

from app.config import settings
from app.queue import store


@pytest.fixture(scope="session")
def db_url() -> str:
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
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
