"""
The inferentiality report: is a program's history predictive of its durations?

Badosa et al. (2019) only fit a regression to execution history after showing
that the history is *inferential* — that the same job under the same conditions
takes a similar time. This module answers the same question per step, from run
records, so Phase 5 (calibration) and Phase 6 (prediction) know which steps are
worth modelling.

Over the usable runs of a program (see :mod:`.usable`) it computes, per
authored ``stepId`` (pooling replicate instances):

* ``n`` measurements, the planned duration, and the observed distribution
  (median, P10, P90, IQR, mean),
* the **coefficient of variation** of the observed durations — the spread
  relative to their own mean, which is what decides whether a forecast can be
  better than a guess,
* the mean **signed deviation** from the planned duration, which says whether
  the author's number is biased as well as noisy,
* a **verdict**:

  ``predictable``          n ≥ k and CV ≤ the threshold (default 0.25)
  ``executor-controlled``  n ≥ k and CV > the threshold — the person, not the
                           process, decides how long this takes; don't predict
  ``insufficient``         n < k — no verdict yet

``fixed`` steps never get a verdict: their observed duration only confirms the
timer. They get a **lag** instead — how much longer than its planned duration
the step really took, observed at the moment its successor was released:

    lag = successor.triggerFiredAt - (actual.start + planned duration)

falling back to the step's own ``actual.end`` when nothing waited on it. The
anchor is the step's *actual* start, not its planned start, so the lag is the
step's own overrun rather than the accumulated drift of everything upstream of
it; ``endDriftSeconds`` keeps the planned-start-anchored figure for callers who
want the drift. A fixed step that always overruns is a step that should have
been ``variable``; proposing that is Phase 5's business.

Roll-ups by task and by duration kind, and a program-level fraction of
predictable steps, come from the same per-step rows.

Pure computation: no I/O, no rendering. ``report_cli.py`` renders.
"""

import datetime
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .recorder import step_identity
from .usable import is_usable_run, planned_duration, step_duration, usable_steps

DEFAULT_MIN_RUNS = 5
DEFAULT_CV_THRESHOLD = 0.25

PREDICTABLE = "predictable"
EXECUTOR_CONTROLLED = "executor-controlled"
INSUFFICIENT = "insufficient"
LAG = "lag"


# ------------------------------------------------------------------ statistics


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """
    Linear-interpolated percentile of ``values`` (``q`` in 0..1).

    The same definition as NumPy's default and as ``durationStats`` in
    ``mcp-api/history.js``: position ``q * (n - 1)`` in the sorted sample.
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def duration_stats(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """
    ``{n, median, p10, p90, iqr, mean, cv}`` for a sample of durations.

    ``cv`` is the sample standard deviation (n − 1 denominator) over the mean,
    and is ``None`` when it is undefined (fewer than two values, or a mean of
    zero). The JavaScript twin is ``durationStats`` in ``mcp-api/history.js``.
    """
    nums = [float(v) for v in values if v is not None]
    n = len(nums)
    if n == 0:
        return {
            "n": 0,
            "median": None,
            "p10": None,
            "p90": None,
            "iqr": None,
            "mean": None,
            "cv": None,
        }
    mean = sum(nums) / n
    if n > 1:
        variance = sum((v - mean) ** 2 for v in nums) / (n - 1)
        stdev = math.sqrt(variance)
        cv = (stdev / mean) if mean else None
    else:
        cv = None
    p75 = percentile(nums, 0.75)
    p25 = percentile(nums, 0.25)
    assert p75 is not None and p25 is not None  # n >= 1 here
    return {
        "n": n,
        "median": percentile(nums, 0.5),
        "p10": percentile(nums, 0.10),
        "p90": percentile(nums, 0.90),
        "iqr": p75 - p25,
        "mean": mean,
        "cv": cv,
    }


# ----------------------------------------------------------------- data model


@dataclass
class StepReport:
    stepId: str
    durationType: Optional[str] = None
    task: Optional[str] = None
    n: int = 0
    plannedSeconds: Optional[float] = None
    median: Optional[float] = None
    p10: Optional[float] = None
    p90: Optional[float] = None
    iqr: Optional[float] = None
    mean: Optional[float] = None
    cv: Optional[float] = None
    meanDeviation: Optional[float] = None
    verdict: str = INSUFFICIENT
    lagSeconds: Optional[float] = None
    endDriftSeconds: Optional[float] = None
    lagN: int = 0
    excluded: Dict[str, int] = field(default_factory=dict)


@dataclass
class RollUp:
    key: str
    steps: int = 0
    n: int = 0
    medianCv: Optional[float] = None
    predictable: int = 0
    executorControlled: int = 0
    insufficient: int = 0
    lag: int = 0


@dataclass
class Report:
    programId: Optional[str] = None
    minRuns: int = DEFAULT_MIN_RUNS
    cvThreshold: float = DEFAULT_CV_THRESHOLD
    runsConsidered: int = 0
    runsUsable: int = 0
    runsExcluded: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[StepReport] = field(default_factory=list)
    byTask: List[RollUp] = field(default_factory=list)
    byDurationKind: List[RollUp] = field(default_factory=list)
    stepsWithVerdict: int = 0
    stepsPredictable: int = 0
    predictableFraction: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------- helpers


def _task_map(program: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """``{stepId: task}`` from an authored program, ``{}`` when none is given."""
    tasks: Dict[str, str] = {}
    if not isinstance(program, dict):
        return tasks
    for track in program.get("tracks", []) or []:
        for step in track.get("steps", []) or []:
            sid = step.get("stepId")
            if sid and step.get("task"):
                tasks[str(sid)] = str(step["task"])
    return tasks


def _authored_ids(record: Dict[str, Any]) -> set:
    return {s.get("stepId") for s in record.get("steps") or [] if s.get("stepId")}


def _successor_triggers(record: Dict[str, Any]) -> Dict[str, List[float]]:
    """
    ``{stepId: [triggerFiredAt of successors that waited on it alone]}``.

    Only successors with a single predecessor are collected: a step that waits
    on four others fires when the *last* of them ends, which says nothing about
    any one of them. ``waitedOn`` holds runtime ids (replicate suffix included);
    they are mapped back to authored ids with the rule the recorder used.
    """
    authored = _authored_ids(record)
    out: Dict[str, List[float]] = {}
    for step in record.get("steps") or []:
        fired = step.get("triggerFiredAt")
        waited = step.get("waitedOn") or []
        if fired is None or len(waited) != 1:
            continue
        base, _instance = step_identity({"stepId": waited[0]}, authored)
        out.setdefault(base, []).append(float(fired))
    return out


def parse_since(value: Any) -> Optional[datetime.datetime]:
    """Parse ``--since`` (a date or an ISO date-time) into an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        dt = datetime.datetime.combine(value, datetime.time.min)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            dt = datetime.datetime.fromisoformat(text + "T00:00:00+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def filter_since(records: Sequence[Dict[str, Any]], since: Any) -> List[Dict[str, Any]]:
    """Records whose ``startedAt`` is at or after ``since`` (all when None)."""
    cutoff = parse_since(since)
    if cutoff is None:
        return list(records)
    kept = []
    for record in records:
        started = record.get("startedAt")
        if not started:
            continue
        try:
            when = parse_since(started)
        except ValueError:
            continue
        if when is not None and when >= cutoff:
            kept.append(record)
    return kept


# -------------------------------------------------------------------- builder


def build_report(
    records: Sequence[Dict[str, Any]],
    program: Optional[Dict[str, Any]] = None,
    k: int = DEFAULT_MIN_RUNS,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
    since: Any = None,
) -> Report:
    """
    Build the inferentiality report for ``records`` (runs of one program).

    ``program`` is the authored program, used only for the ``task`` of each
    step (the roll-up by step type); everything else comes from the records.
    """
    records = filter_since(records, since)
    report = Report(minRuns=int(k), cvThreshold=float(cv_threshold))
    report.runsConsidered = len(records)
    if records:
        report.programId = records[0].get("programId")
    if isinstance(program, dict) and program.get("programId"):
        report.programId = program["programId"]

    tasks = _task_map(program)

    # stepId -> collected measurements
    observed: Dict[str, List[float]] = {}
    planned_by_step: Dict[str, List[float]] = {}
    kinds: Dict[str, Optional[str]] = {}
    excluded: Dict[str, Dict[str, int]] = {}
    lags: Dict[str, List[float]] = {}
    drifts: Dict[str, List[float]] = {}
    order: List[str] = []

    usable_records = []
    for record in records:
        ok, reason = is_usable_run(record)
        if not ok:
            report.runsExcluded.append({"runId": record.get("runId"), "reason": reason})
            continue
        usable_records.append(record)
    report.runsUsable = len(usable_records)

    for record in usable_records:
        successors = _successor_triggers(record)
        for step, reason in usable_steps(record, include_fixed=True):
            sid = step.get("stepId")
            if not sid:
                continue
            if sid not in kinds:
                order.append(sid)
                kinds[sid] = (step.get("planned") or {}).get("durationType")
                excluded[sid] = {}
                observed[sid] = []
                planned_by_step[sid] = []
                lags[sid] = []
                drifts[sid] = []
            plan = planned_duration(step)
            if plan is not None:
                planned_by_step[sid].append(plan)
            if reason is not None:
                excluded[sid][reason] = excluded[sid].get(reason, 0) + 1
                continue
            if kinds[sid] == "fixed":
                planned_end = (step.get("planned") or {}).get("end")
                actual = step.get("actual") or {}
                released = successors.get(sid)
                observed_end = min(released) if released else actual.get("end")
                if observed_end is not None:
                    if plan is not None and actual.get("start") is not None:
                        lags[sid].append(
                            float(observed_end) - (float(actual["start"]) + plan)
                        )
                    if planned_end is not None:
                        drifts[sid].append(float(observed_end) - float(planned_end))
                continue
            duration = step_duration(step)
            if duration is not None:
                observed[sid].append(duration)

    for sid in order:
        kind = kinds[sid]
        plan_values = planned_by_step[sid]
        plan_median = percentile(plan_values, 0.5) if plan_values else None
        row = StepReport(
            stepId=sid,
            durationType=kind,
            task=tasks.get(sid),
            plannedSeconds=plan_median,
            excluded=dict(excluded[sid]),
        )
        if kind == "fixed":
            row.verdict = LAG
            row.lagN = len(lags[sid])
            row.lagSeconds = sum(lags[sid]) / len(lags[sid]) if lags[sid] else None
            row.endDriftSeconds = (
                sum(drifts[sid]) / len(drifts[sid]) if drifts[sid] else None
            )
            row.n = row.lagN
        else:
            stats = duration_stats(observed[sid])
            row.n = int(stats["n"] or 0)
            row.median = stats["median"]
            row.p10 = stats["p10"]
            row.p90 = stats["p90"]
            row.iqr = stats["iqr"]
            row.mean = stats["mean"]
            row.cv = stats["cv"]
            if row.mean is not None and plan_median is not None:
                row.meanDeviation = row.mean - plan_median
            if row.n < report.minRuns or row.cv is None:
                row.verdict = INSUFFICIENT
            elif row.cv <= report.cvThreshold:
                row.verdict = PREDICTABLE
            else:
                row.verdict = EXECUTOR_CONTROLLED
        report.steps.append(row)

    report.byTask = _rollup(report.steps, lambda r: r.task or "(no task)")
    report.byDurationKind = _rollup(
        report.steps, lambda r: r.durationType or "(unknown)"
    )

    verdict_rows = [r for r in report.steps if r.verdict != LAG]
    report.stepsWithVerdict = len(verdict_rows)
    report.stepsPredictable = sum(1 for r in verdict_rows if r.verdict == PREDICTABLE)
    if verdict_rows:
        report.predictableFraction = report.stepsPredictable / len(verdict_rows)
    return report


def _rollup(rows: Sequence[StepReport], key_of) -> List[RollUp]:
    groups: Dict[str, RollUp] = {}
    cvs: Dict[str, List[float]] = {}
    for row in rows:
        key = key_of(row)
        group = groups.setdefault(key, RollUp(key=key))
        cvs.setdefault(key, [])
        group.steps += 1
        group.n += row.n
        if row.cv is not None:
            cvs[key].append(row.cv)
        if row.verdict == PREDICTABLE:
            group.predictable += 1
        elif row.verdict == EXECUTOR_CONTROLLED:
            group.executorControlled += 1
        elif row.verdict == LAG:
            group.lag += 1
        else:
            group.insufficient += 1
    for key, group in groups.items():
        group.medianCv = percentile(cvs[key], 0.5) if cvs[key] else None
    return [groups[k] for k in sorted(groups)]


__all__ = [
    "build_report",
    "duration_stats",
    "percentile",
    "filter_since",
    "parse_since",
    "Report",
    "StepReport",
    "RollUp",
    "DEFAULT_MIN_RUNS",
    "DEFAULT_CV_THRESHOLD",
    "PREDICTABLE",
    "EXECUTOR_CONTROLLED",
    "INSUFFICIENT",
    "LAG",
]
