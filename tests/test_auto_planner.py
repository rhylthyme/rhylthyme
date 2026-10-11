"""Tests for rhylthyme/auto_planner.py.

`auto_planner` is reachable only through its own ``__main__`` CLI — nothing in
the monorepo imports it, and the planners the product actually runs are the
browser ones in ``rhylthyme-server/static/js/auto-plan.js``. It also never
runs replicate expansion, so it cannot honour program schema 0.3.0-alpha's
``instances`` / ``replicates.maxInFlight``. These tests pin the two things
that follow from that: it refuses such programs with a clear message, and it
reads resource constraints in the schema's shape.
"""

import json
from pathlib import Path

import pytest

from rhylthyme.auto_planner import (
    AutoPlanner,
    _unsupported_constructs,
    auto_plan_program,
)

# The examples repo is checked out beside this one.
COOKIES = str(
    Path(__file__).resolve().parents[2]
    / "rhylthyme-examples"
    / "programs"
    / "cookies_three_trays.json"
)


def _write(tmp_path, program, name="program.json"):
    path = tmp_path / name
    path.write_text(json.dumps(program), encoding="utf-8")
    return str(path)


def test_refuses_the_cookie_example():
    if not Path(COOKIES).exists():
        pytest.skip("rhylthyme-examples not checked out beside this repo")
    with pytest.raises(ValueError) as excinfo:
        auto_plan_program(COOKIES, "minimize_length")
    message = str(excinfo.value)
    assert "maxInFlight" in message
    assert "instances" in message
    assert "auto-plan.js" in message


@pytest.mark.parametrize(
    "strategy", ["synchronized_finish", "cover", "minimize_length"]
)
def test_every_strategy_refuses_in_flight_programs(tmp_path, strategy):
    program = {
        "programId": "p",
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "bake",
                        "task": "oven",
                        "duration": {"type": "fixed", "seconds": 720},
                        "replicates": {"count": 3, "mode": "serial", "maxInFlight": 2},
                        "startTrigger": {"type": "programStart"},
                    },
                ],
            }
        ],
    }
    with pytest.raises(ValueError, match="maxInFlight"):
        auto_plan_program(_write(tmp_path, program), strategy)


def test_refuses_instances_inside_a_compound_trigger(tmp_path):
    program = {
        "programId": "p",
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "a",
                        "duration": {"type": "fixed", "seconds": 10},
                        "startTrigger": {"type": "programStart"},
                        "replicates": {"count": 2},
                    },
                    {
                        "stepId": "b",
                        "duration": {"type": "fixed", "seconds": 10},
                        "startTrigger": {
                            "logic": "all",
                            "triggers": [
                                {
                                    "type": "afterStep",
                                    "stepId": "a",
                                    "instances": "each",
                                }
                            ],
                        },
                    },
                ],
            }
        ],
    }
    assert _unsupported_constructs(program) == ["instances"]
    with pytest.raises(ValueError, match="instances"):
        auto_plan_program(_write(tmp_path, program), "cover")


def test_plain_0_2_0_programs_still_plan(tmp_path):
    program = {
        "programId": "p",
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "a",
                        "task": "oven",
                        "duration": {"type": "fixed", "seconds": 60},
                        "startTrigger": {"type": "programStart"},
                    },
                    {
                        "stepId": "b",
                        "task": "oven",
                        "duration": {"type": "fixed", "seconds": 60},
                        "startTrigger": {"type": "afterStep", "stepId": "a"},
                    },
                ],
            }
        ],
        "resourceConstraints": [{"task": "oven", "maxConcurrent": 1}],
    }
    assert _unsupported_constructs(program) == []
    result = auto_plan_program(_write(tmp_path, program), "minimize_length")
    assert result["strategy"] == "minimize_length"
    assert {t["stepId"] for t in result["stepTimings"]} == {"a", "b"}


def test_resource_constraints_read_the_schema_shape():
    planner = AutoPlanner(
        {"tracks": []},
        [
            {"task": "oven", "maxConcurrent": 2},
            {"name": "rack", "capacity": 3},  # legacy shape still accepted
            {"task": "prep", "maxConcurrent": 1, "description": "One cook"},
        ],
    )
    assert sorted(planner.resource_constraints) == ["oven", "prep", "rack"]
    assert planner.resource_constraints["oven"].capacity == 2
    assert planner.resource_constraints["rack"].capacity == 3
    assert planner.resource_constraints["prep"].capacity == 1
