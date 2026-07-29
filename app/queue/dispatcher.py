"""The single serial consumer of the ticket queue.

It is the ONLY caller of the review path, so all pacing/quota decisions are
serialized. ``blocked_until`` is a per-provider soft gate learned only from
Retry-After (via ReviewRateLimited) so we don't fire calls we know will fail;
it is intentionally in-memory — the durable truth is each ticket's not_before.

Delay handling is uniform: any ticket that can't run now gets a placeholder
comment (the notification) and is deferred; the real result later edits that
same comment in place via the comment marker.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import github_app
from app.config import settings
from app.formatting import format_failure, format_placeholder
from app.orchestrator import ReviewRateLimited, attempt_review
from app.queue import store

logger = logging.getLogger(__name__)


def _jitter() -> float:
    """Injectable jitter source — 0.0 unless dispatcher_backoff_jitter_seconds > 0.

    Kept as a module-level seam so tests monkeypatch it to a constant and the
    whole system stays deterministic; a future multi-instance deployment sets
    the config > 0 to spread retries without a code change.
    """
    jitter_max = settings.dispatcher_backoff_jitter_seconds
    if jitter_max <= 0:
        return 0.0
    return random.uniform(0.0, jitter_max)


def compute_backoff(attempts: int, jitter: float = 0.0) -> float:
    """Exponential backoff for a hard-failure retry: min(base*2^(n-1), cap) + jitter.

    ``attempts`` is the 1-based per-ticket hard-failure count (first failure -> base).
    """
    base = settings.dispatcher_failure_base_backoff_seconds
    cap = settings.dispatcher_failure_max_backoff_seconds
    return min(base * 2 ** (attempts - 1), cap) + jitter


_blocked_until: dict[str, datetime] = {}


def reset_blocked_until() -> None:
    """Clear the in-memory provider block map (used to isolate tests)."""
    _blocked_until.clear()


def _has_visible_review(ticket: store.Ticket) -> bool:
    """True when a prior successful review is already on the PR (worth preserving
    over a placeholder or a bare failure notice). Set only by finalize_review."""
    return ticket.last_reviewed_at is not None


@dataclass
class StepResult:
    action: str  # "idle" | "ran" | "deferred" | "failed"
    ticket_id: int | None = None


async def _post_placeholder(repo: str, pr: int, retry_after: float, now: datetime) -> None:
    await asyncio.to_thread(
        github_app.upsert_comment, repo, pr, format_placeholder(pr, retry_after, now)
    )


async def process_next_due(now: datetime) -> StepResult:
    """Claim and process one due ticket. Returns what happened.

    action semantics: "deferred" = RateLimited OR a retryable hard failure;
    "failed" = terminal hard-stop; "ran" = completed; "idle" = nothing due.
    """
    ticket = store.claim_next_due(now.isoformat())
    if ticket is None:
        return StepResult(action="idle")

    # Gate on the CURRENT provider (settings.llm_provider), not the provider
    # recorded on the ticket at enqueue time — attempt_review always runs
    # against whatever provider is active now, so that's what can be blocked.
    provider = settings.llm_provider
    blocked = _blocked_until.get(provider)
    if blocked is not None and now < blocked:
        store.defer_rate_limited(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
        if not _has_visible_review(ticket):
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    try:
        outcome = await attempt_review(ticket.repo_full_name, ticket.pr_number)
    except Exception as exc:  # noqa: BLE001 - hard failure: back off per-ticket, hard-stop at the cap
        logger.exception("review attempt failed for ticket %s", ticket.id)
        next_attempt = ticket.attempts + 1
        if next_attempt >= settings.dispatcher_max_failure_attempts:
            store.mark_failed(ticket.id, now=now.isoformat(), error=str(exc))
            await asyncio.to_thread(
                github_app.upsert_comment,
                ticket.repo_full_name,
                ticket.pr_number,
                format_failure(ticket.pr_number, next_attempt),
            )
            return StepResult(action="failed", ticket_id=ticket.id)
        backoff = compute_backoff(next_attempt, _jitter())
        until = now + timedelta(seconds=backoff)
        store.defer_failed(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        return StepResult(action="deferred", ticket_id=ticket.id)

    if isinstance(outcome, ReviewRateLimited):
        wait = max(outcome.retry_after, settings.dispatcher_min_retry_after_seconds)
        until = now + timedelta(seconds=wait)
        _blocked_until[provider] = until
        store.defer_rate_limited(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        if not _has_visible_review(ticket):
            await _post_placeholder(ticket.repo_full_name, ticket.pr_number, wait, now)
        return StepResult(action="deferred", ticket_id=ticket.id)

    rereview_not_before = (
        now + timedelta(seconds=settings.dispatcher_rereview_cooldown_seconds)
    ).isoformat()
    store.finalize_review(ticket.id, now=now.isoformat(), rereview_not_before=rereview_not_before)
    return StepResult(action="ran", ticket_id=ticket.id)


async def run_forever() -> None:
    """Production loop: drain the queue, idling when empty. Thin wrapper over
    process_next_due (which holds the tested logic).

    Sleeps ``dispatcher_idle_sleep_seconds`` after EVERY iteration, not only
    "idle" ones. A "deferred"/"failed" step can otherwise fire again with
    zero delay (e.g. a ``Retry-After: 0`` or already-past HTTP-date response),
    hammering the same doomed call in a tight loop — the exact 429-hammering
    pattern that has already gotten a provider account-level blocked on this
    project (see CLAUDE.md). This floor is a blunt but robust backstop that
    also defends any future fast-loop path.
    """
    while True:
        try:
            await process_next_due(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
        await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)
