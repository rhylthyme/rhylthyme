"""
Unit tests for replicate expansion with schema 0.3.0-alpha `instances`
triggers ("each" / "all" / "any").

The CLI runner imports the expander from the root ``rhylthyme`` package at
run time (see program_runner.py), so these tests exercise that module
directly. They mirror the JS cases in rhylthyme-timeline/test/expand.test.js.
"""

import copy
import json
from pathlib import Path

import pytest

from rhylthyme.expand_replicates import expand_replicates

pytestmark = pytest.mark.unit

MONOREPO_ROOT = Path(__file__).resolve().parents[2]
COOKIES = MONOREPO_ROOT / "rhylthyme-examples" / "programs" / "cookies_three_trays.json"

requires_examples = pytest.mark.skipif(
    not COOKIES.exists(), reason="rhylthyme-examples checkout not found"
)


def fixed(step_id, seconds, trigger, **extra):
    step = {
        "stepId": step_id,
        "name": step_id.upper(),
        "task": "prep",
        "duration": {"type": "fixed", "seconds": seconds},
        "startTrigger": trigger,
    }
    step.update(extra)
    return step


def program(*tracks):
    return {
        "schemaVersion": "0.3.0-alpha",
        "programId": "p",
        "name": "P",
        "tracks": list(tracks),
        "resourceConstraints": [{"task": "prep", "maxConcurrent": 9}],
    }


def track(track_id, *steps, **extra):
    t = {"trackId": track_id, "name": track_id.upper(), "steps": list(steps)}
    t.update(extra)
    return t


def steps_by_id(expanded):
    return {s["stepId"]: s for t in expanded["tracks"] for s in t["steps"]}


def track_of(expanded):
    return {s["stepId"]: t["trackId"] for t in expanded["tracks"] for s in t["steps"]}


def tracks_by_id(expanded):
    return {t["trackId"]: t for t in expanded["tracks"]}


def no_instances_left(expanded):
    return '"instances"' not in json.dumps(expanded)


# --------------------------------------------------------------------------
# "each": pairing per mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["parallel", "stagger", "serial"])
def test_each_pairs_instance_i_with_instance_i(mode):
    rep = {"count": 3, "mode": mode}
    if mode == "stagger":
        rep["delay"] = 60
    p = program(
        track(
            "t",
            fixed("x", 100, {"type": "programStart"}, replicates=rep),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        )
    )
    e = expand_replicates(p)
    by_id = steps_by_id(e)
    assert "s" not in by_id and "x" not in by_id
    for i in (1, 2, 3):
        s = by_id[f"s-r{i}"]
        assert s["startTrigger"] == {"type": "afterStep", "stepId": f"x-r{i}"}
        assert s["instanceOf"] == "s" and s["instanceIndex"] == i
        assert s["name"] == "S ({} of 3)".format(i)
        x = by_id[f"x-r{i}"]
        assert x["instanceOf"] == "x" and x["instanceIndex"] == i
    assert no_instances_left(e)


def test_each_parallel_places_successor_in_instance_sub_track():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        )
    )
    e = expand_replicates(p)
    where = track_of(e)
    assert where["x-r1"] == where["s-r1"] == "t--x-r1"
    assert where["x-r2"] == where["s-r2"] == "t--x-r2"
    subs = tracks_by_id(e)
    assert subs["t--x-r1"]["parentTrackId"] == "t"
    assert subs["t--x-r2"]["parentTrackId"] == "t"
    # within the sub-track, the successor follows its instance
    assert [s["stepId"] for s in subs["t--x-r1"]["steps"]] == ["x-r1", "s-r1"]
    # the parent track is dropped because every step moved out of it
    assert "t" not in subs


def test_each_serial_keeps_chain_in_track_and_creates_per_instance_sub_tracks():
    p = program(
        track(
            "t",
            fixed("m", 10, {"type": "programStart"}),
            fixed(
                "x",
                100,
                {"type": "afterStep", "stepId": "m"},
                replicates={"count": 3, "mode": "serial"},
            ),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        )
    )
    e = expand_replicates(p)
    subs = tracks_by_id(e)
    # serial chain stays in the parent track, in order
    assert [s["stepId"] for s in subs["t"]["steps"]] == ["m", "x-r1", "x-r2", "x-r3"]
    assert subs["t"]["steps"][2]["startTrigger"] == {
        "type": "afterStep",
        "stepId": "x-r1",
    }
    # each-successors live in per-instance sub-tracks named after the root step
    assert [t["trackId"] for t in e["tracks"]] == ["t", "t--x-r1", "t--x-r2", "t--x-r3"]
    for i in (1, 2, 3):
        sub = subs[f"t--x-r{i}"]
        assert sub["parentTrackId"] == "t"
        assert sub["name"] == f"T - X ({i} of 3)"
        assert [s["stepId"] for s in sub["steps"]] == [f"s-r{i}"]
    assert no_instances_left(e)


def test_each_preserves_offset_buffer_and_event():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed(
                "a",
                5,
                {
                    "type": "afterStep",
                    "stepId": "x",
                    "instances": "each",
                    "offsetSeconds": "2m",
                    "event": "start",
                },
            ),
            fixed(
                "b",
                5,
                {
                    "type": "afterStepWithBuffer",
                    "stepId": "x",
                    "instances": "each",
                    "bufferSeconds": 30,
                },
            ),
        )
    )
    e = expand_replicates(p)
    by_id = steps_by_id(e)
    assert by_id["a-r2"]["startTrigger"] == {
        "type": "afterStep",
        "stepId": "x-r2",
        "offsetSeconds": "2m",
        "event": "start",
    }
    assert by_id["b-r1"]["startTrigger"] == {
        "type": "afterStepWithBuffer",
        "stepId": "x-r1",
        "bufferSeconds": 30,
    }


def test_each_is_transitive_and_downstream_plain_reference_joins_all():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
            fixed("u", 20, {"type": "afterStep", "stepId": "s", "instances": "each"}),
            fixed("z", 20, {"type": "afterStep", "stepId": "u"}),
        )
    )
    e = expand_replicates(p)
    by_id = steps_by_id(e)
    where = track_of(e)
    for i in (1, 2):
        assert by_id[f"u-r{i}"]["startTrigger"] == {
            "type": "afterStep",
            "stepId": f"s-r{i}",
        }
        assert by_id[f"u-r{i}"]["instanceOf"] == "u"
        assert where[f"u-r{i}"] == f"t--x-r{i}"
    # a plain reference to an "each" step gets the default barrier (all)
    assert by_id["z"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "u-r1"},
            {"type": "afterStep", "stepId": "u-r2"},
        ],
    }
    assert where["z"] == "t"


def test_each_across_tracks_places_successor_in_upstream_instance_sub_track():
    p = program(
        track(
            "b",
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        ),
        track(
            "a",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
        ),
    )
    e = expand_replicates(p)
    where = track_of(e)
    assert where["s-r1"] == "a--x-r1" and where["s-r2"] == "a--x-r2"
    assert [t["trackId"] for t in e["tracks"]] == ["a--x-r1", "a--x-r2"]


def test_compound_each_over_two_groups_with_equal_counts_pairs_i_to_i():
    p = program(
        track(
            "a",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
        ),
        track(
            "b",
            fixed(
                "y",
                80,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
        ),
        track(
            "c",
            fixed(
                "s",
                10,
                {
                    "logic": "all",
                    "triggers": [
                        {"type": "afterStep", "stepId": "x", "instances": "each"},
                        {"type": "afterStep", "stepId": "y", "instances": "each"},
                    ],
                },
            ),
        ),
    )
    e = expand_replicates(p)
    by_id = steps_by_id(e)
    assert by_id["s-r2"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "x-r2"},
            {"type": "afterStep", "stepId": "y-r2"},
        ],
    }
    # placed with the first "each" reference's instance
    assert track_of(e)["s-r2"] == "a--x-r2"


def test_compound_each_with_unequal_counts_raises():
    p = program(
        track(
            "a",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
        ),
        track(
            "b",
            fixed(
                "y",
                80,
                {"type": "programStart"},
                replicates={"count": 3, "mode": "parallel"},
            ),
        ),
        track(
            "c",
            fixed(
                "s",
                10,
                {
                    "logic": "all",
                    "triggers": [
                        {"type": "afterStep", "stepId": "x", "instances": "each"},
                        {"type": "afterStep", "stepId": "y", "instances": "each"},
                    ],
                },
            ),
        ),
    )
    with pytest.raises(ValueError, match="E_EACH_COUNT_MISMATCH"):
        expand_replicates(p)


def test_each_on_a_step_that_declares_replicates_raises():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed(
                "s",
                50,
                {"type": "afterStep", "stepId": "x", "instances": "each"},
                replicates={"count": 2},
            ),
        )
    )
    with pytest.raises(ValueError, match="E_EACH_WITH_REPLICATES"):
        expand_replicates(p)


# --------------------------------------------------------------------------
# "all" / "any"
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["parallel", "serial"])
def test_all_is_an_explicit_barrier_over_every_instance(mode):
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 3, "mode": mode},
            ),
            fixed(
                "z",
                20,
                {
                    "type": "afterStep",
                    "stepId": "x",
                    "instances": "all",
                    "offsetSeconds": 15,
                },
            ),
        )
    )
    e = expand_replicates(p)
    z = steps_by_id(e)["z"]
    assert z["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "x-r1", "offsetSeconds": 15},
            {"type": "afterStep", "stepId": "x-r2", "offsetSeconds": 15},
            {"type": "afterStep", "stepId": "x-r3", "offsetSeconds": 15},
        ],
    }
    assert "instanceOf" not in z
    assert no_instances_left(e)


def test_any_fires_on_the_first_instance():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed(
                "z",
                20,
                {
                    "type": "afterStepWithBuffer",
                    "stepId": "x",
                    "instances": "any",
                    "bufferSeconds": 5,
                },
            ),
        )
    )
    e = expand_replicates(p)
    assert steps_by_id(e)["z"]["startTrigger"] == {
        "logic": "any",
        "triggers": [
            {"type": "afterStepWithBuffer", "stepId": "x-r1", "bufferSeconds": 5},
            {"type": "afterStepWithBuffer", "stepId": "x-r2", "bufferSeconds": 5},
        ],
    }


def test_all_inside_a_compound_all_is_flattened():
    p = program(
        track("a", fixed("m", 10, {"type": "programStart"})),
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed(
                "z",
                20,
                {
                    "logic": "all",
                    "triggers": [
                        {"type": "afterStep", "stepId": "x", "instances": "all"},
                        {"type": "afterStep", "stepId": "m"},
                    ],
                },
            ),
        ),
    )
    e = expand_replicates(p)
    assert steps_by_id(e)["z"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "x-r1"},
            {"type": "afterStep", "stepId": "x-r2"},
            {"type": "afterStep", "stepId": "m"},
        ],
    }


def test_instances_on_an_unreplicated_step_is_dropped():
    # Phase 2 adds E_INSTANCES_ON_SINGLE to the validators; the expander
    # just strips the key so downstream 0.2.0 consumers never see it.
    p = program(
        track(
            "t",
            fixed("m", 10, {"type": "programStart"}),
            fixed("z", 20, {"type": "afterStep", "stepId": "m", "instances": "each"}),
        )
    )
    e = expand_replicates(p)
    assert steps_by_id(e)["z"]["startTrigger"] == {"type": "afterStep", "stepId": "m"}
    assert no_instances_left(e)


# --------------------------------------------------------------------------
# Compatibility and the worked example
# --------------------------------------------------------------------------


def test_expand_is_pure_and_0_2_0_programs_expand_as_before():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed("z", 20, {"type": "afterStep", "stepId": "x"}),
        )
    )
    p["schemaVersion"] = "0.2.0"
    before = copy.deepcopy(p)
    e = expand_replicates(p)
    assert p == before
    assert steps_by_id(e)["z"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "x-r1"},
            {"type": "afterStep", "stepId": "x-r2"},
        ],
    }
    # instance metadata is stamped on replicate copies regardless of schema version
    assert steps_by_id(e)["x-r2"]["instanceOf"] == "x"
    assert tracks_by_id(e)["t--x-r2"]["parentTrackId"] == "t"


@requires_examples
def test_cookie_example_expands_to_expected_shape_and_timings():
    from rhylthyme.validate_program import (
        calculate_step_start_time,
        parse_duration_to_seconds,
    )

    p = json.loads(COOKIES.read_text())
    e = expand_replicates(p)
    assert [t["trackId"] for t in e["tracks"]] == [
        "cookies",
        "cookies--bake-r1",
        "cookies--bake-r2",
        "cookies--bake-r3",
    ]
    by_id = steps_by_id(e)
    assert by_id["box"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "cool-r1"},
            {"type": "afterStep", "stepId": "cool-r2"},
            {"type": "afterStep", "stepId": "cool-r3"},
        ],
    }
    got = {}
    for t in e["tracks"]:
        for s in t["steps"]:
            start = calculate_step_start_time(s, t["steps"], e)
            got[s["stepId"]] = {
                "start": start,
                "end": start + parse_duration_to_seconds(s["duration"]),
            }
    assert got == p["metadata"]["expectedTimings"]


# --------------------------------------------------------------------------
# `replicates.maxInFlight` (schema 0.3.0-alpha). Mirrors the JS cases in
# rhylthyme-timeline/test/expand.test.js.
# --------------------------------------------------------------------------

EXAMPLES = MONOREPO_ROOT / "rhylthyme-examples" / "programs"


def gates_of(step):
    trigger = step.get("startTrigger") or {}
    atoms = (
        trigger["triggers"] if isinstance(trigger.get("triggers"), list) else [trigger]
    )
    return [
        a for a in atoms if isinstance(a, dict) and a.get("_synthetic") == "inFlight"
    ]


def timings_of(expanded):
    from rhylthyme.validate_program import (
        calculate_step_start_time,
        parse_duration_to_seconds,
    )

    out = {}
    for t in expanded["tracks"]:
        for s in t["steps"]:
            start = calculate_step_start_time(s, t["steps"], expanded)
            out[s["stepId"]] = {
                "start": start,
                "end": start + parse_duration_to_seconds(s["duration"]),
            }
    return out


def test_max_in_flight_on_a_serial_replicate_gates_through_the_each_leaf():
    e = expand_replicates(
        program(
            track(
                "t",
                fixed("m", 10, {"type": "programStart"}),
                fixed(
                    "x",
                    100,
                    {"type": "afterStep", "stepId": "m"},
                    replicates={"count": 4, "mode": "serial", "maxInFlight": 2},
                ),
                fixed(
                    "s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}
                ),
            )
        )
    )
    by_id = steps_by_id(e)
    # Instances 1..k keep their own trigger untouched: serial chaining only.
    assert by_id["x-r1"]["startTrigger"] == {"type": "afterStep", "stepId": "m"}
    assert by_id["x-r2"]["startTrigger"] == {"type": "afterStep", "stepId": "x-r1"}
    # i > k: own trigger AND the leaf of instance i-k, in one `all`.
    assert by_id["x-r3"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "afterStep", "stepId": "x-r2"},
            {
                "type": "afterStep",
                "stepId": "s-r1",
                "_synthetic": "inFlight",
                "inFlightOf": "x",
                "inFlightLimit": 2,
            },
        ],
    }
    assert [g["stepId"] for g in gates_of(by_id["x-r4"])] == ["s-r2"]
    assert no_instances_left(e)


def test_max_in_flight_on_a_parallel_replicate_is_a_rolling_window():
    p = program(
        track(
            "t",
            fixed(
                "x",
                600,
                {"type": "programStart"},
                replicates={"count": 4, "mode": "parallel", "maxInFlight": 2},
            ),
        )
    )
    e = expand_replicates(p)
    by_id = steps_by_id(e)
    assert by_id["x-r1"]["startTrigger"] == {"type": "programStart"}
    assert by_id["x-r2"]["startTrigger"] == {"type": "programStart"}
    assert [g["stepId"] for g in gates_of(by_id["x-r3"])] == ["x-r1"]
    assert [g["stepId"] for g in gates_of(by_id["x-r4"])] == ["x-r2"]
    # Exactly k start at t0; the next is admitted when the first leaf ends.
    times = timings_of(e)
    assert [times[f"x-r{i}"]["start"] for i in (1, 2, 3, 4)] == [0, 0, 600, 600]
    assert sum(1 for t in times.values() if t["start"] == 0) == 2


def test_max_in_flight_on_a_stagger_replicate_is_a_minimum_gap():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={
                    "count": 4,
                    "mode": "stagger",
                    "delay": 30,
                    "maxInFlight": 2,
                },
            ),
            fixed("s", 500, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        )
    )
    e = expand_replicates(p)
    # The staggered offset survives alongside the gate, both inside one `all`.
    assert steps_by_id(e)["x-r3"]["startTrigger"] == {
        "logic": "all",
        "triggers": [
            {"type": "programStart", "offsetSeconds": 60.0},
            {
                "type": "afterStep",
                "stepId": "s-r1",
                "_synthetic": "inFlight",
                "inFlightOf": "x",
                "inFlightLimit": 2,
            },
        ],
    }
    times = timings_of(e)
    assert [times["x-r1"]["start"], times["x-r2"]["start"]] == [0, 30]
    # The stagger would put x-r3 at 60; s-r1 ends at 600, so the gate wins.
    assert times["s-r1"]["end"] == 600
    assert times["x-r3"]["start"] == 600
    assert times["x-r4"]["start"] == 630


def test_multiple_leaf_chains_each_contribute_one_gate():
    e = expand_replicates(
        program(
            track(
                "t",
                fixed(
                    "x",
                    100,
                    {"type": "programStart"},
                    replicates={"count": 3, "mode": "parallel", "maxInFlight": 1},
                ),
                fixed(
                    "a", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}
                ),
                fixed(
                    "b", 70, {"type": "afterStep", "stepId": "x", "instances": "each"}
                ),
                fixed(
                    "c", 20, {"type": "afterStep", "stepId": "a", "instances": "each"}
                ),
            )
        )
    )
    by_id = steps_by_id(e)
    # `a` has an "each" child (`c`) so it is not a leaf; `c` and `b` are.
    assert [g["stepId"] for g in gates_of(by_id["x-r2"])] == ["b-r1", "c-r1"]
    assert [g["stepId"] for g in gates_of(by_id["x-r3"])] == ["b-r2", "c-r2"]
    for gate in gates_of(by_id["x-r2"]):
        assert (gate["inFlightOf"], gate["inFlightLimit"]) == ("x", 1)


@pytest.mark.parametrize("logic", ["all", "any"])
def test_a_gate_merges_into_all_and_nests_inside_any(logic):
    p = program(
        track(
            "a",
            fixed("m", 10, {"type": "programStart"}),
            fixed("n", 20, {"type": "programStart"}),
        ),
        track(
            "t",
            fixed(
                "x",
                100,
                {
                    "logic": logic,
                    "triggers": [
                        {"type": "afterStep", "stepId": "m"},
                        {"type": "afterStep", "stepId": "n"},
                    ],
                },
                replicates={"count": 2, "mode": "parallel", "maxInFlight": 1},
            ),
        ),
    )
    gate = {
        "type": "afterStep",
        "stepId": "x-r1",
        "_synthetic": "inFlight",
        "inFlightOf": "x",
        "inFlightLimit": 1,
    }
    got = steps_by_id(expand_replicates(p))["x-r2"]["startTrigger"]
    if logic == "all":
        assert got == {
            "logic": "all",
            "triggers": [
                {"type": "afterStep", "stepId": "m"},
                {"type": "afterStep", "stepId": "n"},
                gate,
            ],
        }
    else:
        assert got == {
            "logic": "all",
            "triggers": [
                {
                    "logic": "any",
                    "triggers": [
                        {"type": "afterStep", "stepId": "m"},
                        {"type": "afterStep", "stepId": "n"},
                    ],
                },
                gate,
            ],
        }


@pytest.mark.parametrize("limit", [3, 5])
def test_max_in_flight_at_or_above_count_is_a_no_op(limit):
    e = expand_replicates(
        program(
            track(
                "t",
                fixed(
                    "x",
                    100,
                    {"type": "programStart"},
                    replicates={"count": 3, "mode": "parallel", "maxInFlight": limit},
                ),
                fixed(
                    "s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}
                ),
            )
        )
    )
    by_id = steps_by_id(e)
    for i in (1, 2, 3):
        assert by_id[f"x-r{i}"]["startTrigger"] == {"type": "programStart"}


def test_a_tagged_gate_is_never_re_expanded():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 3, "mode": "parallel", "maxInFlight": 1},
            ),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        )
    )
    once = expand_replicates(p)
    # The expanded program carries no `replicates` and no `instances`, so a
    # second pass matches nothing and the gates are left exactly as they are.
    assert expand_replicates(copy.deepcopy(once)) == once
    assert json.dumps(once).count('"_synthetic": "inFlight"') == 2


@requires_examples
def test_cookie_example_resolves_to_the_prd_schedule_with_max_in_flight():
    p = json.loads(COOKIES.read_text())
    assert p["tracks"][0]["steps"][1]["replicates"]["maxInFlight"] == 2
    by_id = steps_by_id(expand_replicates(p))
    assert [g["stepId"] for g in gates_of(by_id["bake-r3"])] == ["cool-r1"]
    assert not gates_of(by_id["bake-r2"])
    assert timings_of(expand_replicates(p)) == p["metadata"]["expectedTimings"]


@requires_examples
@pytest.mark.parametrize("name", ["pcr_twelve_samples", "airport_landings_taxi_gate"])
def test_in_flight_examples_resolve_to_their_hand_computed_schedules(name):
    p = json.loads((EXAMPLES / f"{name}.json").read_text())
    got = timings_of(expand_replicates(p))
    for step_id, expected in p["metadata"]["expectedTimings"].items():
        assert got[step_id] == expected, step_id
