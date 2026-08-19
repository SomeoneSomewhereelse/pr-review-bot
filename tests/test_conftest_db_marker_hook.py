"""Unit tests for tests/conftest.py's pytest_collection_modifyitems hook: both
the `_touches_shared_postgres` predicate it decides with, and the hook body
itself. Tested directly against a fake item (not a real pytest collection run)
-- the predicate is a plain function and the hook is a plain loop over items,
so driving a full collection just to reach them would be disproportionate
machinery.

The hook-body tests exist because the predicate tests alone did not constrain
it: `item.add_marker(pytest.mark.xdist_group(name="db"))` could be deleted
outright and the entire suite stayed green, since nothing asserted that the
hook actually applies what the predicate selects. `tests/test_xdist_group_
ordering.py` covers the neighbouring property (that the marker, once applied,
survives to the `--dist=loadgroup` scheduler) against a *mirror* conftest in a
pytester temp project -- not against this file's real hook.
"""

import tomllib
from pathlib import Path

import pytest

from conftest import _touches_shared_postgres, pytest_collection_modifyitems


class _FakeItem:
    """Enough of pytest.Item for the predicate and the hook body: a fixture
    closure to select on, plus add_marker/own_markers/iter_markers, whose
    real implementations live on pytest.Node and behave exactly this way for
    a freshly collected item (own_markers appended in application order)."""

    def __init__(self, fixturenames):
        self.fixturenames = fixturenames
        self.own_markers: list[pytest.Mark] = []

    def add_marker(self, marker) -> None:
        self.own_markers.append(marker.mark)

    def iter_markers(self, name=None):
        return [m for m in self.own_markers if name is None or m.name == name]


def test_flags_a_test_requesting_db_url_directly():
    assert _touches_shared_postgres(_FakeItem(["db_url", "monkeypatch"])) is True


def test_flags_a_test_requesting_db_which_depends_on_db_url():
    assert _touches_shared_postgres(_FakeItem(["db", "db_url", "monkeypatch"])) is True


def test_flags_a_test_requesting_db_exec():
    assert _touches_shared_postgres(_FakeItem(["db_exec", "db_url"])) is True


def test_does_not_flag_a_test_with_no_db_dependency():
    assert _touches_shared_postgres(_FakeItem(["monkeypatch", "capsys"])) is False


def test_hook_adds_the_db_marker_to_a_postgres_touching_item():
    item = _FakeItem(["db", "db_url", "monkeypatch"])

    pytest_collection_modifyitems(config=None, items=[item])

    assert [m.name for m in item.iter_markers("db")] == ["db"]


def test_hook_adds_the_db_xdist_group_to_a_postgres_touching_item():
    """The load-bearing half: without this marker every db test becomes its own
    singleton group under --dist=loadgroup and the shared-Postgres TRUNCATE
    race the grouping exists to prevent is reopened."""
    item = _FakeItem(["db_url"])

    pytest_collection_modifyitems(config=None, items=[item])

    groups = item.iter_markers("xdist_group")
    assert len(groups) == 1, item.own_markers
    assert groups[0].kwargs == {"name": "db"}


def test_hook_leaves_a_non_postgres_item_completely_unmarked():
    item = _FakeItem(["monkeypatch", "capsys"])

    pytest_collection_modifyitems(config=None, items=[item])

    assert item.own_markers == []


def test_hook_marks_only_the_postgres_touching_items_of_a_mixed_batch():
    """The hook takes a whole item list, so per-item selection -- not just
    "marks something" -- is the actual contract."""
    touching = _FakeItem(["db_exec", "db_url"])
    plain = _FakeItem(["tmp_path"])

    pytest_collection_modifyitems(config=None, items=[plain, touching, plain])

    assert [m.name for m in touching.own_markers] == ["db", "xdist_group"]
    assert plain.own_markers == []


def test_addopts_still_pins_dist_loadgroup_and_a_concrete_worker_count():
    """--dist=loadgroup is what makes the xdist_group marker above mean
    anything; the default (--dist=load) ignores groups entirely and would
    silently scatter the db tests across workers. And `-n` must stay a pinned
    integer: `-n auto` measured SLOWER than serial on a 24-core machine
    (45.2s vs 30.3s on the fast-iteration subset) because per-worker startup
    cost dominates a workload this size."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    addopts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["addopts"]

    assert "--dist=loadgroup" in addopts, addopts
    workers = addopts.split("-n ")[1].split()[0]
    assert workers.isdigit(), f"-n must be a pinned integer, not {workers!r}: {addopts}"
    assert 1 < int(workers) <= 8, f"-n {workers} is outside the measured sweet spot"
