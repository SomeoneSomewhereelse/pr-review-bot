# tests/test_orchestrator_rate_limited.py
"""attempt_review distinguishes a rate-limited review (defer, no comment) from a
completed one (post comment). A non-quota specialist error still COMPLETES with a
visible failed row — only a real 429 makes the whole review rate-limited.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.config import settings
from app.providers.base import RateLimited
from app.specialists.schemas import SpecialistResult


def _ok(name):
    return SpecialistResult(
        name=name, status="ok", findings=[], elapsed_ms=1, tokens_in=1, tokens_out=1
    )


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "groq")


async def test_attempt_review_returns_rate_limited_and_posts_nothing(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    posted = []
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: posted.append(a))

    async def sec(_):
        return _ok("Security")

    async def perf(_):
        raise RateLimited(30.0)

    async def qual(_):
        raise RateLimited(45.0)

    monkeypatch.setattr(orchestrator, "run_security_specialist", sec)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", perf)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", qual)

    outcome = await orchestrator.attempt_review("owner/repo", 1)

    assert isinstance(outcome, orchestrator.ReviewRateLimited)
    assert outcome.retry_after == 45.0  # max of the two
    assert posted == []                 # no comment on a rate-limited review


async def test_attempt_review_completes_and_posts_when_ok(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    posted = {}

    def fake_upsert(repo, pr, body, comment_id=None):
        posted["body"] = body
        posted["comment_id_in"] = comment_id
        return SimpleNamespace(id=222)

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def mk(name):
        async def _inner(_):
            return _ok(name)
        return _inner

    monkeypatch.setattr(orchestrator, "run_security_specialist", await mk("Security"))
    monkeypatch.setattr(orchestrator, "run_performance_specialist", await mk("Performance"))
    monkeypatch.setattr(orchestrator, "run_quality_specialist", await mk("Code Quality"))

    outcome = await orchestrator.attempt_review("owner/repo", 2, comment_id=555)

    assert isinstance(outcome, orchestrator.ReviewCompleted)
    assert outcome.review.pr_number == 2
    assert "PR #2" in posted["body"]
    assert posted["comment_id_in"] == 555   # incoming id threaded to the post
    assert outcome.comment_id == 222         # posted comment's id captured


async def test_run_review_raises_on_rate_limited(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: None)

    async def rl(_):
        raise RateLimited(12.0)

    async def ok(_):
        return _ok("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", rl)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)

    with pytest.raises(RateLimited):
        await orchestrator.run_review("owner/repo", 3)


async def test_run_specialist_lets_rate_limited_escape(monkeypatch):
    """run_specialist normally never raises — but RateLimited MUST escape so the
    orchestrator can defer instead of rendering a failed row."""
    import app.specialists.base as base

    class FakeProvider:
        async def complete(self, system, user, schema):
            raise RateLimited(20.0)

    monkeypatch.setattr(base, "get_provider", lambda: FakeProvider())

    from app.specialists.security import SecurityFindings, SECURITY_SYSTEM_PROMPT

    with pytest.raises(RateLimited):
        await base.run_specialist(
            name="Security",
            annotated_diff="diff",
            system_prompt=SECURITY_SYSTEM_PROMPT,
            container_schema=SecurityFindings,
        )
