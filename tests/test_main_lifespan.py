"""Tests for app.main's lifespan: init_db, recover_on_startup, and the
dispatcher background task's start/stop.

``ASGITransport`` (used in the existing webhook tests) never fires ASGI
lifespan startup/shutdown events, so this behavior was previously unverified.
We drive ``app.main.lifespan`` directly as an async context manager instead
of spinning up a real ASGI transport — no new runtime/dev dependency needed.
"""
from __future__ import annotations

import asyncio

import pytest

import app.main as main
from app.config import settings
from app.queue import dispatcher, store


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "queue_db_path", str(tmp_path / "queue.db"))
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


async def _hang_forever() -> None:
    # Stands in for the real infinite dispatcher loop without doing any real
    # work or real sleeping; cancelled cleanly on shutdown.
    await asyncio.Event().wait()


async def test_lifespan_inits_db_recovers_running_tickets_and_stops_dispatcher(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)

    # Seed a ticket stuck 'running' (as if the process crashed mid-review),
    # via a real DB at the temp path, mirroring what recover_on_startup will
    # see when the real init_db()/recover_on_startup() run inside lifespan.
    store.init_db()
    tid = store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha1",
        provider="groq", now="2026-01-01T12:00:00+00:00",
    )
    store.claim_next_due(now="2026-01-01T12:00:01+00:00")
    assert store.get_ticket(tid).status == "running"

    # Capture the task created inside lifespan (it's a local variable there,
    # not exposed on the module) by spying on asyncio.create_task.
    created_tasks = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    async with main.lifespan(main.app):
        # (a) init_db ran: a fresh insert works without error.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=2, head_sha="sha2",
            provider="groq", now="2026-01-01T12:00:02+00:00",
        )
        # (b) recover_on_startup ran: the previously-'running' ticket is
        # reset to 'pending'.
        assert store.get_ticket(tid).status == "pending"

    # (c) after exiting the context manager, the dispatcher task was
    # cancelled/awaited — no leaked background task.
    assert len(created_tasks) == 1
    assert created_tasks[0].done()
    assert created_tasks[0].cancelled()
