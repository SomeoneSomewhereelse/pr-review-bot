"""Prerequisite detection for scripts/doctor.py's first stage.

THE OS RULE. platform.system() selects a printed install MESSAGE here and
nothing else -- never a code path, never an availability result. shutil.which
already handles Windows PATHEXT/.exe resolution, so detection itself is
uniform. Every hint ends with the official URL, so an operator on a platform
this module has never heard of still has something actionable.

Deliberately NOT covered: how to install Python or uv. doctor.py runs via
`uv run`, so it cannot advise on getting uv -- that lives in the guide's
prose (design spec 2026-08-18 section 4d).
"""

from __future__ import annotations

import platform
import shutil
import sys
from typing import NamedTuple
from urllib.parse import urlsplit

from bot.config import settings

MINIMUM_PYTHON = (3, 12)

# Hosts treated as "local/CI Postgres" for the purpose of this prerequisite.
# Mirrors tests/conftest.py::_looks_like_local_test_db -- kept in sync
# manually since scripts/ must not import from tests/.
_LOCAL_TEST_DB_HOSTS = {"localhost", "127.0.0.1"}


def _looks_like_local_test_db(url: str) -> bool:
    """Mirrors tests/conftest.py::_looks_like_local_test_db -- kept in sync
    manually since scripts/ must not import from tests/."""
    host = urlsplit(url).hostname or ""
    return host in _LOCAL_TEST_DB_HOSTS or host.endswith(".internal")


class Tool(NamedTuple):
    executable: str          # what shutil.which looks for
    label: str               # what an operator calls it
    hints: dict[str, str]    # platform.system() -> install command
    url: str                 # official install page; the universal fallback


GIT = Tool(
    "git", "Git",
    {
        "Linux": "your package manager, e.g. `sudo apt install git`",
        "Darwin": "`brew install git` (or Xcode Command Line Tools)",
        "Windows": "`winget install Git.Git`",
    },
    "https://git-scm.com/downloads",
)

DOCKER = Tool(
    "docker", "Docker",
    {
        "Linux": "`sudo apt install docker.io`, then add yourself to the `docker` group",
        "Darwin": "`brew install --cask docker`",
        "Windows": "`winget install Docker.DockerDesktop`",
    },
    "https://docs.docker.com/get-docker/",
)

GH = Tool(
    "gh", "the GitHub CLI (gh)",
    {
        "Linux": "see https://github.com/cli/cli/blob/trunk/docs/install_linux.md "
                 "(most distros: add the cli.github.com apt/dnf/... repo, then install `gh`)",
        "Darwin": "`brew install gh`",
        "Windows": "`winget install GitHub.cli`",
    },
    "https://cli.github.com/",
)

# Docker is NOT here: it is only one of the ways to satisfy the database
# prerequisite -- see database_available().
REQUIRED_TOOLS: tuple[Tool, ...] = (GIT, GH)


def is_available(tool: Tool) -> bool:
    """Whether `tool` is on PATH. shutil.which resolves Windows PATHEXT for
    free, which is why there is no platform branch here."""
    return shutil.which(tool.executable) is not None


def install_hint(tool: Tool, system: str | None = None) -> str:
    """How to install `tool` on `system` (default: this machine).

    An unknown platform falls back to the URL alone -- never to nothing.
    """
    system = system or platform.system()
    command = tool.hints.get(system)
    if command:
        return f"install {tool.label}: {command} -- {tool.url}"
    return f"install {tool.label}: see {tool.url}"


def python_version_ok() -> bool:
    return sys.version_info[:2] >= MINIMUM_PYTHON


def database_available() -> bool:
    """Whether the test suite can get a Postgres: Docker present (testcontainers
    spins one up) OR a DATABASE_URL that looks like a local/CI Postgres already
    set. A remote DATABASE_URL (e.g. a Supabase pooler string) does NOT satisfy
    this on its own -- tests/conftest.py refuses to run destructive tests
    against a non-local host, so reporting PASS there would be misleading."""
    if settings.database_url and _looks_like_local_test_db(settings.database_url):
        return True
    return is_available(DOCKER)
