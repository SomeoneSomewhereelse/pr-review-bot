"""The operator CLI that sets the DB usage-cap override. Uses the shared
Postgres test harness -- it writes to the same table the service reads. Mirrors
tests/test_set_cooldown_script.py: no secret is at stake, only numbers, so the
Render check degrades to a notice and there is no --force flag."""
from __future__ import annotations

import pytest

from app.queue import store
from scripts import set_usage_cap


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_tokens_only_leaves_others_untouched():
    assert set_usage_cap.main(["--tokens", "20000"]) == 0
    assert store.get_usage_cap_overrides() == (20000, None, None)


def test_sets_all_three():
    assert set_usage_cap.main(
        ["--tokens", "20000", "--cost", "0.5", "--reset", "06:30"]
    ) == 0
    assert store.get_usage_cap_overrides() == (20000, 0.5, "06:30")


def test_a_second_call_with_one_flag_preserves_the_others():
    set_usage_cap.main(["--tokens", "20000", "--cost", "0.5", "--reset", "06:30"])
    assert set_usage_cap.main(["--cost", "0.25"]) == 0
    assert store.get_usage_cap_overrides() == (20000, 0.25, "06:30")


def test_clear_removes_all_three():
    set_usage_cap.main(["--tokens", "20000", "--cost", "0.5", "--reset", "06:30"])
    assert set_usage_cap.main(["--clear"]) == 0
    assert store.get_usage_cap_overrides() == (None, None, None)


def test_clear_cannot_be_combined(capsys):
    assert set_usage_cap.main(["--clear", "--tokens", "20000"]) == 2
    assert "--clear cannot be combined" in capsys.readouterr().err


def test_requires_at_least_one_flag(capsys):
    assert set_usage_cap.main([]) == 2
    assert "at least one of" in capsys.readouterr().err


def test_refuses_a_non_positive_token_cap(capsys):
    assert set_usage_cap.main(["--tokens", "0"]) == 2
    assert "must be > 0" in capsys.readouterr().err


def test_refuses_an_unparseable_reset_time(capsys):
    assert set_usage_cap.main(["--reset", "half-past-four"]) == 2
    assert "HH:MM" in capsys.readouterr().err


def test_refuses_a_write_that_would_read_back_inert(capsys):
    """A merged trio with an invalid field in the DB + a valid change to another
    field would be written successfully and then ignored on every read -- the
    script would report success while the entire trio is discarded. Refuse instead.

    This tests the merged-trio validity check, not per-argument validation.
    The per-argument check for --cost passes (10 > 0), but the merged trio has
    an invalid token cap from the DB, so the script should refuse."""
    # Seed the DB with an invalid token cap
    store.set_usage_cap_override(tokens=-5, cost=None, reset=None, now="2026-01-01T00:00:00+00:00")
    # Now try to update only cost with a valid value
    assert set_usage_cap.main(["--cost", "10"]) == 2
    assert "non-positive token cap" in capsys.readouterr().err
    # DB should still have the invalid state (unchanged)
    assert store.get_usage_cap_overrides()[0] == -5


def test_setting_default_reset_time_succeeds():
    """Setting a reset time that happens to equal the env default should succeed,
    not be refused as a false positive. This tests that the refusal is based on
    direct validity of the merged trio, not on equality to env defaults."""
    # Fresh DB (no override)
    assert store.get_usage_cap_overrides() == (None, None, None)
    # Set reset to the default 04:00
    assert set_usage_cap.main(["--reset", "04:00"]) == 0
    # Should succeed and write to DB
    assert store.get_usage_cap_overrides() == (None, None, "04:00")


def test_accepts_seconds_in_the_reset_time():
    assert set_usage_cap.main(["--reset", "23:59:30"]) == 0
    assert store.get_usage_cap_overrides()[2] == "23:59:30"
