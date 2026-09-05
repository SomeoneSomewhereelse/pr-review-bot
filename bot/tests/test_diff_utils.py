"""Tests for bot/diff_utils.py — annotation + token-budget truncation."""

from __future__ import annotations

from bot.diff_utils import annotate_and_cap

SAMPLE_DIFF = """diff --git a/app.py b/app.py
@@ -10,3 +10,4 @@ def f():
 context line
-old line
+new line 1
+new line 2
diff --git a/db.py b/db.py
@@ -1,2 +1,3 @@
 unrelated context
+db.py addition
"""


def test_annotate_marks_added_lines_with_file_and_line_number():
    result = annotate_and_cap(SAMPLE_DIFF)

    assert "app.py:11" in result.text
    assert "app.py:12" in result.text
    assert "new line 1" in result.text
    assert "new line 2" in result.text
    assert "db.py:2" in result.text
    assert "db.py addition" in result.text


def test_annotate_does_not_annotate_removed_or_context_lines():
    result = annotate_and_cap(SAMPLE_DIFF)
    # Removed lines shouldn't get a "new" file:line annotation (no target line).
    assert "app.py:" + "10" not in result.text.split("old line")[0][-20:]


def test_annotate_does_not_miscount_the_no_newline_at_eof_marker():
    """GitHub's diff `patch` field includes a bare '\\ No newline at end of
    file' marker line for the last line of a hunk when it isn't
    newline-terminated. That marker isn't a real diff line and must not
    consume a new-file line number -- otherwise every added line after it
    in the same hunk gets annotated one line too high."""
    diff_with_marker = (
        "diff --git a/app.py b/app.py\n"
        "@@ -1,1 +1,3 @@\n"
        "-old last line\n"
        "\\ No newline at end of file\n"
        "+new line 1\n"
        "+new line 2\n"
    )
    result = annotate_and_cap(diff_with_marker)
    assert "app.py:1: +new line 1" in result.text
    assert "app.py:2: +new line 2" in result.text


def test_no_truncation_when_under_budget():
    result = annotate_and_cap(SAMPLE_DIFF, max_tokens=10_000)
    assert result.truncated is False
    assert result.text  # non-empty


def test_truncates_when_over_budget():
    big_diff = "diff --git a/big.py b/big.py\n@@ -1,1 +1,1 @@\n" + "+line\n" * 5000
    result = annotate_and_cap(big_diff, max_tokens=50)
    assert result.truncated is True
    # ~4 chars/token heuristic => budget chars = max_tokens * 4
    assert len(result.text) <= 50 * 4 + 200  # small slack for truncation marker text


def test_truncation_marker_present_when_truncated():
    big_diff = "diff --git a/big.py b/big.py\n@@ -1,1 +1,1 @@\n" + "+line\n" * 5000
    result = annotate_and_cap(big_diff, max_tokens=50)
    assert "truncated" in result.text.lower()
