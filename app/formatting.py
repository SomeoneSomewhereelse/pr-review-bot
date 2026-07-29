"""Render a ``ReviewResult`` into the Markdown PR comment (SPEC.md section 6).

Knows nothing about LLMs or providers — only how to turn the already-computed
``ReviewResult`` envelope into Markdown. Each specialist's findings are
``list[dict]`` (see specialists/schemas.py), so this module maps specialist
name -> column layout to render each finding's fields as a table row.

Generalization note: this step's ``ReviewResult.results`` has exactly one
entry (Security). The section-count and specialist-count language below is
computed from ``len(result.results)``, not hardcoded to "3 specialists" or
"1 specialist" — step 6 adds Performance + Code Quality without touching
this file's control flow, only the ``_SECTION_CONFIG`` table.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.github_app import COMMENT_MARKER
from app.specialists.schemas import ReviewResult, SpecialistResult

_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡"}

PLACEHOLDER_DAILY_THRESHOLD_SECONDS = 300

# Per-specialist section rendering config: emoji/title + table columns.
# Each column is (dict-key-in-finding, header-label, optional formatter).
_SECTION_CONFIG: dict[str, dict] = {
    "Security": {
        "emoji": "🔒",
        "columns": [
            ("severity", "Severity", lambda v: f"{_SEVERITY_EMOJI.get(v, '')} {v}".strip()),
            ("_file_line", "Line", None),
            ("description", "Issue", None),
            ("fix", "Suggested fix", None),
        ],
    },
    "Performance": {
        "emoji": "⚡",
        "columns": [
            ("estimated_impact", "Impact", lambda v: f"{_SEVERITY_EMOJI.get(v, '')} {v}".strip()),
            ("_file_line", "Line", None),
            ("type", "Issue", None),
            ("suggestion", "Suggestion", None),
        ],
    },
    "Code Quality": {
        "emoji": "🧹",
        "columns": [
            ("category", "Category", None),
            ("_file_line", "Line", None),
            ("issue", "Issue", None),
            ("refactoring_suggestion", "Refactoring suggestion", None),
        ],
    },
}


def _file_line(finding: dict) -> str:
    return f"`{finding.get('file', '?')}:{finding.get('line', '?')}`"


def _render_section(spec: SpecialistResult) -> str:
    config = _SECTION_CONFIG.get(spec.name, {"emoji": "", "columns": []})
    emoji = config["emoji"]

    if spec.status == "failed":
        return f"### ❌ {spec.name} check failed\n> `{spec.error}` — other checks completed normally.\n"

    if not spec.findings:
        return f"### {emoji} {spec.name} — ✅ no findings\n"

    header = f"### {emoji} {spec.name} — {len(spec.findings)} finding{'s' if len(spec.findings) != 1 else ''}\n"
    columns = config["columns"]
    col_headers = " | ".join(label for _, label, _ in columns)
    col_sep = " | ".join("---" for _ in columns)
    rows = []
    for finding in spec.findings:
        cells = []
        for key, _label, fmt in columns:
            if key == "_file_line":
                value = _file_line(finding)
            else:
                raw = finding.get(key, "")
                value = fmt(raw) if fmt else raw
            cells.append(str(value))
        rows.append("| " + " | ".join(cells) + " |")

    table = f"| {col_headers} |\n| {col_sep} |\n" + "\n".join(rows)
    return header + table + "\n"


def format_comment(result: ReviewResult) -> str:
    """Render ``result`` into the marker-prefixed Markdown PR comment body."""
    n = len(result.results)
    plural = "specialist" if n == 1 else "specialists"
    runtime_s = result.total_elapsed_ms / 1000
    cost_str = f"~${result.est_cost_usd:.4f}"

    header = (
        f"## 🤖 Automated Code Review — PR #{result.pr_number}\n"
        f"_{n} {plural} · {result.model} ({result.provider}) · {runtime_s:.1f}s · {cost_str}_\n"
    )

    sections = "\n".join(_render_section(spec) for spec in result.results)

    footer = (
        "\n---\n"
        f"<sub>Runtime {runtime_s:.1f}s · {result.total_tokens_in:,} tok in / "
        f"{result.total_tokens_out:,} tok out · est. ${result.est_cost_usd:.4f} · "
        f"provider: {result.provider}</sub>\n"
    )

    body = f"{header}\n{sections}{footer}"
    return f"{COMMENT_MARKER}\n{body}"


def format_placeholder(pr_number: int, retry_after: float, now: datetime) -> str:
    """Marker-prefixed placeholder comment shown while a review is delayed.

    The real result later edits this same comment in place (found via the
    marker). Wording is chosen by wait magnitude: short = per-minute rate
    limit; long = daily quota, with an ETA computed from ``now + retry_after``.
    """
    header = f"## 🤖 Automated Code Review — PR #{pr_number}\n"
    if retry_after < PLACEHOLDER_DAILY_THRESHOLD_SECONDS:
        note = "⏳ Queued behind rate limit — review will appear shortly."
    else:
        eta = (now + timedelta(seconds=retry_after)).strftime("%H:%M UTC")
        note = (
            "⏳ Daily model quota reached — review queued, will post "
            f"automatically after the provider's limit resets (~{eta})."
        )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"


def format_failure(pr_number: int, attempts: int) -> str:
    """Marker-prefixed comment shown when a review is abandoned after repeated
    hard failures. Shows only the attempt count — never raw exception text
    (secrets hygiene). The marker edits any existing review/placeholder in place.
    """
    header = f"## 🤖 Automated Code Review — PR #{pr_number}\n"
    note = (
        f"❌ Automated review could not be completed after {attempts} attempts "
        "due to a service error. It will retry automatically on the next push."
    )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"
