"""
Tests for the synthetic run-corpus generator (execution history, Phase 4).

The generator exists so calibration and prediction can be tested against
history whose truth is known. That is only worth anything if its records are
indistinguishable from recorded ones where it matters: they must validate
against the runs schema, pass the usable-run filter, and carry the noise they
were asked for.
"""

import json
import os

import pytest

from rhylthyme.history import (
    build_report,
    duration_stats,
    is_usable_run,
    list_runs,
    measured_steps,
    program_version,
    synthesize_runs,
    validate_run,
)
from rhylthyme.history.synth import lognormal_factor

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


def _step(step_id, trigger, duration, task="work"):
    return {
        "stepId": step_id,
        "name": step_id.title(),
        "startTrigger": trigger,
        "duration": duration,
        "task": task,
    }


@pytest.fixture
def mixed_program():
    """Fixed, variable and indefinite steps plus a serial replicate."""
    return {
        "programId": "synth-test",
        "name": "Synth Test",
        "actors": 2,
        "metadata": {
            "serves": "4",
            "varianceFactors": [
                {"key": "batchKg", "label": "Batch (kg)", "type": "number"},
                {
                    "key": "rig",
                    "label": "Rig",
                    "type": "enum",
                    "values": ["old", "new"],
                },
            ],
        },
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
                            "minSeconds": 300,
                            "maxSeconds": 1200,
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
                        "rep",
                        {"type": "programStart"},
                        {"type": "fixed", "seconds": 120},
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


@pytest.mark.unit
class TestSchemaValidity:
    def test_every_record_validates(self, mixed_program):
        for record in synthesize_runs(mixed_program, 5, seed=1):
            assert validate_run(record) == []

    def test_every_record_is_usable(self, mixed_program):
        for record in synthesize_runs(mixed_program, 5, seed=1):
            assert is_usable_run(record) == (True, None)
            assert {s["stepId"] for s in measured_steps(record)} == {"steady", "wild"}

    def test_thanksgiving_records_validate(self, thanksgiving_program):
        records = synthesize_runs(thanksgiving_program, 4, seed=3)
        for record in records:
            assert validate_run(record) == []
            assert record["programId"] == "thanksgiving-one-oven"
            assert record["programVersion"] == program_version(thanksgiving_program)

    def test_record_identity_and_ordering(self, mixed_program):
        records = synthesize_runs(mixed_program, 6, seed=2)
        assert len(records) == 6
        assert len({r["runId"] for r in records}) == 6
        starts = [r["startedAt"] for r in records]
        assert starts == sorted(starts), "one run per day, oldest first"
        for record in records:
            assert record["runtime"] == {
                "kind": "cli",
                "version": "synthetic",
                "clockMode": "wall",
                "speed": 1,
            }
            assert record["outcome"] == "completed"

    def test_ended_by_follows_the_duration_kind(self, mixed_program):
        record = synthesize_runs(mixed_program, 1, seed=4)[0]
        ended = {s["stepId"]: s["endedBy"] for s in record["steps"]}
        assert ended == {
            "warmup": "timer",
            "steady": "executor",
            "wild": "executor",
            "rep": "timer",
        }

    def test_planned_comes_from_the_timing_resolver(self, mixed_program):
        from rhylthyme.history import freeze_planned

        planned = freeze_planned(mixed_program)
        record = synthesize_runs(mixed_program, 1, seed=5)[0]
        for step in record["steps"]:
            assert step["planned"] == planned[step["stepId"]]


@pytest.mark.unit
class TestNoise:
    def test_requested_cv_is_reproduced(self, mixed_program):
        records = synthesize_runs(
            mixed_program,
            400,
            noise_by_step={"steady": 0.05, "wild": 0.50},
            default_cv=0.0,
            seed=11,
        )
        for step_id, wanted in (("steady", 0.05), ("wild", 0.50)):
            durations = [
                s["actual"]["end"] - s["actual"]["start"]
                for r in records
                for s in r["steps"]
                if s["stepId"] == step_id
            ]
            stats = duration_stats(durations)
            assert stats["n"] == 400
            assert abs(stats["cv"] - wanted) < wanted * 0.2, (step_id, stats["cv"])

    def test_zero_noise_reproduces_the_plan_exactly(self, mixed_program):
        record = synthesize_runs(mixed_program, 1, default_cv=0.0, seed=6)[0]
        for step in record["steps"]:
            planned = step["planned"]["end"] - step["planned"]["start"]
            observed = step["actual"]["end"] - step["actual"]["start"]
            assert observed == pytest.approx(planned)

    def test_lognormal_factor_has_mean_one(self):
        import random

        rng = random.Random(0)
        values = [lognormal_factor(rng, 0.3) for _ in range(20000)]
        assert all(v > 0 for v in values)
        assert abs(sum(values) / len(values) - 1.0) < 0.02
        assert lognormal_factor(rng, 0) == 1.0

    def test_seed_makes_a_corpus_reproducible(self, mixed_program):
        a = synthesize_runs(mixed_program, 3, noise_by_step={"steady": 0.2}, seed=7)
        b = synthesize_runs(mixed_program, 3, noise_by_step={"steady": 0.2}, seed=7)
        c = synthesize_runs(mixed_program, 3, noise_by_step={"steady": 0.2}, seed=8)
        assert a == b
        assert a != c


@pytest.mark.unit
class TestFactorsAndGeneratingLaw:
    def test_factors_land_in_user_tags(self, mixed_program):
        records = synthesize_runs(
            mixed_program,
            4,
            factors_fn=lambda i: {"batchKg": 2.0 + i, "rig": "new" if i % 2 else "old"},
            seed=9,
        )
        assert [r["context"]["userTags"] for r in records] == [
            {"batchKg": 2.0, "rig": "old"},
            {"batchKg": 3.0, "rig": "new"},
            {"batchKg": 4.0, "rig": "old"},
            {"batchKg": 5.0, "rig": "new"},
        ]
        # The program's own metadata is carried too, as the recorder does.
        assert records[0]["context"]["serves"] == "4"

    def test_duration_fn_imposes_an_exact_law(self, mixed_program):
        """The Phase 6 hook: duration = 600 + 90 * batchKg, no noise."""

        def law(step_id, planned, factors, rng):
            if step_id == "wild":
                return 600 + 90 * factors["batchKg"]
            return planned

        records = synthesize_runs(
            mixed_program,
            5,
            duration_fn=law,
            factors_fn=lambda i: {"batchKg": float(i)},
            default_cv=0.0,
            seed=10,
        )
        observed = [
            s["actual"]["end"] - s["actual"]["start"]
            for r in records
            for s in r["steps"]
            if s["stepId"] == "wild"
        ]
        assert observed == pytest.approx([600, 690, 780, 870, 960])


@pytest.mark.unit
class TestCascadeAndStorage:
    def test_an_overrun_pushes_the_successor(self, mixed_program):
        """A late step releases its successor late: triggerFiredAt follows."""

        def law(step_id, planned, factors, rng):
            return planned * 2 if step_id == "warmup" else planned

        record = synthesize_runs(
            mixed_program, 1, duration_fn=law, default_cv=0.0, seed=12
        )[0]
        steps = {s["stepId"]: s for s in record["steps"]}
        assert steps["warmup"]["actual"]["end"] == 1200
        assert steps["steady"]["triggerFiredAt"] == 1200
        assert steps["steady"]["actual"]["start"] == 1200
        assert steps["steady"]["waitedOn"] == ["warmup"]
        # An early step does not drag its successor back before the plan.
        assert steps["rep"]["actual"]["start"] == 0

    def test_runs_dir_writes_readable_records(self, mixed_program, temp_dir):
        records = synthesize_runs(mixed_program, 3, seed=13, runs_dir=temp_dir)
        on_disk = list_runs(temp_dir, "synth-test")
        assert len(on_disk) == 3
        assert {r["runId"] for r in on_disk} == {r["runId"] for r in records}
        assert build_report(on_disk, mixed_program).runsUsable == 3

    def test_replicates_are_expanded_into_instances(self, mixed_program):
        mixed_program["tracks"][1]["steps"][0]["replicates"] = {
            "count": 3,
            "mode": "serial",
        }
        record = synthesize_runs(mixed_program, 1, seed=14)[0]
        reps = [s for s in record["steps"] if s["stepId"] == "rep"]
        assert [s["instance"] for s in reps] == [1, 2, 3]
        assert validate_run(record) == []
        # Pooled by authored stepId in the report.
        rows = {r.stepId: r for r in build_report([record], mixed_program).steps}
        assert rows["rep"].lagN == 3
