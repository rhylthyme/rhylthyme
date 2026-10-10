"""
RunRecorder: turns ProgramRunner events into a run record.

The recorder is a plain event listener (``runner.add_event_listener``). It
freezes the planned timing of every step when it is attached, records what
actually happens as events arrive, and writes one record per run to the runs
directory when ``finalize`` is called. The runner itself is not restructured;
the events it emits are:

``program_started``  {time, wall_time}
``step_started``     {step_id, time, trigger_fired_at}
``step_completed``   {step_id, time, ended_by}
``step_aborted``     {step_id, time, reason}
``program_paused``   {time, wall_time}
``program_resumed``  {time, wall_time, paused_seconds}
``program_aborted``  {time, reason}  (reason goes to ``context.abortReason``)
``instrument_reply`` {step_id, time, ok, code, errorMessage, metadata}
``remote_command``   {time, id, kind, args, accepted, reason}  (-> ``context.remoteCommands``)

All ``time`` values are the runner's program clock (an epoch float that
advances at ``time_scale``); the record stores them as seconds from the
program start instant.
"""

import copy
import datetime
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..drivers import blocking_action
from .hash import program_version
from .store import write_run

logger = logging.getLogger(__name__)

RUNS_SCHEMA_VERSION = "0.1.0-alpha"
_REPLICATE_SUFFIX = re.compile(r"^(?P<base>.+)-r(?P<index>\d+)$")


def _runtime_version() -> str:
    try:
        from importlib.metadata import version

        return version("rhylthyme-cli-runner")
    except Exception:  # pragma: no cover - fallback for source checkouts
        try:
            from rhylthyme import __version__

            return __version__
        except Exception:
            return "unknown"


def _duration_kind(duration: Any) -> str:
    if isinstance(duration, dict):
        kind = duration.get("type", "fixed")
        if kind == "manual":
            return "indefinite"
        if kind in ("fixed", "variable", "indefinite"):
            return kind
        return "fixed"
    return "fixed"


def freeze_planned(program: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Compute the planned timing of every step of an (expanded) program using
    the reference timing resolver in the root ``rhylthyme`` package.

    Returns ``{runtime_step_id: planned}`` where ``planned`` has ``start``,
    ``end``, ``durationType`` and whichever of ``seconds``, ``minSeconds``,
    ``maxSeconds``, ``defaultSeconds`` the author supplied, all in seconds.
    """
    from rhylthyme.validate_program import (
        calculate_step_start_time,
        parse_duration_to_seconds,
    )

    planned: Dict[str, Dict[str, Any]] = {}
    for track in program.get("tracks", []):
        steps = track.get("steps", [])
        for step in steps:
            step_id = step.get("stepId")
            if not step_id:
                continue
            start = calculate_step_start_time(step, steps, program)
            duration = step.get("duration", {})
            length = parse_duration_to_seconds(duration)
            entry: Dict[str, Any] = {
                "start": start,
                "end": start + length,
                "durationType": _duration_kind(duration),
            }
            if isinstance(duration, dict):
                if entry["durationType"] == "fixed":
                    entry["seconds"] = length
                for key in ("minSeconds", "maxSeconds", "defaultSeconds"):
                    if key in duration and duration[key] is not None:
                        entry[key] = parse_duration_to_seconds(duration[key])
            else:
                entry["seconds"] = length
            planned[step_id] = entry
    return planned


def waited_on(step: Dict[str, Any]) -> List[str]:
    """Ids of predecessor steps whose end gates ``step``'s start trigger."""
    found: List[str] = []

    def visit(trigger: Any) -> None:
        if not isinstance(trigger, dict):
            return
        if "triggers" in trigger:
            for sub in trigger["triggers"]:
                visit(sub)
            return
        if trigger.get("type") in ("afterStep", "afterStepWithBuffer"):
            ref = trigger.get("stepId")
            if ref and ref not in found:
                found.append(ref)

    visit(step.get("startTrigger", {}))
    return found


def step_identity(
    step: Dict[str, Any], source_step_ids: Optional[Set[str]]
) -> Tuple[str, int]:
    """
    Split a runtime (expanded) step into ``(authored stepId, instance)``.

    Prefers ``instanceOf``/``instanceIndex`` stamps when the expander provides
    them; otherwise recognises the ``-r<N>`` suffix, confirmed against the
    authored program's step ids when those are known.
    """
    runtime_id = step["stepId"]
    if step.get("instanceOf") and step.get("instanceIndex") is not None:
        try:
            return str(step["instanceOf"]), int(step["instanceIndex"])
        except (TypeError, ValueError):
            pass
    match = _REPLICATE_SUFFIX.match(runtime_id)
    if match:
        base, index = match.group("base"), int(match.group("index"))
        if source_step_ids is None:
            return base, index
        if base in source_step_ids and runtime_id not in source_step_ids:
            return base, index
    return runtime_id, 1


def runtime_step_id(entry: Dict[str, Any], replicated: bool) -> str:
    """
    Inverse of ``step_identity`` for a record entry: the id the runtime used.

    ``replicated`` says whether the authored step (or its track) carries
    replicates; only then does ``instance`` become a ``-r<N>`` suffix.
    """
    if replicated:
        return f"{entry['stepId']}-r{entry.get('instance', 1)}"
    return entry["stepId"]


def _iso_utc(epoch: float, precision: int = 3) -> str:
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    if precision:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class RunRecorder:
    """Record one execution of a program driven by a ``ProgramRunner``."""

    def __init__(
        self,
        runner: Any,
        source_program: Optional[Dict[str, Any]] = None,
        runs_dir: Optional[str] = None,
        runtime_kind: str = "cli",
        clock_mode: str = "wall",
        environment_id: Optional[str] = None,
    ):
        self.runner = runner
        self.program: Dict[str, Any] = runner.program  # expanded
        self.source_program = source_program
        self.runs_dir = runs_dir
        self.runtime_kind = runtime_kind
        self.clock_mode = clock_mode
        self.environment_id = environment_id or self.program.get("environment")

        hashed = source_program if source_program is not None else self.program
        self.program_version = program_version(hashed)

        source_ids: Optional[Set[str]] = None
        if source_program is not None:
            source_ids = {
                s.get("stepId")
                for t in source_program.get("tracks", [])
                for s in t.get("steps", [])
            }
        self.step_order: List[str] = []
        self.identity: Dict[str, Tuple[str, int]] = {}
        self.waited_on: Dict[str, List[str]] = {}
        for track in self.program.get("tracks", []):
            for step in track.get("steps", []):
                sid = step.get("stepId")
                if not sid:
                    continue
                self.step_order.append(sid)
                self.identity[sid] = step_identity(step, source_ids)
                self.waited_on[sid] = waited_on(step)

        # Planned timings are frozen now, before any step can start.
        self.planned: Dict[str, Dict[str, Any]] = copy.deepcopy(
            freeze_planned(self.program)
        )
        self.context: Dict[str, Any] = self._build_context()

        self.actuals: Dict[str, Dict[str, Any]] = {}
        # step id -> {tool, command, replies: [{attempt, at, code, ...}]}
        self.instrument_log: Dict[str, Dict[str, Any]] = {}
        self.paused_seconds: Dict[str, float] = {sid: 0.0 for sid in self.step_order}
        self._pause_wall: Optional[float] = None
        self._paused_running: List[str] = []
        self._speeds: Set[float] = {float(runner.time_scale)}
        self._started = False
        self._run_suffix = secrets.token_hex(2)
        self.written_path: Optional[Path] = None
        self.record: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ setup
    def attach(self) -> "RunRecorder":
        """Register with the runner and return self."""
        self.runner.add_event_listener(self.on_event)
        return self

    def _build_context(self) -> Dict[str, Any]:
        context: Dict[str, Any] = {}
        metadata = self.program.get("metadata")
        if isinstance(metadata, dict):
            context.update(copy.deepcopy(metadata))
        if self.program.get("environmentType"):
            context.setdefault("environmentType", self.program["environmentType"])
        if self.program.get("sourceUrl"):
            context.setdefault("sourceUrl", self.program["sourceUrl"])
        context["actors"] = getattr(self.runner, "actors_available", None)
        if not isinstance(context.get("userTags"), dict):
            context["userTags"] = {}
        # Which durations negative offsets were resolved against, so a reader
        # of the record knows whether prediction was in play (Phase 7).
        offsets_use = getattr(self.runner, "offsets_use", None)
        context["offsetsUse"] = "predicted" if offsets_use == "predicted" else "planned"
        return context

    # ----------------------------------------------------------------- events
    def on_event(self, event_type: str, data: Dict[str, Any]) -> None:
        self._speeds.add(float(self.runner.time_scale))
        if event_type == "program_started":
            self._started = True
        elif event_type == "step_started":
            self._started = True
            entry = self.actuals.setdefault(data["step_id"], {})
            entry["start"] = data["time"]
            if data.get("trigger_fired_at") is not None:
                entry["triggerFiredAt"] = data["trigger_fired_at"]
        elif event_type == "step_completed":
            entry = self.actuals.setdefault(data["step_id"], {})
            entry.setdefault("start", data["time"])
            entry["end"] = data["time"]
            entry["endedBy"] = data.get("ended_by") or "executor"
        elif event_type == "step_aborted":
            entry = self.actuals.setdefault(data["step_id"], {})
            entry.setdefault("start", data["time"])
            entry["end"] = data["time"]
            entry["endedBy"] = "abort"
        elif event_type == "instrument_reply":
            sid = data["step_id"]
            step = getattr(self.runner, "steps", {}).get(sid)
            instrument = (getattr(step, "instrument", None) or {}) if step else {}
            main = blocking_action(instrument) or {}
            log = self.instrument_log.setdefault(
                sid,
                {
                    "tool": str(instrument.get("tool", "")),
                    "command": str(main.get("command") or ""),
                    "replies": [],
                },
            )
            reply: Dict[str, Any] = {
                "attempt": getattr(step, "instrument_attempts", 0)
                or len(log["replies"]) + 1,
                "at": data["time"],
                "code": str(data.get("code", "")),
            }
            if data.get("phase"):
                # Which call this answers: a start/end/onAbort action, or the
                # step's own command or until
                reply["phase"] = str(data["phase"])
                reply["tool"] = str(data.get("tool") or "")
                reply["command"] = str(data.get("command") or "")
            if data.get("errorMessage"):
                reply["errorMessage"] = str(data["errorMessage"])
            if data.get("metadata"):
                reply["metadata"] = copy.deepcopy(data["metadata"])
            log["replies"].append(reply)
        elif event_type == "remote_command":
            start = self._start_epoch()
            self.context.setdefault("remoteCommands", []).append(
                {
                    "id": data.get("id"),
                    "kind": data.get("kind"),
                    "args": copy.deepcopy(data.get("args") or {}),
                    "atSeconds": (
                        round(max(0.0, data["time"] - start), 3)
                        if start is not None and data.get("time") is not None
                        else None
                    ),
                    "accepted": bool(data.get("accepted")),
                    "reason": data.get("reason") or "",
                }
            )
        elif event_type == "program_aborted":
            self.context["abortReason"] = data.get("reason") or "aborted"
        elif event_type == "program_paused":
            self._pause_wall = data.get("wall_time", time.time())
            self._paused_running = list(getattr(self.runner, "running_steps", []))
        elif event_type == "program_resumed":
            self._settle_pause(data.get("wall_time", time.time()))

    def _settle_pause(self, wall_now: float) -> None:
        if self._pause_wall is None:
            return
        delta = max(0.0, wall_now - self._pause_wall)
        for sid in self._paused_running:
            self.paused_seconds[sid] = self.paused_seconds.get(sid, 0.0) + delta
        self._pause_wall = None
        self._paused_running = []

    # ----------------------------------------------------------------- record
    def _start_epoch(self) -> Optional[float]:
        return getattr(self.runner, "program_start_time", None)

    def infer_outcome(self) -> str:
        """completed if every step ended normally, aborted if any was aborted, else abandoned."""
        from ..program_runner import StepStatus

        statuses = [s.status for s in self.runner.steps.values()]
        if statuses and all(s == StepStatus.COMPLETED for s in statuses):
            return "completed"
        if any(s == StepStatus.ABORTED for s in statuses):
            return "aborted"
        return "abandoned"

    def build_record(self, outcome: Optional[str] = None) -> Dict[str, Any]:
        """Assemble the run record (does not write it)."""
        start_epoch = self._start_epoch()
        now = time.time()
        if self._pause_wall is not None:
            # Still paused: attribute the open pause without closing it.
            pending = max(0.0, now - self._pause_wall)
        else:
            pending = 0.0

        def rel(value: Optional[float]) -> Optional[float]:
            if value is None or start_epoch is None:
                return None
            return round(max(0.0, value - start_epoch), 3)

        steps = []
        for sid in self.step_order:
            base, instance = self.identity[sid]
            entry: Dict[str, Any] = {
                "stepId": base,
                "instance": instance,
                "planned": copy.deepcopy(self.planned.get(sid, {})),
            }
            if self.waited_on.get(sid):
                entry["waitedOn"] = list(self.waited_on[sid])
            paused = self.paused_seconds.get(sid, 0.0)
            if sid in self._paused_running:
                paused += pending
            entry["pausedSeconds"] = round(paused, 3)
            # Which number a negative offset was actually fired from: present
            # only when the runner used a prediction rather than the author's
            # defaultSeconds for this step's anchor (metadata.offsetsUse).
            predicted_anchor = getattr(self.runner, "predicted_anchor_seconds", {})
            if isinstance(predicted_anchor, dict) and sid in predicted_anchor:
                entry["predictedAnchorSeconds"] = round(float(predicted_anchor[sid]), 3)
            actual = self.actuals.get(sid)
            if actual and "start" in actual:
                entry["actual"] = {"start": rel(actual["start"])}
                if "end" in actual:
                    entry["actual"]["end"] = rel(actual["end"])
                    entry["endedBy"] = actual.get("endedBy", "executor")
                if "triggerFiredAt" in actual:
                    entry["triggerFiredAt"] = rel(actual["triggerFiredAt"])
            log = self.instrument_log.get(sid)
            if log:
                entry["instrument"] = {
                    "tool": log["tool"],
                    "command": log["command"],
                    "replies": [dict(r, at=rel(r["at"])) for r in log["replies"]],
                }
            steps.append(entry)

        started_iso = _iso_utc(start_epoch) if start_epoch is not None else None
        run_id = (
            f"{_iso_utc(start_epoch, precision=0)}-{self._run_suffix}"
            if start_epoch is not None
            else None
        )
        speeds = sorted(self._speeds | {float(self.runner.time_scale)})
        record: Dict[str, Any] = {
            "schemaVersion": RUNS_SCHEMA_VERSION,
            "runId": run_id,
            "programId": self.program.get("programId"),
            "programVersion": self.program_version,
            "runtime": {
                "kind": self.runtime_kind,
                "version": _runtime_version(),
                "clockMode": self.clock_mode,
                "speed": speeds[-1] if speeds else 1.0,
            },
            "environmentId": self.environment_id,
            "startedAt": started_iso,
            "endedAt": _iso_utc(now),
            "outcome": outcome or self.infer_outcome(),
            "context": copy.deepcopy(self.context),
            "steps": steps,
        }
        return record

    def finalize(
        self, outcome: Optional[str] = None, runs_dir: Optional[str] = None
    ) -> Optional[Path]:
        """
        Write the record and return its path. Returns ``None`` (and writes
        nothing) if the program never started. Safe to call more than once;
        later calls rewrite the same file.
        """
        if not self._started and not self.actuals:
            return None
        if self._start_epoch() is None:
            return None
        if self._pause_wall is not None:
            self._settle_pause(time.time())
        self.record = self.build_record(outcome=outcome)
        try:
            self.written_path = write_run(self.record, runs_dir or self.runs_dir)
        except OSError as exc:
            logger.error("Could not write run record: %s", exc)
            return None
        return self.written_path


__all__ = [
    "RunRecorder",
    "freeze_planned",
    "waited_on",
    "step_identity",
    "runtime_step_id",
    "RUNS_SCHEMA_VERSION",
]
