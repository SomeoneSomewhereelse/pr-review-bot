"""GitHub PR webhook route: HMAC verification, delivery dedup, background review."""

import json
import logging
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.config import settings
from app.hmac_verify import verify_signature
from app.orchestrator import run_review as _orchestrator_run_review

logger = logging.getLogger(__name__)

# PR actions we react to. GitHub sends the `pull_request` event for many
# actions (closed, labeled, assigned, ...) — per SPEC.md's confirmed
# decision, only these three trigger a review; everything else is a no-op.
_REVIEW_TRIGGER_ACTIONS = {"opened", "reopened", "synchronize"}

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


async def run_review(payload: dict) -> None:
    """Background-task entry point: filter irrelevant PR actions, then review.

    GitHub sends the ``pull_request`` webhook event for many actions
    (closed, labeled, assigned, ...); only ``opened``/``reopened``/
    ``synchronize`` trigger an actual review (SPEC.md's confirmed decision).
    Everything else is a silent no-op — no orchestrator call, no comment.
    """
    action = payload.get("action")
    if action not in _REVIEW_TRIGGER_ACTIONS:
        return

    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    pr_number = pull_request.get("number")

    if not repo_full_name or pr_number is None:
        logger.warning("pull_request webhook missing repo/pr number; skipping review")
        return

    await _orchestrator_run_review(repo_full_name, pr_number)


@router.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    if not verify_signature(raw_body, signature_header, settings.github_webhook_secret):
        logger.warning("Rejected webhook: invalid signature (delivery=%s)", delivery_id)
        return Response(status_code=401)

    if delivery_id is not None and _is_duplicate_delivery(delivery_id):
        return Response(status_code=200, content="already processed")

    payload = json.loads(raw_body)
    background_tasks.add_task(run_review, payload)
    return Response(status_code=202)
