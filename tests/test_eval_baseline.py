"""
Tests for the committed eval numbers and the regression gate.

Two things are asserted here. First, that ``eval/baseline.json`` and
``eval/four-turn.json`` -- the files CI compares against -- keep the shape
the harness writes and agree on the model pin, so a re-baseline that
forgets half the job is caught. Second, that
``rhylthyme.eval.compare`` fails on exactly the two regressions
the plan names (relationships F1 down more than 5 points, end-to-end pass
rate down at all) and passes on equal-or-better.

No model calls, no network: everything reads committed JSON or hand-built
dictionaries.
"""

import json
from pathlib import Path

import pytest

from rhylthyme.eval.compare import (
    DEFAULT_E2E_DROP,
    DEFAULT_REL_F1_DROP,
    Scores,
    compare_files,
    compare_scores,
    comparison_to_dict,
    load_scores,
    parse_scores,
    render_comparison,
)
from rhylthyme.eval.metrics import ComponentScores

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval"
BASELINE_FILE = EVAL_DIR / "baseline.json"
FOUR_TURN_FILE = EVAL_DIR / "four-turn.json"
STORED_FILES = (BASELINE_FILE, FOUR_TURN_FILE)

# eval/four-turn.json is the gate; eval/baseline.json is the historical
# single-message number kept for the README comparison.
GATE_FILE = FOUR_TURN_FILE


def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _summary(**overrides):
    """A complete headline summary, so a delta is the only thing under test."""
    base = {key: 0.5 for key in ComponentScores.HEADLINE_KEYS}
    base.update(overrides)
    return base


def _scores(programs=None, **overrides):
    """Build a Scores with a full summary and optional per-program rows."""
    return Scores(
        summary=_summary(**overrides),
        programs=programs or {},
        model="claude-haiku-4-5-20251001",
        pattern="four-turn",
        date="2026-09-14",
    )


# --------------------------------------------------------------------------
# The committed files
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", STORED_FILES, ids=lambda p: p.name)
def test_stored_file_exists(path):
    assert path.exists(), f"{path} is missing; re-run eval-prompts --write-baseline"


@pytest.mark.unit
@pytest.mark.parametrize("path", STORED_FILES, ids=lambda p: p.name)
def test_stored_file_has_the_baseline_shape(path):
    payload = _load(path)
    assert set(payload) >= {"model", "pattern", "date", "git_note", "results"}
    assert payload["model"], "no model pin"
    assert payload["pattern"] in {"baseline", "four-turn"}
    # ISO date, not a timestamp: the pin and the day it was measured.
    assert len(payload["date"]) == 10 and payload["date"].count("-") == 2
    results = payload["results"]
    assert set(results) >= {"meta", "summary", "programs"}
    assert results["meta"]["model"] == payload["model"]
    assert results["meta"]["pattern"] == payload["pattern"]


@pytest.mark.unit
@pytest.mark.parametrize("path", STORED_FILES, ids=lambda p: p.name)
def test_stored_summary_has_every_headline_metric(path):
    summary = _load(path)["results"]["summary"]
    missing = [key for key in ComponentScores.HEADLINE_KEYS if key not in summary]
    assert not missing, missing
    assert all(0.0 <= float(summary[key]) <= 1.0 for key in summary)


@pytest.mark.unit
def test_stored_files_share_one_model_pin():
    """A re-baseline moves both files to the same model, or neither."""
    models = {path.name: _load(path)["model"] for path in STORED_FILES}
    assert len(set(models.values())) == 1, models


@pytest.mark.unit
def test_stored_files_cover_the_same_programs():
    slugs = {
        path.name: sorted(p["slug"] for p in _load(path)["results"]["programs"])
        for path in STORED_FILES
    }
    assert len(set(map(tuple, slugs.values()))) == 1, slugs


@pytest.mark.unit
def test_gate_file_covers_the_whole_gold_set():
    """The gate is only as strong as its coverage: all 24 gold programs."""
    programs = _load(GATE_FILE)["results"]["programs"]
    assert len(programs) == 24, f"{len(programs)} programs in {GATE_FILE.name}"


@pytest.mark.unit
def test_gate_file_loads_as_scores():
    scores = load_scores(GATE_FILE)
    assert scores.model and scores.pattern == "four-turn"
    assert len(scores.programs) == 24
    assert "relationships_f1" in scores.summary


@pytest.mark.unit
def test_a_file_compared_with_itself_passes():
    comparison = compare_files(GATE_FILE, GATE_FILE)
    assert comparison.ok, comparison.failures
    assert comparison.only_in_reference == []
    assert comparison.only_in_results == []


@pytest.mark.unit
def test_four_turn_beats_the_historical_baseline():
    """The PRD hypothesis, asserted on the committed numbers."""
    baseline = load_scores(BASELINE_FILE).summary
    four_turn = load_scores(FOUR_TURN_FILE).summary
    assert four_turn["relationships_f1"] > baseline["relationships_f1"]
    assert four_turn["end_to_end_pass"] >= baseline["end_to_end_pass"]


@pytest.mark.unit
def test_parse_scores_accepts_a_bare_results_json():
    """A run's results.json has no outer {model, results} wrapper."""
    payload = _load(GATE_FILE)["results"]
    scores = parse_scores(payload, path="results.json")
    assert scores.model == _load(GATE_FILE)["model"]
    assert len(scores.programs) == 24


@pytest.mark.unit
def test_parse_scores_rejects_a_file_without_a_summary():
    with pytest.raises(ValueError):
        parse_scores({"meta": {}, "programs": []}, path="broken.json")


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_five_point_one_relationship_drop_fails():
    reference = _scores(relationships_f1=0.50)
    results = _scores(relationships_f1=0.449)  # 5.1 points down
    comparison = compare_scores(reference, results)
    assert not comparison.ok
    assert any("relationships_f1" in reason for reason in comparison.failures)


@pytest.mark.unit
def test_a_five_point_relationship_drop_is_tolerated():
    """Exactly the allowance passes; the gate is "more than 5 points"."""
    comparison = compare_scores(
        _scores(relationships_f1=0.50), _scores(relationships_f1=0.45)
    )
    assert comparison.ok, comparison.failures


@pytest.mark.unit
def test_any_end_to_end_drop_fails():
    reference = _scores(end_to_end_pass=0.50)
    results = _scores(end_to_end_pass=0.49)
    comparison = compare_scores(reference, results)
    assert not comparison.ok
    assert any("end_to_end_pass" in reason for reason in comparison.failures)


@pytest.mark.unit
def test_an_equal_run_passes():
    comparison = compare_scores(_scores(), _scores())
    assert comparison.ok, comparison.failures


@pytest.mark.unit
def test_a_better_run_passes():
    reference = _scores(relationships_f1=0.50, end_to_end_pass=0.50)
    results = _scores(relationships_f1=0.70, end_to_end_pass=0.75)
    comparison = compare_scores(reference, results)
    assert comparison.ok, comparison.failures
    deltas = {d.metric: d for d in comparison.deltas}
    assert deltas["relationships_f1"].points == pytest.approx(20.0)


@pytest.mark.unit
def test_an_ungated_metric_may_fall_freely():
    """Only relationships F1 and the end-to-end rate stop a merge."""
    comparison = compare_scores(
        _scores(steps_f1=0.90, unsupported_rate=0.01),
        _scores(steps_f1=0.10, unsupported_rate=0.90),
    )
    assert comparison.ok, comparison.failures


@pytest.mark.unit
def test_thresholds_are_configurable():
    reference = _scores(relationships_f1=0.50)
    results = _scores(relationships_f1=0.48)  # 2 points down
    assert compare_scores(reference, results, rel_f1_drop=1.0).ok is False
    assert compare_scores(reference, results, rel_f1_drop=5.0).ok is True


@pytest.mark.unit
def test_defaults_are_the_plans_thresholds():
    assert DEFAULT_REL_F1_DROP == 5.0
    assert DEFAULT_E2E_DROP == 0.0


@pytest.mark.unit
def test_a_program_missing_from_the_results_is_reported_and_fails():
    reference = _scores(programs={"kitchen-a": _summary(), "kitchen-b": _summary()})
    results = _scores(programs={"kitchen-a": _summary()})
    comparison = compare_scores(reference, results)
    assert comparison.only_in_reference == ["kitchen-b"]
    assert comparison.only_in_results == []
    assert not comparison.ok
    assert any("kitchen-b" in reason for reason in comparison.failures)


@pytest.mark.unit
def test_missing_coverage_can_be_waived():
    reference = _scores(programs={"kitchen-a": _summary(), "kitchen-b": _summary()})
    results = _scores(programs={"kitchen-a": _summary()})
    comparison = compare_scores(reference, results, allow_missing=True)
    assert comparison.ok, comparison.failures


@pytest.mark.unit
def test_a_new_program_is_reported_but_does_not_fail():
    """Growing the gold set is not a regression."""
    reference = _scores(programs={"kitchen-a": _summary()})
    results = _scores(programs={"kitchen-a": _summary(), "lab-new": _summary()})
    comparison = compare_scores(reference, results)
    assert comparison.only_in_results == ["lab-new"]
    assert comparison.ok, comparison.failures


@pytest.mark.unit
def test_per_program_deltas_name_the_program_that_moved():
    reference = _scores(
        programs={
            "kitchen-a": _summary(relationships_f1=0.8),
            "kitchen-b": _summary(relationships_f1=0.8),
        }
    )
    results = _scores(
        programs={
            "kitchen-a": _summary(relationships_f1=0.8),
            "kitchen-b": _summary(relationships_f1=0.2),
        }
    )
    comparison = compare_scores(reference, results)
    rendered = render_comparison(comparison)
    assert "kitchen-b" in rendered
    moved = {
        d.metric: d.points
        for d in comparison.program_deltas["kitchen-b"]
        if abs(d.delta) > 0
    }
    assert moved["relationships_f1"] == pytest.approx(-60.0)


@pytest.mark.unit
def test_a_model_pin_change_is_a_warning_not_a_failure():
    reference = _scores()
    results = _scores()
    results.model = "claude-something-else"
    comparison = compare_scores(reference, results)
    assert comparison.model_mismatch == (reference.model, results.model)
    assert comparison.ok, comparison.failures
    assert "model pin differs" in render_comparison(comparison)


@pytest.mark.unit
def test_render_says_pass_or_fail():
    assert "PASS" in render_comparison(compare_scores(_scores(), _scores()))
    failing = compare_scores(_scores(end_to_end_pass=0.9), _scores(end_to_end_pass=0.1))
    assert "FAIL" in render_comparison(failing)


@pytest.mark.unit
def test_comparison_to_dict_is_json_serialisable():
    comparison = compare_scores(
        _scores(relationships_f1=0.5), _scores(relationships_f1=0.1)
    )
    payload = json.loads(json.dumps(comparison_to_dict(comparison)))
    assert payload["ok"] is False
    assert payload["summary"]["relationships_f1"]["regressed"] is True
    assert payload["summary"]["steps_f1"]["regressed"] is False


# --------------------------------------------------------------------------
# The CLI wrapper
# --------------------------------------------------------------------------


def _write(path, reference_summary):
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "pattern": "four-turn",
        "date": "2026-09-14",
        "git_note": "test fixture",
        "results": {
            "meta": {"model": "claude-haiku-4-5-20251001", "pattern": "four-turn"},
            "summary": reference_summary,
            "programs": [],
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.cli
def test_cli_compare_passes_on_the_committed_file(cli_runner):
    from rhylthyme.cli.main import cli

    result = cli_runner.invoke(
        cli,
        [
            "eval-prompts",
            "compare",
            "--baseline",
            str(GATE_FILE),
            "--results",
            str(GATE_FILE),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "relationships_f1" in result.output


@pytest.mark.cli
def test_cli_compare_exits_non_zero_on_a_regression(cli_runner, tmp_path):
    from rhylthyme.cli.main import cli

    reference = _write(tmp_path / "four-turn.json", _summary(relationships_f1=0.60))
    results = _write(tmp_path / "results.json", _summary(relationships_f1=0.50))

    result = cli_runner.invoke(
        cli,
        [
            "eval-prompts",
            "compare",
            "--baseline",
            str(reference),
            "--results",
            str(results),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "relationships_f1" in result.output


@pytest.mark.cli
def test_cli_compare_json_format(cli_runner, tmp_path):
    from rhylthyme.cli.main import cli

    reference = _write(tmp_path / "four-turn.json", _summary())
    results = _write(tmp_path / "results.json", _summary())

    result = cli_runner.invoke(
        cli,
        [
            "eval-prompts",
            "compare",
            "--baseline",
            str(reference),
            "--results",
            str(results),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True


@pytest.mark.cli
def test_cli_compare_thresholds_are_flags(cli_runner, tmp_path):
    from rhylthyme.cli.main import cli

    reference = _write(tmp_path / "four-turn.json", _summary(end_to_end_pass=0.60))
    results = _write(tmp_path / "results.json", _summary(end_to_end_pass=0.55))

    args = [
        "eval-prompts",
        "compare",
        "--baseline",
        str(reference),
        "--results",
        str(results),
    ]
    assert cli_runner.invoke(cli, args).exit_code == 1
    assert cli_runner.invoke(cli, args + ["--e2e-drop", "10"]).exit_code == 0


@pytest.mark.cli
def test_cli_eval_prompts_still_works_without_a_subcommand(cli_runner, tmp_path):
    """Making eval-prompts a group must not break its own flags."""
    from rhylthyme.cli.main import cli

    result = cli_runner.invoke(
        cli, ["eval-prompts", "--gold", str(tmp_path), "--list-patterns"]
    )
    assert result.exit_code == 0, result.output
    assert "four-turn" in result.output
