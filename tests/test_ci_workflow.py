"""The drift job is what makes generated docs a guarantee rather than a habit.

Asserted structurally rather than by running CI: the job must exist, must
regenerate, and must fail on a diff.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_a_docs_job_exists_alongside_lint_and_test():
    jobs = _workflow()["jobs"]
    assert "docs" in jobs
    assert "lint-and-test" in jobs, "the existing job must not be replaced"


def test_the_docs_job_regenerates_and_fails_on_drift():
    steps = _workflow()["jobs"]["docs"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.gen_docs" in commands
    assert "git add -A" in commands
    assert "git diff --cached --exit-code" in commands
    assert "guide/reference" in commands


def test_the_docs_job_regenerates_before_diffing():
    """Order matters, not just presence: a diff-then-regenerate reordering
    would never observe drift, but a substring-only check on the joined
    commands (as above) can't tell the two orderings apart."""
    steps = _workflow()["jobs"]["docs"]["steps"]
    regenerate_index = next(
        i for i, step in enumerate(steps) if "scripts.gen_docs" in step.get("run", "")
    )
    diff_index = next(
        i for i, step in enumerate(steps) if "git diff --cached --exit-code" in step.get("run", "")
    )
    assert regenerate_index < diff_index


def test_the_docs_job_needs_no_database():
    """gen_docs reads class metadata and module constants only, so wiring a
    Postgres service to this job would be pure noise."""
    assert "services" not in _workflow()["jobs"]["docs"]
