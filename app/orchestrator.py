"""Fetch diff -> run specialists -> merge into ReviewResult -> post PR comment.

This step (build order step 5) runs the Security specialist solo — no
``asyncio.gather`` fan-out yet (that's step 6, once Performance + Code
Quality exist behind the same interface). The loop below is written so
adding more specialists is a small diff: replace the single ``await``
with ``asyncio.gather(sec.run(...), perf.run(...), qual.run(...),
return_exceptions=True)`` and extend the merge step to wrap any raised
``Exception`` into a failed ``SpecialistResult`` (matching what
``specialists/base.py`` already does internally for a single specialist).
"""

from __future__ import annotations

import time

from app import github_app
from app.config import settings
from app.diff_utils import annotate_and_cap
from app.formatting import format_comment
from app.providers.pricing import estimate_cost_usd
from app.specialists.schemas import ReviewResult
from app.specialists.security import run_security_specialist


def _active_model() -> str:
    """Return the model name for whichever provider is actually active.

    ``settings.llm_model`` only applies to the google-genai family
    (vertex/gemini); groq uses its own ``settings.groq_model`` (see
    config.py's comment on why a single shared var became ambiguous).
    """
    if settings.llm_provider == "groq":
        return settings.groq_model
    return settings.llm_model


async def run_review(repo_full_name: str, pr_number: int) -> ReviewResult:
    """Run the full review pipeline for one PR and post the comment.

    Returns the ``ReviewResult`` (useful for tests/logging) in addition to
    posting it as a Markdown comment via ``github_app.upsert_comment``.
    """
    started = time.monotonic()

    raw_diff = github_app.fetch_pr_diff(repo_full_name, pr_number)
    annotated = annotate_and_cap(raw_diff)

    security_result = await run_security_specialist(annotated.text)
    results = [security_result]

    total_tokens_in = sum(r.tokens_in for r in results)
    total_tokens_out = sum(r.tokens_out for r in results)
    total_elapsed_ms = int((time.monotonic() - started) * 1000)

    provider = settings.llm_provider
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
    github_app.upsert_comment(repo_full_name, pr_number, body)

    return review_result
