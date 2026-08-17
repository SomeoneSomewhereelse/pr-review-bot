"""Tests for app.main's lifespan: init_pool, recover_on_startup, and the
dispatcher background task's start/stop.

``ASGITransport`` (used in the existing webhook tests) never fires ASGI
lifespan startup/shutdown events, so this behavior was previously unverified.
We drive ``app.main.lifespan`` directly as an async context manager instead
of spinning up a real ASGI transport — no new runtime/dev dependency needed.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import app.main as main
from app.config import settings
from app.queue import dispatcher, store


@pytest.fixture(autouse=True)
def _env(db, monkeypatch):
    # Ambient GITHUB_WEBHOOK_SECRET (e.g. local dev's .env) is not guaranteed
    # in every test environment (e.g. a fresh git worktree has no untracked
    # .env file) -- lifespan now refuses to start with an empty secret, so
    # every test that isn't specifically exercising that check needs a
    # non-empty stand-in.
    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


async def _hang_forever() -> None:
    # Stands in for the real infinite dispatcher loop without doing any real
    # work or real sleeping; cancelled cleanly on shutdown.
    await asyncio.Event().wait()


def test_importing_app_main_configures_the_root_logger_for_info_output():
    """ISSUES.md 2026-08-17: the root logger defaults to WARNING when
    unconfigured, which made every logger.info(...) call in the app
    (app/webhook.py included) permanently unreachable in production --
    confirmed live via Render's Logs API returning no match for a line known
    to have fired. force=True matters specifically: a plain basicConfig() is
    a silent no-op once any handler already exists on root, which is exactly
    what happens under pytest itself (its own logging plugin attaches one) --
    the same failure shape this fix exists to eliminate, from a different
    cause. Not scoped to a fresh import: app.main is already imported (by
    this file, above) by the time this runs, which is the real-world case
    every other test file in this suite relies on too.
    """
    assert logging.getLogger("app.webhook").isEnabledFor(logging.INFO)


async def test_lifespan_inits_db_recovers_running_tickets_and_stops_dispatcher(monkeypatch):
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    # Ambient GitHub App config (e.g. local dev's .env) is not real in CI, so
    # pin installation_id here to skip the discovery branch (see
    # test_lifespan_skips_discovery_when_installation_id_already_set below) —
    # this test isn't exercising discovery, just db init/recovery/dispatcher.
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)

    # Seed a ticket stuck 'running' (as if the process crashed mid-review),
    # via the real test DB, mirroring what recover_on_startup will see when
    # the real init_pool()/recover_on_startup() run inside lifespan.
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
        # (a) init_pool ran: a fresh insert works without error.
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


async def test_lifespan_skips_discovery_when_installation_id_already_set(monkeypatch):
    """When GITHUB_APP_INSTALLATION_ID is already configured (e.g. local dev's
    .env), lifespan must not spend a GitHub App JWT call rediscovering it."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)

    def _boom() -> int:
        raise AssertionError(
            "discover_installation_id_for_app must not be called when already set"
        )

    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", _boom)

    async with main.lifespan(main.app):
        pass

    assert settings.github_app_installation_id == 123456


async def test_lifespan_discovers_installation_id_when_unset(monkeypatch):
    """When GITHUB_APP_INSTALLATION_ID is unset (0, e.g. on Render — see design
    spec §6, "becomes optional (auto-discovered)"), lifespan must resolve it
    via github_app.discover_installation_id_for_app before the dispatcher
    starts, and assign the resolved id onto settings -- app-level discovery,
    so this works regardless of whether GITHUB_TARGET_REPO is set (multi-repo
    support design doc §3d)."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 0)
    monkeypatch.setattr(settings, "github_target_repo", "")

    calls = []

    def _fake_discover() -> int:
        calls.append(1)
        return 999999

    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", _fake_discover)

    async with main.lifespan(main.app):
        pass

    assert calls == [1]
    assert settings.github_app_installation_id == 999999


async def test_lifespan_fails_loudly_when_webhook_secret_is_empty(monkeypatch):
    """An empty GITHUB_WEBHOOK_SECRET makes verify_signature accept any
    signature (HMAC with an empty key) -- an effective auth bypass. Startup
    must refuse to run rather than silently degrade."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "github_webhook_secret", "")

    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        async with main.lifespan(main.app):
            pass


async def test_lifespan_fails_loudly_when_postgres_is_unreachable(monkeypatch):
    """Design spec section 11: "If Postgres is unreachable at boot, startup fails
    loudly (correct)". Guards that init_pool()'s diagnostic rewrite did not soften
    that into a warning, and that no dispatcher task is left running."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)

    # The autouse _env(db) fixture already opened a pool on the test Postgres.
    store.close_pool()
    monkeypatch.setattr(
        settings, "database_url", "postgresql://u:p@127.0.0.1:1/postgres?connect_timeout=1"
    )
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    created_tasks = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    with pytest.raises(RuntimeError):
        async with main.lifespan(main.app):
            pass

    # init_pool() raised before create_task was reached: no leaked dispatcher.
    assert created_tasks == []
