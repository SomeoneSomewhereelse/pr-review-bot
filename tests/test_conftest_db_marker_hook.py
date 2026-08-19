"""Unit tests for the predicate tests/conftest.py's pytest_collection_modifyitems
hook uses to decide which tests touch the shared Postgres. Tested directly
against a fake item (not a real collection run) -- the predicate is a plain
function, and driving a full pytest collection just to test it would be
disproportionate machinery for a one-line check."""

from conftest import _touches_shared_postgres


class _FakeItem:
    def __init__(self, fixturenames):
        self.fixturenames = fixturenames


def test_flags_a_test_requesting_db_url_directly():
    assert _touches_shared_postgres(_FakeItem(["db_url", "monkeypatch"])) is True


def test_flags_a_test_requesting_db_which_depends_on_db_url():
    assert _touches_shared_postgres(_FakeItem(["db", "db_url", "monkeypatch"])) is True


def test_flags_a_test_requesting_db_exec():
    assert _touches_shared_postgres(_FakeItem(["db_exec", "db_url"])) is True


def test_does_not_flag_a_test_with_no_db_dependency():
    assert _touches_shared_postgres(_FakeItem(["monkeypatch", "capsys"])) is False
