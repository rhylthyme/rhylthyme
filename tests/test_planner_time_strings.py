"""`rhylthyme plan` must accept the duration forms the schema allows."""

from rhylthyme.program_planner import Step


def _step(duration):
    return Step({"stepId": "s", "name": "S", "duration": duration}, "t")


def test_durations_as_time_strings():
    assert _step({"type": "fixed", "seconds": "90m"}).calculate_duration() == 5400
    ranged = _step({"type": "variable", "minSeconds": "20m", "maxSeconds": "30m"})
    assert ranged.calculate_duration() == 1500
    assert (ranged.get_min_duration(), ranged.get_max_duration()) == (1200, 1800)
    assert _step({"type": "fixed", "seconds": 60}).calculate_duration() == 60


def test_indefinite_plans_on_its_default():
    step = _step({"type": "indefinite", "defaultSeconds": "16h"})
    assert step.calculate_duration() == 16 * 3600
