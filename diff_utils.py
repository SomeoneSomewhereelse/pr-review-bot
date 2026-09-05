"""Annotate a unified diff with ``file:line`` so specialist findings are trustworthy.

``github_app.fetch_pr_diff`` returns a raw unified diff (concatenated per-file
patches). A specialist LLM call is asked to report a ``line`` number per
finding — but a raw diff's own line numbers are diff-relative (`@@ -a,b +c,d
@@` hunk headers + a running count), not the actual file line number. If we
handed the model a raw diff and trusted its arithmetic, `SecurityFinding.line`
would be unreliable. So this module pre-computes the real target-file line
number for every added/changed line and prefixes it directly onto that line,
in a format an LLM can read and simply echo back:

    app.py:14: +API_KEY = "sk-abc123"

Only *added* lines (`+`) get a real target-file line number — that's the
"new" version of the file the PR is actually changing, which is what a
finding's ``file``/``line`` should point at. Removed lines (`-`) have no
target-file line (they don't exist in the new file) and are left
unannotated but still shown for context. Unchanged context lines are passed
through as-is (no line number needed — the model isn't meant to flag them).

Token budget: enforced via a simple, documented heuristic — ~4 characters
per token (a common rough approximation for English/code text; no tokenizer
dependency needed for a soft budget). If the annotated diff exceeds
``max_tokens`` chars/4, it is truncated to fit and a `truncated` flag is
returned so the caller (orchestrator/formatting) can surface this visibly in
the PR comment instead of silently dropping context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4  # rough heuristic, documented above — not a real tokenizer
DEFAULT_MAX_TOKENS = 6000

_FILE_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")

TRUNCATION_MARKER = "\n[... diff truncated: exceeded token budget ...]\n"


@dataclass
class AnnotatedDiff:
    text: str
    truncated: bool


def _annotate(raw_diff: str) -> str:
    """Prefix every added line with its real ``file:line`` target."""
    out_lines: list[str] = []
    current_file = None
    new_lineno = None

    for line in raw_diff.splitlines():
        file_match = _FILE_HEADER_RE.match(line)
        if file_match:
            current_file = file_match.group("b")
            new_lineno = None
            out_lines.append(line)
            continue

        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            new_lineno = int(hunk_match.group("new_start"))
            out_lines.append(line)
            continue

        if current_file is None or new_lineno is None:
            # Diff metadata lines (index, ---/+++ headers) before the first hunk.
            out_lines.append(line)
            continue

        if line.startswith("+") and not line.startswith("+++"):
            out_lines.append(f"{current_file}:{new_lineno}: {line}")
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            # Removed line: no target-file line number, still shown for context.
            out_lines.append(line)
        elif line.startswith("\\ No newline at end of file"):
            # Diff metadata, not a real line -- must not consume a new-file
            # line number, or every added line after it in the same hunk
            # gets annotated one line too high.
            out_lines.append(line)
        else:
            # Context line: still consumes a new-file line number.
            out_lines.append(line)
            new_lineno += 1

    return "\n".join(out_lines)


def annotate_and_cap(raw_diff: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> AnnotatedDiff:
    """Annotate ``raw_diff`` with file:line, then enforce a token budget.

    Returns an ``AnnotatedDiff`` with ``.text`` (possibly truncated) and
    ``.truncated`` (True if the budget was exceeded).
    """
    annotated = _annotate(raw_diff)

    char_budget = max_tokens * CHARS_PER_TOKEN
    if len(annotated) <= char_budget:
        return AnnotatedDiff(text=annotated, truncated=False)

    truncated_text = annotated[:char_budget] + TRUNCATION_MARKER
    return AnnotatedDiff(text=truncated_text, truncated=True)
