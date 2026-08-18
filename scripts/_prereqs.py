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

from app.config import settings

MINIMUM_PYTHON = (3, 12)


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

TUNNEL_TOOL = Tool(
    "cloudflared", "cloudflared",
    {
        "Linux": "`sudo apt install cloudflared` (or download the binary)",
        "Darwin": "`brew install cloudflared`",
        "Windows": "`winget install Cloudflare.cloudflared`",
    },
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
)

# Docker is NOT here: it is only one of the ways to satisfy the database
# prerequisite -- see database_available().
REQUIRED_TOOLS: tuple[Tool, ...] = (GIT,)


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
    spins one up) OR DATABASE_URL already set. tests/conftest.py's db_url
    fixture needs exactly one of these, so it is one conditional prerequisite
    rather than two independent ones."""
    return bool(settings.database_url) or is_available(DOCKER)
