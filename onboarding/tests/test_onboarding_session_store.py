"""Tests for onboarding.session_store -- see
docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md
section 5 for what this module must guarantee."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from onboarding import session_store
from onboarding.config import settings


@pytest.fixture(autouse=True)
def _configure_encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "onboarding_session_encryption_key", Fernet.generate_key().decode()
    )


pytestmark = pytest.mark.usefixtures("onboarding_db")


def test_create_session_mints_a_fresh_id_each_call():
    a = session_store.create_session()
    b = session_store.create_session()
    assert a != b


def test_get_session_returns_none_for_an_unknown_id():
    assert session_store.get_session("nonexistent") is None


def test_get_session_returns_empty_frames_for_a_fresh_session():
    session_id = session_store.create_session()
    result = session_store.get_session(session_id)
    assert result.frames == {}


def test_update_frame_then_read_frame_round_trips():
    session_id = session_store.create_session()
    result = session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    assert result is None
    assert session_store.read_frame(session_id, "render") == {"api_key": "rnd_abc"}


def test_update_frame_merges_rather_than_replaces():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    session_store.update_frame(session_id, "render", {"service_id": "srv-1"})
    assert session_store.read_frame(session_id, "render") == {
        "api_key": "rnd_abc",
        "service_id": "srv-1",
    }


def test_update_frame_does_not_clobber_a_different_frame():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    session_store.update_frame(session_id, "github_app", {"app_id": 1})
    assert session_store.read_frame(session_id, "render") == {"api_key": "rnd_abc"}
    assert session_store.read_frame(session_id, "github_app") == {"app_id": 1}


def test_update_frame_against_a_missing_session_id_fails_closed():
    result = session_store.update_frame("nonexistent", "render", {"api_key": "x"})
    assert isinstance(result, session_store.SessionNotFound)


def test_update_frame_against_a_deleted_session_does_not_resurrect_it():
    """The fork-risk scenario raised during design review: two successive
    update_frame calls against a session id that was deleted between them
    must not silently recreate a session containing only the second call's
    data."""
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    session_store.delete_session(session_id)
    result = session_store.update_frame(session_id, "render", {"api_key": "rnd_xyz"})
    assert isinstance(result, session_store.SessionNotFound)
    assert session_store.get_session(session_id) is None


def test_update_frame_replace_discards_the_frames_existing_content():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_old", "service_id": "srv-1"})
    session_store.update_frame(session_id, "render", {"api_key": "rnd_new"}, replace=True)
    assert session_store.read_frame(session_id, "render") == {"api_key": "rnd_new"}


def test_update_frame_replace_does_not_clobber_a_different_frame():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_x"})
    session_store.update_frame(session_id, "github_app", {"app_id": 1})
    session_store.update_frame(session_id, "render", {"api_key": "rnd_y"}, replace=True)
    assert session_store.read_frame(session_id, "github_app") == {"app_id": 1}


def test_update_frame_returns_session_not_found_when_the_update_affects_zero_rows(monkeypatch):
    """Simulates the narrower race window inside update_frame itself: its
    own internal get_session() succeeds, but the row is gone by the time
    its UPDATE runs (deleted by something else in that gap -- a TTL sweep,
    a concurrent reset). The UPDATE then genuinely affects 0 rows, which
    must be reported as SessionNotFound, not silently treated as success --
    a zero-row UPDATE is not a write that happened."""
    session_id = session_store.create_session()
    stale_view = session_store.get_session(session_id)
    session_store.delete_session(session_id)
    monkeypatch.setattr(session_store, "get_session", lambda sid: stale_view)
    result = session_store.update_frame(session_id, "render", {"api_key": "x"})
    assert isinstance(result, session_store.SessionNotFound)


def test_read_frame_returns_none_for_a_frame_never_written():
    session_id = session_store.create_session()
    assert session_store.read_frame(session_id, "render") is None


def test_delete_session_is_a_noop_against_a_missing_id():
    session_store.delete_session("nonexistent")  # must not raise


def test_get_session_treats_an_expired_row_as_missing_and_sweeps_it():
    session_id = session_store.create_session()
    with session_store._require_pool().connection() as conn:
        conn.execute(
            "UPDATE wizard_sessions SET expires_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(seconds=1), session_id),
        )
    assert session_store.get_session(session_id) is None
    with session_store._require_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM wizard_sessions WHERE id = %s", (session_id,)
        ).fetchone()
    assert row is None


def test_create_session_sweeps_expired_rows_from_earlier_sessions():
    session_id = session_store.create_session()
    with session_store._require_pool().connection() as conn:
        conn.execute(
            "UPDATE wizard_sessions SET expires_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(seconds=1), session_id),
        )
    session_store.create_session()
    with session_store._require_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM wizard_sessions WHERE id = %s", (session_id,)
        ).fetchone()
    assert row is None


def test_a_frame_that_fails_to_decrypt_is_omitted_not_raised(monkeypatch):
    """Simulates a key rotation: a frame written under one Fernet key is
    unreadable under another, and must read as absent, not crash."""
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    monkeypatch.setattr(
        settings, "onboarding_session_encryption_key", Fernet.generate_key().decode()
    )
    result = session_store.get_session(session_id)
    assert result.frames == {}


def test_raw_row_content_never_contains_the_plaintext_value():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_super_secret_value"})
    with session_store._require_pool().connection() as conn:
        row = conn.execute(
            "SELECT frame_data FROM wizard_sessions WHERE id = %s", (session_id,)
        ).fetchone()
    assert "rnd_super_secret_value" not in str(row["frame_data"])
