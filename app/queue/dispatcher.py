"""The single serial consumer of the ticket queue.

It is the ONLY caller of the review path, so all pacing/quota decisions are
serialized. ``blocked_until`` is a per-provider soft gate learned only from
Retry-After (via ReviewRateLimited) so we don't fire calls we know will fail;
it is intentionally in-memory — the durable truth is each ticket's not_before.

Delay handling: a ticket that can't run now is deferred; it also gets a
placeholder comment UNLESS a good review is already visible on the PR
(``_has_visible_review``), in which case a self-cleaning "re-review
scheduled" footnote is shown instead (posted/refreshed by the
``post_pending_notices`` sweep, run once per loop iteration) rather than
staying fully silent. The real result later edits that same comment in
place via the comment marker; claiming a ticket strips any pending
schedule footnote first, since the wait is over regardless of outcome.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import github_app
from app.config import settings
from app.formatting import (
    format_failure,
    format_failure_footnote,
    format_placeholder,
    format_schedule_notice,
)
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


async def _post_placeholder(
    repo: str, pr: int, retry_after: float, now: datetime, comment_id: int | None = None
) -> None:
    await asyncio.to_thread(
        github_app.upsert_comment, repo, pr, format_placeholder(pr, retry_after, now), comment_id
    )


async def post_pending_notices(now: datetime) -> int:
    """Refresh the schedule footnote on every deferred ticket whose not_before
    changed since the last notice. Returns the count posted. Called once per
    run_forever iteration, alongside process_next_due."""
    posted = 0
    for ticket in store.tickets_needing_notice(now.isoformat()):
        try:
            await asyncio.to_thread(
                github_app.append_schedule_notice,
                ticket.repo_full_name,
                ticket.pr_number,
                format_schedule_notice(datetime.fromisoformat(ticket.not_before)),
                ticket.comment_id,
            )
            store.mark_notice_posted(ticket.id, ticket.not_before)
            posted += 1
        except Exception:  # noqa: BLE001 - one ticket's failure must not block the rest
            logger.exception("failed to post schedule notice for ticket %s", ticket.id)
    return posted


async def process_next_due(now: datetime) -> StepResult:
    """Claim and process one due ticket. Returns what happened.

    action semantics: "deferred" = RateLimited OR a retryable hard failure;
    "failed" = terminal hard-stop; "ran" = completed; "idle" = nothing due.
    """
    ticket = store.claim_next_due(now.isoformat())
    if ticket is None:
        return StepResult(action="idle")

    if ticket.notice_not_before is not None:
        try:
            await asyncio.to_thread(
                github_app.clear_schedule_notice,
                ticket.repo_full_name, ticket.pr_number, ticket.comment_id,
            )
            store.clear_notice(ticket.id)
        except Exception:  # noqa: BLE001 - a stale note is cosmetic; must not block the review
            logger.exception("failed to clear schedule notice for ticket %s", ticket.id)

    # Gate on the CURRENT provider (settings.llm_provider), not the provider
    # recorded on the ticket at enqueue time — attempt_review always runs
    # against whatever provider is active now, so that's what can be blocked.
    provider = settings.llm_provider
    blocked = _blocked_until.get(provider)
    if blocked is not None and now < blocked:
        store.defer_rate_limited(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
        if not _has_visible_review(ticket):
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now,
                ticket.comment_id,
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    try:
        outcome = await attempt_review(
            ticket.repo_full_name, ticket.pr_number, comment_id=ticket.comment_id
        )
    except Exception as exc:  # noqa: BLE001 - hard failure: back off per-ticket, hard-stop at the cap
        logger.exception("review attempt failed for ticket %s", ticket.id)
        next_attempt = ticket.attempts + 1
        if next_attempt >= settings.dispatcher_max_failure_attempts:
            try:
                if _has_visible_review(ticket):
                    # Preserve the good review; append a self-cleaning footnote.
                    await asyncio.to_thread(
                        github_app.append_review_footnote,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure_footnote(next_attempt),
                        ticket.comment_id,
                    )
                else:
                    # No good review to preserve — the notice takes the marker comment.
                    await asyncio.to_thread(
                        github_app.upsert_comment,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure(ticket.pr_number, next_attempt),
                        ticket.comment_id,
                    )
            except Exception:  # noqa: BLE001 - couldn't post the notice; don't strand as terminal
                logger.exception("failed to post terminal failure notice for ticket %s", ticket.id)
                notice_post_ceiling = (
                    settings.dispatcher_max_failure_attempts
                    + settings.dispatcher_max_notice_post_attempts
                )
                if next_attempt > notice_post_ceiling:
                    # The notice itself has now failed to post
                    # dispatcher_max_notice_post_attempts times in a row on top
                    # of the original hard-stop -- retrying forever would be an
                    # unbounded retry loop for what is evidently a persistent
                    # failure (not transient). Give up on the notice and go
                    # terminal anyway: a lost notice is strictly better than
                    # looping forever.
                    store.mark_failed(ticket.id, now=now.isoformat(), error=str(exc))
                    return StepResult(action="failed", ticket_id=ticket.id)
                backoff = compute_backoff(next_attempt, _jitter())
                store.defer_failed(
                    ticket.id,
                    not_before=(now + timedelta(seconds=backoff)).isoformat(),
                    now=now.isoformat(),
                )
                return StepResult(action="deferred", ticket_id=ticket.id)
            store.mark_failed(ticket.id, now=now.isoformat(), error=str(exc))
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
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, wait, now, ticket.comment_id
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

    level = ticket.cooldown_level
    rereview_not_before = (
        now + timedelta(seconds=store.effective_cooldown(level))
    ).isoformat()
    store.finalize_review(
        ticket.id,
        now=now.isoformat(),
        rereview_not_before=rereview_not_before,
        rereview_cooldown_level=store.next_cooldown_level(level),
        comment_id=outcome.comment_id,
    )
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
        now = datetime.now(timezone.utc)
        try:
            await process_next_due(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
        try:
            await post_pending_notices(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
        await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)
