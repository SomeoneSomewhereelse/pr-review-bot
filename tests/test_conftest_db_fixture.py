"""Coverage for tests/conftest.py's `db` fixture: the connection pool must be
created once per worker and reused across tests, not torn down and rebuilt on
every one. Before this, `store.close_pool()` ran unconditionally at the start
of every test's setup (plus another at teardown) -- `store.init_pool()` is
already idempotent and reuses an existing pool, so forcing it closed first was
pure, avoidable churn. Measured via cProfile: `store.close_pool()` alone cost
~17s cumulative across this suite's 234 db-marked tests, over half the
db-only group's serial runtime.

Two sequential tests sharing a module-level list, mirroring
tests/test_github_app.py's key-material tests: both request `db` (which
depends on `db_url`), so tests/conftest.py's auto-tagging hook already puts
both in `xdist_group(name="db")` -- the same group as every other db-marked
test, no extra marker needed.
"""

from review_queue import store

_seen_pool_ids: list[int] = []


def test_pool_identity_is_recorded(db):
    _seen_pool_ids.append(id(store._require_pool()))


def test_pool_is_reused_not_recreated(db):
    assert _seen_pool_ids, "test_pool_identity_is_recorded must run first"
    assert id(store._require_pool()) == _seen_pool_ids[0], (
        "store's connection pool was torn down and recreated between tests -- "
        "the db fixture must reuse one pool per worker, not rebuild it per test"
    )
