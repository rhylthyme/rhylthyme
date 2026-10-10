"""
Calibration: propose durations from recorded runs, and write accepted ones.

Phase 2 of the execution-history PRD (§6). The plan's durable rule is that
**calibration never writes**: :func:`propose_calibration` is a pure function of
a program and its run records that returns a *proposal* — evidence and a
suggested value per step — and nothing else. Writing is a separate, explicit
step (:func:`apply_calibration`) driven by the author's acceptance, and it
returns a new program rather than mutating the one it was given.

For each step, over the *usable* measurements of the usable runs (see
:mod:`.usable`, applied by :func:`.report.build_report`):

``variable``
    ``defaultSeconds`` <- the observed **median**; ``minSeconds``/``maxSeconds``
    <- the observed **P10/P90**, *widened* to include whatever the author
    already wrote. A proposed range is never narrower than the author's on
    either side — that is the one hard guarantee of this module, and it holds
    after rounding because the lower bound is floored and the upper bound
    ceiled. The range is also widened to contain the new default and any
    ``optimalSeconds`` the author set, so the result stays internally
    consistent.

``indefinite``
    ``defaultSeconds`` <- the median. An indefinite step has no authored range
    and gets no proposed one unless the caller asks
    (``range_for_indefinite=True``), because its end is the executor's decision,
    not a bound the plan should pretend to know.

``fixed``
    Never a value change. A fixed step's observed duration only confirms its
    timer, so history can only say how *late* its planned end really was: the
    **lag** from :func:`.report.build_report`. When the lag clears a threshold
    (10 % of the planned duration, floored at 60 s) the step gets a
    ``"consider variable"`` note carrying that lag — a fixed step that always
    overruns is a step the author modelled wrongly, and only the author can
    decide that.

Fewer than ``k`` measurements (default 5) means no proposal: the step is
``skipped`` with a reason code, with whatever statistics exist attached so a UI
can still show them.

There is deliberately **no CV gate**. A step the inferentiality report calls
``executor-controlled`` still gets a proposal — its median is a better guess
than the author's invention even when the spread is wide — but the report's
verdict travels in the evidence so the caller can warn instead of silently
accepting. ``rhylthyme runs report`` decides predictability; ``calibrate``
decides the number.

The proposal also carries the **effect if accepted**: the planned makespan and
critical path before and after applying every proposal, and the per-step start
and end shifts, computed with the reference timing resolver over the expanded
program.

:meth:`CalibrationProposal.to_dict` is JSON-serialisable and is exactly what the
``calibrate_program`` MCP tool returns and what the web editor's calibrate panel
renders; both are thin wrappers over this module.
"""

import copy
import datetime
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .hash import program_version
from .report import DEFAULT_MIN_RUNS, LAG, build_report, filter_since
from .usable import is_usable_run

DEFAULT_LOW_PCT = 10
DEFAULT_HIGH_PCT = 90

#: A fixed step's lag must exceed ``max(LAG_FLOOR, LAG_FRACTION * planned)``
#: before it is worth telling the author their fixed duration is wrong.
DEFAULT_LAG_FRACTION = 0.10
DEFAULT_LAG_FLOOR_SECONDS = 60.0

# Proposal statuses
PROPOSED = "proposed"
SKIPPED = "skipped"
NOTE = "note"

# Skip reasons
INSUFFICIENT_RUNS = "insufficient-runs"
FIXED_DURATION = "fixed-duration"
NO_MEASUREMENTS = "no-measurements"
NOT_IN_PROGRAM = "not-in-program"
NOT_A_DURATION_OBJECT = "not-a-duration-object"

NOTE_CONSIDER_VARIABLE = "consider variable"

#: Duration keys a proposal may write, in the order they are rendered.
VALUE_KEYS = ("seconds", "minSeconds", "defaultSeconds", "maxSeconds")


# --------------------------------------------------------------- small helpers


def _seconds(value: Any) -> Optional[float]:
    """A duration field (number or time string like ``"5m"``) as seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    from ..validate_program import parse_time_string_to_seconds

    try:
        return float(parse_time_string_to_seconds(value))
    except (TypeError, ValueError):
        return None


def _round_seconds(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value))


def _floor_seconds(value: Optional[float]) -> Optional[int]:
    """Round a lower bound *down*, so rounding can never narrow a range."""
    return None if value is None else int(math.floor(value))


def _ceil_seconds(value: Optional[float]) -> Optional[int]:
    """Round an upper bound *up*, so rounding can never narrow a range."""
    return None if value is None else int(math.ceil(value))


def _duration_kind(duration: Any) -> Optional[str]:
    if isinstance(duration, dict):
        return str(duration.get("type", "fixed"))
    if duration is None:
        return None
    return "fixed"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _authored_steps(program: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every authored step of ``program``, in track then step order."""
    steps: List[Dict[str, Any]] = []
    if not isinstance(program, dict):
        return steps
    for track in program.get("tracks") or []:
        for step in track.get("steps") or []:
            if isinstance(step, dict) and step.get("stepId"):
                steps.append(step)
    return steps


def current_values(duration: Any) -> Dict[str, Any]:
    """
    The author's duration as plain seconds: ``{type, seconds?, minSeconds?, …}``.

    A number or time string is reported as ``{"type": "fixed", "seconds": n}``,
    which is how the timing resolver reads it.
    """
    kind = _duration_kind(duration)
    out: Dict[str, Any] = {"type": kind}
    if not isinstance(duration, dict):
        value = _seconds(duration)
        if value is not None:
            out["seconds"] = value
        return out
    for key in (
        "seconds",
        "minSeconds",
        "maxSeconds",
        "defaultSeconds",
        "optimalSeconds",
    ):
        if duration.get(key) is not None:
            out[key] = _seconds(duration[key])
    if isinstance(duration.get("calibratedFrom"), dict):
        out["calibratedFrom"] = copy.deepcopy(duration["calibratedFrom"])
    return out


def effective_seconds(duration: Any) -> Optional[float]:
    """The single number the timing resolver uses for ``duration``."""
    from ..validate_program import parse_duration_to_seconds

    try:
        return float(parse_duration_to_seconds(duration))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ data model


@dataclass
class StepProposal:
    """One row of a proposal: the evidence, and the value it implies."""

    stepId: str
    durationType: Optional[str] = None
    task: Optional[str] = None
    status: str = SKIPPED
    reason: Optional[str] = None
    note: Optional[str] = None
    verdict: Optional[str] = None
    n: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def proposed(self) -> Dict[str, Any]:
        value = self.evidence.get("proposed")
        return value if isinstance(value, dict) else {}

    @property
    def current(self) -> Dict[str, Any]:
        value = self.evidence.get("current")
        return value if isinstance(value, dict) else {}

    @property
    def delta_seconds(self) -> Optional[float]:
        return self.evidence.get("deltaSeconds")


@dataclass
class CalibrationProposal:
    """
    A whole-program proposal. Nothing here has been written anywhere.

    ``programVersion`` hashes the program that was passed in; ``runsProgramVersion``
    hashes the program the measurements were taken against, and is what
    :func:`apply_calibration` records in ``calibratedFrom`` — the provenance
    question is "which plan produced these numbers", not "which plan received
    them".
    """

    programId: Optional[str] = None
    programVersion: Optional[str] = None
    runsProgramVersion: Optional[str] = None
    asOf: str = ""
    minRuns: int = DEFAULT_MIN_RUNS
    lowPercentile: float = DEFAULT_LOW_PCT
    highPercentile: float = DEFAULT_HIGH_PCT
    since: Optional[str] = None
    runsConsidered: int = 0
    runsUsable: int = 0
    runsExcluded: List[Dict[str, Any]] = field(default_factory=list)
    programVersionsSeen: List[str] = field(default_factory=list)
    steps: List[StepProposal] = field(default_factory=list)
    effect: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form; the payload of the ``calibrate_program`` tool."""
        return asdict(self)

    # -- convenience for callers (CLI, MCP tool, editor) ---------------------

    def by_step(self) -> Dict[str, StepProposal]:
        return {row.stepId: row for row in self.steps}

    def proposed_steps(self) -> List[StepProposal]:
        """Rows that carry a value change, i.e. the ones ``--accept all`` takes."""
        return [row for row in self.steps if row.status == PROPOSED]

    def notes(self) -> List[StepProposal]:
        return [row for row in self.steps if row.status == NOTE]


# --------------------------------------------------------------- the proposal


def _widen(
    low: Optional[float],
    high: Optional[float],
    include: Sequence[Optional[float]],
) -> Tuple[Optional[float], Optional[float]]:
    """Extend ``[low, high]`` until it contains every value in ``include``."""
    for value in include:
        if value is None:
            continue
        if low is None or value < low:
            low = value
        if high is None or value > high:
            high = value
    return low, high


def _propose_values(
    kind: str,
    stats: Dict[str, Any],
    current: Dict[str, Any],
    low_pct: float,
    high_pct: float,
    range_for_indefinite: bool,
) -> Dict[str, Any]:
    """
    The duration fields a step's measurements imply, already rounded.

    ``stats`` holds the report's ``median``/``p10``/``p90`` (renamed to the
    requested percentiles by the caller).
    """
    median = stats.get("median")
    if median is None:
        return {}
    default = _round_seconds(median)
    assert default is not None  # `median` is not None here
    if kind == "indefinite" and not range_for_indefinite:
        return {"type": kind, "defaultSeconds": default}

    low, high = stats.get("low"), stats.get("high")
    low, high = _widen(
        low,
        high,
        [
            current.get("minSeconds"),
            current.get("maxSeconds"),
            current.get("optimalSeconds"),
            float(default),
        ],
    )
    proposed: Dict[str, Any] = {"type": kind, "defaultSeconds": default}
    proposed["minSeconds"] = _floor_seconds(low)
    proposed["maxSeconds"] = _ceil_seconds(high)
    # Flooring the low bound and ceiling the high one can only widen, so the
    # author's range is still contained; the default is inside by construction.
    return proposed


def _stats_from_row(row, low_pct: float, high_pct: float, values) -> Dict[str, Any]:
    """
    ``{median, low, high, iqr, …}`` for one step.

    The report already computes P10/P90; any other percentile pair is computed
    here from the same measurements.
    """
    from .report import percentile

    stats: Dict[str, Any] = {
        "median": row.median,
        "p10": row.p10,
        "p90": row.p90,
        "iqr": row.iqr,
        "mean": row.mean,
        "cv": row.cv,
    }
    if float(low_pct) == 10.0 and float(high_pct) == 90.0:
        stats["low"], stats["high"] = row.p10, row.p90
    else:
        stats["low"] = percentile(values, float(low_pct) / 100.0)
        stats["high"] = percentile(values, float(high_pct) / 100.0)
    return stats


def _measurements(records: Sequence[Dict[str, Any]]) -> Dict[str, List[float]]:
    """``{stepId: [observed durations]}`` over usable runs and usable steps."""
    from .usable import step_duration, usable_steps

    out: Dict[str, List[float]] = {}
    for record in records:
        for step, reason in usable_steps(record, include_fixed=False):
            sid = step.get("stepId")
            if not sid or reason is not None:
                continue
            duration = step_duration(step)
            if duration is not None:
                out.setdefault(str(sid), []).append(duration)
    return out


def propose_calibration(
    program: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    *,
    k: int = DEFAULT_MIN_RUNS,
    since: Any = None,
    low_pct: float = DEFAULT_LOW_PCT,
    high_pct: float = DEFAULT_HIGH_PCT,
    lag_fraction: float = DEFAULT_LAG_FRACTION,
    lag_floor_seconds: float = DEFAULT_LAG_FLOOR_SECONDS,
    range_for_indefinite: bool = False,
    as_of: Optional[str] = None,
    with_effect: bool = True,
) -> CalibrationProposal:
    """
    Propose durations for ``program`` from ``records``. Writes nothing.

    Args:
        k: measurements a step needs before it gets a proposal.
        since: drop runs started before this date (``YYYY-MM-DD`` or ISO).
        low_pct/high_pct: the percentiles proposed as ``minSeconds``/``maxSeconds``
            before widening (default 10 and 90).
        lag_fraction/lag_floor_seconds: a fixed step's lag must exceed
            ``max(floor, fraction * planned)`` to earn a "consider variable" note.
        range_for_indefinite: also propose a range for ``indefinite`` steps,
            which by default get only ``defaultSeconds``.
        as_of: the timestamp written into ``calibratedFrom`` (defaults to now);
            pass it to make a proposal reproducible.
        with_effect: compute the makespan/critical-path effect if accepted.

    Returns:
        A :class:`CalibrationProposal`; ``to_dict()`` is JSON-serialisable.
    """
    kept = filter_since(records, since)
    report = build_report(kept, program, k=k, since=None)
    rows = {row.stepId: row for row in report.steps}

    usable_records = [r for r in kept if is_usable_run(r)[0]]
    values_by_step = _measurements(usable_records)
    versions: List[str] = []
    for record in usable_records:
        version = record.get("programVersion")
        if version and version not in versions:
            versions.append(str(version))

    proposal = CalibrationProposal(
        programId=(program.get("programId") if isinstance(program, dict) else None)
        or report.programId,
        programVersion=program_version(program) if isinstance(program, dict) else None,
        asOf=as_of or _now_iso(),
        minRuns=int(k),
        lowPercentile=float(low_pct),
        highPercentile=float(high_pct),
        since=str(since) if since else None,
        runsConsidered=report.runsConsidered,
        runsUsable=report.runsUsable,
        runsExcluded=list(report.runsExcluded),
        programVersionsSeen=versions,
    )
    proposal.runsProgramVersion = _dominant_version(
        usable_records, versions, proposal.programVersion
    )

    seen: Set[str] = set()
    for step in _authored_steps(program):
        sid = str(step["stepId"])
        seen.add(sid)
        proposal.steps.append(
            _step_proposal(
                sid,
                step.get("duration"),
                step.get("task"),
                rows.get(sid),
                values_by_step.get(sid, []),
                k=k,
                low_pct=low_pct,
                high_pct=high_pct,
                lag_fraction=lag_fraction,
                lag_floor_seconds=lag_floor_seconds,
                range_for_indefinite=range_for_indefinite,
            )
        )

    # Steps that appear in history but not in the program any more: reported so
    # the author can see the history is about a different plan, never proposed.
    for sid, row in rows.items():
        if sid in seen:
            continue
        proposal.steps.append(
            StepProposal(
                stepId=sid,
                durationType=row.durationType,
                task=row.task,
                status=SKIPPED,
                reason=NOT_IN_PROGRAM,
                verdict=row.verdict,
                n=row.n,
                evidence={
                    "n": row.n,
                    "median": row.median,
                    "p10": row.p10,
                    "p90": row.p90,
                    "iqr": row.iqr,
                    "verdict": row.verdict,
                },
            )
        )

    if with_effect:
        proposal.effect = calibration_effect(program, proposal)
    return proposal


def _dominant_version(
    records: Sequence[Dict[str, Any]],
    versions: Sequence[str],
    fallback: Optional[str],
) -> Optional[str]:
    """The programVersion most of the measurements were taken against."""
    if not versions:
        return fallback
    if len(versions) == 1:
        return versions[0]
    counts: Dict[str, int] = {}
    for record in records:
        version = record.get("programVersion")
        if version:
            counts[str(version)] = counts.get(str(version), 0) + 1
    return max(counts, key=lambda v: (counts[v], v)) if counts else fallback


def _step_proposal(
    step_id: str,
    duration: Any,
    task: Optional[str],
    row,
    values: List[float],
    *,
    k: int,
    low_pct: float,
    high_pct: float,
    lag_fraction: float,
    lag_floor_seconds: float,
    range_for_indefinite: bool,
) -> StepProposal:
    kind = _duration_kind(duration) or "fixed"
    current = current_values(duration)
    out = StepProposal(
        stepId=step_id,
        durationType=kind,
        task=task if task is None else str(task),
        verdict=(row.verdict if row is not None else None),
        n=(row.n if row is not None else 0),
        evidence={"current": current, "n": (row.n if row is not None else 0)},
    )
    planned = effective_seconds(duration)
    out.evidence["plannedSeconds"] = planned
    if row is None:
        out.status = SKIPPED
        out.reason = NO_MEASUREMENTS
        return out

    out.evidence.update(
        {
            "median": row.median,
            "p10": row.p10,
            "p90": row.p90,
            "iqr": row.iqr,
            "mean": row.mean,
            "cv": row.cv,
            "verdict": row.verdict,
        }
    )

    if kind == "fixed" or row.verdict == LAG:
        return _fixed_step_proposal(
            out, row, planned, k=k, fraction=lag_fraction, floor=lag_floor_seconds
        )

    if row.n < int(k) or row.median is None:
        out.status = SKIPPED
        out.reason = INSUFFICIENT_RUNS
        out.evidence["minRuns"] = int(k)
        return out

    if not isinstance(duration, dict):
        out.status = SKIPPED
        out.reason = NOT_A_DURATION_OBJECT
        return out

    stats = _stats_from_row(row, low_pct, high_pct, values)
    out.evidence["low"] = stats.get("low")
    out.evidence["high"] = stats.get("high")
    proposed = _propose_values(
        kind, stats, current, low_pct, high_pct, range_for_indefinite
    )
    if not proposed:
        out.status = SKIPPED
        out.reason = NO_MEASUREMENTS
        return out
    out.status = PROPOSED
    out.evidence["proposed"] = proposed
    current_default = current.get("defaultSeconds")
    if current_default is None:
        current_default = planned
    if current_default is not None and proposed.get("defaultSeconds") is not None:
        out.evidence["deltaSeconds"] = float(proposed["defaultSeconds"]) - float(
            current_default
        )
    else:
        out.evidence["deltaSeconds"] = None
    return out


def _fixed_step_proposal(
    out: StepProposal,
    row,
    planned: Optional[float],
    *,
    k: int,
    fraction: float,
    floor: float,
) -> StepProposal:
    """A fixed step never gets a value; it gets its lag, and maybe a note."""
    threshold = max(float(floor), float(fraction) * float(planned or 0.0))
    out.n = row.lagN
    out.evidence.update(
        {
            "n": row.lagN,
            "lagSeconds": row.lagSeconds,
            "endDriftSeconds": row.endDriftSeconds,
            "lagThresholdSeconds": threshold,
            "median": None,
            "p10": None,
            "p90": None,
            "iqr": None,
        }
    )
    if row.lagN < int(k):
        out.status = SKIPPED
        out.reason = INSUFFICIENT_RUNS
        out.evidence["minRuns"] = int(k)
        return out
    if row.lagSeconds is not None and row.lagSeconds > threshold:
        out.status = NOTE
        out.note = NOTE_CONSIDER_VARIABLE
        return out
    out.status = SKIPPED
    out.reason = FIXED_DURATION
    return out


# ----------------------------------------------------------------- acceptance


def resolve_accepted(
    proposal: CalibrationProposal,
    accept: Union[str, Iterable[str]] = "all",
) -> List[str]:
    """
    The step ids ``accept`` names, checked against ``proposal``.

    ``"all"`` expands to every row that carries a value change. An explicit
    list must name only such rows: an id with no proposal, or one that was
    skipped or only noted, is an error rather than a silent no-op, because a
    caller asking to accept it has misread the proposal.
    """
    by_step = proposal.by_step()
    if isinstance(accept, str):
        if accept != "all":
            raise ValueError(
                f"accept must be 'all' or a list of step ids, not {accept!r}"
            )
        return [row.stepId for row in proposal.proposed_steps()]
    wanted = list(dict.fromkeys(str(s) for s in accept))
    unknown = [s for s in wanted if s not in by_step]
    if unknown:
        raise ValueError("no proposal for step(s): " + ", ".join(sorted(unknown)))
    blocked = [s for s in wanted if by_step[s].status != PROPOSED]
    if blocked:
        detail = ", ".join(
            f"{s} ({by_step[s].reason or by_step[s].note or by_step[s].status})"
            for s in sorted(blocked)
        )
        raise ValueError("no proposed value to accept for: " + detail)
    return wanted


def apply_calibration(
    program: Dict[str, Any],
    proposal: CalibrationProposal,
    accept: Union[str, Iterable[str]] = "all",
) -> Dict[str, Any]:
    """
    Return a copy of ``program`` with the accepted proposals written in.

    ``accept`` is ``"all"`` (every row with a value change) or an iterable of
    step ids. Each written duration object gains, beside its values::

        "calibratedFrom": {"runs": n, "asOf": "…Z", "programVersion": "sha256:…"}

    ``program`` is never mutated, and the result still validates: only the
    schema's own duration fields are touched, ``calibratedFrom`` is an extra
    property on a duration variant, and the range always contains the default.

    Raises:
        ValueError: for a step id with no proposal, a row that carries no value
            change (the reason is in the message), or a step that is missing
            from ``program``.
    """
    by_step = proposal.by_step()
    wanted = resolve_accepted(proposal, accept)

    out = copy.deepcopy(program)
    targets = set(wanted)
    written: Set[str] = set()
    for track in out.get("tracks") or []:
        for step in track.get("steps") or []:
            if not isinstance(step, dict):
                continue
            sid = step.get("stepId")
            if sid is None or str(sid) not in targets:
                continue
            duration = step.get("duration")
            if not isinstance(duration, dict):
                raise ValueError(f"step '{sid}' has no duration object to calibrate")
            row = by_step[str(sid)]
            for key in VALUE_KEYS:
                if key in row.proposed and row.proposed[key] is not None:
                    duration[key] = row.proposed[key]
            duration["calibratedFrom"] = {
                "runs": int(row.n),
                "asOf": proposal.asOf,
                "programVersion": proposal.runsProgramVersion,
            }
            written.add(str(sid))
    missing = targets - written
    if missing:
        raise ValueError(
            "step(s) not found in the program: " + ", ".join(sorted(missing))
        )
    return out


# --------------------------------------------------------- effect if accepted


def _expanded(program: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from rhylthyme.expand_replicates import expand_replicates

        return expand_replicates(copy.deepcopy(program))
    except Exception:  # pragma: no cover - the expander is optional
        return copy.deepcopy(program)


def _timing(program: Dict[str, Any]):
    """``(makespan, criticalPath, {stepId: (start, end)})`` for a program."""
    from ..eval import metrics

    expanded = _expanded(program)
    timeline = metrics.compute_timeline(expanded)
    makespan = max((end for _start, end in timeline.values()), default=0.0)
    return makespan, metrics.critical_path(expanded), timeline


def calibration_effect(
    program: Dict[str, Any],
    proposal: CalibrationProposal,
    accept: Union[str, Iterable[str]] = "all",
) -> Dict[str, Any]:
    """
    What accepting ``accept`` would do to the plan.

    ``{makespanBeforeSeconds, makespanAfterSeconds, makespanDeltaSeconds,
    criticalPathBefore, criticalPathAfter, criticalPathChanged, acceptedSteps,
    stepShifts}`` — ``stepShifts`` lists only the steps whose planned start or
    end moves. Empty when there is nothing to accept.
    """
    try:
        accepted = resolve_accepted(proposal, accept)
    except ValueError:
        return {}
    if not accepted:
        before, path, _timeline = _timing(program)
        return {
            "acceptedSteps": [],
            "makespanBeforeSeconds": before,
            "makespanAfterSeconds": before,
            "makespanDeltaSeconds": 0.0,
            "criticalPathBefore": path,
            "criticalPathAfter": path,
            "criticalPathChanged": False,
            "stepShifts": [],
        }
    try:
        after_program = apply_calibration(program, proposal, accepted)
    except ValueError:
        return {}
    before, path_before, timeline_before = _timing(program)
    after, path_after, timeline_after = _timing(after_program)
    shifts = []
    for step_id in timeline_before:
        if step_id not in timeline_after:
            continue
        (s0, e0), (s1, e1) = timeline_before[step_id], timeline_after[step_id]
        if abs(s1 - s0) < 1e-9 and abs(e1 - e0) < 1e-9:
            continue
        shifts.append(
            {
                "stepId": step_id,
                "startBefore": s0,
                "startAfter": s1,
                "endBefore": e0,
                "endAfter": e1,
                "startShiftSeconds": s1 - s0,
                "endShiftSeconds": e1 - e0,
            }
        )
    return {
        "acceptedSteps": accepted,
        "makespanBeforeSeconds": before,
        "makespanAfterSeconds": after,
        "makespanDeltaSeconds": after - before,
        "criticalPathBefore": path_before,
        "criticalPathAfter": path_after,
        "criticalPathChanged": path_before != path_after,
        "stepShifts": shifts,
    }


__all__ = [
    "propose_calibration",
    "apply_calibration",
    "resolve_accepted",
    "calibration_effect",
    "current_values",
    "effective_seconds",
    "CalibrationProposal",
    "StepProposal",
    "DEFAULT_MIN_RUNS",
    "DEFAULT_LOW_PCT",
    "DEFAULT_HIGH_PCT",
    "DEFAULT_LAG_FRACTION",
    "DEFAULT_LAG_FLOOR_SECONDS",
    "PROPOSED",
    "SKIPPED",
    "NOTE",
    "INSUFFICIENT_RUNS",
    "FIXED_DURATION",
    "NO_MEASUREMENTS",
    "NOT_IN_PROGRAM",
    "NOT_A_DURATION_OBJECT",
    "NOTE_CONSIDER_VARIABLE",
    "VALUE_KEYS",
]
