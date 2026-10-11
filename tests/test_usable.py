"""
Tests for the shared usable-run filter (execution history, Phase 4).

The fixture ``tests/fixtures/history/usable-cases.json`` is the contract
between this implementation and the JavaScript one in
``rhylthyme-server/mcp-api/history.js`` (asserted there by
``mcp-api/history.test.js``, which reads the same file). Any change to the
filter's semantics has to change the fixture, which makes the drift visible in
both test suites at once.
"""

import json
import os

import pytest

from rhylthyme.history import (
    is_usable_run,
    measured_steps,
    planned_duration,
    step_duration,
    step_reason,
    synthesize_runs,
    usable_steps,
    validate_run,
)

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "history", "usable-cases.json"
)

# The Thanksgiving example lives in the server subproject (it is the program the
# MCP server serves as rhylthyme://examples/thanksgiving_one_oven); tests that
# want a real program with declared variance factors skip without it.
THANKSGIVING = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "rhylthyme-server",
    "static",
    "examples",
    "thanksgiving_one_oven.json",
)


@pytest.fixture
def thanksgiving_program():
    if not os.path.isfile(THANKSGIVING):
        pytest.skip("Thanksgiving example not available in this checkout")
    with open(THANKSGIVING, encoding="utf-8") as fh:
        return json.load(fh)


def load_cases():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


CASES = load_cases()


def _ids(case):
    return [c["name"] for c in case]


# ------------------------------------------------------------------- parity


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_run_verdict_matches_fixture(case):
    usable, reason = is_usable_run(case["record"])
    assert usable is case["expectUsable"]
    assert reason == case["expectReason"]


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
@pytest.mark.parametrize("include_fixed", [False, True], ids=["strict", "with-fixed"])
def test_step_reasons_match_fixture(case, include_fixed):
    key = "stepReasonsIncludingFixed" if include_fixed else "stepReasons"
    got = {
        step["stepId"]: reason
        for step, reason in usable_steps(case["record"], include_fixed=include_fixed)
    }
    assert got == case[key]

    expected_ids = (
        case["usableStepIdsIncludingFixed"] if include_fixed else case["usableStepIds"]
    )
    assert [
        s["stepId"] for s in measured_steps(case["record"], include_fixed=include_fixed)
    ] == expected_ids


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_usable_steps_covers_every_step_in_order(case):
    entries = usable_steps(case["record"])
    assert [s["stepId"] for s, _ in entries] == [
        s["stepId"] for s in case["record"]["steps"]
    ]


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_fixture_records_validate_when_they_claim_to(case):
    errors = validate_run(case["record"])
    if case["schemaValid"]:
        assert errors == []
    else:
        assert errors != [], "case is marked schemaValid: false but validates"


@pytest.mark.unit
def test_fixture_covers_every_reason():
    """The corpus must exercise every reason code, or parity is untested."""
    seen = set()
    for case in CASES:
        seen.update(case["stepReasons"].values())
        seen.update(case["stepReasonsIncludingFixed"].values())
        seen.add(case["expectReason"])
    assert {
        None,
        "outcome-not-completed",
        "clock-not-wall",
        "speed-not-1",
        "no-actual",
        "not-ended",
        "paused",
        "fixed-duration",
        "ended-by-timer",
        "ended-by-abort",
        "ended-by-none",
    } <= seen


# ------------------------------------------------------------- unit checks


@pytest.mark.unit
class TestRunFilter:
    def base(self, **overrides):
        record = {
            "runId": "2026-02-01T10:00:00Z-0001",
            "programId": "p",
            "programVersion": "sha256:" + "ab" * 32,
            "runtime": {
                "kind": "cli",
                "version": "0.1.0a0",
                "clockMode": "wall",
                "speed": 1,
            },
            "startedAt": "2026-02-01T10:00:00.000Z",
            "outcome": "completed",
            "steps": [],
        }
        record.update(overrides)
        return record

    def test_conditions_are_checked_in_a_fixed_order(self):
        assert is_usable_run(self.base()) == (True, None)
        assert is_usable_run(self.base(outcome="aborted")) == (
            False,
            "outcome-not-completed",
        )
        # Outcome first: a scaled, simulated, abandoned run reports the outcome.
        bad = self.base(
            outcome="abandoned",
            runtime={
                "kind": "web",
                "version": "1",
                "clockMode": "simulated",
                "speed": 4,
            },
        )
        assert is_usable_run(bad)[1] == "outcome-not-completed"
        # Then the clock, then the speed.
        assert (
            is_usable_run(
                self.base(
                    runtime={
                        "kind": "web",
                        "version": "1",
                        "clockMode": "simulated",
                        "speed": 4,
                    }
                )
            )[1]
            == "clock-not-wall"
        )
        assert (
            is_usable_run(
                self.base(
                    runtime={
                        "kind": "cli",
                        "version": "1",
                        "clockMode": "wall",
                        "speed": 4,
                    }
                )
            )[1]
            == "speed-not-1"
        )

    def test_absent_speed_is_real_time(self):
        record = self.base(runtime={"kind": "cli", "version": "1", "clockMode": "wall"})
        assert is_usable_run(record) == (True, None)

    def test_speed_1_0_and_1_are_the_same_speed(self):
        for value in (1, 1.0, "1"):
            record = self.base(
                runtime={
                    "kind": "cli",
                    "version": "1",
                    "clockMode": "wall",
                    "speed": value,
                }
            )
            assert is_usable_run(record)[0] is True, value

    def test_junk_is_not_usable(self):
        assert is_usable_run(None)[0] is False
        assert is_usable_run({})[1] == "outcome-not-completed"
        assert usable_steps({}) == []


@pytest.mark.unit
class TestStepFilter:
    def step(self, kind, **overrides):
        step = {
            "stepId": "s",
            "planned": {"start": 0, "end": 600, "durationType": kind},
            "actual": {"start": 0, "end": 630},
            "endedBy": "executor",
            "pausedSeconds": 0,
        }
        step.update(overrides)
        return step

    def test_fixed_steps_are_never_measured_by_default(self):
        assert step_reason(self.step("fixed", endedBy="timer")) == "fixed-duration"
        assert step_reason(self.step("fixed", endedBy="executor")) == "fixed-duration"
        assert (
            step_reason(self.step("fixed", endedBy="timer"), include_fixed=True) is None
        )
        assert (
            step_reason(self.step("fixed", endedBy="executor"), include_fixed=True)
            is None
        )
        assert (
            step_reason(self.step("fixed", endedBy="abort"), include_fixed=True)
            == "ended-by-abort"
        )

    def test_non_fixed_steps_need_an_executor_ending(self):
        assert step_reason(self.step("variable")) is None
        assert step_reason(self.step("indefinite")) is None
        assert step_reason(self.step("variable", endedBy="timer")) == "ended-by-timer"
        assert (
            step_reason(self.step("variable", endedBy="trigger")) == "ended-by-trigger"
        )

    def test_a_pause_disqualifies_a_step(self):
        assert step_reason(self.step("variable", pausedSeconds=0.5)) == "paused"
        # include_fixed does not relax the pause rule
        assert (
            step_reason(self.step("fixed", endedBy="timer", pausedSeconds=1), True)
            == "paused"
        )

    def test_unfinished_steps(self):
        assert step_reason(self.step("variable", actual={"start": 10})) == "not-ended"
        step = self.step("variable")
        del step["actual"]
        assert step_reason(step) == "no-actual"

    def test_durations(self):
        step = self.step("variable")
        assert step_duration(step) == 630
        assert planned_duration(step) == 600
        assert step_duration(self.step("variable", actual={"start": 5})) is None


# --------------------------------------------------- against real records


@pytest.mark.unit
def test_synthetic_runs_are_usable(thanksgiving_program):
    runs = synthesize_runs(thanksgiving_program, 3, seed=1)
    for record in runs:
        assert validate_run(record) == []
        assert is_usable_run(record) == (True, None)
        measured = measured_steps(record)
        # Only the non-fixed steps of the Thanksgiving example are measurements.
        assert {s["stepId"] for s in measured} == {"turkey-roast", "potatoes-boil"}
        # With fixed steps included, every step of a clean run is usable.
        assert len(measured_steps(record, include_fixed=True)) == len(record["steps"])
