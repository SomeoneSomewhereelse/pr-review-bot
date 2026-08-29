"""The step model is pure: given a state, it names where you are and what to
run next. Both tracks share steps 1-4 and diverge after (design spec
2026-08-18 section 4a)."""
from __future__ import annotations

import pytest

from bot.config import settings
from scripts import doctor

ALL_SATISFIED = doctor.State(
    prereqs=True, app_credentials=True, app_installed=True, llm_ready=True,
    database=True, public_url=True, webhook=True, keepalive=True,
)


def _state(**overrides) -> doctor.State:
    return ALL_SATISFIED._replace(**overrides)


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_each_track_has_eight_consecutively_numbered_steps(track):
    steps = doctor.steps_for(track)
    assert [s.number for s in steps] == list(range(1, 9))
    for step in steps:
        assert step.title.strip()
        assert step.command.strip(), f"step {step.number} must name a next action"


def test_both_tracks_share_their_first_four_steps():
    local, hosted = doctor.steps_for("local"), doctor.steps_for("hosted")
    assert local[:4] == hosted[:4]
    assert local[4:] != hosted[4:], "tracks must actually diverge after step 4"


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_every_step_field_exists_on_state(track):
    """A typo'd field name would make a step silently never satisfiable."""
    for step in doctor.steps_for(track):
        assert step.field in doctor.State._fields


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_current_step_is_the_first_unsatisfied_one(track):
    assert doctor.current_step(track, _state(prereqs=False)).number == 1
    assert doctor.current_step(track, _state(app_credentials=False)).number == 2
    assert doctor.current_step(track, ALL_SATISFIED) is None


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_the_earliest_unsatisfied_step_wins(track):
    """Reporting a later gap first would send an operator down the wrong path."""
    step = doctor.current_step(track, _state(prereqs=False, keepalive=False))
    assert step.number == 1


def test_resolve_track_prefers_the_explicit_flag(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    assert doctor.resolve_track("local") == "local"


def test_resolve_track_detects_hosted_from_render_signals(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    assert doctor.resolve_track(None) == "hosted"

    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    assert doctor.resolve_track(None) == "hosted"


def test_resolve_track_defaults_to_local(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://a-tunnel.example.com")
    assert doctor.resolve_track(None) == "local"
