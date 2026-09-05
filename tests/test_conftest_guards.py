"""Safety-net coverage for tests/conftest.py's TRUNCATE guard.

_looks_like_local_test_db is the sole gate deciding whether tests/conftest.py's
db_url fixture is allowed to point destructive (TRUNCATE) tests at a
DATABASE_URL taken from the environment, versus refusing and raising. This
project's 2026-08-19 test-suite-performance design makes setting
DATABASE_URL locally more common (bot/scripts/test_db.py), which raises the cost
of this guard silently regressing -- and until now nothing exercised it at
all.

Tests the predicate directly rather than driving tests/conftest.py::db_url's
generator end-to-end: db_url is session-scoped, and by the time this file's
tests run, some earlier test in the same pytest process may already have
resolved and cached it -- monkeypatching DATABASE_URL afterward would have
no effect on that cached value, making an end-to-end version of this test
order-dependent and flaky. The predicate is what the fixture's raise
decision is entirely computed from, so testing it directly covers the same
decision without that fragility.
"""

from conftest import _looks_like_local_test_db


def test_accepts_localhost():
    assert _looks_like_local_test_db("postgresql://postgres:x@localhost:5432/postgres") is True


def test_accepts_loopback_ip():
    assert _looks_like_local_test_db("postgresql://postgres:x@127.0.0.1:5432/postgres") is True


def test_accepts_dot_internal_hosts():
    assert _looks_like_local_test_db("postgresql://postgres:x@db.internal:5432/postgres") is True


def test_refuses_a_production_shaped_supabase_url():
    url = "postgresql://postgres.abcdefgh:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
    assert _looks_like_local_test_db(url) is False


def test_refuses_an_arbitrary_remote_host():
    url = "postgresql://postgres:x@db.example.com:5432/postgres"
    assert _looks_like_local_test_db(url) is False
