"""The step model is pure: given a state, it names where you are and what to
run next."""
from __future__ import annotations

from scripts import doctor

ALL_SATISFIED = doctor.State(
    prereqs=True, app_credentials=True, app_installed=True, llm_ready=True,
    database=True, public_url=True, webhook=True, keepalive=True,
)


def _state(**overrides) -> doctor.State:
    return ALL_SATISFIED._replace(**overrides)


def test_there_are_eight_consecutively_numbered_steps():
    steps = doctor.steps_for()
    assert [s.number for s in steps] == list(range(1, 9))
    for step in steps:
        assert step.title.strip()
        assert step.command.strip(), f"step {step.number} must name a next action"


def test_every_step_field_exists_on_state():
    """A typo'd field name would make a step silently never satisfiable."""
    for step in doctor.steps_for():
        assert step.field in doctor.State._fields


def test_current_step_is_the_first_unsatisfied_one():
    assert doctor.current_step(_state(prereqs=False)).number == 1
    assert doctor.current_step(_state(app_credentials=False)).number == 2
    assert doctor.current_step(ALL_SATISFIED) is None


def test_the_earliest_unsatisfied_step_wins():
    """Reporting a later gap first would send an operator down the wrong path."""
    step = doctor.current_step(_state(prereqs=False, keepalive=False))
    assert step.number == 1
