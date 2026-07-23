"""Golden-ish tests for app/formatting.py — ReviewResult -> Markdown comment."""

from __future__ import annotations

from app.formatting import format_comment
from app.github_app import COMMENT_MARKER
from app.specialists.schemas import ReviewResult, SpecialistResult


def test_format_comment_includes_marker_first():
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=0.0001,
    )
    body = format_comment(result)
    assert body.startswith(COMMENT_MARKER)


def test_format_comment_renders_findings_table():
    result = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[
                    {
                        "severity": "critical",
                        "file": "app.py",
                        "line": 14,
                        "description": "Hardcoded API key",
                        "fix": "Move to env var",
                    }
                ],
                elapsed_ms=1200,
                tokens_in=100,
                tokens_out=50,
            )
        ],
        total_elapsed_ms=1200,
        total_tokens_in=100,
        total_tokens_out=50,
        est_cost_usd=0.0021,
    )
    body = format_comment(result)

    assert "PR #42" in body
    assert "groq" in body
    assert "llama-3.3-70b-versatile" in body
    assert "Security" in body
    assert "app.py:14" in body
    assert "Hardcoded API key" in body
    assert "Move to env var" in body
    assert "critical" in body


def test_format_comment_renders_no_findings():
    result = ReviewResult(
        pr_number=7,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=500)],
        total_elapsed_ms=500,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=0.00005,
    )
    body = format_comment(result)
    assert "no findings" in body.lower()


def test_format_comment_renders_failed_specialist_visibly():
    result = ReviewResult(
        pr_number=7,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="failed",
                findings=[],
                error="DeadlineExceeded",
                elapsed_ms=500,
            )
        ],
        total_elapsed_ms=500,
        total_tokens_in=0,
        total_tokens_out=0,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    assert "failed" in body.lower()
    assert "DeadlineExceeded" in body


def test_format_comment_does_not_hardcode_specialist_count():
    result = ReviewResult(
        pr_number=1,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=1,
        total_tokens_out=1,
        est_cost_usd=0.0,
    )
    body = format_comment(result)
    assert "3 specialists" not in body
    assert "1 specialist" in body
