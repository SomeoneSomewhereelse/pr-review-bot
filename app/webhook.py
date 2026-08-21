"""GitHub PR webhook route: HMAC verification, delivery dedup, durable enqueue."""

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.config import settings
from app.hmac_verify import verify_signature
from app.providers.active import active_provider
from app.queue import store

logger = logging.getLogger(__name__)

# PR actions we react to. GitHub sends the `pull_request` event for many
# actions (closed, labeled, assigned, ...) — per SPEC.md's confirmed
# decision, only these three trigger a review; everything else is a no-op
# except _CANCEL_ACTIONS below.
_REVIEW_TRIGGER_ACTIONS = {"opened", "reopened", "synchronize"}

# "closed" covers both a merge and a plain close (distinguished only by
# pull_request.merged, which doesn't matter here) -- either way the PR is no
# longer actionable, so any ticket that hasn't started running yet is
# cancelled rather than left to waste a review on it.
_CANCEL_ACTIONS = {"closed"}

router = APIRouter()

_DEDUP_CAPACITY = 1000
_seen_deliveries: OrderedDict[str, None] = OrderedDict()


def reset_dedup_cache() -> None:
    """Clear the in-memory delivery-id cache. Used to isolate tests."""
    _seen_deliveries.clear()


def _is_duplicate_delivery(delivery_id: str) -> bool:
    """Check-and-mark a delivery id against the bounded LRU dedup cache."""
    if delivery_id in _seen_deliveries:
        _seen_deliveries.move_to_end(delivery_id)
        return True

    _seen_deliveries[delivery_id] = None
    if len(_seen_deliveries) > _DEDUP_CAPACITY:
        _seen_deliveries.popitem(last=False)
    return False


async def _handle_pull_request_payload(payload: dict) -> None:
    """React to a `pull_request` webhook payload: enqueue a durable review
    ticket for a triggering action, cancel a queued-but-not-yet-running
    ticket when the PR closes or merges, or no-op for everything else."""
    action = payload.get("action")
    if action not in _REVIEW_TRIGGER_ACTIONS and action not in _CANCEL_ACTIONS:
        return
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    pr_number = pull_request.get("number")
    if not repo_full_name or pr_number is None:
        logger.warning("pull_request webhook missing repo/pr number; skipping")
        return
    target_repos = settings.target_repos()
    if target_repos and repo_full_name.casefold() not in {r.casefold() for r in target_repos}:
        logger.info("Ignoring webhook for non-target repo %s", repo_full_name)
        return

    if action in _CANCEL_ACTIONS:
        await asyncio.to_thread(
            store.cancel_ticket,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            now=datetime.now(timezone.utc).isoformat(),
        )
        return

    head_sha = (pull_request.get("head") or {}).get("sha")
    await asyncio.to_thread(
        store.enqueue_or_update,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        provider=active_provider(),
        now=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/webhook")
async def webhook(request: Request) -> Response:
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    if not verify_signature(raw_body, signature_header, settings.github_webhook_secret):
        logger.warning("Rejected webhook: invalid signature (delivery=%s)", delivery_id)
        return Response(status_code=401)

    if delivery_id is not None and _is_duplicate_delivery(delivery_id):
        return Response(status_code=200, content="already processed")

    payload = json.loads(raw_body)
    await _handle_pull_request_payload(payload)
    return Response(status_code=202)
