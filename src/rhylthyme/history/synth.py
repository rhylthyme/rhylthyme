"""
Synthetic run corpora with known generating functions.

Calibration and prediction can only be tested against history whose truth is
known, so this module manufactures schema-valid run records from a program: the
plan comes from the real timing resolver (``freeze_planned``), and each step's
observed duration is its planned duration multiplied by a lognormal factor with
mean 1 and a chosen coefficient of variation. Downstream starts are pushed by
upstream overruns, so ``triggerFiredAt`` and the fixed-step lag behave the way
they do in a real run.

    from rhylthyme.history import synthesize_runs
    runs = synthesize_runs(program, 20,
                           noise_by_step={"potatoes-boil": 0.05,
                                          "turkey-roast": 0.45},
                           factors_fn=lambda i: {"turkeyKg": 5 + i % 4},
                           seed=7)

Every record is ``completed``, wall-clock, speed 1, with non-fixed steps ended
by the executor, i.e. usable by :mod:`.usable`. Pass ``duration_fn`` to impose
an exact generating function (Phase 6 fits it back):

    duration_fn=lambda step_id, planned, factors, rng: (
        600 + 90 * factors["turkeyKg"] if step_id == "turkey-roast" else planned)
"""

import copy
import datetime
import math
import random
from typing import Any, Callable, Dict, List, Optional, Sequence

from .hash import program_version
from .recorder import RUNS_SCHEMA_VERSION, freeze_planned, step_identity, waited_on
from .store import write_run

DEFAULT_START = datetime.datetime(2026, 1, 5, 16, 0, 0, tzinfo=datetime.timezone.utc)
DEFAULT_INTERVAL_SECONDS = 86400


def _iso(dt: datetime.datetime, precision: int = 3) -> str:
    if precision:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def lognormal_factor(rng: random.Random, cv: float) -> float:
    """
    A positive multiplier with mean 1 and coefficient of variation ``cv``.

    ``cv <= 0`` returns exactly 1, which makes a step perfectly predictable.
    """
    if not cv or cv <= 0:
        return 1.0
    sigma = math.sqrt(math.log(1.0 + cv * cv))
    return math.exp(rng.gauss(0.0, sigma) - 0.5 * sigma * sigma)


def _expand(program: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from rhylthyme.expand_replicates import expand_replicates

        return expand_replicates(copy.deepcopy(program))
    except Exception:  # pragma: no cover - expander is optional
        return copy.deepcopy(program)


def _topological(order: Sequence[str], deps: Dict[str, List[str]]) -> List[str]:
    """``order`` rearranged so every dependency precedes its dependents."""
    placed: List[str] = []
    seen = set()
    known = set(order)

    def visit(node: str, stack: set) -> None:
        if node in seen or node in stack or node not in known:
            return
        stack.add(node)
        for dep in deps.get(node, []):
            visit(dep, stack)
        stack.discard(node)
        seen.add(node)
        placed.append(node)

    for node in order:
        visit(node, set())
    return placed


def synthesize_runs(
    program: Dict[str, Any],
    n: int,
    *,
    noise_by_step: Optional[Dict[str, float]] = None,
    factors_fn: Optional[Callable[[int], Dict[str, Any]]] = None,
    duration_fn: Optional[
        Callable[[str, float, Dict[str, Any], random.Random], float]
    ] = None,
    seed: Optional[int] = None,
    default_cv: float = 0.05,
    start: Optional[datetime.datetime] = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    runtime_kind: str = "cli",
    runtime_version: str = "synthetic",
    environment_id: Optional[str] = None,
    outcome: str = "completed",
    runs_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return ``n`` run records for ``program``.

    Args:
        noise_by_step: ``{authored stepId: coefficient of variation}``; steps
            not listed use ``default_cv``.
        factors_fn: ``i -> {factor key: value}``, stored in
            ``context.userTags`` of run ``i``.
        duration_fn: ``(stepId, plannedSeconds, factors, rng) -> seconds``,
            applied before the noise multiplier, for an exact generating law.
        seed: base seed; run ``i`` uses ``seed + i`` so corpora are reproducible.
        runs_dir: when given, every record is also written to that directory.

    Every record validates against ``runs_schema_0.1.0-alpha.json``.
    """
    noise_by_step = noise_by_step or {}
    expanded = _expand(program)
    planned = freeze_planned(expanded)

    source_ids = {
        s.get("stepId")
        for t in program.get("tracks", []) or []
        for s in t.get("steps", []) or []
    }

    order: List[str] = []
    deps: Dict[str, List[str]] = {}
    identity: Dict[str, Any] = {}
    for track in expanded.get("tracks", []) or []:
        for step in track.get("steps", []) or []:
            rid = step.get("stepId")
            if not rid:
                continue
            order.append(rid)
            deps[rid] = [d for d in waited_on(step) if d]
            identity[rid] = step_identity(step, source_ids)
    ordered = _topological(order, deps)

    metadata = program.get("metadata")
    base_context: Dict[str, Any] = {}
    if isinstance(metadata, dict):
        base_context.update(copy.deepcopy(metadata))
    if program.get("environmentType"):
        base_context.setdefault("environmentType", program["environmentType"])
    if program.get("sourceUrl"):
        base_context.setdefault("sourceUrl", program["sourceUrl"])
    base_context["actors"] = program.get("actors")

    version = program_version(program)
    begin = start or DEFAULT_START
    records: List[Dict[str, Any]] = []

    for index in range(int(n)):
        rng = random.Random((seed + index) if seed is not None else None)
        factors = dict(factors_fn(index)) if factors_fn else {}

        actual_start: Dict[str, float] = {}
        actual_end: Dict[str, float] = {}
        trigger: Dict[str, float] = {}
        for rid in ordered:
            plan = planned.get(rid) or {}
            plan_start = float(plan.get("start", 0.0))
            plan_len = max(0.0, float(plan.get("end", 0.0)) - plan_start)
            base, _instance = identity[rid]
            length = plan_len
            if duration_fn is not None:
                length = float(duration_fn(base, plan_len, factors, rng))
            cv = noise_by_step.get(base, default_cv)
            length = max(0.0, length * lognormal_factor(rng, cv))
            ready = [actual_end[d] for d in deps.get(rid, []) if d in actual_end]
            fired = max(ready) if ready else plan_start
            trigger[rid] = fired
            begin_at = max(plan_start, fired)
            actual_start[rid] = begin_at
            actual_end[rid] = begin_at + length

        steps = []
        for rid in order:
            plan = planned.get(rid) or {}
            base, instance = identity[rid]
            kind = plan.get("durationType", "fixed")
            entry: Dict[str, Any] = {
                "stepId": base,
                "instance": instance,
                "planned": copy.deepcopy(plan),
                "actual": {
                    "start": round(actual_start[rid], 3),
                    "end": round(actual_end[rid], 3),
                },
                "endedBy": "timer" if kind == "fixed" else "executor",
                "triggerFiredAt": round(trigger[rid], 3),
                "pausedSeconds": 0,
            }
            if deps.get(rid):
                entry["waitedOn"] = list(deps[rid])
            steps.append(entry)

        started = begin + datetime.timedelta(seconds=index * interval_seconds)
        makespan = max(actual_end.values()) if actual_end else 0.0
        record = {
            "schemaVersion": RUNS_SCHEMA_VERSION,
            "runId": f"{_iso(started, precision=0)}-{rng.getrandbits(16):04x}",
            "programId": program.get("programId"),
            "programVersion": version,
            "runtime": {
                "kind": runtime_kind,
                "version": runtime_version,
                "clockMode": "wall",
                "speed": 1,
            },
            "environmentId": environment_id,
            "startedAt": _iso(started),
            "endedAt": _iso(started + datetime.timedelta(seconds=makespan)),
            "outcome": outcome,
            "context": dict(base_context, userTags=factors),
            "steps": steps,
        }
        if runs_dir:
            write_run(record, runs_dir)
        records.append(record)
    return records


__all__ = ["synthesize_runs", "lognormal_factor", "DEFAULT_START"]
