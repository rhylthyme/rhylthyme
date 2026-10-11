"""
Tests for history-based duration prediction (execution history, Phase 6).

Three contracts are pinned here:

1. ``tests/fixtures/history/predict-cases.json`` is the parity fixture between
   this implementation and ``predictDurations`` in
   ``rhylthyme-server/mcp-api/history.js`` (asserted there by
   ``mcp-api/history.test.js``, which reads the same file). Any change to the
   lookup's semantics has to change the fixture, which makes the drift visible
   in both test suites at once.
2. :mod:`rhylthyme.history.predict` carries its own copy of the
   usable-run filter, because it is a byte-identical copy of
   ``rhylthyme_server/rhylthyme/predict.py`` and rhylthyme-server may not
   import this package. That copy is asserted against the real
   :mod:`rhylthyme.history.usable` over every case of
   ``usable-cases.json``.
3. The model recovers the generating law of
   ``tests/fixtures/history/thanksgiving-synth-runs.json``
   (``turkey-roast = 600 + 90 * turkeyKg`` seconds) to within 5%, which is the
   plan's Phase 6 acceptance criterion.
"""

import copy
import json
import os

import pytest

from rhylthyme.history import predict as P
from rhylthyme.history import predict_durations, predicted_seconds
from rhylthyme.history import usable as U

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "history")
PREDICT_CASES = os.path.join(FIXTURE_DIR, "predict-cases.json")
USABLE_CASES = os.path.join(FIXTURE_DIR, "usable-cases.json")
SYNTH_RUNS = os.path.join(FIXTURE_DIR, "thanksgiving-synth-runs.json")
THANKSGIVING = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "rhylthyme-server",
    "static",
    "examples",
    "thanksgiving_one_oven.json",
)

# The JavaScript option spelling the fixture uses, mapped onto the keyword
# arguments of predict_durations. Both sides read the same context object.
CAMEL_TO_SNAKE = {
    "environmentId": "environment_id",
    "userTags": "user_tags",
    "userId": "user_id",
    "programVersion": "program_version",
    "minIdentical": "min_identical",
    "minModel": "min_model",
    "corrThreshold": "corr_threshold",
    "verdicts": "verdicts",
}


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


FIXTURE = load(PREDICT_CASES)
CASES = FIXTURE["cases"]
TOLERANCE = FIXTURE.get("tolerance", 1e-6)


def case_program(case):
    if not isinstance(case["program"], str):
        return copy.deepcopy(case["program"])
    if case["program"] == "thanksgiving":
        if not os.path.isfile(THANKSGIVING):
            pytest.skip("Thanksgiving example not available in this checkout")
        return load(THANKSGIVING)
    raise AssertionError("unknown program reference %r" % case["program"])


def case_records(case):
    if case.get("recordsFixture"):
        return load(os.path.join(FIXTURE_DIR, case["recordsFixture"]))["runs"]
    return copy.deepcopy(case["records"])


def run_case(case):
    kwargs = {CAMEL_TO_SNAKE[k]: v for k, v in (case.get("context") or {}).items()}
    return predict_durations(case_program(case), case_records(case), **kwargs)


def ids(cases):
    return [c["name"] for c in cases]


# ----------------------------------------------------------------- 1. parity


def test_the_fixture_covers_the_whole_lookup():
    assert len(CASES) >= 8, "the parity corpus should exercise every basis"
    bases = set()
    for case in CASES:
        for prediction in case["expect"].values():
            bases.add((prediction["basis"], prediction.get("method")))
    assert ("identical", "median") in bases
    assert ("model", "ols") in bases
    assert ("model", "median") in bases
    assert ("none", None) in bases
    sources = {p.get("source") for c in CASES for p in c["expect"].values()}
    assert sources == {"user", "all"}, "both whose-history answers appear"


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_prediction_matches_the_fixture(case):
    got = run_case(case)
    assert sorted(got) == sorted(case["expect"]), case["name"]
    for step_id, want in case["expect"].items():
        mine = got[step_id]
        assert mine["basis"] == want["basis"], step_id
        assert mine["n"] == want["n"], step_id
        assert mine["source"] == want["source"], step_id
        assert mine.get("method") == want.get("method"), step_id
        assert mine.get("reason") == want.get("reason"), step_id
        for key in ("seconds", "low", "high"):
            if want[key] is None:
                assert mine[key] is None, (step_id, key)
            else:
                assert mine[key] == pytest.approx(want[key], abs=TOLERANCE), (
                    step_id,
                    key,
                )
        if "factors" not in want:
            assert "factors" not in mine, step_id
        else:
            assert [f["key"] for f in mine["factors"]] == [
                f["key"] for f in want["factors"]
            ], step_id
            for mine_f, want_f in zip(mine["factors"], want["factors"]):
                assert mine_f["coef"] == pytest.approx(want_f["coef"], abs=TOLERANCE), (
                    step_id,
                    want_f["key"],
                )
        if want["seconds"] is not None:
            assert mine["low"] <= mine["seconds"] <= mine["high"], step_id
    if case.get("expectEmpty"):
        assert got == {}, case["name"]


APPROX_CASES = [c for c in CASES if c.get("expectApprox")]


@pytest.mark.parametrize("case", APPROX_CASES, ids=ids(APPROX_CASES))
def test_the_generating_law_holds_independently_of_the_fixture(case):
    """The fixture also states what the truth is, not only what we computed."""
    got = run_case(case)
    for step_id, want in case["expectApprox"].items():
        tolerance = want.get("tolerance")
        if tolerance is None:
            tolerance = want["tolerancePct"] / 100.0 * want["seconds"]
        assert got[step_id]["seconds"] == pytest.approx(
            want["seconds"], abs=tolerance
        ), step_id


# -------------------------------------- 2. the inlined usable-run filter copy


@pytest.mark.parametrize("case", load(USABLE_CASES)["cases"], ids=lambda c: c["name"])
def test_the_inlined_filter_agrees_with_the_real_one(case):
    """
    predict.py is a standalone copy (see its module docstring), so its filter
    has to agree with rhylthyme.history.usable case for case.
    """
    record = case["record"]
    assert P.is_usable_run(record) == U.is_usable_run(record), case["name"]
    # is_measured_step is the step half of the filter (the run half is
    # is_usable_run), so it matches step_reason with include_fixed off.
    for step in record.get("steps") or []:
        expected = U.step_reason(step) is None
        assert P.is_measured_step(step) is expected, (case["name"], step.get("stepId"))
        assert P.step_duration(step) == U.step_duration(step)


def test_the_inlined_percentile_agrees_with_the_real_one():
    from rhylthyme.history import percentile

    for values in ([], [42], [1, 2], [5, 3, 1, 9, 7, 2, 8, 4, 6], [600.5, 601.25]):
        for q in (0.0, 0.10, 0.25, 0.5, 0.75, 0.90, 1.0):
            assert P.percentile(values, q) == percentile(values, q), (values, q)


# ------------------------------------------------ 3. the law, fitted directly


@pytest.fixture
def thanksgiving():
    if not os.path.isfile(THANKSGIVING):
        pytest.skip("Thanksgiving example not available in this checkout")
    return load(THANKSGIVING), load(SYNTH_RUNS)["runs"]


def test_ols_recovers_the_thanksgiving_law(thanksgiving):
    program, runs = thanksgiving
    # plan Phase 6 acceptance: duration = 600 + 90 * turkeyKg, so a 7 kg turkey
    # is 1230 s. (A test law, not a roasting rule.)
    predictions = predict_durations(
        program,
        runs,
        program_version=runs[0]["programVersion"],
        environment_id="home-kitchen",
        user_tags={"turkeyKg": 7, "oven": "electric"},
    )
    roast = predictions["turkey-roast"]
    assert roast["basis"] == "model"
    assert roast["method"] == "ols"
    assert roast["n"] == 20
    assert [f["key"] for f in roast["factors"]] == ["turkeyKg"]
    assert roast["seconds"] == pytest.approx(1230, rel=0.05)
    assert roast["factors"][0]["coef"] == pytest.approx(90, rel=0.25)
    assert roast["low"] < roast["seconds"] < roast["high"]
    assert (
        roast["high"] - roast["low"] < 9900
    ), "an interval wider than the plan is no forecast"

    # A step that obeys no law keeps no factor and answers with the median.
    boil = predictions["potatoes-boil"]
    assert boil["basis"] == "model"
    assert boil["method"] == "median"
    assert boil["factors"] == []

    # Fixed steps are never predicted: their observed length confirms a timer.
    assert "turkey-prep" not in predictions
    assert "stuffing-bake" not in predictions

    # And the law extrapolates the right way.
    big = predict_durations(
        program,
        runs,
        program_version=runs[0]["programVersion"],
        environment_id="home-kitchen",
        user_tags={"turkeyKg": 12, "oven": "electric"},
    )["turkey-roast"]
    assert big["seconds"] > roast["seconds"]
    assert big["seconds"] == pytest.approx(600 + 90 * 12, rel=0.10)


def test_identical_context_beats_the_model_on_the_same_corpus(thanksgiving):
    """Three runs share 7 kg in a gas oven — exactly minIdentical."""
    program, runs = thanksgiving
    predictions = predict_durations(
        program,
        runs,
        program_version=runs[0]["programVersion"],
        environment_id="home-kitchen",
        user_tags={"turkeyKg": 7, "oven": "gas"},
    )
    roast = predictions["turkey-roast"]
    assert roast["basis"] == "identical"
    assert roast["n"] == 3
    assert roast["source"] == "all"
    assert "factors" not in roast

    # All three are cook-1's own, so asking as cook-1 narrows to this kitchen.
    mine = predict_durations(
        program,
        runs,
        program_version=runs[0]["programVersion"],
        environment_id="home-kitchen",
        user_tags={"turkeyKg": 7, "oven": "gas"},
        user_id="cook-1",
    )["turkey-roast"]
    assert mine["source"] == "user"
    assert mine["n"] == 3
    assert mine["seconds"] == roast["seconds"]

    # A cook with no history of that context falls back to everyone's.
    theirs = predict_durations(
        program,
        runs,
        program_version=runs[0]["programVersion"],
        environment_id="home-kitchen",
        user_tags={"turkeyKg": 7, "oven": "gas"},
        user_id="cook-4",
    )["turkey-roast"]
    assert theirs["source"] == "all"
    assert theirs["n"] == 3


def test_the_synthetic_corpus_is_usable_history(thanksgiving):
    """Every record the fixture carries is a measurement, or the rest is moot."""
    _program, runs = thanksgiving
    from rhylthyme.history import validate_run

    assert len(runs) == 20
    for record in runs:
        assert U.is_usable_run(record) == (True, None), record["runId"]
        # context.userId is an extension of the open `context` object, so the
        # records still validate against runs_schema_0.1.0-alpha.
        assert validate_run(record) == [], record["runId"]


# -------------------------------------------------------------- 4. the knobs


def test_verdicts_accept_a_report_a_mapping_or_a_list(thanksgiving):
    program, runs = thanksgiving
    from rhylthyme.history import build_report

    report = build_report(runs, program)
    by_map = predict_durations(
        program,
        runs,
        environment_id="home-kitchen",
        verdicts={"turkey-roast": "executor-controlled"},
    )
    assert by_map["turkey-roast"]["basis"] == "none"
    assert by_map["turkey-roast"]["reason"] == "executor-controlled"
    assert by_map["turkey-roast"]["n"] == 20, "the measurements are counted, not used"

    by_rows = predict_durations(
        program,
        runs,
        environment_id="home-kitchen",
        verdicts=[{"stepId": "turkey-roast", "verdict": "executor-controlled"}],
    )
    assert by_rows["turkey-roast"]["basis"] == "none"

    # A whole Report works too; this corpus's roast is predictable, so nothing
    # is blocked and the lookup answers as usual.
    from_report = predict_durations(
        program, runs, environment_id="home-kitchen", verdicts=report
    )
    verdicts = {row.stepId: row.verdict for row in report.steps}
    assert verdicts["turkey-roast"] == "predictable"
    assert from_report["turkey-roast"]["basis"] != "none"


def test_predicted_seconds_keeps_only_usable_numbers():
    assert predicted_seconds(
        {
            "a": {"seconds": 1200.0, "basis": "identical"},
            "b": {"seconds": None, "basis": "none"},
            "c": {"basis": "none"},
            "d": "not a prediction",
        }
    ) == {"a": 1200.0}
    assert predicted_seconds(None) == {}
    assert predicted_seconds({}) == {}


def test_no_usable_history_predicts_nothing():
    program = {
        "programId": "p",
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "s",
                        "duration": {"type": "variable", "defaultSeconds": 600},
                        "startTrigger": {"type": "programStart"},
                    }
                ],
            }
        ],
    }
    step = {
        "stepId": "s",
        "planned": {"start": 0, "end": 600, "durationType": "variable"},
        "actual": {"start": 0, "end": 900},
        "endedBy": "executor",
        "pausedSeconds": 0,
    }

    def record(**over):
        base = {
            "runId": "2026-05-01T10:00:00Z-0001",
            "programId": "p",
            "programVersion": "sha256:" + "aa" * 32,
            "runtime": {
                "kind": "cli",
                "version": "t",
                "clockMode": "wall",
                "speed": 1,
            },
            "startedAt": "2026-05-01T10:00:00.000Z",
            "outcome": "completed",
            "context": {"userTags": {}},
            "steps": [copy.deepcopy(step)],
        }
        base.update(over)
        return base

    assert predict_durations(program, []) == {}
    assert predict_durations(program, None) == {}
    assert predict_durations(program, [record(outcome="abandoned")]) == {}
    assert predict_durations(program, [record(programId="elsewhere")]) == {}
    paused = record()
    paused["steps"][0]["pausedSeconds"] = 30
    assert predict_durations(program, [paused]) == {}
    timed = record()
    timed["steps"][0]["endedBy"] = "timer"
    assert predict_durations(program, [timed]) == {}
    # One usable run is enough for a median, under the model basis.
    one = predict_durations(program, [record()])["s"]
    assert one == {
        "seconds": 900.0,
        "low": 900.0,
        "high": 900.0,
        "basis": "model",
        "n": 1,
        "source": "all",
        "method": "median",
        "factors": [],
    }


def test_pearson_and_ols_edge_cases():
    assert P.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert P.pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert P.pearson([1, 1, 1], [1, 2, 3]) is None
    assert P.pearson([1], [1]) is None
    beta = P.ols_fit([[1], [2], [3], [4], [5]], [3, 5, 7, 9, 11])
    assert beta[0] == pytest.approx(1.0)
    assert beta[1] == pytest.approx(2.0)
    assert P.ols_fit([[1], [2]], [1, 2]) is None, "too few rows for slope and intercept"
    assert P.ols_fit([[1], [1], [1], [1]], [1, 2, 3, 4]) is None, "collinear"
    assert P.ols_fit([], []) is None


def test_program_reading_helpers():
    program = load(THANKSGIVING) if os.path.isfile(THANKSGIVING) else None
    if program is None:
        pytest.skip("Thanksgiving example not available in this checkout")
    assert P.declared_factor_keys(program) == ["turkeyKg", "oven"]
    assert P.declared_factor_keys({}) == []
    assert "turkey-roast" in P.program_step_ids(program)
    # Replicate instances pool back onto the authored step id.
    assert P.program_step_ids(
        {
            "tracks": [
                {
                    "steps": [
                        {"stepId": "bake-r1", "instanceOf": "bake"},
                        {"stepId": "bake-r2", "instanceOf": "bake"},
                        {"stepId": "box"},
                    ]
                }
            ]
        }
    ) == ["bake", "box"]
    assert P.record_user_id({"context": {"userId": "me"}}) == "me"
    assert P.record_user_id({"userId": "me"}) == "me"
    assert P.record_user_id({}) is None
