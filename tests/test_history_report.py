"""
Tests for the inferentiality report (execution history, Phase 4).

The report answers Badosa's precondition — is this program's history predictive
of its durations? — so the tests are built on synthetic corpora whose noise is
known: a step generated with a coefficient of variation of 0.05 must come back
`predictable`, one generated at 0.6 must come back `executor-controlled`, and a
step with fewer than k measurements must come back `insufficient` whatever its
spread.
"""

import json
import os

import pytest
from click.testing import CliRunner

from rhylthyme.cli.history.report_cli import render_report
from rhylthyme.history import (
    build_report,
    duration_stats,
    filter_since,
    percentile,
    synthesize_runs,
)
from rhylthyme.history.report import EXECUTOR_CONTROLLED, INSUFFICIENT, LAG, PREDICTABLE

LOW_CV = 0.05
HIGH_CV = 0.60


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
    One fixed step, one tight variable step, one wild indefinite step, and a
    second variable step on another task so the roll-ups have something to
    group.
    """
    return {
        "programId": "report-test",
        "name": "Report Test",
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
                        "tidy",
                        {"type": "programStart"},
                        {
                            "type": "variable",
                            "minSeconds": 60,
                            "maxSeconds": 600,
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


@pytest.fixture
def runs20(program):
    return synthesize_runs(program, 20, noise_by_step=NOISE, default_cv=0.02, seed=101)


def _rows(report):
    return {row.stepId: row for row in report.steps}


# --------------------------------------------------------------- verdicts


@pytest.mark.unit
class TestVerdicts:
    def test_low_variance_step_is_predictable(self, runs20, program):
        row = _rows(build_report(runs20, program))["steady"]
        assert row.n == 20
        assert row.durationType == "variable"
        assert row.cv < 0.25
        assert row.verdict == PREDICTABLE
        # The distribution is reported, not just the verdict.
        assert row.p10 < row.median < row.p90
        assert row.iqr > 0
        assert row.plannedSeconds == 600
        assert row.median == pytest.approx(600, rel=0.15)

    def test_high_variance_step_is_executor_controlled(self, runs20, program):
        row = _rows(build_report(runs20, program))["wild"]
        assert row.n == 20
        assert row.cv > 0.25
        assert row.verdict == EXECUTOR_CONTROLLED

    def test_four_runs_are_insufficient(self, program):
        runs = synthesize_runs(program, 4, noise_by_step=NOISE, seed=102)
        rows = _rows(build_report(runs, program))
        assert rows["steady"].n == 4
        assert rows["steady"].verdict == INSUFFICIENT
        assert rows["wild"].verdict == INSUFFICIENT
        # ... and five are enough.
        runs = synthesize_runs(program, 5, noise_by_step=NOISE, seed=102)
        assert _rows(build_report(runs, program))["steady"].verdict == PREDICTABLE

    def test_thresholds_are_parameters(self, runs20, program):
        # A stricter CV threshold reclassifies the tight step.
        strict = _rows(build_report(runs20, program, cv_threshold=0.01))
        assert strict["steady"].verdict == EXECUTOR_CONTROLLED
        # A larger k withholds every verdict.
        demanding = _rows(build_report(runs20, program, k=50))
        assert demanding["steady"].verdict == INSUFFICIENT
        assert demanding["wild"].verdict == INSUFFICIENT
        # A looser CV threshold accepts the wild step.
        loose = _rows(build_report(runs20, program, cv_threshold=0.99))
        assert loose["wild"].verdict == PREDICTABLE

    def test_mean_deviation_reports_bias_not_just_noise(self, program):
        """A step that always runs 50% long is biased, whatever its spread."""

        def law(step_id, planned, factors, rng):
            return planned * 1.5 if step_id == "steady" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=103)
        row = _rows(build_report(runs, program))["steady"]
        assert row.plannedSeconds == 600
        assert row.median == pytest.approx(900)
        assert row.meanDeviation == pytest.approx(300)
        assert row.cv == pytest.approx(0.0, abs=1e-9)
        assert row.verdict == PREDICTABLE

    def test_unusable_runs_are_excluded_with_a_reason(self, runs20, program):
        runs20[0]["outcome"] = "abandoned"
        runs20[1]["runtime"]["speed"] = 10
        runs20[2]["runtime"]["clockMode"] = "simulated"
        report = build_report(runs20, program)
        assert report.runsConsidered == 20
        assert report.runsUsable == 17
        assert [r["reason"] for r in report.runsExcluded] == [
            "outcome-not-completed",
            "speed-not-1",
            "clock-not-wall",
        ]
        assert _rows(report)["steady"].n == 17

    def test_paused_and_timer_ended_steps_are_counted_out(self, runs20, program):
        for record in runs20[:6]:
            for step in record["steps"]:
                if step["stepId"] == "steady":
                    step["pausedSeconds"] = 30
                if step["stepId"] == "wild":
                    step["endedBy"] = "timer"
        rows = _rows(build_report(runs20, program))
        assert rows["steady"].n == 14
        assert rows["steady"].excluded == {"paused": 6}
        assert rows["wild"].n == 14
        assert rows["wild"].excluded == {"ended-by-timer": 6}


# ------------------------------------------------------------ fixed steps


@pytest.mark.unit
class TestFixedStepLag:
    def test_fixed_steps_get_a_lag_row_not_a_verdict(self, runs20, program):
        row = _rows(build_report(runs20, program))["warmup"]
        assert row.durationType == "fixed"
        assert row.verdict == LAG
        assert row.cv is None and row.median is None
        assert row.lagN == 20
        assert row.lagSeconds == pytest.approx(0, abs=30)

    def test_a_consistently_overrunning_fixed_step_shows_its_lag(self, program):
        """The Phase 5 signal: this step should have been variable."""

        def law(step_id, planned, factors, rng):
            return planned + 300 if step_id == "warmup" else planned

        runs = synthesize_runs(program, 10, duration_fn=law, default_cv=0.0, seed=104)
        row = _rows(build_report(runs, program))["warmup"]
        assert row.lagSeconds == pytest.approx(300)
        assert row.lagN == 10
        # endDriftSeconds is the same figure anchored on the planned start,
        # which for the first step of the program is identical.
        assert row.endDriftSeconds == pytest.approx(300)

    def test_lag_is_the_step_s_own_overrun_not_inherited_drift(self, program):
        """
        `steady` overruns by an hour; the fixed `warmup` before it is on time and
        the `wild` step after it is measured, so nothing inherits a fake lag.
        """

        def law(step_id, planned, factors, rng):
            return planned + 3600 if step_id == "steady" else planned

        runs = synthesize_runs(program, 8, duration_fn=law, default_cv=0.0, seed=105)
        rows = _rows(build_report(runs, program))
        assert rows["warmup"].lagSeconds == pytest.approx(0)
        assert rows["warmup"].endDriftSeconds == pytest.approx(0)
        assert rows["steady"].meanDeviation == pytest.approx(3600)
        # `wild` starts an hour late but takes exactly as long as planned.
        assert rows["wild"].meanDeviation == pytest.approx(0)


# --------------------------------------------------------------- roll-ups


@pytest.mark.unit
class TestRollUps:
    def test_by_task(self, runs20, program):
        report = build_report(runs20, program)
        groups = {g.key: g for g in report.byTask}
        assert set(groups) == {"prep", "work"}
        # prep holds the fixed warmup (lag) and the tight tidy (predictable)
        assert groups["prep"].steps == 2
        assert groups["prep"].lag == 1
        assert groups["prep"].predictable == 1
        # work holds the tight steady and the wild indefinite
        assert groups["work"].predictable == 1
        assert groups["work"].executorControlled == 1
        assert groups["work"].medianCv == pytest.approx(
            percentile(
                [r.cv for r in report.steps if r.task == "work" and r.cv is not None],
                0.5,
            )
        )

    def test_by_duration_kind(self, runs20, program):
        groups = {g.key: g for g in build_report(runs20, program).byDurationKind}
        assert set(groups) == {"fixed", "variable", "indefinite"}
        assert groups["fixed"].lag == 1
        assert groups["variable"].predictable == 2
        assert groups["indefinite"].executorControlled == 1

    def test_program_level_predictable_fraction(self, runs20, program):
        report = build_report(runs20, program)
        # steady, tidy, wild get verdicts; warmup is fixed and gets a lag row.
        assert report.stepsWithVerdict == 3
        assert report.stepsPredictable == 2
        assert report.predictableFraction == pytest.approx(2 / 3)

    def test_without_a_program_the_task_rollup_degrades_gracefully(self, runs20):
        report = build_report(runs20)
        assert [g.key for g in report.byTask] == ["(no task)"]
        assert all(row.task is None for row in report.steps)
        # Everything else still works: verdicts come from the records alone.
        assert _rows(report)["steady"].verdict == PREDICTABLE
        assert report.programId == "report-test"


# ------------------------------------------------------------------ --since


@pytest.mark.unit
class TestSince:
    def test_filter_since_keeps_the_newer_runs(self, runs20):
        # One run per day from 2026-01-05.
        assert len(filter_since(runs20, None)) == 20
        assert len(filter_since(runs20, "2026-01-05")) == 20
        assert len(filter_since(runs20, "2026-01-15")) == 10
        assert len(filter_since(runs20, "2026-03-01")) == 0
        assert len(filter_since(runs20, "2026-01-15T00:00:00Z")) == 10

    def test_build_report_applies_since(self, runs20, program):
        report = build_report(runs20, program, since="2026-01-15")
        assert report.runsConsidered == 10
        assert _rows(report)["steady"].n == 10

    def test_runs_without_a_start_are_dropped_by_since(self, runs20):
        runs20[0].pop("startedAt")
        assert len(filter_since(runs20, "2026-01-05")) == 19


# ----------------------------------------------------------------- rendering


@pytest.mark.unit
class TestRendering:
    def test_table(self, runs20, program):
        text = render_report(build_report(runs20, program), "table")
        assert "Inferentiality report: report-test" in text
        assert "Runs: 20 usable of 20" in text
        assert "Predictable steps: 2/3" in text
        for header in ("Step", "Median", "P10", "P90", "IQR", "CV", "Verdict"):
            assert header in text
        assert PREDICTABLE in text and EXECUTOR_CONTROLLED in text
        assert "lag " in text
        assert "By step type (task)" in text
        assert "By duration kind" in text

    def test_markdown(self, runs20, program):
        text = render_report(build_report(runs20, program), "md")
        assert text.startswith("# Inferentiality report: report-test")
        assert "## Per step" in text
        assert "| Step | Type | Task |" in text
        assert "|---|" in text
        assert "| steady | variable | work |" in text

    def test_json(self, runs20, program):
        payload = json.loads(render_report(build_report(runs20, program), "json"))
        assert payload["programId"] == "report-test"
        assert payload["runsUsable"] == 20
        assert payload["minRuns"] == 5
        assert payload["cvThreshold"] == 0.25
        rows = {s["stepId"]: s for s in payload["steps"]}
        assert rows["steady"]["verdict"] == PREDICTABLE
        assert rows["warmup"]["verdict"] == LAG
        assert rows["warmup"]["lagN"] == 20
        assert payload["predictableFraction"] == pytest.approx(2 / 3)

    def test_excluded_runs_are_listed(self, runs20, program):
        runs20[0]["outcome"] = "aborted"
        text = render_report(build_report(runs20, program), "table")
        assert "1 excluded" in text
        assert "Excluded runs" in text
        assert "outcome-not-completed" in text


# ---------------------------------------------------------------- statistics


@pytest.mark.unit
class TestStatistics:
    def test_duration_stats_on_known_values(self):
        """The same numbers mcp-api/history.test.js asserts."""
        stats = duration_stats([5, 3, 1, 9, 7, 2, 8, 4, 6])
        assert stats["n"] == 9
        assert stats["mean"] == 5
        assert stats["median"] == 5
        assert stats["p10"] == pytest.approx(1.8)
        assert stats["p90"] == pytest.approx(8.2)
        assert stats["iqr"] == 4
        assert stats["cv"] == pytest.approx((60 / 8) ** 0.5 / 5)

    def test_duration_stats_edges(self):
        assert duration_stats([]) == {
            "n": 0,
            "median": None,
            "p10": None,
            "p90": None,
            "iqr": None,
            "mean": None,
            "cv": None,
        }
        one = duration_stats([42])
        assert (one["n"], one["median"], one["iqr"], one["cv"]) == (1, 42, 0, None)
        assert duration_stats([600, 600, 600])["cv"] == 0
        assert duration_stats([10, 20])["median"] == 15
        assert duration_stats([0, 0])["cv"] is None
        assert duration_stats([10, None, 20])["n"] == 2

    def test_percentile(self):
        assert percentile([], 0.5) is None
        assert percentile([9, 1, 5, 3, 7], 0) == 1
        assert percentile([9, 1, 5, 3, 7], 1) == 9
        assert percentile([9, 1, 5, 3, 7], 0.5) == 5


# ---------------------------------------------------------------------- CLI


@pytest.mark.cli
class TestReportCommand:
    def _runs(self, program, temp_dir, n=20, seed=201):
        return synthesize_runs(
            program,
            n,
            noise_by_step=NOISE,
            default_cv=0.02,
            seed=seed,
            runs_dir=temp_dir,
        )

    def test_table_output(self, program, temp_dir):
        self._runs(program, temp_dir)
        from rhylthyme.cli.main import cli

        result = CliRunner().invoke(
            cli, ["runs", "report", "report-test", "--runs-dir", temp_dir]
        )
        assert result.exit_code == 0, result.output
        assert "Inferentiality report: report-test" in result.output
        assert PREDICTABLE in result.output
        assert EXECUTOR_CONTROLLED in result.output

    def test_program_file_supplies_the_tasks(self, program, temp_dir, programs_dir):
        self._runs(program, temp_dir)
        path = os.path.join(programs_dir, "report_test.json")
        with open(path, "w") as fh:
            json.dump(program, fh)
        from rhylthyme.cli.main import cli

        result = CliRunner().invoke(
            cli, ["runs", "report", path, "--runs-dir", temp_dir, "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        tasks = {s["stepId"]: s["task"] for s in payload["steps"]}
        assert tasks == {
            "warmup": "prep",
            "steady": "work",
            "wild": "work",
            "tidy": "prep",
        }

    def test_flags(self, program, temp_dir):
        from rhylthyme.cli.main import cli

        self._runs(program, temp_dir)
        runner = CliRunner()

        md = runner.invoke(
            cli,
            ["runs", "report", "report-test", "--runs-dir", temp_dir, "--format", "md"],
        )
        assert md.exit_code == 0
        assert md.output.startswith("# Inferentiality report")

        strict = runner.invoke(
            cli,
            [
                "runs",
                "report",
                "report-test",
                "--runs-dir",
                temp_dir,
                "--min-runs",
                "50",
                "--format",
                "json",
            ],
        )
        payload = json.loads(strict.output)
        assert payload["minRuns"] == 50
        assert all(s["verdict"] in (INSUFFICIENT, LAG) for s in payload["steps"])

        since = runner.invoke(
            cli,
            [
                "runs",
                "report",
                "report-test",
                "--runs-dir",
                temp_dir,
                "--since",
                "2026-01-15",
                "--format",
                "json",
            ],
        )
        assert json.loads(since.output)["runsConsidered"] == 10

    def test_out_file(self, program, temp_dir):
        from rhylthyme.cli.main import cli

        self._runs(program, temp_dir)
        out = os.path.join(temp_dir, "report.md")
        result = CliRunner().invoke(
            cli,
            [
                "runs",
                "report",
                "report-test",
                "--runs-dir",
                temp_dir,
                "--format",
                "md",
                "--out",
                out,
            ],
        )
        assert result.exit_code == 0
        assert "Report written to" in result.output
        with open(out) as fh:
            assert fh.read().startswith("# Inferentiality report")

    def test_all_programs(self, program, temp_dir):
        from rhylthyme.cli.main import cli

        self._runs(program, temp_dir)
        other = dict(program, programId="other-program")
        synthesize_runs(other, 6, noise_by_step=NOISE, seed=202, runs_dir=temp_dir)

        result = CliRunner().invoke(
            cli, ["runs", "report", "--all", "--runs-dir", temp_dir]
        )
        assert result.exit_code == 0, result.output
        assert "Inferentiality report: other-program" in result.output
        assert "Inferentiality report: report-test" in result.output

        as_json = CliRunner().invoke(
            cli, ["runs", "report", "--all", "--runs-dir", temp_dir, "--format", "json"]
        )
        payload = json.loads(as_json.output)
        assert [r["programId"] for r in payload] == ["other-program", "report-test"]

    def test_no_runs_is_an_error(self, temp_dir):
        from rhylthyme.cli.main import cli

        result = CliRunner().invoke(
            cli, ["runs", "report", "nothing-here", "--runs-dir", temp_dir]
        )
        assert result.exit_code == 1
        assert "No runs recorded" in result.output

    def test_program_or_all_is_required(self, temp_dir):
        from rhylthyme.cli.main import cli

        result = CliRunner().invoke(cli, ["runs", "report", "--runs-dir", temp_dir])
        assert result.exit_code != 0
        assert "--all" in result.output
