"""Tests for app/orchestrator.py — asyncio.gather fan-out across all three
specialists (step 6), including partial-failure resilience.
"""

from __future__ import annotations

from app.config import settings
from app.specialists.schemas import SpecialistResult


def _ok_result(name: str, tokens_in: int = 10, tokens_out: int = 5) -> SpecialistResult:
    return SpecialistResult(
        name=name,
        status="ok",
        findings=[],
        elapsed_ms=1,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


async def test_run_review_runs_all_three_specialists_and_posts_comment(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "raw diff text")

    posted = {}

    def fake_upsert(repo, pr, body):
        posted["repo"] = repo
        posted["pr"] = pr
        posted["body"] = body

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def fake_security(annotated_diff):
        return _ok_result("Security", tokens_in=10, tokens_out=5)

    async def fake_performance(annotated_diff):
        return _ok_result("Performance", tokens_in=8, tokens_out=4)

    async def fake_quality(annotated_diff):
        return _ok_result("Code Quality", tokens_in=6, tokens_out=3)

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(settings, "llm_provider", "groq")

    result = await orchestrator.run_review("owner/repo", 99)

    assert result.pr_number == 99
    assert len(result.results) == 3
    assert {r.name for r in result.results} == {"Security", "Performance", "Code Quality"}
    assert result.total_tokens_in == 24
    assert result.total_tokens_out == 12

    assert posted["repo"] == "owner/repo"
    assert posted["pr"] == 99
    assert "PR #99" in posted["body"]


async def test_run_review_survives_one_specialist_raising(monkeypatch):
    """A specialist coroutine that raises (bypassing its own internal
    never-raise contract, e.g. a genuine bug) must not blank the comment or
    drop the other two specialists' results — SPEC's core resilience
    guarantee, enforced at the orchestrator's gather/merge layer too.
    """
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "raw diff text")

    posted = {}
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment", lambda repo, pr, body: posted.update(body=body)
    )

    async def fake_security(annotated_diff):
        return _ok_result("Security")

    async def fake_performance(annotated_diff):
        raise RuntimeError("boom")

    async def fake_quality(annotated_diff):
        return _ok_result("Code Quality")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(settings, "llm_provider", "groq")

    result = await orchestrator.run_review("owner/repo", 1)

    assert len(result.results) == 3
    by_name = {r.name: r for r in result.results}
    assert by_name["Security"].status == "ok"
    assert by_name["Code Quality"].status == "ok"
    assert by_name["Performance"].status == "failed"
    assert "boom" in by_name["Performance"].error

    assert "❌ Performance check failed" in posted["body"]
    assert "Security" in posted["body"]
    assert "Code Quality" in posted["body"]


async def test_run_review_reflects_active_model_per_provider(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: None)

    async def ok(name):
        async def _inner(annotated_diff):
            return _ok_result(name)

        return _inner

    monkeypatch.setattr(orchestrator, "run_security_specialist", await ok("Security"))
    monkeypatch.setattr(orchestrator, "run_performance_specialist", await ok("Performance"))
    monkeypatch.setattr(orchestrator, "run_quality_specialist", await ok("Code Quality"))

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    result = await orchestrator.run_review("owner/repo", 1)
    assert result.model == "llama-3.3-70b-versatile"

    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "llm_model", "gemini-flash-latest")
    result = await orchestrator.run_review("owner/repo", 1)
    assert result.model == "gemini-flash-latest"
