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


def test_refuses_a_write_that_would_read_back_inert(capsys, monkeypatch):
    """A cap that effective_caps() discards would be written successfully and
    then ignored on every read -- the script would report success while
    changing nothing. Refuse instead."""
    from app.config import settings

    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    assert set_usage_cap.main(["--tokens", "-5"]) == 2
    assert store.get_usage_cap_overrides() == (None, None, None)


def test_accepts_seconds_in_the_reset_time():
    assert set_usage_cap.main(["--reset", "23:59:30"]) == 0
    assert store.get_usage_cap_overrides()[2] == "23:59:30"
