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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import github_app
from app.config import settings
from app.formatting import format_placeholder
from app.orchestrator import ReviewRateLimited, attempt_review
from app.queue import store

logger = logging.getLogger(__name__)

_blocked_until: dict[str, datetime] = {}


def reset_blocked_until() -> None:
    """Clear the in-memory provider block map (used to isolate tests)."""
    _blocked_until.clear()


@dataclass
class StepResult:
    action: str  # "idle" | "ran" | "deferred"
    ticket_id: int | None = None


def _post_placeholder(repo: str, pr: int, retry_after: float, now: datetime) -> None:
    github_app.upsert_comment(repo, pr, format_placeholder(pr, retry_after, now))


async def process_next_due(now: datetime) -> StepResult:
    """Claim and process one due ticket. Returns what happened."""
    ticket = store.claim_next_due(now.isoformat())
    if ticket is None:
        return StepResult(action="idle")

    provider = ticket.provider
    blocked = _blocked_until.get(provider)
    if blocked is not None and now < blocked:
        remaining = (blocked - now).total_seconds()
        store.defer(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
        _post_placeholder(ticket.repo_full_name, ticket.pr_number, remaining, now)
        return StepResult(action="deferred", ticket_id=ticket.id)

    outcome = await attempt_review(ticket.repo_full_name, ticket.pr_number)

    if isinstance(outcome, ReviewRateLimited):
        until = now + timedelta(seconds=outcome.retry_after)
        _blocked_until[provider] = until
        store.defer(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        _post_placeholder(ticket.repo_full_name, ticket.pr_number, outcome.retry_after, now)
        return StepResult(action="deferred", ticket_id=ticket.id)

    store.mark_done(ticket.id, now=now.isoformat())
    return StepResult(action="ran", ticket_id=ticket.id)


async def run_forever() -> None:
    """Production loop: drain the queue, idling when empty. Thin wrapper over
    process_next_due (which holds the tested logic)."""
    while True:
        try:
            result = await process_next_due(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
            result = StepResult(action="idle")
        if result.action == "idle":
            await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)
