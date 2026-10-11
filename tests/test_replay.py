"""
Replay parity (PRD §8 item 1, plan Phase 2).

Feed a recorded run's OBSERVED ENDS back into the Python resolver and every
start it computes must be the instant the runtime recorded the trigger
firing (``triggerFiredAt``), within half a second — the tick with which the
fixture was recorded. The JavaScript half of the same check lives in
``rhylthyme-timeline/test/engine.test.js``; the last test here asserts the
two resolvers agree step for step.

Two trigger forms cannot be reproduced from the observed ends and are
skipped, exactly as the JS test skips them:

* ``manual`` — the executor picks the moment,
* a negative ``offsetSeconds`` — the live runtime fires it from the anchor's
  *projected* end, before the anchor has finished, while a hindsight replay
  resolves it against the end that actually happened.

``event: "start"`` was a third until Phase 7 taught the runner to anchor on
the referenced step's start, as the timing engine does.
``history.replay.executor_gated`` is the single place that names them.
"""

import json
import os
import shutil
import subprocess

import pytest

from rhylthyme.history import validate_run
from rhylthyme.history.replay import (
    actual_ends,
    actual_intervals,
    executor_gated,
    record_step_id,
    replay_timings,
)

HERE = os.path.dirname(os.path.abspath(__file__))
TIMELINE = os.path.normpath(os.path.join(HERE, "..", "..", "rhylthyme-timeline"))
PROGRAM_FILE = os.path.join(
    TIMELINE, "test", "fixtures", "programs", "thanksgiving_one_oven.json"
)
RUN_FILE = os.path.join(
    TIMELINE, "test", "fixtures", "runs", "thanksgiving_one_oven.run.json"
)
RENDERER = os.path.join(TIMELINE, "src", "index.js")

TOLERANCE = 0.5
EXPECTED_SKIPS = {
    "guests-seated": "manual gate",
    "potatoes-peel": "negative offset",
}


def _load(path):
    if not os.path.isfile(path):
        pytest.skip(
            "rhylthyme-timeline fixtures not checked out beside rhylthyme-cli-runner"
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def program():
    return _load(PROGRAM_FILE)


@pytest.fixture
def record():
    return _load(RUN_FILE)


@pytest.fixture
def expanded_steps(program):
    from rhylthyme.expand_replicates import expand_replicates

    return {
        step["stepId"]: step
        for track in expand_replicates(program).get("tracks", [])
        for step in track.get("steps", [])
    }


@pytest.mark.unit
class TestRecordFixture:
    """The fixture the parity checks run on, and what it is meant to contain."""

    def test_fixture_is_a_valid_completed_run(self, record):
        assert validate_run(record) == []
        assert record["programId"] == "thanksgiving-one-oven"
        assert record["outcome"] == "completed"
        assert record["runtime"] == {
            "kind": "cli",
            "version": record["runtime"]["version"],
            "clockMode": "wall",
            "speed": 1.0,
        }
        assert len(record["steps"]) == 13
        assert all(s["pausedSeconds"] == 0 for s in record["steps"])
        assert all("actual" in s and "end" in s["actual"] for s in record["steps"])

    def test_fixture_covers_early_on_time_and_late(self, record):
        signed = {
            s["stepId"]: round(s["actual"]["end"] - s["planned"]["end"])
            for s in record["steps"]
        }
        assert signed["turkey-prep"] == -200, "the cook finished the prep early"
        assert signed["stuffing-prep"] == 0, "untouched by the roast, exactly on time"
        assert signed["turkey-roast"] == 1300, "the indefinite roast ran over"
        assert signed["serve"] == 1902, "the slip reaches the table"
        assert sum(1 for v in signed.values() if v > 30) == 8
        # The negative-offset chain fires from the roast's PROJECTED end, so
        # it tracks the roast's early start instead of the roast's overrun.
        assert signed["potatoes-peel"] == -200
        assert sum(1 for v in signed.values() if v < -30) == 4

    def test_hashes_the_program_it_ran(self, record, program):
        from rhylthyme.history import program_version

        assert record["programVersion"] == program_version(program)


@pytest.mark.unit
class TestActuals:
    def test_actual_intervals_and_ends_key_by_expanded_step_id(self, record):
        intervals = actual_intervals(record)
        assert len(intervals) == 13
        assert intervals["turkey-prep"] == {"start": 0.0, "end": 1000.0}
        ends = actual_ends(record)
        assert ends["turkey-roast"] == 12400.25
        assert set(ends) == set(intervals)

    def test_record_step_id_rebuilds_the_replicate_suffix(self):
        assert record_step_id({"stepId": "bake", "instance": 3}) == "bake-r3"
        assert record_step_id({"stepId": "mix", "instance": 1}) == "mix"
        assert record_step_id({"stepId": "mix"}) == "mix"
        # An authored id that happens to end in -r1 survives when it is known.
        assert record_step_id({"stepId": "x-r1", "instance": 1}, {"x-r1"}) == "x-r1"
        assert record_step_id({"stepId": "x", "instance": 1}, {"x-r1"}) == "x-r1"


@pytest.mark.unit
class TestExecutorGated:
    def test_names_the_unpredictable_trigger_forms(self, expanded_steps):
        gated = {
            step_id: reason
            for step_id, step in expanded_steps.items()
            if (reason := executor_gated(step))
        }
        assert gated == EXPECTED_SKIPS

    def test_plain_dependencies_are_not_gated(self, expanded_steps):
        for step_id in ("turkey-prep", "turkey-roast", "gravy-make", "serve"):
            assert executor_gated(expanded_steps[step_id]) is None

    def test_event_start_is_no_longer_gated(self, expanded_steps):
        """Phase 7: the runner anchors on the referenced step's start too."""
        assert executor_gated(expanded_steps["salad"]) is None

    def test_detects_a_gate_inside_a_compound_trigger(self):
        step = {
            "stepId": "s",
            "startTrigger": {
                "logic": "all",
                "triggers": [
                    {"type": "afterStep", "stepId": "a"},
                    {"type": "afterStep", "stepId": "b", "offsetSeconds": "-20m"},
                ],
            },
        }
        assert executor_gated(step) == "negative offset"


@pytest.mark.unit
class TestReplayParity:
    def test_observed_ends_reproduce_every_recorded_trigger_firing(
        self, program, record, expanded_steps
    ):
        replayed = replay_timings(program, record)
        compared, mismatches, skipped = 0, [], {}
        for entry in record["steps"]:
            step_id = record_step_id(entry, set(expanded_steps))
            reason = executor_gated(expanded_steps[step_id])
            if reason:
                skipped[step_id] = reason
                continue
            fired = entry.get("triggerFiredAt", entry["actual"]["start"])
            compared += 1
            if abs(replayed[step_id]["start"] - fired) > TOLERANCE:
                mismatches.append(
                    f"{step_id}: replay {replayed[step_id]['start']} vs recorded {fired}"
                )
        assert mismatches == []
        assert skipped == EXPECTED_SKIPS
        assert compared == 11

    def test_replay_uses_the_observed_end_not_the_planned_duration(
        self, program, record
    ):
        replayed = replay_timings(program, record)
        for entry in record["steps"]:
            assert replayed[entry["stepId"]]["end"] == entry["actual"]["end"]
        # The roast ran 1500 s past its defaultSeconds, so the replay ends late.
        assert replayed["turkey-roast"]["duration"] == pytest.approx(11400, abs=0.5)
        assert all(t["resolved"] for t in replayed.values())

    def test_without_actuals_replay_reproduces_the_plan(self, program, record):
        planned = replay_timings(program, {"steps": []})
        for entry in record["steps"]:
            assert planned[entry["stepId"]]["start"] == entry["planned"]["start"]
            assert planned[entry["stepId"]]["end"] == entry["planned"]["end"]

    def test_explicit_ends_override_the_record(self, program, record):
        replayed = replay_timings(program, record, ends={"turkey-prep": 600.0})
        assert replayed["turkey-prep"]["end"] == 600.0
        assert replayed["turkey-roast"]["start"] == 600.0

    def test_cycles_and_dangling_references_are_unresolved(self):
        program = {
            "programId": "cyc",
            "name": "Cyc",
            "tracks": [
                {
                    "trackId": "t",
                    "steps": [
                        {
                            "stepId": "x",
                            "duration": {"type": "fixed", "seconds": 10},
                            "startTrigger": {"type": "afterStep", "stepId": "y"},
                        },
                        {
                            "stepId": "y",
                            "duration": {"type": "fixed", "seconds": 10},
                            "startTrigger": {"type": "afterStep", "stepId": "x"},
                        },
                        {
                            "stepId": "z",
                            "duration": {"type": "fixed", "seconds": 10},
                            "startTrigger": {"type": "afterStep", "stepId": "nope"},
                        },
                    ],
                }
            ],
        }
        replayed = replay_timings(program, {"steps": []})
        assert not any(replayed[s]["resolved"] for s in ("x", "y", "z"))
        assert replayed["x"]["start"] == 0.0


@pytest.mark.unit
def test_python_and_javascript_resolvers_agree_on_the_replay(program, record):
    """The two resolvers of PRD §8 item 1, compared step for step."""
    node = shutil.which("node")
    if node is None or not os.path.isfile(RENDERER):
        pytest.skip("node or the JavaScript timeline engine is not available")
    script = (
        "const R = require(process.argv[1]);"
        "const fs = require('fs');"
        "const program = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));"
        "const record = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));"
        "const ends = R.actualFromRun(record, program, { endsOnly: true });"
        "process.stdout.write(JSON.stringify(R.computeStepTimings(program, { actual: ends })));"
    )
    result = subprocess.run(
        [node, "-e", script, RENDERER, PROGRAM_FILE, RUN_FILE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    js = json.loads(result.stdout)
    py = replay_timings(program, record)
    assert set(js) == set(py)
    disagreements = [
        f"{step_id}: js {js[step_id]['start']}-{js[step_id]['end']} "
        f"py {py[step_id]['start']}-{py[step_id]['end']}"
        for step_id in py
        if abs(js[step_id]["start"] - py[step_id]["start"]) > TOLERANCE
        or abs(js[step_id]["end"] - py[step_id]["end"]) > TOLERANCE
    ]
    assert disagreements == []
