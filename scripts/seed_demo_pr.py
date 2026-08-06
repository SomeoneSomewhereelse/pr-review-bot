"""Open a real demo PR carrying fixtures/bad_code's planted issues.

Mirrors the style of scripts/manual_verify_*.py: not part of the pytest
suite, depends on real network access (git + GitHub) and the `gh` CLI being
authenticated as the repo owner. This is the "live rehearsal" fixture named
in SPEC.md section 8/11 and CLAUDE.md step 5/9.

What it does:
  1. Clones the configured test repo (GITHUB_TARGET_REPO) into a fresh temp
     directory via `gh repo clone`.
  2. Creates a new branch off the default branch.
  3. Copies fixtures/bad_code/*.py into the clone.
  4. Commits + pushes the branch.
  5. Opens a PR via `gh pr create`.

Each run creates a NEW branch/PR (timestamped branch name) so it never
collides with or reuses an existing PR (e.g. PR #1 from an earlier build
step) — the milestone step explicitly calls for a fresh PR, not reusing one.

Run it directly:

    uv run python scripts/seed_demo_pr.py

Prints the opened PR's URL on success. Never prints any secret.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.config import settings

REPO = settings.github_target_repo
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "bad_code"


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"$ {' '.join(cmd)}", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return result.stdout.strip()


def main() -> None:
    if not FIXTURES_DIR.is_dir():
        raise SystemExit(f"fixtures dir not found: {FIXTURES_DIR}")

    branch_name = f"demo/bad-code-{int(time.time())}"

    with tempfile.TemporaryDirectory(prefix="seed-demo-pr-") as tmp:
        clone_dir = Path(tmp) / "repo"
        print(f"Cloning {REPO} ...")
        _run(["gh", "repo", "clone", REPO, str(clone_dir)])

        print(f"Creating branch {branch_name} ...")
        _run(["git", "checkout", "-b", branch_name], cwd=clone_dir)

        dest = clone_dir / "bad_code"
        dest.mkdir(exist_ok=True)
        for f in FIXTURES_DIR.glob("*.py"):
            shutil.copy(f, dest / f.name)
            print(f"  copied {f.name}")

        _run(["git", "add", "."], cwd=clone_dir)
        _run(
            [
                "git",
                "commit",
                "-m",
                "Add billing report module (demo PR for the AI code-review bot)",
            ],
            cwd=clone_dir,
        )

        print("Pushing branch ...")
        _run(["git", "push", "-u", "origin", branch_name], cwd=clone_dir)

        print("Opening PR ...")
        pr_url = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                REPO,
                "--head",
                branch_name,
                "--title",
                "Add monthly billing report module",
                "--body",
                (
                    "Adds a nightly billing summary job. Straightforward "
                    "addition, should be a quick review."
                ),
            ],
            cwd=clone_dir,
        )

    print(f"\nOpened demo PR: {pr_url}")


if __name__ == "__main__":
    main()
