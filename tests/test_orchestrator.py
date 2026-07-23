"""Tests for app/orchestrator.py — solo-Security run_review() (step 5)."""

from __future__ import annotations

from app.config import settings
from app.specialists.schemas import SpecialistResult


async def test_run_review_fetches_diff_runs_security_and_posts_comment(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "raw diff text")

    posted = {}

    def fake_upsert(repo, pr, body):
        posted["repo"] = repo
        posted["pr"] = pr
        posted["body"] = body
        return None

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def fake_run_security_specialist(annotated_diff):
        assert "raw diff text" in annotated_diff or annotated_diff  # got something
        return SpecialistResult(
            name="Security",
            status="ok",
            findings=[{"severity": "high", "file": "a.py", "line": 1, "description": "x", "fix": "y"}],
            elapsed_ms=42,
            tokens_in=10,
            tokens_out=5,
        )

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_run_security_specialist)
    monkeypatch.setattr(settings, "llm_provider", "groq")

    result = await orchestrator.run_review("owner/repo", 99)

    assert result.pr_number == 99
    assert result.provider == "groq"
    assert len(result.results) == 1
    assert result.results[0].name == "Security"
    assert result.total_tokens_in == 10
    assert result.total_tokens_out == 5
    assert result.est_cost_usd >= 0

    assert posted["repo"] == "owner/repo"
    assert posted["pr"] == 99
    assert "PR #99" in posted["body"]


async def test_run_review_reflects_active_model_per_provider(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: None)

    async def fake_run_security_specialist(annotated_diff):
        return SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=1)

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_run_security_specialist)

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    result = await orchestrator.run_review("owner/repo", 1)
    assert result.model == "llama-3.3-70b-versatile"

    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "llm_model", "gemini-flash-latest")
    result = await orchestrator.run_review("owner/repo", 1)
    assert result.model == "gemini-flash-latest"
