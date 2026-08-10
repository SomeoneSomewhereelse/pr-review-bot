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
        # driver=None gives a bare "postgresql://" scheme for raw psycopg3.
        # driver="psycopg" (the previous value) builds a SQLAlchemy-style
        # "postgresql+psycopg://" dialect+driver URL, which psycopg3's own
        # parser cannot read at all ('missing "=" after "postgresql+psycopg:...'"').
        # This was masked until now: CI's services:postgres sets DATABASE_URL
        # directly and never calls this method, and every local run before
        # Docker/WSL integration was enabled failed earlier on
        # docker.errors.DockerException, before this code path ever ran.
        yield pg.get_connection_url(driver=None)


@pytest.fixture
def db(db_url, monkeypatch):
    """Point the store at the test Postgres, ensure schema, and truncate between
    tests. Opt-in (DB-touching test modules request it via an autouse wrapper)."""
    monkeypatch.setattr(settings, "database_url", db_url)
    store.close_pool()
    store.init_pool()
    with store._require_pool().connection() as conn:
        conn.execute("TRUNCATE tickets, runtime_config RESTART IDENTITY")
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


# Operator-tooling credentials are read only by scripts/deploy.py, and they
# point at REAL infrastructure. A test that forgets to monkeypatch them runs
# against production: during this plan's Task 2 exactly that happened, and a
# live Render service had GITHUB_TARGET_REPO overwritten with a dummy value.
# Same reasoning as the DATABASE_URL guard above -- default to inert, and make
# reaching a live API something a test has to ask for by name.
_LIVE_OPERATOR_KEYS = ("render_api_key", "uptimerobot_api_key")


@pytest.fixture(autouse=True)
def _quarantine_operator_apis(request, monkeypatch):
    if "live_operator_apis_allowed" in request.fixturenames:
        return
    for name in _LIVE_OPERATOR_KEYS:
        monkeypatch.setattr(settings, name, "")
    # settings.database_url defaults to whatever this working copy's real
    # .env points at (a Supabase pooler host in this repo). scripts/deploy.py
    # now opens raw psycopg connections against it (check_database,
    # check_provider, --sync-env's masking guard) and scripts/set_provider.py
    # WRITES to it -- a test that forgets to request the `db` fixture must
    # not be able to reach that real database by accident. The `db` fixture
    # (tests/conftest.py) sets settings.database_url explicitly and runs
    # after this autouse fixture within the same test, so it always wins;
    # verified by running the full suite after this change.
    monkeypatch.setattr(settings, "database_url", "")


@pytest.fixture
def live_operator_apis_allowed():
    """Opt out of the quarantine. Requesting this fixture is a deliberate
    statement that the test mocks its own transport (respx) or genuinely
    intends a live call."""
    return True
