"""
Held-out evaluation (PRD §8 last item, plan Phase 7).

The question is whether a prediction from history is closer to what happens
than the number the author typed. These tests answer it on synthetic corpora
whose generating law is known, so the expected verdict is not a matter of
opinion:

* a step whose duration really is a function of a declared factor
  (``turkey-roast = 5400 + 300 * turkeyKg``) must show a large positive
  improvement, because the author's single ``defaultSeconds`` cannot follow
  the factor;
* a step whose durations scatter around the author's own number must show no
  meaningful improvement, because there is nothing left to learn.

The split is checked separately from the metrics: it must be chronological,
hold out the latest runs and never train on a run it is about to predict.
"""

import json
import os

import pytest

from rhylthyme.history import synthesize_runs
from rhylthyme.history.evaluate import (
    REASON_NO_USABLE_RUNS,
    REASON_TOO_FEW_RUNS,
    evaluate_all,
    evaluate_program,
    group_by_program,
    program_stub_from_records,
    sort_records,
    split_chronologically,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SYNTH_FIXTURE = os.path.join(
    HERE, "fixtures", "history", "thanksgiving-synth-runs.json"
)
TIMELINE = os.path.normpath(os.path.join(HERE, "..", "..", "rhylthyme-timeline"))
PROGRAM_FILE = os.path.join(
    TIMELINE, "test", "fixtures", "programs", "thanksgiving_one_oven.json"
)

ROAST_INTERCEPT = 5400.0
ROAST_PER_KG = 300.0


def _load(path):
    if not os.path.isfile(path):
        pytest.skip(f"{path} is not present")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def program():
    """The Thanksgiving example with one declared numeric variance factor."""
    data = _load(PROGRAM_FILE)
    data.setdefault("metadata", {})["varianceFactors"] = [
        {"key": "turkeyKg", "label": "Turkey weight (kg)", "type": "number"}
    ]
    return data


@pytest.fixture
def records(program):
    """
    Twenty runs in which the roast follows a law of the turkey's weight and
    the potatoes scatter around the author's own number.
    """

    def law(step_id, planned, factors, rng):
        if step_id == "turkey-roast":
            return ROAST_INTERCEPT + ROAST_PER_KG * float(factors["turkeyKg"])
        return planned

    return synthesize_runs(
        program,
        20,
        noise_by_step={"turkey-roast": 0.02, "potatoes-boil": 0.08},
        factors_fn=lambda i: {"turkeyKg": 5 + i % 4},
        duration_fn=law,
        seed=11,
        environment_id="home-kitchen",
    )


# --------------------------------------------------------------------- split


@pytest.mark.unit
class TestChronologicalSplit:
    def _runs(self, n):
        return [
            {"runId": f"r{i}", "startedAt": f"2026-01-{i + 1:02d}T10:00:00Z"}
            for i in range(n)
        ]

    def test_the_latest_fifth_is_held_out(self):
        train, held = split_chronologically(self._runs(20))
        assert len(train) == 16 and len(held) == 4
        assert [r["runId"] for r in held] == ["r16", "r17", "r18", "r19"]

    def test_records_are_ordered_oldest_first_whatever_order_they_arrive_in(self):
        shuffled = list(reversed(self._runs(5)))
        assert [r["runId"] for r in sort_records(shuffled)] == [
            "r0",
            "r1",
            "r2",
            "r3",
            "r4",
        ]
        train, held = split_chronologically(shuffled)
        assert [r["runId"] for r in train] == ["r0", "r1", "r2", "r3"]
        assert [r["runId"] for r in held] == ["r4"]

    def test_at_least_one_run_is_held_out_and_one_kept(self):
        train, held = split_chronologically(self._runs(2))
        assert len(train) == 1 and len(held) == 1
        # A holdout of 1.0 would leave nothing to train on.
        train, held = split_chronologically(self._runs(4), holdout=1.0)
        assert len(train) == 1 and len(held) == 3
        train, held = split_chronologically(self._runs(4), holdout=0.01)
        assert len(train) == 3 and len(held) == 1

    def test_a_single_run_cannot_be_split(self):
        train, held = split_chronologically(self._runs(1))
        assert len(train) == 1 and held == []

    def test_the_holdout_share_is_honoured(self):
        train, held = split_chronologically(self._runs(20), holdout=0.5)
        assert len(train) == 10 and len(held) == 10

    def test_training_and_held_out_runs_never_overlap(self):
        train, held = split_chronologically(self._runs(17))
        ids = {r["runId"] for r in train}
        assert not ids & {r["runId"] for r in held}
        assert len(ids) + len(held) == 17


# ------------------------------------------------------------------- metrics


@pytest.mark.unit
class TestEvaluation:
    def test_prediction_beats_the_plan_where_a_factor_drives_the_duration(
        self, records, program
    ):
        result = evaluate_program(records, program)
        assert result.runsUsable == 20
        assert result.runsTrain == 16
        assert result.runsHeldOut == 4
        rows = {row.stepId: row for row in result.steps}

        # The roast: the author wrote 9900 s, the truth is 5400 + 300 * kg, so
        # the plan is out by more than an hour and the forecast is not.
        roast = rows["turkey-roast"]
        assert roast.verdict == "predictable"
        assert roast.n == 4
        assert roast.plannedSeconds == 9900
        assert roast.maePlanned > 2000
        assert roast.maePredicted < 300
        assert roast.improvement > 0.8

        # The potatoes: the author's number IS the centre of the distribution,
        # so there is nothing for history to add.
        boil = rows["potatoes-boil"]
        assert boil.plannedSeconds == 1200
        assert abs(boil.improvement) < 0.75

    def test_only_paired_observations_are_scored(self, records, program):
        result = evaluate_program(records, program)
        for row in result.steps:
            assert row.n <= row.observations
            assert sum(row.basisCounts.values()) == row.observations
        assert result.n == sum(row.n for row in result.steps)
        assert result.observations == sum(row.observations for row in result.steps)
        assert sum(result.basisCounts.values()) == result.observations

    def test_fixed_steps_are_never_evaluated(self, records, program):
        result = evaluate_program(records, program)
        kinds = {row.durationType for row in result.steps}
        assert "fixed" not in kinds
        assert {row.stepId for row in result.steps} == {
            "turkey-roast",
            "potatoes-boil",
        }

    def test_the_verdicts_come_from_the_training_runs(self, records, program):
        result = evaluate_program(records, program)
        by_step = {row.stepId: row.verdict for row in result.steps}
        assert by_step["turkey-roast"] == "predictable"
        # Raising k above the training size leaves every step unjudged, and
        # the acceptance claim untested rather than true.
        strict = evaluate_program(records, program, verdict_min_runs=99)
        assert {row.verdict for row in strict.steps} == {"insufficient"}
        assert strict.claim["holds"] is None
        assert strict.claim["stepTypes"] == []

    def test_the_claim_names_the_step_types_it_was_read_off(self, records, program):
        result = evaluate_program(records, program)
        # The roast is the oven step and the boil is a stove step.
        assert set(result.claim["stepTypes"]) <= {"oven", "stove"}
        assert "oven" in result.claim["stepTypes"]
        assert isinstance(result.claim["holds"], bool)
        assert set(result.claim["failures"]) <= set(result.claim["stepTypes"])
        oven = {g.key: g for g in result.byTask}["oven"]
        assert oven.predictableSteps == 1
        assert oven.predictableMaePredicted < oven.predictableMaePlanned
        assert "oven" not in result.claim["failures"]

    def test_roll_ups_pool_the_same_observations(self, records, program):
        result = evaluate_program(records, program)
        assert sum(g.n for g in result.byTask) == result.n
        assert sum(g.n for g in result.byDurationKind) == result.n
        assert [g.key for g in result.byDurationKind] == ["indefinite", "variable"]
        assert [g.key for g in result.byTask] == sorted(g.key for g in result.byTask)

    def test_improvement_is_one_minus_the_error_ratio(self, records, program):
        result = evaluate_program(records, program)
        for row in result.steps:
            if row.maePlanned:
                assert row.improvement == pytest.approx(
                    1 - row.maePredicted / row.maePlanned, abs=1e-3
                )
        assert result.improvement == pytest.approx(
            1 - result.maePredicted / result.maePlanned, abs=1e-3
        )

    def test_holdout_changes_the_split_and_the_sample(self, records, program):
        half = evaluate_program(records, program, holdout=0.5)
        assert half.runsTrain == 10
        assert half.runsHeldOut == 10
        assert half.n > evaluate_program(records, program).n
        assert len(half.heldOutRunIds) == 10

    def test_k_gates_the_whole_evaluation(self, records, program):
        assert evaluate_program(records[:3], program, min_runs=5).reason == (
            REASON_TOO_FEW_RUNS
        )
        assert evaluate_program(records[:3], program, min_runs=3).reason is None
        # Nothing usable at all is a different answer from "too few".
        unusable = [dict(r, outcome="abandoned") for r in records]
        result = evaluate_program(unusable, program)
        assert result.reason == REASON_NO_USABLE_RUNS
        assert result.runsConsidered == 20 and result.runsUsable == 0
        assert result.claim == {"stepTypes": [], "holds": None, "failures": []}

    def test_since_drops_older_runs(self, records, program):
        cutoff = records[10]["startedAt"][:10]
        result = evaluate_program(records, program, since=cutoff, min_runs=3)
        assert result.runsConsidered == 10
        assert result.runsUsable == 10

    def test_evaluating_without_a_program_still_works(self, records):
        # `--all` has no program file: the factors and the programId are
        # recovered from the records' context snapshot.
        stub = program_stub_from_records(records)
        assert stub["programId"] == "thanksgiving-one-oven"
        assert [f["key"] for f in stub["metadata"]["varianceFactors"]] == ["turkeyKg"]
        result = evaluate_program(records, None)
        assert result.programId == "thanksgiving-one-oven"
        rows = {row.stepId: row for row in result.steps}
        assert rows["turkey-roast"].improvement > 0.8
        # ...only the task of each step is lost, so the roll-up is by "(no task)".
        assert [g.key for g in result.byTask] == ["(no task)"]

    def test_evaluate_all_groups_by_program(self, records):
        other = [dict(r, programId="something-else") for r in records[:6]]
        grouped = group_by_program(records + other)
        assert set(grouped) == {"thanksgiving-one-oven", "something-else"}
        results = evaluate_all(records + other)
        assert [r.programId for r in results] == [
            "something-else",
            "thanksgiving-one-oven",
        ]


# ------------------------------------------------------------- stable output


@pytest.mark.unit
class TestStableJson:
    def test_the_json_report_is_reproducible(self, records, program):
        first = evaluate_program(records, program).to_dict()
        second = evaluate_program(records, program).to_dict()
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_every_number_is_rounded_for_a_committed_report(self, records, program):
        result = evaluate_program(records, program).to_dict()

        def check(value, where):
            if isinstance(value, float):
                assert round(value, 3) == value, where
            elif isinstance(value, dict):
                for key, item in value.items():
                    check(item, f"{where}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    check(item, f"{where}[{index}]")

        check(result, "$")

    def test_the_shape_is_the_one_the_report_documents(self, records, program):
        result = evaluate_program(records, program).to_dict()
        assert set(result) == {
            "programId",
            "holdout",
            "minRuns",
            "verdictMinRuns",
            "cvThreshold",
            "runsConsidered",
            "runsUsable",
            "runsTrain",
            "runsHeldOut",
            "heldOutRunIds",
            "observations",
            "n",
            "maePredicted",
            "maePlanned",
            "improvement",
            "basisCounts",
            "steps",
            "byTask",
            "byDurationKind",
            "claim",
            "reason",
        }
        assert list(result["basisCounts"]) == ["identical", "model", "none"]
        assert set(result["steps"][0]) == {
            "stepId",
            "durationType",
            "task",
            "verdict",
            "n",
            "observations",
            "plannedSeconds",
            "medianActual",
            "maePredicted",
            "maePlanned",
            "improvement",
            "basisCounts",
        }


# ------------------------------------------------------------ the corpus fixture


@pytest.mark.unit
class TestCommittedCorpus:
    """
    The corpus behind the committed synthetic report
    (``rhylthyme-docs/docs/development/reports/duration-evaluation-synthetic.md``).
    """

    def test_the_committed_report_numbers_are_reproducible(self):
        corpus = _load(SYNTH_FIXTURE)
        program = _load(
            os.path.normpath(os.path.join(HERE, "..", "..", corpus["program"]))
        )
        result = evaluate_program(corpus["runs"], program)
        rows = {row.stepId: row for row in result.steps}
        assert result.runsTrain == 16 and result.runsHeldOut == 4
        # The law is turkey-roast = 600 + 90 * turkeyKg against a planned
        # 9900 s, so the plan is wrong by well over two hours.
        assert rows["turkey-roast"].maePlanned > 8000
        assert rows["turkey-roast"].maePredicted < 200
        assert rows["turkey-roast"].improvement > 0.95
        # The boil scatters around its own planned value: history can only add
        # estimation noise, and on this corpus it does.
        assert rows["potatoes-boil"].improvement < 0
        assert result.claim["failures"] == ["stove"]
        assert result.claim["holds"] is False
