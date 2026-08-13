"""Fetch diff -> run specialists -> merge into ReviewResult -> post PR comment.

Runs Security, Performance, and Code Quality concurrently via
``asyncio.gather(..., return_exceptions=True)``. Each specialist's own
``run_specialist()`` (specialists/base.py) already never raises — a bad LLM
response becomes a ``status="failed"`` ``SpecialistResult``, not an
exception. The ``return_exceptions=True`` + merge step below is a second,
belt-and-suspenders layer of the same guarantee: even if a specialist
function raised for a reason outside that contract (a genuine bug, a
cancellation, ...), one specialist's exception can never blank the comment
or drop the other two specialists' results (SPEC.md's core resilience
requirement — "partial failure is always visible").
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app import github_app
from app.config import settings
from app.diff_utils import annotate_and_cap
from app.formatting import format_comment
from app.providers.active import active_provider
from app.providers.base import RateLimited
from app.providers.pricing import estimate_cost_usd
from app.queue import store
from app.specialists.performance import run_performance_specialist
from app.specialists.quality import run_quality_specialist
from app.specialists.schemas import ReviewResult, SpecialistResult
from app.specialists.security import run_security_specialist

logger = logging.getLogger(__name__)

_SPECIALIST_NAMES = ("Security", "Performance", "Code Quality")


def _active_model() -> str:
    """Return the model name for whichever provider is actually active.

    ``settings.llm_model`` only applies to the gemini provider; groq has its
    own model var (see config.py's comment on why a single shared var became
    ambiguous once more than one provider family entered the picture).
    """
    provider = active_provider()
    if provider == "groq":
        return settings.groq_model
    return settings.llm_model


@dataclass
class ReviewCompleted:
    review: ReviewResult
    comment_id: int | None = None


@dataclass
class ReviewRateLimited:
    retry_after: float


async def attempt_review(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> ReviewCompleted | ReviewRateLimited:
    """Run the full review pipeline once for one PR.

    On completion, posts the Markdown comment via ``github_app.upsert_comment``
    and returns ``ReviewCompleted``. If any specialist call is rate-limited,
    the whole review is atomic: no comment is posted, and the max
    ``retry_after`` across the rate-limited calls is returned via
    ``ReviewRateLimited`` so a caller (e.g. the durable queue) can retry later.
    """
    started = time.monotonic()

    raw_diff = await asyncio.to_thread(github_app.fetch_pr_diff, repo_full_name, pr_number)
    annotated = annotate_and_cap(raw_diff)

    # Referencing these as bare module-level names (not a precomputed tuple
    # of function objects) means they resolve at call time, so tests can
    # monkeypatch `orchestrator.run_security_specialist` etc. per-call.
    raw_results = await asyncio.gather(
        run_security_specialist(annotated.text),
        run_performance_specialist(annotated.text),
        run_quality_specialist(annotated.text),
        return_exceptions=True,
    )

    rate_limits = [r.retry_after for r in raw_results if isinstance(r, RateLimited)]
    if rate_limits:
        return ReviewRateLimited(retry_after=max(rate_limits))

    results = [
        outcome
        if isinstance(outcome, SpecialistResult)
        else SpecialistResult(
            name=name, status="failed", findings=[], error=str(outcome), elapsed_ms=0
        )
        for name, outcome in zip(_SPECIALIST_NAMES, raw_results)
    ]

    total_tokens_in = sum(r.tokens_in for r in results)
    total_tokens_out = sum(r.tokens_out for r in results)
    total_elapsed_ms = int((time.monotonic() - started) * 1000)

    provider = active_provider()
    model = _active_model()
    est_cost_usd = estimate_cost_usd(provider, model, total_tokens_in, total_tokens_out)

    review_result = ReviewResult(
        pr_number=pr_number,
        provider=provider,
        model=model,
        results=results,
        total_elapsed_ms=total_elapsed_ms,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        est_cost_usd=est_cost_usd,
    )

    body = format_comment(review_result)
    posted = await asyncio.to_thread(
        github_app.upsert_comment, repo_full_name, pr_number, body, comment_id
    )
    try:
        await asyncio.to_thread(
            store.record_review,
            repo_full_name,
            pr_number,
            review_result,
            posted.id,
            datetime.now(timezone.utc).isoformat(),
        )
    # a dashboard-persistence failure must never fail an already-posted review
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to record review for the dashboard (repo=%s pr=%s)",
            repo_full_name, pr_number,
        )
    return ReviewCompleted(review=review_result, comment_id=posted.id)


async def run_review(repo_full_name: str, pr_number: int) -> ReviewResult:
    """Back-compat entry point for scripts/tests: returns the ``ReviewResult``
    on completion, raises ``RateLimited`` if the review was rate-limited.
    """
    outcome = await attempt_review(repo_full_name, pr_number)
    if isinstance(outcome, ReviewRateLimited):
        raise RateLimited(outcome.retry_after)
    return outcome.review
