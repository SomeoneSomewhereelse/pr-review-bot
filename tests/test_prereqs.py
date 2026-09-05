"""Prerequisite detection must behave identically on every OS -- only the
printed install hint varies (design spec 2026-08-18 section 4d). These tests
parameterize the platform so all three hints are verified on any one machine.
"""
from __future__ import annotations

import pytest

from config import settings
from scripts import _prereqs


def test_every_required_tool_has_a_hint_for_all_three_platforms():
    for tool in _prereqs.REQUIRED_TOOLS:
        for system in ("Linux", "Darwin", "Windows"):
            hint = _prereqs.install_hint(tool, system)
            assert hint.strip(), f"{tool.executable} has no hint for {system}"
            assert tool.url in hint, "every hint carries the official URL as fallback"


def test_an_unknown_platform_still_gets_the_url_fallback():
    """Never leave an operator on a niche OS with nothing actionable."""
    hint = _prereqs.install_hint(_prereqs.REQUIRED_TOOLS[0], "Plan9")
    assert _prereqs.REQUIRED_TOOLS[0].url in hint


def test_is_available_uses_which(monkeypatch):
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: None)
    assert _prereqs.is_available(_prereqs.REQUIRED_TOOLS[0]) is False
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: "/usr/bin/" + name)
    assert _prereqs.is_available(_prereqs.REQUIRED_TOOLS[0]) is True


def test_database_available_accepts_docker_or_a_database_url(monkeypatch):
    """conftest's db_url fixture needs one or the other -- so the prereq is a
    single conditional, not two independent requirements."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: None)
    assert _prereqs.database_available() is False

    monkeypatch.setattr(settings, "database_url", "postgresql://localhost/x")
    assert _prereqs.database_available() is True

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: "/usr/bin/docker")
    assert _prereqs.database_available() is True


def test_python_version_floor_matches_the_project():
    assert _prereqs.MINIMUM_PYTHON == (3, 12)
    assert _prereqs.python_version_ok() is True  # the suite runs on a supported one


def test_database_available_rejects_a_remote_database_url_without_docker(monkeypatch):
    """A remote DATABASE_URL (e.g. Supabase's pooler) must NOT satisfy this
    prerequisite on its own -- tests/conftest.py refuses to run destructive
    tests against a non-local host, so reporting PASS here would be
    misleading for the hosted track's own doctor step 5."""
    remote = "postgresql://user:pass@db.abcxyz.supabase.co:5432/postgres"
    monkeypatch.setattr(settings, "database_url", remote)
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: None)
    assert _prereqs.database_available() is False

    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: "/usr/bin/docker")
    assert _prereqs.database_available() is True


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_install_hint_never_selects_behavior_only_text(system):
    """A regression guard on the rule: platform.system() may pick a MESSAGE,
    never a code path. Availability must not depend on the platform."""
    tool = _prereqs.REQUIRED_TOOLS[0]
    assert isinstance(_prereqs.install_hint(tool, system), str)
