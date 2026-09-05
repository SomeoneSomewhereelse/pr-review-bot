"""Schema-shape tests for bot/specialists/schemas.py (SPEC.md section 3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bot.specialists.schemas import (
    PerformanceFinding,
    QualityFinding,
    ReviewResult,
    SecurityFinding,
    SpecialistResult,
)


def test_security_finding_valid():
    f = SecurityFinding(
        severity="critical",
        file="app.py",
        line=14,
        description="Hardcoded API key",
        fix="Move to env var",
    )
    assert f.severity == "critical"


def test_security_finding_rejects_bad_severity():
    with pytest.raises(ValidationError):
        SecurityFinding(
            severity="apocalyptic",
            file="app.py",
            line=14,
            description="x",
            fix="y",
        )


def test_performance_finding_valid():
    PerformanceFinding(
        type="N+1",
        estimated_impact="high",
        file="views.py",
        line=52,
        suggestion="select_related()",
    )


def test_quality_finding_valid():
    QualityFinding(
        category="magic-number",
        file="db.py",
        line=10,
        issue="unexplained literal",
        refactoring_suggestion="name the constant",
    )


def test_specialist_result_defaults():
    r = SpecialistResult(name="Security", status="ok", elapsed_ms=100)
    assert r.findings == []
    assert r.error is None
    assert r.tokens_in == 0
    assert r.tokens_out == 0


def test_specialist_result_rejects_bad_name():
    with pytest.raises(ValidationError):
        SpecialistResult(name="Nope", status="ok", elapsed_ms=1)


def test_review_result_valid():
    rr = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", elapsed_ms=100)],
        total_elapsed_ms=100,
        total_tokens_in=10,
        total_tokens_out=5,
        est_cost_usd=0.0001,
    )
    assert rr.pr_number == 42
