"""
Video timeline run records (runs schema 0.1.0-alpha, amended 2026-11-05).

A video timeline pairs a video with a program and a run record whose times
are seconds into the video: ``runtime.kind`` is ``video``,
``runtime.clockMode`` is ``media``, and each step is ended by ``annotation``
(a dataset label) or ``observer`` (a person marking while watching). A step
that never happened in the video has no ``actual``.

Three things are pinned here:

1. :func:`rhylthyme.history.validate_run` accepts such a record, and still
   rejects values the schema does not list.
2. The usable-run filter requires ``clockMode == "wall"``, so a video record
   is never a measurement: it adds nothing to duration predictions, to the
   history report, or to calibration, whatever its steps say ended them.
3. In a wall-clock run, the two new endings are not the executor either, so
   such steps are not measurements (the parity cases in
   ``fixtures/history/usable-cases.json`` pin the reason codes).
"""

import copy

import pytest

from rhylthyme.history import (
    build_report,
    is_usable_run,
    predict_durations,
    synthesize_runs,
    validate_run,
)
from rhylthyme.history.calibrate import (
    NO_MEASUREMENTS,
    PROPOSED,
    SKIPPED,
    propose_calibration,
)

pytestmark = pytest.mark.unit

AS_OF = "2026-11-05T12:00:00Z"
NEW_ENDINGS = ("annotation", "observer")


@pytest.fixture
def program():
    """A fixed, a variable and an indefinite step on one track."""
    return {
        "programId": "video-records-test",
        "name": "Video Records Test",
        "actors": 1,
        "tracks": [
            {
                "trackId": "a",
                "name": "A",
                "steps": [
                    {
                        "stepId": "first",
                        "name": "First",
                        "task": "work",
                        "startTrigger": {"type": "programStart"},
                        "duration": {"type": "fixed", "seconds": 300},
                    },
                    {
                        "stepId": "second",
                        "name": "Second",
                        "task": "work",
                        "startTrigger": {"type": "afterStep", "stepId": "first"},
                        "duration": {
                            "type": "variable",
                            "minSeconds": 500,
                            "maxSeconds": 700,
                            "defaultSeconds": 600,
                        },
                    },
                    {
                        "stepId": "third",
                        "name": "Third",
                        "task": "work",
                        "startTrigger": {"type": "afterStep", "stepId": "second"},
                        "duration": {"type": "indefinite", "defaultSeconds": 900},
                    },
                ],
            }
        ],
        "resourceConstraints": [{"task": "work", "maxConcurrent": 1}],
    }


def as_video_record(record, ended_by=None):
    """
    The same steps as a video timeline would record them: media clock, video
    runtime, every ended step ended by a label or an observer in turn (or by
    ``ended_by`` for all of them, to test the clock rule on its own).
    """
    video = copy.deepcopy(record)
    video["runtime"] = {
        "kind": "video",
        "version": "0.1.0",
        "clockMode": "media",
        "speed": 1,
    }
    video.pop("endedAt", None)
    ended = [s for s in video["steps"] if "end" in (s.get("actual") or {})]
    for i, step in enumerate(ended):
        step["endedBy"] = ended_by or NEW_ENDINGS[i % len(NEW_ENDINGS)]
    return video


@pytest.fixture
def wall_runs(program):
    return synthesize_runs(program, 6, default_cv=0.05, seed=31)


@pytest.fixture
def video_runs(program):
    """Six video records whose durations are half the plan: far from the wall runs."""
    runs = synthesize_runs(
        program,
        6,
        default_cv=0.05,
        seed=97,
        duration_fn=lambda _sid, planned, _f, _rng: planned * 0.5,
    )
    return [as_video_record(r) for r in runs]


# ------------------------------------------------------------- 1. the schema


def test_a_video_timeline_record_validates(video_runs):
    record = copy.deepcopy(video_runs[0])
    # A step that never happened in the video has no actual and no ending.
    del record["steps"][-1]["actual"]
    del record["steps"][-1]["endedBy"]
    # Dataset error tags go in the step's notes.
    record["steps"][0]["notes"] = "Measurement error: used the wrong amount"
    assert {s.get("endedBy") for s in record["steps"]} == {*NEW_ENDINGS, None}
    assert validate_run(record) == []


@pytest.mark.parametrize("ended_by", NEW_ENDINGS)
def test_the_new_endings_validate_in_a_wall_clock_run(wall_runs, ended_by):
    record = copy.deepcopy(wall_runs[0])
    for step in record["steps"]:
        step["endedBy"] = ended_by
    assert validate_run(record) == []


@pytest.mark.parametrize(
    "where, value",
    [
        ("runtime/kind", "videos"),
        ("runtime/kind", "Video"),
        ("runtime/kind", "dataset"),
        ("runtime/clockMode", "Media"),
        ("runtime/clockMode", "video"),
        ("runtime/clockMode", "frames"),
        ("steps/0/endedBy", "label"),
        ("steps/0/endedBy", "Annotation"),
        ("steps/0/endedBy", "watcher"),
    ],
)
def test_unknown_values_are_still_rejected(video_runs, where, value):
    record = copy.deepcopy(video_runs[0])
    assert validate_run(record) == [], "the unchanged record must be valid"
    *parents, key = where.split("/")
    target = record
    for part in parents:
        target = target[int(part)] if part.isdigit() else target[part]
    target[key] = value
    errors = validate_run(record)
    assert errors, f"{where} = {value!r} was accepted"
    assert all(e.startswith(where + ":") for e in errors), errors


@pytest.mark.parametrize("where", ["", "runtime", "steps/0", "steps/0/actual"])
def test_video_records_stay_closed_to_extra_properties(video_runs, where):
    """The video's URL, hash and credit belong to the timeline, not the record."""
    record = copy.deepcopy(video_runs[0])
    target = record
    for part in filter(None, where.split("/")):
        target = target[int(part)] if part.isdigit() else target[part]
    target["videoUrl"] = "https://example.com/clip.mp4"
    errors = validate_run(record)
    assert errors and all("videoUrl" in e for e in errors), errors


# --------------------------------------------------- 2. never a measurement


def test_video_records_are_not_usable_runs(video_runs):
    for record in video_runs:
        assert validate_run(record) == []
        assert is_usable_run(record) == (False, "clock-not-wall")


def test_video_records_add_nothing_to_predictions(program, wall_runs, video_runs):
    assert all(validate_run(r) == [] for r in video_runs)
    alone = predict_durations(program, wall_runs)
    assert alone, "the wall-clock runs alone predict something"
    assert predict_durations(program, wall_runs + video_runs) == alone
    assert predict_durations(program, video_runs) == {}


def test_the_clock_alone_keeps_video_records_out(program, wall_runs, video_runs):
    """Even a media record whose steps say executor is not history."""
    as_executor = [as_video_record(r, ended_by="executor") for r in video_runs]
    assert all(validate_run(r) == [] for r in as_executor)
    assert predict_durations(program, as_executor) == {}
    alone = predict_durations(program, wall_runs)
    assert predict_durations(program, wall_runs + as_executor) == alone
    proposal = propose_calibration(program, as_executor, as_of=AS_OF)
    assert proposal.runsUsable == 0
    assert proposal.proposed_steps() == []


def test_the_history_report_excludes_video_records(program, wall_runs, video_runs):
    assert all(validate_run(r) == [] for r in video_runs)
    report = build_report(wall_runs + video_runs, program)
    assert report.runsConsidered == len(wall_runs) + len(video_runs)
    assert report.runsUsable == len(wall_runs)
    excluded = {row["runId"]: row["reason"] for row in report.runsExcluded}
    assert excluded == {r["runId"]: "clock-not-wall" for r in video_runs}
    alone = {
        row.stepId: (row.n, row.median)
        for row in build_report(wall_runs, program).steps
    }
    mixed = {row.stepId: (row.n, row.median) for row in report.steps}
    assert mixed == alone


def test_calibration_ignores_video_records(program, wall_runs, video_runs):
    assert all(validate_run(r) == [] for r in video_runs)
    alone = propose_calibration(program, wall_runs, as_of=AS_OF)
    mixed = propose_calibration(program, wall_runs + video_runs, as_of=AS_OF)
    assert mixed.runsUsable == alone.runsUsable == len(wall_runs)
    assert {row["runId"] for row in mixed.runsExcluded} == {
        r["runId"] for r in video_runs
    }
    assert [(r.stepId, r.status, r.n, r.evidence) for r in mixed.steps] == [
        (r.stepId, r.status, r.n, r.evidence) for r in alone.steps
    ]
    assert any(r.status == PROPOSED for r in alone.steps)


def test_video_records_alone_propose_nothing(program, video_runs):
    assert all(validate_run(r) == [] for r in video_runs)
    proposal = propose_calibration(program, video_runs, as_of=AS_OF)
    assert proposal.runsConsidered == len(video_runs)
    assert proposal.runsUsable == 0
    # Exactly as with no history at all: every step, fixed or not, has none.
    reasons = {row.stepId: (row.status, row.reason) for row in proposal.steps}
    assert reasons == {
        step["stepId"]: (SKIPPED, NO_MEASUREMENTS)
        for step in program["tracks"][0]["steps"]
    }
    assert reasons == {
        row.stepId: (row.status, row.reason)
        for row in propose_calibration(program, [], as_of=AS_OF).steps
    }
