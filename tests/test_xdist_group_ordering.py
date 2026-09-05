"""Regression guard for the hook-ordering bug that silently defeated
`conftest.py`'s `xdist_group(name="db")` tagging.

`--dist=loadgroup` never reads the `xdist_group` marker at scheduling time.
pytest-xdist's *worker-side* `WorkerInteractor.pytest_collection_modifyitems`
(`xdist/remote.py`) rewrites each item's `_nodeid` to `...::test@<group>`, and
`LoadGroupScheduling._split_scope` then derives the group by splitting that
**nodeid string** on the last `@`. That nodeid-stamping hookimpl is
undecorated, so pluggy orders it by plain LIFO registration -- and an
*initial* conftest (which the repo-root `conftest.py` is, ahead of every
directory in `testpaths`) is registered *before* `WorkerInteractor`, so LIFO
runs xdist's stamper *first*, while no item carries the marker yet. Result:
every nodeid stays unsuffixed, every test becomes its own singleton group,
and the db tests load-balance across all workers -- each spinning its own
testcontainers Postgres. The fix is `@pytest.hookimpl(tryfirst=True)` on
`conftest.py`'s hook, which pluggy runs ahead of all normal-priority
hookimpls regardless of registration order.

This failure mode is *silent*: all workers produce the same unsuffixed
collection, so xdist raises nothing, every test still passes, and `-m db`
still selects correctly (marker-based selection is evaluated against the
final item state, long after the nodeid was frozen). Only the scheduling is
wrong. So the guard has to exercise real xdist worker scheduling rather than
assert on markers -- hence `pytester` plus a real `runpytest_subprocess`.

The temp project below mirrors the real mechanism (an initial conftest that
tags items from `pytest_collection_modifyitems` based on their fixture
closure) rather than importing the real one; the property under test is the
hook *ordering* contract with pytest-xdist, not this repo's db fixtures.
"""

from __future__ import annotations

import re

import pytest

pytest_plugins = ["pytester"]

# `xdist_meta` is applied per-test below, NOT module-wide: only the pytester
# test is slow (it spins real xdist worker subprocesses, ~5s). A module-level
# `pytestmark` also excluded `test_this_projects_conftest_hook_declares_
# tryfirst` -- a sub-5ms assertion, and the only direct check on the real
# conftest -- from the `-m "not db and not xdist_meta"` fast loop, for no
# saving at all.

_GROUP = "sharedgroup"

_PYPROJECT = f"""
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "{_GROUP}: tagged by the conftest hook under test",
]
"""

# Mirrors this repo's own conftest.py: an initial conftest whose
# pytest_collection_modifyitems adds xdist_group to every item whose fixture
# closure includes a shared session-scoped resource.
_CONFTEST = f"""
import pytest


@pytest.fixture(scope="session")
def shared_resource():
    return "shared"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "shared_resource" in item.fixturenames:
            item.add_marker(pytest.mark.{_GROUP})
            item.add_marker(pytest.mark.xdist_group(name="{_GROUP}"))
"""

_TESTS = """
def test_shared_1(shared_resource):
    pass


def test_shared_2(shared_resource):
    pass


def test_shared_3(shared_resource):
    pass


def test_shared_4(shared_resource):
    pass


def test_plain_1():
    pass


def test_plain_2():
    pass
"""

# xdist's verbose per-test line, e.g.
#   [gw0] [ 16%] PASSED tests/test_grouping.py::test_shared_1@sharedgroup
_RESULT_LINE = re.compile(r"^\[(gw\d+)\] \[[^\]]*\] PASSED (\S+)")


def _run_grouped_suite(pytester: pytest.Pytester) -> dict[str, str]:
    """Run the temp project under real xdist workers; return {nodeid: worker}."""
    pytester.makepyprojecttoml(_PYPROJECT)
    pytester.makepyfile(
        **{"tests/conftest.py": _CONFTEST, "tests/test_grouping.py": _TESTS}
    )
    # Subprocess is mandatory: xdist workers are separate processes, and the
    # nodeid stamping under test only happens worker-side.
    result = pytester.runpytest_subprocess("-v", "-n", "4", "--dist=loadgroup")
    result.assert_outcomes(passed=6)
    ran = {}
    for line in result.outlines:
        match = _RESULT_LINE.match(line.strip())
        if match:
            ran[match.group(2)] = match.group(1)
    assert len(ran) == 6, f"expected 6 reported results, parsed {ran}"
    return ran


# Slow (spins real xdist worker subprocesses) and touches no Postgres, so it
# gets its own marker rather than reusing `db`.
@pytest.mark.xdist_meta
def test_hook_applied_xdist_group_reaches_the_loadgroup_scheduler(pytester):
    """A conftest hook that adds xdist_group must run before xdist's worker-side
    nodeid stamping, or --dist=loadgroup silently ignores the grouping."""
    ran = _run_grouped_suite(pytester)

    grouped = {nodeid: worker for nodeid, worker in ran.items() if "test_shared_" in nodeid}
    assert len(grouped) == 4, f"expected 4 grouped tests, got {grouped}"

    # The scheduler reads *only* this suffix. No suffix => no grouping.
    unsuffixed = sorted(n for n in grouped if not n.endswith(f"@{_GROUP}"))
    assert not unsuffixed, (
        "xdist did not stamp the group suffix onto these nodeids, so "
        f"--dist=loadgroup scheduled them individually: {unsuffixed}"
    )

    # And the observable consequence: one worker ran all of them.
    assert len(set(grouped.values())) == 1, (
        f"grouped tests were spread across workers instead of one: {grouped}"
    )

    # Sanity: ungrouped tests keep a bare nodeid (proves the suffix above is a
    # real signal and not something every nodeid carries).
    plain = [n for n in ran if "test_plain_" in n]
    assert len(plain) == 2, f"expected 2 ungrouped tests, got {plain}"
    assert all("@" not in n for n in plain), plain


def test_this_projects_conftest_hook_declares_tryfirst(request):
    """Bind the mirror-test above to the real file. On its own the pytester run
    proves *why* `tryfirst` is required, but it would stay green if the real
    conftest lost the decorator -- so assert directly that this suite's own
    registered hookimpl still carries it."""
    hookimpls = request.config.pluginmanager.hook.pytest_collection_modifyitems.get_hookimpls()
    ours = [
        impl
        for impl in hookimpls
        if getattr(impl.plugin, "__file__", "").replace("\\", "/").endswith("conftest.py")
    ]
    assert len(ours) == 1, f"expected exactly one conftest.py hookimpl, got {ours}"
    assert ours[0].tryfirst is True, (
        "conftest.py's pytest_collection_modifyitems must be tryfirst=True, or its "
        "xdist_group markers land after pytest-xdist has already stamped worker-side nodeids "
        "and --dist=loadgroup silently stops grouping the db tests"
    )
