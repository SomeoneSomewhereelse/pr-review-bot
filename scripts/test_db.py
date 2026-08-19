"""Idempotent local test-only Postgres, for fast pytest iteration without
paying testcontainers' cold-boot cost on every invocation.

    uv run python -m scripts.test_db            # start (or reuse) it
    eval "$(uv run python -m scripts.test_db)"   # also export DATABASE_URL
    uv run python -m scripts.test_db down        # stop and remove it

Separate from guide/setup/local/05-postgres.md's `pr-review-pg` container --
that one is the local-hosting track's app runtime database, on port 5432.
This one is disposable and test-iteration-only, on port 5433 so the two can
coexist without colliding. tests/conftest.py's db_url fixture already reads
DATABASE_URL when set -- no test code depends on this script existing.

The printed connection string's password is a fixed, script-generated,
throwaway local value that authenticates nothing real -- not the kind of
secret CLAUDE.md's "Secret handling" section governs. This script must never
accept, read, or print a *real* DATABASE_URL.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time

from scripts._prereqs import _looks_like_local_test_db

_CONTAINER_NAME = "pr-review-test-pg"
_PORT = 5433
_PASSWORD = "x"
_DATABASE_URL = f"postgresql://postgres:{_PASSWORD}@localhost:{_PORT}/postgres"

_READY_TIMEOUT_SECONDS = 15.0
_READY_POLL_INTERVAL_SECONDS = 0.5

_DOCKER_MISSING_MESSAGE = (
    "error: Docker not found on PATH. Install it -- see "
    "guide/setup/01-prerequisites.md -- or set DATABASE_URL yourself to skip "
    "this script entirely."
)


def _container_status() -> str:
    """Returns 'running', 'stopped', or 'absent'."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", _CONTAINER_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "absent"
    return "running" if result.stdout.strip() == "true" else "stopped"


def _wait_until_ready() -> bool:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", "postgres"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(_READY_POLL_INTERVAL_SECONDS)
    return False


def up() -> int:
    if shutil.which("docker") is None:
        print(_DOCKER_MISSING_MESSAGE, file=sys.stderr)
        return 1

    status = _container_status()
    if status == "absent":
        subprocess.run(
            [
                "docker", "run", "-d", "--name", _CONTAINER_NAME,
                "-p", f"{_PORT}:5432",
                "-e", f"POSTGRES_PASSWORD={_PASSWORD}",
                "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
        )
    elif status == "stopped":
        subprocess.run(["docker", "start", _CONTAINER_NAME], check=True, capture_output=True)
    # status == "running": idempotent no-op, already up.

    if not _wait_until_ready():
        print(f"error: {_CONTAINER_NAME} did not become ready in time", file=sys.stderr)
        return 1

    assert _looks_like_local_test_db(_DATABASE_URL), "constructed URL must be local"
    print(f"export DATABASE_URL={_DATABASE_URL}")
    return 0


def down() -> int:
    if shutil.which("docker") is None:
        print(_DOCKER_MISSING_MESSAGE, file=sys.stderr)
        return 1
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotent local test-only Postgres for fast pytest iteration."
    )
    parser.add_argument("action", nargs="?", default="up", choices=["up", "down"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return down() if args.action == "down" else up()


if __name__ == "__main__":
    sys.exit(main())
