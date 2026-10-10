"""
Replay: re-resolve a program's triggers against the ends that actually
happened, so a recorded run can be checked against the planner.

``freeze_planned`` answers "when did the plan say this step would run?".
``replay_timings`` answers the complementary question: "given the durations
that actually occurred, when *should* every trigger have fired?". Feeding a
record's observed ends back into the resolver and comparing the result with
``triggerFiredAt`` is the replay-parity check of PRD §8 item 1: it says the
timing engine and the live runtime agree about what a trigger means.

The resolver here is a port of the JavaScript ``computeStepTimings``
(``rhylthyme-timeline/src/index.js``), which agrees with the Python
reference ``calculate_step_start_time`` on the whole parity corpus. It is a
separate, small function rather than a patch to the validator because the
validator resolves a step at a time from the program alone, while a replay
needs one memoised pass with the observed ends substituted:

    programStart / programStartOffset -> max(0, offsetSeconds)
    afterStep                         -> ref.end (ref.start with event
                                         "start") + offsetSeconds
    afterStepWithBuffer               -> the same plus bufferSeconds
    a negative offset                 -> max(ref.start, ref.end + offset)
    manual / previousStepComplete     -> end of the previous step in the track
    { logic: "all" | "any", ... }      -> max / min of the sub-triggers

``ref.end`` is the step's OBSERVED end when the record has one, otherwise
its planned duration added to its resolved start, so an unfinished step does
not stall the pass.

Two trigger forms cannot be reproduced from the plan graph plus the observed
ends, and ``executor_gated`` names them (the replay tests skip exactly these):

* ``manual`` — the executor decides when, and nothing in the record predicts
  it;
* a negative ``offsetSeconds`` — a *hindsight* replay resolves it against the
  end that happened (``ref.end + offset``), but the live runtime had to fire
  it before the anchor ended, from the anchor's **projected** end
  (``ref.start + defaultSeconds + offset``, or the predicted duration when
  ``metadata.offsetsUse`` is ``"predicted"``). The two instants coincide only
  when the anchor took exactly as long as expected, so the replay cannot
  check this trigger — that is what ``rhylthyme runs evaluate`` measures
  instead.

``event: "start"`` used to be a third: the runner waited for the referenced
step to complete where the engine anchors on its start. Phase 7 fixed the
runner, and the form is now reproduced like any other dependency.
"""

from typing import Any, Dict, List, Optional, Set

from ..validate_program import _signed_seconds, parse_duration_to_seconds

_REF_TRIGGERS = ("afterStep", "afterStepWithBuffer", "onAbort")
_PREV_TRIGGERS = ("manual", "previousStepComplete")


def _expand(program: Dict[str, Any]) -> Dict[str, Any]:
    from rhylthyme.expand_replicates import expand_replicates

    return expand_replicates(program)


def expanded_step_ids(program: Dict[str, Any]) -> Set[str]:
    """Every step id of ``program`` after replicate expansion."""
    return {
        step["stepId"]
        for track in _expand(program).get("tracks", [])
        for step in track.get("steps", [])
        if step.get("stepId")
    }


def record_step_id(entry: Dict[str, Any], known_ids: Optional[Set[str]] = None) -> str:
    """
    The expanded step id a record entry refers to.

    Records key steps by the authored ``stepId`` plus a 1-based ``instance``;
    the runtime id of a replicated step is ``<stepId>-r<instance>``. When the
    program's expanded ids are known the choice is confirmed against them,
    which keeps an authored id that happens to end in ``-r1`` intact.
    JavaScript twin: ``Rhylthyme.runtimeStepId``.
    """
    base = entry.get("stepId")
    instance = entry.get("instance", 1) or 1
    suffixed = f"{base}-r{instance}"
    if known_ids is not None:
        if suffixed in known_ids:
            return suffixed
        if base in known_ids:
            return str(base)
    return suffixed if instance > 1 else str(base)


def actual_intervals(
    record: Dict[str, Any], program: Optional[Dict[str, Any]] = None
) -> Dict[str, Dict[str, float]]:
    """``{expanded step id: {"start": s, "end": e}}`` from a run record."""
    known = expanded_step_ids(program) if program is not None else None
    out: Dict[str, Dict[str, float]] = {}
    for entry in record.get("steps", []):
        actual = entry.get("actual")
        if not entry.get("stepId") or not isinstance(actual, dict):
            continue
        interval = {
            key: float(actual[key])
            for key in ("start", "end")
            if isinstance(actual.get(key), (int, float))
        }
        if interval:
            out[record_step_id(entry, known)] = interval
    return out


def actual_ends(
    record: Dict[str, Any], program: Optional[Dict[str, Any]] = None
) -> Dict[str, float]:
    """``{expanded step id: observed end}`` for every step that finished."""
    return {
        step_id: interval["end"]
        for step_id, interval in actual_intervals(record, program).items()
        if "end" in interval
    }


def trigger_atoms(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The step's single triggers, a compound trigger flattened."""
    trigger = step.get("startTrigger") or {}
    if isinstance(trigger, dict) and isinstance(trigger.get("triggers"), list):
        return [t for t in trigger["triggers"] if isinstance(t, dict)]
    return [trigger] if isinstance(trigger, dict) else []


def executor_gated(step: Dict[str, Any]) -> Optional[str]:
    """
    Why ``step``'s start is not reproducible from the plan graph, or ``None``.

    See the module docstring: a ``manual`` gate is the executor's choice, and
    a negative offset fired from a projection the observed end has since
    contradicted, so a replay check must skip both.
    """
    for atom in trigger_atoms(step):
        if atom.get("type") == "manual":
            return "manual gate"
        if atom.get("type") in ("afterStep", "afterStepWithBuffer"):
            if _signed_seconds(atom.get("offsetSeconds", 0)) < 0:
                return "negative offset"
    return None


def replay_timings(
    program: Dict[str, Any],
    record: Dict[str, Any],
    ends: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Resolve every step of ``program`` against the ends observed in ``record``.

    Returns ``{expanded step id: {"start", "end", "duration", "resolved"}}``
    in seconds from the run start. ``resolved`` is ``False`` for a step whose
    trigger could not be satisfied (a cycle or a dangling reference), which is
    then placed at ``t = 0`` exactly as the JS engine does. Pass ``ends`` to
    override the map derived from the record.
    """
    expanded = _expand(program)
    steps: Dict[str, Dict[str, Any]] = {}
    prev_in_track: Dict[str, Optional[str]] = {}
    for track in expanded.get("tracks", []):
        previous: Optional[str] = None
        for step in track.get("steps", []):
            step_id = step.get("stepId")
            if not step_id:
                continue
            steps[step_id] = step
            prev_in_track[step_id] = previous
            previous = step_id

    observed = actual_ends(record, expanded) if ends is None else dict(ends)
    out: Dict[str, Dict[str, Any]] = {}

    def resolve_single(step_id: str, trigger: Dict[str, Any]) -> Optional[float]:
        kind = trigger.get("type", "programStart")
        if kind in ("programStart", "programStartOffset"):
            return max(0.0, _signed_seconds(trigger.get("offsetSeconds", 0)))
        if kind in _REF_TRIGGERS:
            ref_id = trigger.get("stepId")
            if not ref_id:
                return 0.0
            ref = out.get(ref_id)
            if ref is None:
                return None
            offset = _signed_seconds(trigger.get("offsetSeconds", 0))
            if kind == "afterStepWithBuffer":
                offset += _signed_seconds(trigger.get("bufferSeconds", 0))
            if offset < 0:
                return max(ref["start"], ref["end"] + offset)
            base = ref["start"] if trigger.get("event") == "start" else ref["end"]
            return max(0.0, base + offset)
        if kind in _PREV_TRIGGERS:
            previous = prev_in_track.get(step_id)
            offset = _signed_seconds(trigger.get("offsetSeconds", 0))
            if previous is None:
                return max(0.0, offset)
            if previous not in out:
                return None
            return max(0.0, out[previous]["end"] + offset)
        return 0.0

    def resolve(step_id: str, trigger: Any) -> Optional[float]:
        if not isinstance(trigger, dict):
            return 0.0
        if trigger.get("logic") and isinstance(trigger.get("triggers"), list):
            times = []
            for sub in trigger["triggers"]:
                value = resolve(step_id, sub)
                if value is None:
                    return None
                times.append(value)
            if not times:
                return 0.0
            return min(times) if trigger["logic"] == "any" else max(times)
        return resolve_single(step_id, trigger)

    step_ids = list(steps)
    passes, max_passes = 0, max(20, len(step_ids) + 2)
    progressed = True
    while progressed and passes < max_passes:
        progressed = False
        passes += 1
        for step_id in step_ids:
            if step_id in out:
                continue
            start = resolve(step_id, steps[step_id].get("startTrigger") or {})
            if start is None:
                continue
            if step_id in observed:
                end = observed[step_id]
            else:
                end = start + parse_duration_to_seconds(
                    steps[step_id].get("duration", "0s")
                )
            out[step_id] = {
                "start": float(start),
                "end": float(end),
                "duration": float(end) - float(start),
                "resolved": True,
            }
            progressed = True

    for step_id in step_ids:
        if step_id not in out:
            duration = float(
                parse_duration_to_seconds(steps[step_id].get("duration", "0s"))
            )
            out[step_id] = {
                "start": 0.0,
                "end": duration,
                "duration": duration,
                "resolved": False,
            }
    return out


__all__ = [
    "actual_ends",
    "actual_intervals",
    "executor_gated",
    "expanded_step_ids",
    "record_step_id",
    "replay_timings",
    "trigger_atoms",
]
