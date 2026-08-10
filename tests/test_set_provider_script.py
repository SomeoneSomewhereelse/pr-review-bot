"""The operator CLI that sets the DB provider override. Uses the shared
Postgres test harness -- it writes to the same table the service reads."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.queue import store
from scripts import set_provider

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_the_override():
    assert set_provider.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"


def test_clear_removes_the_override():
    set_provider.main(["groq"])
    assert set_provider.main(["--clear"]) == 0
    assert store.get_provider_override() is None


def test_rejects_an_unsupported_provider(capsys):
    """It can only validate the name -- it runs locally and cannot know
    whether the credential exists on the service."""
    assert set_provider.main(["vertex"]) == 2
    err = capsys.readouterr().err
    assert "vertex" in err
    assert "groq" in err
    assert store.get_provider_override() is None


def test_requires_a_provider_or_clear(capsys):
    assert set_provider.main([]) == 2
    assert "provider" in capsys.readouterr().err


def test_entry_point_runs_as_a_documented_module_invocation():
    """`uv run python scripts/set_provider.py` (the form the docs used to
    show) raises ModuleNotFoundError: No module named 'app' before main() ever
    runs, because `python scripts/x.py` puts scripts/ on sys.path[0] rather
    than the repo root, and nothing installs `app` into the venv. Every other
    test in this module calls main() in-process, which is exactly why that
    was never caught. --help needs no database and exits before any DB code
    runs, so this does not depend on the `db` fixture actually doing anything."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_provider", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_rejects_an_abbreviated_flag(capsys):
    """allow_abbrev=False: a truncated --clear must not be silently accepted.
    scripts/deploy.py carries the same guard after an argparse abbreviation
    (--sync-en matched to --sync-env) triggered a real production incident."""
    with pytest.raises(SystemExit) as exc:
        set_provider.main(["--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err
