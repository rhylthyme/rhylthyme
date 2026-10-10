"""
The usable-run filter: which recorded runs and steps measure the executor.

A recorded run is a measurement only under conditions that are checked once,
here, and mirrored byte-for-byte in semantics by ``mcp-api/history.js``
(parity fixture: ``tests/fixtures/history/usable-cases.json``).

A **run** is usable when

* ``outcome == "completed"`` — a run that was abandoned or aborted stopped for
  a reason that is not about how long the work takes,
* ``runtime.clockMode == "wall"`` — a simulated clock may have drifted,
* ``runtime.speed == 1`` — a scaled run measures the timer, not the cook.

Within a usable run a **step** is usable when

* it started and ended (``actual.start`` and ``actual.end`` are present),
* ``pausedSeconds == 0`` — a pause while the step was running makes its
  observed length meaningless,
* it was ended by the executor (``endedBy == "executor"``).

The last condition is why ``fixed`` steps are never "measured": a fixed step
ends when its timer expires, so its observed duration only confirms the timer.
Pass ``include_fixed=True`` to keep fixed steps (ended by the timer *or* by the
executor) for lag analysis — how late the step's planned end really was — which
is the only thing history can say about a fixed duration.

Reason codes are deliberately value-free strings so Python and JavaScript agree
character for character:

``outcome-not-completed``, ``clock-not-wall``, ``speed-not-1``, ``no-actual``,
``not-ended``, ``paused``, ``fixed-duration``, ``ended-by-timer``,
``ended-by-trigger``, ``ended-by-abort``, ``ended-by-none``.
"""

from typing import Any, Dict, List, Optional, Tuple

# Run-level reasons
OUTCOME_NOT_COMPLETED = "outcome-not-completed"
CLOCK_NOT_WALL = "clock-not-wall"
SPEED_NOT_1 = "speed-not-1"

# Step-level reasons
NO_ACTUAL = "no-actual"
NOT_ENDED = "not-ended"
PAUSED = "paused"
FIXED_DURATION = "fixed-duration"

RUN_REASONS = (OUTCOME_NOT_COMPLETED, CLOCK_NOT_WALL, SPEED_NOT_1)


def is_usable_run(record: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Return ``(usable, reason)`` for one run record.

    ``reason`` is ``None`` when the run is usable, otherwise the first failing
    condition in the fixed order outcome, clock mode, speed.
    """
    if not isinstance(record, dict):
        return False, OUTCOME_NOT_COMPLETED
    if record.get("outcome") != "completed":
        return False, OUTCOME_NOT_COMPLETED
    runtime = record.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    if runtime.get("clockMode") != "wall":
        return False, CLOCK_NOT_WALL
    speed = runtime.get("speed", 1)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        return False, SPEED_NOT_1
    if speed != 1.0:
        return False, SPEED_NOT_1
    return True, None


def step_reason(step: Dict[str, Any], include_fixed: bool = False) -> Optional[str]:
    """
    Return the reason ``step`` is not a measurement, or ``None`` if it is.

    Assumes the enclosing run is usable; ``usable_steps`` applies the run-level
    reason to every step when it is not.
    """
    planned = step.get("planned") or {}
    actual = step.get("actual") or {}
    if actual.get("start") is None:
        return NO_ACTUAL
    if actual.get("end") is None:
        return NOT_ENDED
    if (step.get("pausedSeconds") or 0) > 0:
        return PAUSED
    ended_by = step.get("endedBy")
    if planned.get("durationType") == "fixed":
        if not include_fixed:
            return FIXED_DURATION
        if ended_by in ("executor", "timer"):
            return None
        return "ended-by-" + (str(ended_by) if ended_by else "none")
    if ended_by == "executor":
        return None
    return "ended-by-" + (str(ended_by) if ended_by else "none")


def usable_steps(
    record: Dict[str, Any], include_fixed: bool = False
) -> List[Tuple[Dict[str, Any], Optional[str]]]:
    """
    Return ``[(step, reason)]`` for every step of ``record``, in record order.

    ``reason`` is ``None`` for steps whose observed duration is a measurement.
    When the run itself is not usable every step carries the run's reason, so
    callers can report why a whole run was dropped without a second call.
    """
    ok, run_reason = is_usable_run(record)
    steps = record.get("steps") or []
    if not ok:
        return [(s, run_reason) for s in steps]
    return [(s, step_reason(s, include_fixed)) for s in steps]


def measured_steps(
    record: Dict[str, Any], include_fixed: bool = False
) -> List[Dict[str, Any]]:
    """Just the usable steps of ``record`` (convenience over ``usable_steps``)."""
    return [s for s, reason in usable_steps(record, include_fixed) if reason is None]


def step_duration(step: Dict[str, Any]) -> Optional[float]:
    """Observed duration of ``step`` in seconds, or ``None`` if it has none."""
    actual = step.get("actual") or {}
    start, end = actual.get("start"), actual.get("end")
    if start is None or end is None:
        return None
    return float(end) - float(start)


def planned_duration(step: Dict[str, Any]) -> Optional[float]:
    """Planned duration of ``step`` in seconds (``planned.end - planned.start``)."""
    planned = step.get("planned") or {}
    start, end = planned.get("start"), planned.get("end")
    if start is None or end is None:
        return None
    return float(end) - float(start)


__all__ = [
    "is_usable_run",
    "step_reason",
    "usable_steps",
    "measured_steps",
    "step_duration",
    "planned_duration",
    "OUTCOME_NOT_COMPLETED",
    "CLOCK_NOT_WALL",
    "SPEED_NOT_1",
    "NO_ACTUAL",
    "NOT_ENDED",
    "PAUSED",
    "FIXED_DURATION",
    "RUN_REASONS",
]
