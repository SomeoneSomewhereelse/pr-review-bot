import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from bot.config import settings
from bot.main import app
from bot import webhook
from bot.queue import store

TEST_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
def _isolate(db, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    webhook.reset_dedup_cache()
    yield
    webhook.reset_dedup_cache()


async def _client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_valid_signature_returns_202():
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "11111111-1111-1111-1111-111111111111",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 202


async def test_invalid_signature_returns_401():
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
        "X-GitHub-Delivery": "22222222-2222-2222-2222-222222222222",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401


async def test_missing_signature_header_returns_401():
    body = b'{"action": "opened"}'
    headers = {"X-GitHub-Delivery": "33333333-3333-3333-3333-333333333333"}
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401


async def test_replayed_delivery_id_is_noop(monkeypatch):
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "44444444-4444-4444-4444-444444444444",
    }
    async with await _client() as c:
        first = await c.post("/webhook", content=body, headers=headers)
        second = await c.post("/webhook", content=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200


async def test_opened_action_enqueues_ticket():
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "55555555-5555-5555-5555-555555555555",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 7
    assert ticket.head_sha == "abc123"


async def test_ready_for_review_action_enqueues_ticket():
    """GitHub fires this independent of any push -- the only way a draft PR
    marked ready with zero new commits still gets reviewed (ISSUES.md's
    'Draft PRs are reviewed identically to ready-for-review PRs' gap)."""
    payload = {
        "action": "ready_for_review",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 8, "head": {"sha": "abc123"}, "draft": False},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 8


async def test_edited_action_with_base_change_enqueues_ticket():
    """A base-branch retarget can change the effective diff entirely --
    ISSUES.md's 'pull_request.edited never triggers a re-review' gap."""
    payload = {
        "action": "edited",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
        "changes": {"base": {"ref": {"from": "old-branch"}, "sha": {"from": "oldsha"}}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "88888888-8888-8888-8888-888888888888",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 9


async def test_edited_action_without_base_change_does_not_enqueue():
    """A title/body-only edit must stay a no-op -- only a base retarget
    changes the effective diff."""
    payload = {
        "action": "edited",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
        "changes": {"title": {"from": "old title"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "99999999-9999-9999-9999-999999999999",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_edited_action_with_no_changes_key_does_not_enqueue():
    """Defensive: a malformed/incomplete edited payload with no `changes` at
    all must never be mistaken for a base retarget."""
    payload = {
        "action": "edited",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_ignored_action_does_not_enqueue():
    payload = {
        "action": "labeled",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_closed_action_cancels_a_pending_ticket(db_query):
    opened = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 9, "head": {"sha": "abc123"}},
    }
    closed = {**opened, "action": "closed"}
    async with await _client() as c:
        await c.post(
            "/webhook", content=json.dumps(opened).encode(),
            headers={
                "X-Hub-Signature-256": _sign(json.dumps(opened).encode()),
                "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
            },
        )
        response = await c.post(
            "/webhook", content=json.dumps(closed).encode(),
            headers={
                "X-Hub-Signature-256": _sign(json.dumps(closed).encode()),
                "X-GitHub-Delivery": "88888888-8888-8888-8888-888888888888",
            },
        )

    assert response.status_code == 202
    assert db_query(
        "SELECT status FROM tickets WHERE repo_full_name = 'owner/repo' AND pr_number = 9"
    ) == [("cancelled",)]


async def test_closed_action_on_untracked_pr_is_a_noop():
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 10, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "99999999-9999-9999-9999-999999999999",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None


async def test_webhook_ignores_non_target_repo(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/target-repo")
    payload = {"action": "opened",
               "repository": {"full_name": "someone/OTHER-repo"},
               "pull_request": {"number": 5, "head": {"sha": "abc"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)   # existing helper in this module
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-nonmatch"})
    assert resp.status_code == 202                      # accepted, but...
    assert db_query("SELECT count(*) FROM tickets") == [(0,)]   # ...no ticket enqueued


async def test_webhook_accepts_repo_listed_in_comma_separated_allowlist(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo-a,owner/repo-b")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/repo-b"},
               "pull_request": {"number": 9, "head": {"sha": "def"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-multi-match"},
        )
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]


async def test_webhook_rejects_repo_not_in_comma_separated_allowlist(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo-a,owner/repo-b")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/OTHER-repo"},
               "pull_request": {"number": 9, "head": {"sha": "def"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-multi-nonmatch"},
        )
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(0,)]


async def test_webhook_matches_allowlist_entry_case_insensitively(monkeypatch, db_query):
    """GitHub repo names are case-insensitive; a webhook payload's casing need
    not match GITHUB_TARGET_REPO's casing exactly."""
    monkeypatch.setattr(settings, "github_target_repo", "Owner/Repo-A")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/repo-a"},
               "pull_request": {"number": 11, "head": {"sha": "ghi"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-casefold"},
        )
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]


async def test_webhook_accepts_any_repo_when_target_repo_unset(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "")
    payload = {"action": "opened",
               "repository": {"full_name": "someone/any-repo"},
               "pull_request": {"number": 3, "head": {"sha": "xyz"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-trackall"})
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]
