"""
Tests for calibration (execution history, Phase 5).

Calibration turns recorded runs into a duration *proposal*; the durable rule is
that it never writes. These tests are built on synthetic corpora whose noise is
known, so every proposed number is checkable: a step generated around its
planned duration must come back with that median, a step with four measurements
must come back skipped, and a range must never come back narrower than the one
the author wrote — that last one as a randomised property test over 200 cases.
"""

import copy
import json
import random

import jsonschema
import pytest
from click.testing import CliRunner

from rhylthyme.cli.history.calibrate_cli import (
    describe_values,
    effect_line,
    parse_accept,
    render_proposal,
)
from rhylthyme.cli.main import cli
from rhylthyme.history import synthesize_runs
from rhylthyme.history.calibrate import (
    FIXED_DURATION,
    INSUFFICIENT_RUNS,
    NO_MEASUREMENTS,
    NOT_IN_PROGRAM,
    NOTE,
    NOTE_CONSIDER_VARIABLE,
    PROPOSED,
    SKIPPED,
    apply_calibration,
    calibration_effect,
    propose_calibration,
)
from rhylthyme.history.report import EXECUTOR_CONTROLLED, PREDICTABLE

LOW_CV = 0.04
HIGH_CV = 0.40


def _step(step_id, trigger, duration, task="work"):
    return {
        "stepId": step_id,
        "name": step_id.title(),
        "startTrigger": trigger,
        "duration": duration,
        "task": task,
    }


@pytest.fixture
def program():
    """
    A fixed step, a *narrow* variable step, and a wild indefinite step.

    The variable step's authored range (580-620) is narrower than the observed
    spread, so P10/P90 widen it; that is the interesting direction. The fixed
    step is where the "consider variable" note can appear.
    """
    return {
        "programId": "calibrate-test",
        "name": "Calibrate Test",
        "actors": 2,
        "tracks": [
            {
                "trackId": "a",
                "name": "A",
                "steps": [
                    _step(
                        "warmup",
                        {"type": "programStart"},
                        {"type": "fixed", "seconds": 600},
                        task="prep",
                    ),
                    _step(
                        "steady",
                        {"type": "afterStep", "stepId": "warmup"},
                        {
                            "type": "variable",
                            "minSeconds": 580,
                            "maxSeconds": 620,
                            "defaultSeconds": 600,
                        },
                    ),
                    _step(
                        "wild",
                        {"type": "afterStep", "stepId": "steady"},
                        {"type": "indefinite", "defaultSeconds": 900},
                    ),
                ],
            },
            {
                "trackId": "b",
                "name": "B",
                "steps": [
                    _step(
                        "tidy",
                        {"type": "programStart"},
                        {
                            "type": "variable",
                            "minSeconds": 60,
                            "maxSeconds": 6000,
                            "defaultSeconds": 300,
                        },
                        task="prep",
                    ),
                ],
            },
        ],
        "resourceConstraints": [
            {"task": "prep", "maxConcurrent": 2},
            {"task": "work", "maxConcurrent": 2},
        ],
    }


NOISE = {"steady": LOW_CV, "wild": HIGH_CV, "tidy": LOW_CV}
AS_OF = "2026-09-14T12:00:00Z"


@pytest.fixture
def runs20(program):
    return synthesize_runs(program, 20, noise_by_step=NOISE, default_cv=0.01, seed=211)


@pytest.fixture
def proposal(program, runs20):
    return propose_calibration(program, runs20, as_of=AS_OF)


def _rows(proposal):
    return proposal.by_step()


def _schema(version):
    from rhylthyme_spec import get_program_schema_path

    with open(get_program_schema_path(version), "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------- proposals


@pytest.mark.unit
class TestProposal:
    def test_median_and_percentiles_per_non_fixed_step(self, proposal, runs20):
        rows = _rows(proposal)
        assert proposal.runsUsable == 20 and proposal.runsConsidered == 20
        for step_id, planned in (("steady", 600), ("wild", 900), ("tidy", 300)):
            row = rows[step_id]
            assert row.status == PROPOSED, step_id
            assert row.n == 20
            evidence = row.evidence
            # The evidence a UI shows: n, median, the percentile pair, IQR.
            assert evidence["median"] == pytest.approx(planned, rel=0.25)
            assert evidence["p10"] < evidence["median"] < evidence["p90"]
            assert evidence["iqr"] > 0
            assert row.proposed["defaultSeconds"] == round(evidence["median"])
            assert row.delta_seconds == pytest.approx(
                row.proposed["defaultSeconds"] - planned
            )

    def test_variable_range_comes_from_p10_p90(self, proposal):
        row = _rows(proposal)["steady"]
        assert row.durationType == "variable"
        # The author's 580-620 is narrower than the observed spread, so the
        # proposal is the observed percentiles (floored/ceiled).
        assert row.proposed["minSeconds"] <= row.evidence["p10"]
        assert row.proposed["maxSeconds"] >= row.evidence["p90"]
        assert row.proposed["minSeconds"] < 580 or row.proposed["maxSeconds"] > 620

    def test_a_wider_authored_range_is_kept(self, proposal):
        """`tidy` is authored 60-6000; the observation must not narrow it."""
        row = _rows(proposal)["tidy"]
        assert row.proposed["minSeconds"] <= 60
        assert row.proposed["maxSeconds"] >= 6000

    def test_indefinite_gets_only_a_default(self, proposal):
        row = _rows(proposal)["wild"]
        assert row.durationType == "indefinite"
        assert set(row.proposed) == {"type", "defaultSeconds"}

    def test_indefinite_range_on_request(self, program, runs20):
        row = _rows(
            propose_calibration(program, runs20, range_for_indefinite=True, as_of=AS_OF)
        )["wild"]
        assert row.proposed["minSeconds"] <= row.proposed["defaultSeconds"]
        assert row.proposed["maxSeconds"] >= row.proposed["defaultSeconds"]

    def test_no_cv_gate_but_the_verdict_travels(self, proposal):
        """
        A high-CV step is still proposed — its median beats the author's
        invention — but the report's verdict rides along so a UI can warn.
        """
        rows = _rows(proposal)
        assert rows["wild"].evidence["cv"] > 0.25
        assert rows["wild"].status == PROPOSED
        assert rows["wild"].verdict == EXECUTOR_CONTROLLED
        assert rows["wild"].evidence["verdict"] == EXECUTOR_CONTROLLED
        assert rows["steady"].verdict == PREDICTABLE

    def test_four_runs_are_skipped_with_a_reason(self, program):
        runs = synthesize_runs(program, 4, noise_by_step=NOISE, seed=212)
        rows = _rows(propose_calibration(program, runs, as_of=AS_OF))
        for step_id in ("steady", "wild", "tidy"):
            assert rows[step_id].status == SKIPPED
            assert rows[step_id].reason == INSUFFICIENT_RUNS
            assert rows[step_id].n == 4
            assert rows[step_id].evidence["minRuns"] == 5
            assert not rows[step_id].proposed
        # ...and five are enough.
        runs = synthesize_runs(program, 5, noise_by_step=NOISE, seed=212)
        assert _rows(propose_calibration(program, runs))["steady"].status == PROPOSED

    def test_min_runs_is_a_parameter(self, program, runs20):
        rows = _rows(propose_calibration(program, runs20, k=50, as_of=AS_OF))
        assert all(rows[s].status == SKIPPED for s in ("steady", "wild", "tidy"))

    def test_percentiles_are_parameters(self, program, runs20):
        wide = _rows(
            propose_calibration(program, runs20, low_pct=1, high_pct=99, as_of=AS_OF)
        )["steady"]
        narrow = _rows(
            propose_calibration(program, runs20, low_pct=25, high_pct=75, as_of=AS_OF)
        )["steady"]
        assert wide.proposed["minSeconds"] <= narrow.proposed["minSeconds"]
        assert wide.proposed["maxSeconds"] >= narrow.proposed["maxSeconds"]

    def test_a_step_with_no_history_is_skipped(self, program, runs20):
        program["tracks"][1]["steps"].append(
            _step(
                "newcomer",
                {"type": "afterStep", "stepId": "tidy"},
                {"type": "variable", "minSeconds": 10, "maxSeconds": 20},
                task="prep",
            )
        )
        row = _rows(propose_calibration(program, runs20, as_of=AS_OF))["newcomer"]
        assert (row.status, row.reason) == (SKIPPED, NO_MEASUREMENTS)

    def test_a_step_only_in_history_is_reported_never_proposed(self, program, runs20):
        program["tracks"][0]["steps"] = [
            s for s in program["tracks"][0]["steps"] if s["stepId"] != "wild"
        ]
        row = _rows(propose_calibration(program, runs20, as_of=AS_OF))["wild"]
        assert (row.status, row.reason) == (SKIPPED, NOT_IN_PROGRAM)
        assert row.n == 20

    def test_provenance_records_the_hash_the_runs_were_measured_against(
        self, program, runs20
    ):
        from rhylthyme.history import program_version

        before = program_version(program)
        proposal = propose_calibration(program, runs20, as_of=AS_OF)
        assert proposal.runsProgramVersion == before
        assert proposal.programVersion == before
        # Editing the program moves programVersion but not the runs' version.
        edited = copy.deepcopy(program)
        edited["tracks"][0]["steps"][0]["duration"]["seconds"] = 601
        moved = propose_calibration(edited, runs20, as_of=AS_OF)
        assert moved.programVersion != before
        assert moved.runsProgramVersion == before

    def test_unusable_runs_are_excluded_with_a_reason(self, program, runs20):
        runs20[0]["outcome"] = "abandoned"
        runs20[1]["runtime"]["speed"] = 10
        proposal = propose_calibration(program, runs20, as_of=AS_OF)
        assert (proposal.runsConsidered, proposal.runsUsable) == (20, 18)
        assert [r["reason"] for r in proposal.runsExcluded] == [
            "outcome-not-completed",
            "speed-not-1",
        ]
        assert _rows(proposal)["steady"].n == 18

    def test_since_filters_the_corpus(self, program, runs20):
        proposal = propose_calibration(program, runs20, since="2026-01-15", as_of=AS_OF)
        assert proposal.runsConsidered == 10
        assert proposal.since == "2026-01-15"
        assert _rows(proposal)["steady"].n == 10


# ------------------------------------------------------ never-narrow property


@pytest.mark.unit
class TestNeverNarrow:
    """
    The one hard guarantee: a proposed range contains the author's range.

    Randomised over 200 cases with a fixed seed (hypothesis is not a dependency
    of this project), covering ranges much narrower and much wider than the
    observations, fractional bounds, and unbounded authored ranges.
    """

    @staticmethod
    def _record(index, *, observed, planned, current):
        planned_block = {
            "start": 0,
            "end": planned,
            "durationType": current["type"],
        }
        for key in ("minSeconds", "maxSeconds", "defaultSeconds"):
            if current.get(key) is not None:
                planned_block[key] = current[key]
        return {
            "schemaVersion": "0.1.0-alpha",
            "runId": f"2026-01-01T00:00:0{index % 10}Z-{index:04x}",
            "programId": "narrow-test",
            "programVersion": "sha256:" + "0" * 64,
            "runtime": {
                "kind": "cli",
                "version": "test",
                "clockMode": "wall",
                "speed": 1,
            },
            "startedAt": "2026-01-01T00:00:00Z",
            "outcome": "completed",
            "steps": [
                {
                    "stepId": "only",
                    "instance": 1,
                    "planned": planned_block,
                    "actual": {"start": 0, "end": observed},
                    "endedBy": "executor",
                    "triggerFiredAt": 0,
                    "pausedSeconds": 0,
                }
            ],
        }

    def test_proposed_range_is_never_narrower(self):
        rng = random.Random(20260914)
        for case in range(200):
            kind = "variable" if case % 4 else "indefinite"
            centre = rng.uniform(5, 20000)
            spread = rng.uniform(0.0, 1.5)
            observed = [
                max(0.1, centre * (1 + rng.gauss(0, spread)))
                for _ in range(rng.randint(5, 25))
            ]
            current = {"type": kind}
            if kind == "variable":
                low = rng.uniform(0, centre * 2)
                high = low + rng.uniform(0, centre * 2)
                current["minSeconds"] = round(low, 3)
                current["maxSeconds"] = round(high, 3)
                if rng.random() < 0.7:
                    current["defaultSeconds"] = round(
                        rng.uniform(low, high) if high > low else low, 3
                    )
                if rng.random() < 0.3:
                    # An unbounded side: the author wrote only one end.
                    current.pop(rng.choice(["minSeconds", "maxSeconds"]))
            else:
                current["defaultSeconds"] = round(centre, 3)

            duration = {k: v for k, v in current.items()}
            program = {
                "programId": "narrow-test",
                "name": "N",
                "tracks": [
                    {
                        "trackId": "t",
                        "name": "T",
                        "steps": [
                            {
                                "stepId": "only",
                                "name": "Only",
                                "startTrigger": {"type": "programStart"},
                                "duration": duration,
                            }
                        ],
                    }
                ],
            }
            planned = (
                current.get("defaultSeconds")
                or current.get("maxSeconds")
                or current.get("minSeconds")
                or 60
            )
            records = [
                self._record(i, observed=value, planned=planned, current=current)
                for i, value in enumerate(observed)
            ]
            row = propose_calibration(
                program,
                records,
                range_for_indefinite=(kind == "indefinite"),
                with_effect=False,
                as_of=AS_OF,
            ).by_step()["only"]
            assert row.status == PROPOSED, (case, current)
            proposed = row.proposed
            if current.get("minSeconds") is not None:
                assert proposed["minSeconds"] <= current["minSeconds"], (case, current)
            if current.get("maxSeconds") is not None:
                assert proposed["maxSeconds"] >= current["maxSeconds"], (case, current)
            # The range always contains the proposed default.
            assert (
                proposed["minSeconds"]
                <= proposed["defaultSeconds"]
                <= proposed["maxSeconds"]
            ), (case, proposed)


# ------------------------------------------------------------- fixed steps


@pytest.mark.unit
class TestFixedSteps:
    def test_an_on_time_fixed_step_is_skipped_not_changed(self, proposal):
        row = _rows(proposal)["warmup"]
        assert (row.status, row.reason) == (SKIPPED, FIXED_DURATION)
        assert not row.proposed
        assert row.evidence["lagSeconds"] == pytest.approx(0, abs=30)

    def test_a_consistently_overrunning_fixed_step_gets_a_note(self, program):
        def law(step_id, planned, factors, rng):
            return planned + 300 if step_id == "warmup" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=213)
        row = _rows(propose_calibration(program, runs, as_of=AS_OF))["warmup"]
        assert row.status == NOTE
        assert row.note == NOTE_CONSIDER_VARIABLE
        assert row.evidence["lagSeconds"] == pytest.approx(300)
        # A note is not a value change: nothing to accept, nothing written.
        assert not row.proposed
        assert "warmup" not in [
            r.stepId
            for r in propose_calibration(program, runs, as_of=AS_OF).proposed_steps()
        ]

    def test_a_small_overrun_stays_below_the_threshold(self, program):
        def law(step_id, planned, factors, rng):
            return planned + 30 if step_id == "warmup" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=214)
        row = _rows(propose_calibration(program, runs, as_of=AS_OF))["warmup"]
        # 30 s on a 600 s step is under both 60 s and 10 %.
        assert row.evidence["lagThresholdSeconds"] == pytest.approx(60)
        assert (row.status, row.reason) == (SKIPPED, FIXED_DURATION)

    def test_the_lag_threshold_is_a_parameter(self, program):
        def law(step_id, planned, factors, rng):
            return planned + 30 if step_id == "warmup" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=215)
        row = _rows(
            propose_calibration(
                program, runs, lag_floor_seconds=5, lag_fraction=0.01, as_of=AS_OF
            )
        )["warmup"]
        assert row.status == NOTE

    def test_a_fixed_step_below_k_is_insufficient_not_noted(self, program):
        def law(step_id, planned, factors, rng):
            return planned + 300 if step_id == "warmup" else planned

        runs = synthesize_runs(program, 3, duration_fn=law, default_cv=0.0, seed=216)
        row = _rows(propose_calibration(program, runs, as_of=AS_OF))["warmup"]
        assert (row.status, row.reason) == (SKIPPED, INSUFFICIENT_RUNS)


# ------------------------------------------------------------- acceptance


@pytest.mark.unit
class TestApplyCalibration:
    def test_writes_values_and_calibrated_from(self, program, proposal):
        before = copy.deepcopy(program)
        out = apply_calibration(program, proposal, "all")
        assert program == before, "apply_calibration must not mutate its input"

        durations = {
            s["stepId"]: s["duration"] for t in out["tracks"] for s in t["steps"]
        }
        rows = _rows(proposal)
        for step_id in ("steady", "wild", "tidy"):
            duration = durations[step_id]
            assert (
                duration["defaultSeconds"] == rows[step_id].proposed["defaultSeconds"]
            )
            assert duration["calibratedFrom"] == {
                "runs": 20,
                "asOf": AS_OF,
                "programVersion": proposal.runsProgramVersion,
            }
        # Fixed steps are untouched, calibratedFrom included.
        assert durations["warmup"] == {"type": "fixed", "seconds": 600}

    def test_accepting_one_step_leaves_the_others_alone(self, program, proposal):
        out = apply_calibration(program, proposal, ["steady"])
        durations = {
            s["stepId"]: s["duration"] for t in out["tracks"] for s in t["steps"]
        }
        assert "calibratedFrom" in durations["steady"]
        assert "calibratedFrom" not in durations["wild"]
        assert durations["wild"]["defaultSeconds"] == 900

    def test_the_result_validates_under_both_schema_versions(self, program, proposal):
        out = apply_calibration(program, proposal, "all")
        for version in ("0.2.0-alpha", "0.3.0-alpha"):
            jsonschema.Draft7Validator(_schema(version)).validate(out)

    def test_the_result_passes_the_full_validator(self, program, proposal, tmp_path):
        from rhylthyme_spec import get_program_schema_path

        from rhylthyme.validate_program import validate_program_file

        out = apply_calibration(program, proposal, "all")
        path = tmp_path / "calibrated.json"
        path.write_text(json.dumps(out), encoding="utf-8")
        assert validate_program_file(
            str(path), schema_file=str(get_program_schema_path("0.3.0-alpha"))
        )

    def test_range_still_contains_the_default_after_writing(self, program, proposal):
        out = apply_calibration(program, proposal, "all")
        for track in out["tracks"]:
            for step in track["steps"]:
                duration = step["duration"]
                if duration.get("type") != "variable":
                    continue
                assert (
                    duration["minSeconds"]
                    <= duration["defaultSeconds"]
                    <= duration["maxSeconds"]
                )

    def test_accepting_an_unknown_step_is_refused(self, program, proposal):
        with pytest.raises(ValueError, match="no proposal for step"):
            apply_calibration(program, proposal, ["nope"])

    def test_accepting_a_skipped_step_is_refused(self, program, proposal):
        with pytest.raises(ValueError, match="no proposed value to accept"):
            apply_calibration(program, proposal, ["warmup"])

    def test_accept_must_be_all_or_a_list(self, program, proposal):
        with pytest.raises(ValueError, match="accept must be"):
            apply_calibration(program, proposal, "steady")

    def test_calibrating_twice_replaces_the_provenance(self, program, runs20):
        first = propose_calibration(program, runs20, as_of=AS_OF)
        once = apply_calibration(program, first, "all")
        second = propose_calibration(once, runs20, as_of="2026-10-01T00:00:00Z")
        twice = apply_calibration(once, second, "all")
        duration = twice["tracks"][0]["steps"][1]["duration"]
        assert duration["calibratedFrom"]["asOf"] == "2026-10-01T00:00:00Z"
        # A second pass over the same history is a no-op on the values.
        assert (
            duration["defaultSeconds"]
            == once["tracks"][0]["steps"][1]["duration"]["defaultSeconds"]
        )


# ------------------------------------------------------- effect if accepted


@pytest.mark.unit
class TestEffect:
    def test_makespan_moves_by_the_accepted_deltas(self, program):
        """`steady` runs 5 minutes long every time; the makespan follows it."""

        def law(step_id, planned, factors, rng):
            return planned + 300 if step_id == "steady" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=217)
        proposal = propose_calibration(program, runs, as_of=AS_OF)
        effect = proposal.effect
        assert effect["makespanBeforeSeconds"] == pytest.approx(600 + 600 + 900)
        assert effect["makespanAfterSeconds"] == pytest.approx(600 + 900 + 900)
        assert effect["makespanDeltaSeconds"] == pytest.approx(300)
        assert set(effect["acceptedSteps"]) == {"steady", "wild", "tidy"}
        # The downstream step is pushed, and says so.
        shifts = {s["stepId"]: s for s in effect["stepShifts"]}
        assert shifts["wild"]["startShiftSeconds"] == pytest.approx(300)
        assert shifts["steady"]["startShiftSeconds"] == pytest.approx(0)
        assert shifts["steady"]["endShiftSeconds"] == pytest.approx(300)

    def test_critical_path_before_and_after(self, program, proposal):
        effect = proposal.effect
        assert effect["criticalPathBefore"] == ["warmup", "steady", "wild"]
        assert effect["criticalPathAfter"] == ["warmup", "steady", "wild"]
        assert effect["criticalPathChanged"] is False

    def test_the_critical_path_can_change(self, program):
        """`tidy` overruns the whole A track and takes over the makespan."""

        def law(step_id, planned, factors, rng):
            return 99999 if step_id == "tidy" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=218)
        effect = propose_calibration(program, runs, as_of=AS_OF).effect
        assert effect["criticalPathBefore"] == ["warmup", "steady", "wild"]
        assert effect["criticalPathAfter"] == ["tidy"]
        assert effect["criticalPathChanged"] is True
        assert effect["makespanAfterSeconds"] == pytest.approx(99999)

    def test_effect_of_a_partial_acceptance(self, program, proposal):
        whole = proposal.effect["makespanAfterSeconds"]
        partial = calibration_effect(program, proposal, ["tidy"])
        assert partial["acceptedSteps"] == ["tidy"]
        assert partial["makespanAfterSeconds"] != whole
        empty = calibration_effect(program, proposal, [])
        assert empty["makespanDeltaSeconds"] == 0
        assert empty["stepShifts"] == []

    def test_effect_can_be_skipped(self, program, runs20):
        assert propose_calibration(program, runs20, with_effect=False).effect == {}


# --------------------------------------------------------------- serialising


@pytest.mark.unit
class TestToDict:
    def test_round_trips_through_json(self, proposal):
        payload = json.loads(json.dumps(proposal.to_dict()))
        assert payload["programId"] == "calibrate-test"
        assert payload["asOf"] == AS_OF
        assert payload["minRuns"] == 5
        assert payload["lowPercentile"] == 10 and payload["highPercentile"] == 90
        assert payload["runsUsable"] == 20
        rows = {s["stepId"]: s for s in payload["steps"]}
        assert rows["steady"]["status"] == PROPOSED
        assert rows["steady"]["evidence"]["proposed"]["type"] == "variable"
        assert rows["steady"]["evidence"]["current"]["minSeconds"] == 580
        assert rows["steady"]["verdict"] == PREDICTABLE
        assert rows["warmup"]["reason"] == FIXED_DURATION
        assert payload["effect"]["criticalPathBefore"] == ["warmup", "steady", "wild"]
        # Everything is a JSON primitive, so the MCP tool can return it as is.
        assert isinstance(payload["steps"], list)


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def workspace(tmp_path, program):
    """A program file and 20 recorded runs of it in a temporary runs dir."""
    path = tmp_path / "program.json"
    path.write_text(json.dumps(program, indent=2), encoding="utf-8")
    runs_dir = tmp_path / "runs"
    synthesize_runs(
        program,
        20,
        noise_by_step=NOISE,
        default_cv=0.01,
        seed=211,
        runs_dir=str(runs_dir),
    )
    return {"program": str(path), "runs_dir": str(runs_dir), "tmp": tmp_path}


@pytest.mark.cli
class TestCli:
    def _run(self, args):
        return CliRunner().invoke(cli, args)

    def test_table_renders(self, workspace):
        result = self._run(
            ["calibrate", workspace["program"], "--runs-dir", workspace["runs_dir"]]
        )
        assert result.exit_code == 0, result.output
        assert "Calibration proposal: calibrate-test" in result.output
        assert "Runs: 20 usable of 20" in result.output
        for header in ("Step", "Current", "Proposed", "Median", "IQR", "Delta", "Note"):
            assert header in result.output
        assert "Effect if accepted: makespan" in result.output
        assert "Nothing has been written" in result.output

    def test_markdown_and_json(self, workspace):
        md = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--format",
                "md",
            ]
        )
        assert md.exit_code == 0
        assert md.output.startswith("# Calibration proposal: calibrate-test")
        assert "| Step | Type | n |" in md.output

        js = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--format",
                "json",
            ]
        )
        assert js.exit_code == 0
        payload = json.loads(js.output)
        assert payload["programId"] == "calibrate-test"

    def test_out_writes_the_proposal_not_the_program(self, workspace):
        out = workspace["tmp"] / "proposal.json"
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--format",
                "json",
                "--out",
                str(out),
            ]
        )
        assert result.exit_code == 0
        assert json.loads(out.read_text())["programId"] == "calibrate-test"
        # The program itself is untouched.
        assert (
            "calibratedFrom"
            not in workspace["tmp"].joinpath("program.json").read_text()
        )

    def test_without_accept_nothing_is_written(self, workspace):
        original = open(workspace["program"], encoding="utf-8").read()
        before = sorted(p.name for p in workspace["tmp"].iterdir())
        result = self._run(
            ["calibrate", workspace["program"], "--runs-dir", workspace["runs_dir"]]
        )
        assert result.exit_code == 0
        assert open(workspace["program"], encoding="utf-8").read() == original
        assert sorted(p.name for p in workspace["tmp"].iterdir()) == before

    def test_accept_all_write_produces_a_calibrated_program(self, workspace):
        target = workspace["tmp"] / "calibrated.json"
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--accept",
                "all",
                "--write",
                str(target),
            ]
        )
        assert result.exit_code == 0, result.output
        assert "Calibrated 3 step(s)" in result.output
        written = json.loads(target.read_text())
        durations = {
            s["stepId"]: s["duration"] for t in written["tracks"] for s in t["steps"]
        }
        assert durations["steady"]["calibratedFrom"]["runs"] == 20
        assert "programVersion" in durations["steady"]["calibratedFrom"]
        assert "calibratedFrom" not in durations["warmup"]
        jsonschema.Draft7Validator(_schema("0.3.0-alpha")).validate(written)
        # The input program is untouched.
        assert (
            "calibratedFrom" not in open(workspace["program"], encoding="utf-8").read()
        )

    def test_accept_a_single_step(self, workspace):
        target = workspace["tmp"] / "one.json"
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--accept",
                "steady",
                "--write",
                str(target),
            ]
        )
        assert result.exit_code == 0, result.output
        durations = {
            s["stepId"]: s["duration"]
            for t in json.loads(target.read_text())["tracks"]
            for s in t["steps"]
        }
        assert "calibratedFrom" in durations["steady"]
        assert "calibratedFrom" not in durations["wild"]

    def test_write_refuses_to_overwrite_the_input(self, workspace):
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--accept",
                "all",
                "--write",
                workspace["program"],
            ]
        )
        assert result.exit_code != 0
        assert "would overwrite PROGRAM" in result.output
        assert (
            "calibratedFrom" not in open(workspace["program"], encoding="utf-8").read()
        )

    def test_in_place_writes_over_the_input(self, workspace):
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--accept",
                "all",
                "--in-place",
            ]
        )
        assert result.exit_code == 0, result.output
        assert "calibratedFrom" in open(workspace["program"], encoding="utf-8").read()

    def test_write_without_accept_is_refused(self, workspace):
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--write",
                str(workspace["tmp"] / "no.json"),
            ]
        )
        assert result.exit_code != 0
        assert "needs --accept" in result.output
        assert not (workspace["tmp"] / "no.json").exists()

    def test_accept_without_a_destination_is_refused(self, workspace):
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--accept",
                "all",
            ]
        )
        assert result.exit_code != 0
        assert "--write PATH or --in-place" in result.output

    def test_accepting_a_fixed_step_is_refused(self, workspace):
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--accept",
                "warmup",
                "--write",
                str(workspace["tmp"] / "no.json"),
            ]
        )
        assert result.exit_code != 0
        assert "no proposed value to accept" in result.output

    def test_runs_limit_and_min_runs(self, workspace):
        result = self._run(
            [
                "calibrate",
                workspace["program"],
                "--runs-dir",
                workspace["runs_dir"],
                "--runs",
                "4",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["runsConsidered"] == 4
        rows = {s["stepId"]: s for s in payload["steps"]}
        assert rows["steady"]["reason"] == INSUFFICIENT_RUNS

    def test_no_runs_exits_non_zero(self, workspace, tmp_path):
        result = self._run(
            ["calibrate", workspace["program"], "--runs-dir", str(tmp_path / "empty")]
        )
        assert result.exit_code == 1
        assert "No runs recorded" in result.output


# ------------------------------------------------------------ CLI rendering


@pytest.mark.unit
class TestRendering:
    def test_describe_values(self):
        assert describe_values({}) == "-"
        assert describe_values({"type": "fixed", "seconds": 600}) == "0:10:00"
        assert (
            describe_values({"type": "indefinite", "defaultSeconds": 900}) == "0:15:00"
        )
        assert (
            describe_values(
                {
                    "type": "variable",
                    "minSeconds": 60,
                    "defaultSeconds": 120,
                    "maxSeconds": 180,
                }
            )
            == "0:01:00–0:02:00–0:03:00"
        )

    def test_effect_line_reports_a_path_change(self, program):
        def law(step_id, planned, factors, rng):
            return 99999 if step_id == "tidy" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=219)
        line = effect_line(propose_calibration(program, runs, as_of=AS_OF))
        assert "critical path changes to tidy" in line

    def test_note_row_shows_the_lag(self, program):
        def law(step_id, planned, factors, rng):
            return planned + 300 if step_id == "warmup" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=220)
        text = render_proposal(propose_calibration(program, runs, as_of=AS_OF), "table")
        assert "consider variable (lag +0:05:00)" in text
        assert "notes on 1 fixed step(s)" in text

    def test_nothing_to_accept(self, program):
        runs = synthesize_runs(program, 2, noise_by_step=NOISE, seed=221)
        text = render_proposal(propose_calibration(program, runs, as_of=AS_OF), "table")
        assert "Nothing to accept." in text

    def test_parse_accept(self):
        assert parse_accept(()) == []
        assert parse_accept(("all",)) == "all"
        assert parse_accept(("a,b", "c")) == ["a", "b", "c"]
        assert parse_accept(("a", "a")) == ["a"]
        assert parse_accept(("a,all",)) == "all"
        assert parse_accept((" a , ,b ",)) == ["a", "b"]
