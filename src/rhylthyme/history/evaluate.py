"""
Held-out evaluation: does prediction actually beat the author's guess?

Phase 6 made ``analyze_schedule`` report a predicted duration; Phase 7 lets a
negative offset fire from one. Neither is worth anything unless the prediction
is closer to what happens than the number the author typed, so this module
answers PRD §8's last acceptance criterion with the standard measurement:
**hold out the most recent runs, predict them from the rest, and compare the
mean absolute error of the prediction against the mean absolute error of the
planned ``defaultSeconds``.**

The split is chronological, not random. Prediction is a forecast, so training
on runs that happened *after* the run being predicted would flatter it; the
latest 20 % of a program's usable runs (at least one) are held out and the
earlier ones are the training set.

Every held-out run is predicted *in its own context*: its ``userTags``, its
``environmentId``, its ``programVersion`` and its owner are what
:func:`~.predict.predict_durations` is asked about, so the identical-context
branch is exercised exactly as it would be at run time.

Per authored step, over the held-out measurements that have both a prediction
and a planned value:

* ``maePredicted``  mean ``|predicted − actual|``
* ``maePlanned``    mean ``|planned − actual|``
* ``improvement``   ``1 − maePredicted / maePlanned`` — positive when the
  forecast is better than the guess, negative when it is worse
* ``basisCounts``   how often each lookup branch answered
  (``identical`` / ``model`` / ``none``)

The comparison is over the *same* observations on both sides: a measurement
with no prediction contributes to neither mean (it is counted in
``basisCounts`` under ``none`` instead), so ``improvement`` is never the
artefact of a prediction that quietly declined the hard cases.

Rows are cross-referenced with the inferentiality verdicts of
:mod:`.report`, computed on the *training* runs. The verdicts are only an
annotation here — they are deliberately NOT passed to the predictor, because
the point of the table is to show the contrast between the step types the
report called ``predictable`` and the ones it called ``executor-controlled``.
The acceptance claim is read off the predictable rows only:

    predicted beats planned on every step type with a predictable step

``fixed`` steps are not evaluated at all: their observed duration only
confirms their timer, so there is nothing to forecast (:mod:`.usable`).

Pure computation, no I/O and no formatting; ``evaluate_cli.py`` renders.
"""

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .predict import (
    DEFAULT_CORR_THRESHOLD,
    DEFAULT_MIN_IDENTICAL,
    DEFAULT_MIN_MODEL,
    predict_durations,
    program_step_ids,
    record_user_id,
)
from .report import (
    DEFAULT_CV_THRESHOLD,
    DEFAULT_MIN_RUNS,
    INSUFFICIENT,
    PREDICTABLE,
    build_report,
    filter_since,
    percentile,
)
from .usable import is_usable_run, measured_steps, planned_duration, step_duration

DEFAULT_HOLDOUT = 0.2
DEFAULT_MIN_TOTAL_RUNS = 5

BASIS_ORDER = ("identical", "model", "none")

# Why a program could not be evaluated.
REASON_NO_USABLE_RUNS = "no-usable-runs"
REASON_TOO_FEW_RUNS = "too-few-runs"
REASON_NO_OBSERVATIONS = "no-measured-steps-held-out"


def _r3(value: Any) -> Optional[float]:
    """Round to 3 decimals so a committed JSON report is byte-stable."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 3)


def _mae(errors: Sequence[float]) -> Optional[float]:
    return (sum(errors) / len(errors)) if errors else None


def _improvement(predicted: Optional[float], planned: Optional[float]):
    """``1 - predicted/planned``, or None when there is nothing to divide by."""
    if predicted is None or planned is None or planned == 0:
        return None
    return 1.0 - (predicted / planned)


# ----------------------------------------------------------------- data model


@dataclass
class StepEvaluation:
    stepId: str
    durationType: Optional[str] = None
    task: Optional[str] = None
    verdict: str = INSUFFICIENT
    n: int = 0
    observations: int = 0
    plannedSeconds: Optional[float] = None
    medianActual: Optional[float] = None
    maePredicted: Optional[float] = None
    maePlanned: Optional[float] = None
    improvement: Optional[float] = None
    basisCounts: Dict[str, int] = field(default_factory=dict)


@dataclass
class GroupEvaluation:
    key: str
    steps: int = 0
    n: int = 0
    maePredicted: Optional[float] = None
    maePlanned: Optional[float] = None
    improvement: Optional[float] = None
    predictableSteps: int = 0
    predictableN: int = 0
    predictableMaePredicted: Optional[float] = None
    predictableMaePlanned: Optional[float] = None
    predictableImprovement: Optional[float] = None


@dataclass
class Evaluation:
    programId: Optional[str] = None
    holdout: float = DEFAULT_HOLDOUT
    minRuns: int = DEFAULT_MIN_TOTAL_RUNS
    verdictMinRuns: int = DEFAULT_MIN_RUNS
    cvThreshold: float = DEFAULT_CV_THRESHOLD
    runsConsidered: int = 0
    runsUsable: int = 0
    runsTrain: int = 0
    runsHeldOut: int = 0
    heldOutRunIds: List[str] = field(default_factory=list)
    observations: int = 0
    n: int = 0
    maePredicted: Optional[float] = None
    maePlanned: Optional[float] = None
    improvement: Optional[float] = None
    basisCounts: Dict[str, int] = field(default_factory=dict)
    steps: List[StepEvaluation] = field(default_factory=list)
    byTask: List[GroupEvaluation] = field(default_factory=list)
    byDurationKind: List[GroupEvaluation] = field(default_factory=list)
    claim: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -------------------------------------------------------------------- helpers


def sort_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Records oldest first, by ``startedAt`` then ``runId`` (stable, total)."""
    return sorted(
        records,
        key=lambda r: (str(r.get("startedAt") or ""), str(r.get("runId") or "")),
    )


def split_chronologically(
    records: Sequence[Dict[str, Any]], holdout: float = DEFAULT_HOLDOUT
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    ``(train, held_out)`` — the latest ``holdout`` share of ``records``.

    At least one run is held out and at least one is kept for training, so a
    two-run corpus splits 1/1. ``holdout`` outside ``(0, 1)`` is clamped.
    """
    ordered = sort_records(records)
    n = len(ordered)
    if n < 2:
        return list(ordered), []
    share = float(holdout)
    if not math.isfinite(share) or share <= 0:
        share = DEFAULT_HOLDOUT
    share = min(share, 1.0)
    count = int(round(n * share))
    count = max(1, min(count, n - 1))
    return ordered[: n - count], ordered[n - count :]


def program_stub_from_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    A stand-in program for ``--all``, when no program file was given.

    Prediction needs a program for the ``programId``, the declared variance
    factors and the implicit serves/actors context. A run record's ``context``
    is a snapshot of ``metadata``, so all three can be recovered from it; step
    ids cannot, which only means the prediction is not restricted to the
    program's current steps.
    """
    stub: Dict[str, Any] = {"programId": None, "metadata": {}}
    for record in records:
        if not isinstance(record, dict):
            continue
        if stub["programId"] is None and record.get("programId"):
            stub["programId"] = record["programId"]
        context = record.get("context")
        if not isinstance(context, dict):
            continue
        if isinstance(context.get("varianceFactors"), list) and not stub[
            "metadata"
        ].get("varianceFactors"):
            stub["metadata"]["varianceFactors"] = context["varianceFactors"]
        if context.get("serves") is not None and "serves" not in stub["metadata"]:
            stub["metadata"]["serves"] = context["serves"]
        if context.get("actors") is not None and stub.get("actors") is None:
            stub["actors"] = context["actors"]
    return stub


def _task_map(program: Optional[Dict[str, Any]]) -> Dict[str, str]:
    tasks: Dict[str, str] = {}
    if not isinstance(program, dict):
        return tasks
    for track in program.get("tracks", []) or []:
        for step in track.get("steps", []) or []:
            sid = step.get("stepId")
            if sid and step.get("task"):
                tasks[str(sid)] = str(step["task"])
    return tasks


def _planned_seconds(step: Dict[str, Any]) -> Optional[float]:
    """
    The author's expected duration of ``step``, as frozen in the record.

    ``planned.defaultSeconds`` is the author's own number for a variable or
    indefinite step; the planned interval is the fallback (and is the same
    number whenever the plan was computed from it).
    """
    planned = step.get("planned") or {}
    for key in ("defaultSeconds", "seconds"):
        value = planned.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return planned_duration(step)


def _empty_counts() -> Dict[str, int]:
    return {basis: 0 for basis in BASIS_ORDER}


def _bump(counts: Dict[str, int], basis: Optional[str]) -> None:
    key = basis if basis in BASIS_ORDER else "none"
    counts[key] = counts.get(key, 0) + 1


# -------------------------------------------------------------------- builder


def evaluate_program(
    records: Sequence[Dict[str, Any]],
    program: Optional[Dict[str, Any]] = None,
    *,
    holdout: float = DEFAULT_HOLDOUT,
    min_runs: int = DEFAULT_MIN_TOTAL_RUNS,
    verdict_min_runs: int = DEFAULT_MIN_RUNS,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
    min_identical: int = DEFAULT_MIN_IDENTICAL,
    min_model: int = DEFAULT_MIN_MODEL,
    corr_threshold: float = DEFAULT_CORR_THRESHOLD,
    since: Any = None,
) -> Evaluation:
    """
    Evaluate prediction against the plan on the held-out runs of one program.

    ``records`` are runs of a single program (foreign ones are ignored by the
    predictor anyway). ``program`` is the authored program; it supplies each
    step's ``task`` for the roll-up and the declared factors for the lookup,
    and a stand-in is reconstructed from the records when it is missing.
    """
    records = filter_since(records, since)
    stub = program if isinstance(program, dict) else program_stub_from_records(records)
    result = Evaluation(
        programId=stub.get("programId")
        or (records[0].get("programId") if records else None),
        holdout=float(holdout),
        minRuns=int(min_runs),
        verdictMinRuns=int(verdict_min_runs),
        cvThreshold=float(cv_threshold),
        runsConsidered=len(records),
        basisCounts=_empty_counts(),
    )

    usable = [r for r in records if isinstance(r, dict) and is_usable_run(r)[0]]
    result.runsUsable = len(usable)
    if not usable:
        result.reason = REASON_NO_USABLE_RUNS
        result.claim = {"stepTypes": [], "holds": None, "failures": []}
        return result
    if len(usable) < int(min_runs):
        result.reason = REASON_TOO_FEW_RUNS
        result.claim = {"stepTypes": [], "holds": None, "failures": []}
        return result

    train, held = split_chronologically(usable, holdout)
    result.runsTrain = len(train)
    result.runsHeldOut = len(held)
    result.heldOutRunIds = [str(r.get("runId")) for r in held]

    report = build_report(
        train,
        program if isinstance(program, dict) else None,
        k=verdict_min_runs,
        cv_threshold=cv_threshold,
    )
    verdicts = {row.stepId: row.verdict for row in report.steps}
    kinds = {row.stepId: row.durationType for row in report.steps}

    tasks = _task_map(program)

    # stepId -> collected per-observation numbers
    errors_pred: Dict[str, List[float]] = {}
    errors_plan: Dict[str, List[float]] = {}
    actuals: Dict[str, List[float]] = {}
    planned_values: Dict[str, List[float]] = {}
    counts: Dict[str, Dict[str, int]] = {}
    observed: Dict[str, int] = {}
    seen_order: List[str] = []

    for run in held:
        predictions = predict_durations(
            stub,
            train,
            environment_id=run.get("environmentId"),
            user_tags=(run.get("context") or {}).get("userTags") or {},
            user_id=record_user_id(run),
            program_version=run.get("programVersion"),
            min_identical=min_identical,
            min_model=min_model,
            corr_threshold=corr_threshold,
        )
        for step in measured_steps(run):
            sid = step.get("stepId")
            if not sid:
                continue
            if sid not in counts:
                seen_order.append(sid)
                counts[sid] = _empty_counts()
                errors_pred[sid] = []
                errors_plan[sid] = []
                actuals[sid] = []
                planned_values[sid] = []
                observed[sid] = 0
                if sid not in kinds:
                    kinds[sid] = (step.get("planned") or {}).get("durationType")
            observed[sid] += 1
            result.observations += 1
            actual = step_duration(step)
            planned = _planned_seconds(step)
            if actual is not None:
                actuals[sid].append(actual)
            if planned is not None:
                planned_values[sid].append(planned)

            prediction = predictions.get(sid) or {}
            basis = prediction.get("basis")
            seconds = prediction.get("seconds")
            usable_prediction = (
                basis not in (None, "none")
                and seconds is not None
                and actual is not None
                and planned is not None
            )
            _bump(counts[sid], basis if usable_prediction else "none")
            _bump(result.basisCounts, basis if usable_prediction else "none")
            if not usable_prediction:
                continue
            assert seconds is not None and actual is not None and planned is not None
            errors_pred[sid].append(abs(float(seconds) - actual))
            errors_plan[sid].append(abs(float(planned) - actual))

    order = [sid for sid in program_step_ids(stub) if sid in counts]
    order += [sid for sid in seen_order if sid not in order]

    for sid in order:
        row = StepEvaluation(
            stepId=sid,
            durationType=kinds.get(sid),
            task=tasks.get(sid),
            verdict=verdicts.get(sid, INSUFFICIENT),
            n=len(errors_pred[sid]),
            observations=observed[sid],
            plannedSeconds=_r3(
                percentile(planned_values[sid], 0.5) if planned_values[sid] else None
            ),
            medianActual=_r3(percentile(actuals[sid], 0.5) if actuals[sid] else None),
            basisCounts={k: counts[sid].get(k, 0) for k in BASIS_ORDER},
        )
        mae_pred, mae_plan = _mae(errors_pred[sid]), _mae(errors_plan[sid])
        row.maePredicted = _r3(mae_pred)
        row.maePlanned = _r3(mae_plan)
        row.improvement = _r3(_improvement(mae_pred, mae_plan))
        result.steps.append(row)

    all_pred = [e for sid in order for e in errors_pred[sid]]
    all_plan = [e for sid in order for e in errors_plan[sid]]
    result.n = len(all_pred)
    result.maePredicted = _r3(_mae(all_pred))
    result.maePlanned = _r3(_mae(all_plan))
    result.improvement = _r3(_improvement(_mae(all_pred), _mae(all_plan)))

    result.byTask = _rollup(
        result.steps, errors_pred, errors_plan, lambda r: r.task or "(no task)"
    )
    result.byDurationKind = _rollup(
        result.steps, errors_pred, errors_plan, lambda r: r.durationType or "(unknown)"
    )
    result.claim = _claim(result.byTask)
    if not result.observations:
        result.reason = REASON_NO_OBSERVATIONS
    return result


def _rollup(
    rows: Sequence[StepEvaluation],
    errors_pred: Dict[str, List[float]],
    errors_plan: Dict[str, List[float]],
    key_of,
) -> List[GroupEvaluation]:
    groups: Dict[str, GroupEvaluation] = {}
    pooled: Dict[str, Tuple[List[float], List[float], List[float], List[float]]] = {}
    for row in rows:
        key = key_of(row)
        group = groups.setdefault(key, GroupEvaluation(key=key))
        buckets = pooled.setdefault(key, ([], [], [], []))
        group.steps += 1
        group.n += row.n
        buckets[0].extend(errors_pred.get(row.stepId, []))
        buckets[1].extend(errors_plan.get(row.stepId, []))
        if row.verdict == PREDICTABLE:
            group.predictableSteps += 1
            group.predictableN += row.n
            buckets[2].extend(errors_pred.get(row.stepId, []))
            buckets[3].extend(errors_plan.get(row.stepId, []))
    for key, group in groups.items():
        pred, plan, ppred, pplan = pooled[key]
        group.maePredicted = _r3(_mae(pred))
        group.maePlanned = _r3(_mae(plan))
        group.improvement = _r3(_improvement(_mae(pred), _mae(plan)))
        group.predictableMaePredicted = _r3(_mae(ppred))
        group.predictableMaePlanned = _r3(_mae(pplan))
        group.predictableImprovement = _r3(_improvement(_mae(ppred), _mae(pplan)))
    return [groups[k] for k in sorted(groups)]


def _claim(groups: Sequence[GroupEvaluation]) -> Dict[str, Any]:
    """
    The acceptance claim of PRD §8: *predicted beats planned in mean absolute
    error for at least the step types the inferentiality report marked
    predictable*.

    ``holds`` is ``None`` when no step type has a predictable step with a
    held-out measurement — the claim is then untested, not true.
    """
    tested, failures = [], []
    for group in groups:
        if not group.predictableN or group.predictableMaePlanned is None:
            continue
        tested.append(group.key)
        if (
            group.predictableMaePredicted is None
            or group.predictableMaePredicted >= group.predictableMaePlanned
        ):
            failures.append(group.key)
    return {
        "stepTypes": tested,
        "holds": (len(failures) == 0) if tested else None,
        "failures": failures,
    }


def group_by_program(
    records: Sequence[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """``{programId: records}``, for ``runs evaluate --all``."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        if isinstance(record, dict):
            grouped.setdefault(str(record.get("programId")), []).append(record)
    return grouped


def evaluate_all(records: Sequence[Dict[str, Any]], **kwargs: Any) -> List[Evaluation]:
    """Evaluate every program present in ``records``, by programId."""
    return [
        evaluate_program(group, None, **kwargs)
        for _pid, group in sorted(group_by_program(records).items())
    ]


__all__ = [
    "evaluate_program",
    "evaluate_all",
    "group_by_program",
    "split_chronologically",
    "sort_records",
    "program_stub_from_records",
    "Evaluation",
    "StepEvaluation",
    "GroupEvaluation",
    "DEFAULT_HOLDOUT",
    "DEFAULT_MIN_TOTAL_RUNS",
    "BASIS_ORDER",
    "REASON_NO_USABLE_RUNS",
    "REASON_TOO_FEW_RUNS",
    "REASON_NO_OBSERVATIONS",
]
