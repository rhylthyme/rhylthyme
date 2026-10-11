"""
Connect a ProgramRunner to lab instruments (``rhylthyme run --workcell``).

Each workcell tool names an instrument driver (``drivers``): galago-tools
through the optional ``rhylthyme-galago`` package (``pip install
rhylthyme[galago]``) when it names none. When a step with an ``instrument``
starts, its tool's driver sends the command without blocking the runner; the
reply comes back through ``ProgramRunner.post_instrument_reply`` and ends (or
fails) the step.
"""

import copy
import threading
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import drivers as D
from .instance_checks import Finding

INSTALL_HINT = D.install_hint(D.DEFAULT_DRIVER)

#: Seconds each safe stop may take, and the run waits for them at the end.
STOP_TIMEOUT = 30.0


class InstrumentSetupError(RuntimeError):
    """The workcell cannot run this program's instrument steps."""


def program_uses_instruments(program: Mapping[str, Any]) -> bool:
    return any(
        step.get("instrument")
        for track in program.get("tracks", [])
        for step in track.get("steps", [])
    )


Call = Tuple[str, Dict[str, Any]]  # (stepId, resolved action)


def _step_seconds(step: Mapping[str, Any]) -> Optional[float]:
    """The longest a step can run on its own clock: None if open-ended."""
    from .validate_program import parse_time_string_to_seconds

    duration = step.get("duration")
    if duration is None:
        return None
    if isinstance(duration, Mapping):
        if duration.get("type") == "indefinite":
            return None
        value = duration.get("maxSeconds", duration.get("seconds"))
        if value is None:
            return None
        return float(parse_time_string_to_seconds(value))
    return float(parse_time_string_to_seconds(duration))


def _calls(
    program: Mapping[str, Any], steps: Optional[List[Mapping[str, Any]]] = None
) -> List[Call]:
    """Every call of every instrument step, in program order, each knowing
    how long its step may run (``stepSeconds``)."""
    if steps is None:
        steps = list(D.instrument_steps(program))
    out: List[Call] = []
    for step in steps:
        seconds = _step_seconds(step)
        for call in D.step_calls(step["instrument"]):
            if seconds is not None:
                call["stepSeconds"] = seconds
            out.append((str(step.get("stepId")), call))
    return out


def _route(
    calls: List[Call], workcell: Optional[D.Workcell]
) -> Tuple[Dict[str, List[Call]], List[Call]]:
    """
    Calls by driver, and the calls naming a tool the workcell lacks.
    Without a workcell each call goes to the driver that claims it.
    """
    by_driver: Dict[str, List[Call]] = {}
    unknown: List[Call] = []
    for step_id, call in calls:
        driver: Optional[str]
        if workcell is None:
            driver = D.claiming_driver(call)
        else:
            driver = workcell.driver_of(call.get("tool"))
            if driver is None:
                unknown.append((step_id, call))
                continue
        by_driver.setdefault(driver, []).append((step_id, call))
    return by_driver, unknown


def _load_workcell(workcell_source) -> D.Workcell:
    try:
        return D.load_workcell(workcell_source)
    except D.WorkcellError as e:
        raise InstrumentSetupError(str(e)) from None


def _shape_findings(program: Mapping[str, Any]) -> List[Finding]:
    """Driver-independent checks of each step's actions."""
    findings = []
    for step in D.instrument_steps(program):
        step_id = str(step.get("stepId"))
        instrument = step["instrument"]
        if D.blocking_action(instrument) is None and "duration" not in step:
            findings.append(
                Finding(
                    code="instrument_no_end",
                    message=f"Step '{step_id}' has no command, no until and no "
                    "duration: it ends only when the operator ends it",
                    where=f"step:{step_id}",
                    fix="Add a duration, or an until action whose reply ends it",
                    severity="warning",
                )
            )
    return findings


def instrument_findings(
    program: Mapping[str, Any], workcell_source=None
) -> List[Finding]:
    """
    Validation findings for a program's instrument steps. Every call a step
    may send (its command or until, and its start, end and onAbort actions)
    is checked by its tool's driver: against the workcell's tools when
    ``workcell_source`` (a path or dict) is given, else against what the step
    itself names (galago: its toolType).
    """
    if workcell_source is None and not program_uses_instruments(program):
        return []
    workcell = None
    if workcell_source is not None:
        try:
            workcell = D.load_workcell(workcell_source)
        except D.WorkcellError as e:
            return [Finding(code="workcell_invalid", message=str(e), where="workcell")]
    by_driver, unknown = _route(_calls(program), workcell)
    findings: List[Finding] = _shape_findings(program)
    reported = set()
    for step_id, call in unknown:
        assert workcell is not None  # only a workcell lacks tools
        tool = call.get("tool")
        if (step_id, tool) in reported:
            continue
        reported.add((step_id, tool))
        known = ", ".join(sorted(workcell.tools))
        findings.append(
            Finding(
                code="instrument_unknown_tool",
                message=f"Step '{step_id}': workcell {workcell.id!r} has no tool "
                f"{tool!r} (tools: {known})",
                where=f"step:{step_id}",
            )
        )
    if workcell is not None:
        # Every driver in the workcell checks its tool entries, used or not
        for name in workcell.by_driver():
            by_driver.setdefault(name, [])
    for name, calls in by_driver.items():
        try:
            driver = D.open_driver(workcell, name)
        except D.DriverUnavailable as e:
            findings.append(
                Finding(
                    code="instrument_unchecked",
                    message="Instrument steps were not checked",
                    where="program",
                    fix=str(e),
                    severity="error" if workcell is not None else "warning",
                )
            )
            continue
        except D.WorkcellError as e:
            return [Finding(code="workcell_invalid", message=str(e), where="workcell")]
        if workcell is not None:
            findings.extend(driver.workcell_findings())
        if calls:
            findings.extend(driver.check(D.call_program(program, calls)))
    order = D.step_order(program)

    def place(f: Finding) -> int:
        where = f.where or ""
        return order.get(where[len("step:") :], -1) if where.startswith("step:") else -1

    return sorted(_dedupe(findings), key=place)


def _dedupe(findings: List[Finding]) -> List[Finding]:
    seen, out = set(), []
    for f in findings:
        key = (f.code, f.where, f.message)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def describe_estimates(estimates: List[D.Estimate]) -> List[str]:
    """One line per filled duration, for command-line reports."""
    lines = []
    for e in estimates:
        how = (
            "default, no estimate available"
            if e.source == "default"
            else f"from {e.detail}"
        )
        lines.append(f"  {e.step_id}: {e.seconds:g} s ({how})")
    return lines


def fill_instrument_durations(
    program: Dict[str, Any], workcell_source=None
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Give instrument steps that end on a reply (a command or an until) and
    have no duration an estimated one, for planning, each from its driver
    (galago: the tool's EstimateDuration with a workcell, else a duration-like
    command param, else a default). Returns the new program and report lines;
    steps whose driver is not installed keep no duration, with a note.
    """
    missing = [
        step
        for step in D.instrument_steps(program, ("tracks",))
        if "duration" not in step and D.blocking_action(step["instrument"])
    ]
    if not missing:
        return program, []
    workcell = _load_workcell(workcell_source) if workcell_source is not None else None
    calls: List[Call] = [
        (str(step.get("stepId")), D.blocking_action(step["instrument"]) or {})
        for step in missing
    ]
    by_driver, unknown = _route(calls, workcell)
    if unknown:
        # Not in the workcell: estimated offline by the default driver
        by_driver.setdefault(D.DEFAULT_DRIVER, []).extend(unknown)
    notes: List[str] = []
    estimates: List[D.Estimate] = []
    filled: Dict[str, Dict[str, Any]] = {}
    for name, driver_calls in by_driver.items():
        try:
            driver = D.open_driver(workcell, name)
        except D.DriverUnavailable as e:
            notes.append(
                f"{len(driver_calls)} instrument step(s) have no duration and were "
                f"not estimated. {e}"
            )
            continue
        except D.WorkcellError as e:
            raise InstrumentSetupError(str(e)) from None
        out, found = driver.fill_durations(D.call_program(program, driver_calls))
        estimates.extend(found)
        for step in D.instrument_steps(out, ("tracks",)):
            if "duration" in step:
                filled[str(step.get("stepId"))] = step
    if not filled:
        return program, notes
    result = copy.deepcopy(program)
    for step in D.instrument_steps(result, ("tracks",)):
        done = filled.get(str(step.get("stepId")))
        if done is not None and "duration" not in step:
            step["duration"] = copy.deepcopy(done["duration"])
            estimate = (done.get("metadata") or {}).get("durationEstimate")
            if estimate is not None:
                step.setdefault("metadata", {})["durationEstimate"] = estimate
    order = D.step_order(program)
    estimates.sort(key=lambda e: order.get(e.step_id, -1))
    return result, notes + ["Estimated instrument durations:"] + describe_estimates(
        estimates
    )


def _instrument_steps(program: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        step
        for track in program.get("tracks", [])
        for step in track.get("steps", [])
        if step.get("instrument")
    ]


def _params_text(params: Optional[Mapping[str, Any]]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in (params or {}).items())


def _calls_text(instrument: Mapping[str, Any]) -> str:
    """A step's calls on one line: ``shaker.start_shake(speed=1000)``, with
    the phase in front of any call that is not the step's own command."""
    parts = []
    for call in D.step_calls(instrument):
        text = f"{call['tool']}.{call['command']}({_params_text(call['params'])})"
        parts.append(text if call["phase"] == "call" else f"{call['phase']} {text}")
    return "; ".join(parts) or f"{instrument.get('tool')} (no calls)"


def _program_tools(program: Mapping[str, Any]) -> List[str]:
    return sorted(
        {
            t
            for step in _instrument_steps(program)
            for t in D.step_tools(step["instrument"])
        }
    )


def _not_sent(message: str) -> Dict[str, Any]:
    return {"ok": False, "code": "NOT_SENT", "errorMessage": message, "metadata": {}}


class InstrumentSession:
    """
    The workcell tools one run uses, each through its driver. Opening it
    touches no instrument; ``summary()`` only reads tool status; ``prepare()``
    readies the tools (simulated unless the session is live); ``attach()``
    wires the runner so each instrument step's command is sent when the step
    starts.

    Safe stops: when a step fails, its ``onAbort`` actions are sent, or by
    default its tools' drivers' stops (``default_stops``); when a step or the
    run is aborted, the same for every tool used since it was last stopped.
    """

    def __init__(
        self,
        workcell: D.Workcell,
        drivers: Dict[str, D.InstrumentDriver],
        tools: List[str],
        *,
        live: bool = False,
    ):
        self.workcell = workcell
        self.drivers = drivers
        self.tools = tools
        self.checks: List[D.ToolCheck] = []
        self.inspected: List[D.ToolCheck] = []
        self._live = live
        # tool -> the last step that sent it a call; tools whose last calls
        # were stops are "safe" until something else is sent to them
        self._last_step: Dict[str, str] = {}
        self._safe: set = set()
        self._stopped_steps: set = set()
        # tool -> the step whose tool was held when the schedule paused
        self._held: Dict[str, str] = {}
        self._stops_in_flight = 0
        self._stops_done = threading.Condition()

    @property
    def live(self) -> bool:
        return self._live

    def driver(self, tool: str) -> D.InstrumentDriver:
        return self.drivers[self.workcell.driver_of(tool) or ""]

    def inspect(self) -> List[D.ToolCheck]:
        """
        Ask every tool what it is, without changing or moving anything (for
        the live pre-flight). Drivers that cannot tell before ``prepare``
        report status only.
        """
        checks: List[D.ToolCheck] = []
        for name, driver in self.drivers.items():
            mine = [t for t in self.tools if self.workcell.driver_of(t) == name]
            checks.extend(driver.inspect(mine))
        self.inspected = sorted(checks, key=lambda c: c.tool)
        return self.inspected

    def hazards(self, program: Mapping[str, Any]) -> List[str]:
        """Every hazard-kind call the program can send, with its params."""
        lines = []
        for step in _instrument_steps(program):
            for call in D.step_calls(step["instrument"]):
                tool = call["tool"]
                if self.workcell.driver_of(tool) is None:
                    continue
                if self.driver(tool).call_kind(tool, call["command"]) != "hazard":
                    continue
                text = f"{tool}.{call['command']}({_params_text(call['params'])})"
                phase = "" if call["phase"] == "call" else f"{call['phase']} "
                lines.append(f"  {step['stepId']}: {phase}{text}")
        return lines

    def summary(self, program: Mapping[str, Any], limit: int = 12) -> List[str]:
        """
        What a run will do, for the --live confirmation: each tool, what it
        is and in which mode with which limits, the steps, and every
        hazard-kind action. Nothing is configured or moved.
        """
        mode = "LIVE: real hardware will move" if self.live else "simulated"
        lines = [f"Workcell {self.workcell.id!r} ({mode})", "Tools:"]
        for check in self.inspect():
            driver = self.driver(check.tool)
            lines.append(
                f"  {check.tool} ({driver.tool_type(check.tool)}) @ {check.address}: "
                f"{check.status}"
            )
            details = []
            if check.identity:
                details.append(f"instrument: {check.identity}")
            if check.simulated is not None:
                details.append("mode: " + ("SIMULATED" if check.simulated else "live"))
            if check.limits:
                details.append("limits: " + ", ".join(check.limits))
            if details:
                lines.append("      " + "; ".join(details))
            if not check.ready and check.detail:
                lines.append(f"      not ready: {check.detail}")
        steps = _instrument_steps(program)
        lines.append(f"Instrument steps ({len(steps)}):")
        for step in steps[:limit]:
            lines.append(f"  {step['stepId']}: {_calls_text(step['instrument'])}")
        if len(steps) > limit:
            lines.append(f"  ... and {len(steps) - limit} more")
        hazards = self.hazards(program)
        if hazards:
            lines.append(
                f"Hazard actions ({len(hazards)}): these heat, move, dispense "
                "or energise"
            )
            lines.extend(hazards)
        return lines

    def preflight_problems(self) -> List[str]:
        """
        Tools the pre-flight found not ready, one line each with no address:
        a live run does not start while there are any.
        """
        problems = []
        for check in self.inspected or self.inspect():
            if check.ready:
                continue
            kind = self.driver(check.tool).tool_type(check.tool)
            line = f"{check.tool} ({kind}): {check.status}"
            if check.detail:
                line += f": {check.detail.replace(check.address, check.tool)}"
            problems.append(line)
        return problems

    def prepare(self) -> None:
        """Ready every tool; raise InstrumentSetupError unless all are ready."""
        checks: List[D.ToolCheck] = []
        for name, driver in self.drivers.items():
            mine = [t for t in self.tools if self.workcell.driver_of(t) == name]
            checks.extend(driver.prepare(mine))
        self.checks = sorted(checks, key=lambda c: c.tool)
        if not all(check.ready for check in self.checks):
            raise InstrumentSetupError("\n".join(self.report()))

    def report(self) -> List[str]:
        mode = "LIVE" if self.live else "simulated"
        lines = [f"Workcell {self.workcell.id!r} ({mode}):"]
        for check in self.checks:
            mark = "ok" if check.ready else "NOT READY"
            line = f"  {check.tool} @ {check.address}: {check.status} [{mark}]"
            lines.append(
                line + (f" {check.detail}" if check.detail and not check.ready else "")
            )
        return lines

    def describe_tools(self) -> List[Dict[str, str]]:
        """The prepared tools for the bridge: name, type, status; no address."""
        tools = [t for driver in self.drivers.values() for t in driver.describe()]
        return sorted(tools, key=lambda t: t["name"])

    def bridge_workcell(self) -> Any:
        """The workcell as the bridge sees it: its name, and what to scrub."""
        return bridge_workcell(self.workcell, self.drivers)

    @property
    def version(self) -> str:
        return ", ".join(v for v in (d.version for d in self.drivers.values()) if v)

    def attach(self, runner) -> None:
        """
        Send each instrument step's calls as the runner reaches them: when a
        step starts (and on each retry) its ``start`` actions in order, then
        its ``command`` or ``until``, whose reply ends the step; when it ends,
        however it ends, its ``end`` actions in order; when it fails or is
        aborted, its safe stops. Every reply goes back through
        ``runner.post_instrument_reply`` tagged with its phase, tool and
        command.
        """

        def on_event(event_type: str, data: Dict[str, Any]) -> None:
            if event_type == "program_aborted":
                self.stop_all(runner)
                return
            if event_type == "program_paused":
                self._hold(runner, "pause")
                return
            if event_type == "program_resumed":
                self._hold(runner, "resume")
                return
            step = runner.steps.get(data.get("step_id"))
            if step is None or not step.instrument:
                return
            if event_type in ("step_started", "step_retry"):
                self._stopped_steps.discard(step.step_id)
                step.instrument_attempts += 1
                self._begin(runner, step)
            elif event_type == "step_completed":
                self._finish(runner, step)
            elif event_type in ("step_failed", "step_aborted"):
                if step.step_id not in self._stopped_steps:
                    self._stopped_steps.add(step.step_id)
                    self._safe_stop(runner, step.step_id, self.stop_plan(step))

        runner.add_event_listener(on_event)

    # -- Pause and resume ----------------------------------------------------

    def _hold(self, runner, phase: str) -> None:
        """
        When the schedule pauses, hold the work of every running step's
        tools that can be held (e.g. an Opentrons run); when it resumes,
        continue exactly those. Replies are recorded under the step.
        """
        if phase == "pause":
            self._held = {}
            for step in runner.steps.values():
                if (
                    not step.instrument
                    or getattr(step.status, "value", "") != "RUNNING"
                ):
                    continue
                for tool in D.step_tools(step.instrument):
                    if tool in self._held or self.workcell.driver_of(tool) is None:
                        continue
                    calls = self.driver(tool).pause_calls(tool)
                    if calls:
                        self._held[tool] = step.step_id
                        self._send_all(runner, step.step_id, tool, calls, "pause")
        else:
            held, self._held = self._held, {}
            for tool, step_id in held.items():
                calls = self.driver(tool).resume_calls(tool)
                self._send_all(runner, step_id, tool, calls, "resume")

    def _send_all(self, runner, step_id: str, tool: str, calls, phase: str) -> None:
        for i, c in enumerate(calls):
            call = {
                "phase": phase,
                "tool": tool,
                "command": c["command"],
                "params": dict(c.get("params") or {}),
            }
            self._execute(
                f"{step_id}/{phase}{i}",
                call,
                lambda _k, reply, call=call: self._post(runner, step_id, call, reply),
            )

    # -- Safe stops --------------------------------------------------------

    def stop_plan(self, step) -> List[Dict[str, Any]]:
        """
        What a failed or aborted step sends: its ``onAbort`` actions, or else
        the default stops of every tool it touches.
        """
        actions = D.phase_actions(step.instrument, "onAbort")
        if actions:
            return actions
        calls = []
        for tool in D.step_tools(step.instrument):
            calls += self._default_stops(tool)
        return calls

    def _default_stops(self, tool: str) -> List[Dict[str, Any]]:
        if self.workcell.driver_of(tool) is None:
            return []
        return [
            {
                "phase": "onAbort",
                "tool": tool,
                "command": stop["command"],
                "params": dict(stop.get("params") or {}),
                "timeoutSeconds": STOP_TIMEOUT,
            }
            for stop in self.driver(tool).default_stops(tool)
        ]

    def stop_all(self, runner) -> None:
        """
        Stop every tool the run has used since it was last stopped (the run
        was aborted, or the runner is closing mid-run). Each tool's stops are
        recorded under the last step that used it.
        """
        for tool, step_id in sorted(self._last_step.items()):
            if tool not in self._safe:
                self._safe_stop(runner, step_id, self._default_stops(tool))

    def _safe_stop(self, runner, step_id: str, calls: List[Dict[str, Any]]) -> None:
        """Send ``calls`` in order; a failed stop does not stop the rest."""
        if not calls:
            return
        runner.note_safe_stops(step_id, len(calls))
        with self._stops_done:
            self._stops_in_flight += len(calls)
        self._safe.update(c["tool"] for c in calls)

        def send(i: int) -> None:
            if i >= len(calls):
                return
            call = calls[i]

            def got(_key: str, reply: Dict[str, Any]) -> None:
                self._post(runner, step_id, call, reply)
                with self._stops_done:
                    self._stops_in_flight -= 1
                    self._stops_done.notify_all()
                send(i + 1)

            self._execute(f"{step_id}/stop{i}", call, got)

        send(0)

    def finish(self, runner, timeout: float = STOP_TIMEOUT) -> None:
        """
        Before the runner closes: stop what a run that did not finish left
        running, wait (up to ``timeout``) for every stop to reply, and apply
        the replies so the run record holds them.
        """
        unfinished = any(
            getattr(step.status, "value", step.status) in ("RUNNING", "FAILED")
            for step in runner.steps.values()
        )
        if unfinished and not getattr(runner, "program_abort_reason", None):
            self.stop_all(runner)
        with self._stops_done:
            self._stops_done.wait_for(lambda: self._stops_in_flight <= 0, timeout)
        runner.apply_instrument_replies()

    def _post(self, runner, step_id: str, call, reply: Dict[str, Any]) -> None:
        runner.post_instrument_reply(
            step_id,
            {
                **reply,
                "phase": call["phase"],
                "tool": call["tool"],
                "command": call["command"],
            },
        )

    def _begin(self, runner, step) -> None:
        instrument = step.instrument
        attempt = step.instrument_attempts
        start = D.phase_actions(instrument, "start")
        main = D.blocking_action(instrument)

        def still_running() -> bool:
            # A step that failed, ended or was retried meanwhile sends no more
            return (
                step.instrument_attempts == attempt
                and getattr(step.status, "value", step.status) == "RUNNING"
            )

        def send(i: int) -> None:
            if i < len(start):
                call = start[i]

                def got(_key: str, reply: Dict[str, Any]) -> None:
                    self._post(runner, step.step_id, call, reply)
                    if reply.get("ok") and still_running():
                        send(i + 1)

                self._execute(f"{step.step_id}/start{i}", call, got)
            elif main is not None and (not start or still_running()):
                self._execute(
                    step.step_id,
                    main,
                    lambda _k, reply: self._post(runner, step.step_id, main, reply),
                )

        send(0)

    def _finish(self, runner, step) -> None:
        """Send the step's end actions in order; a failed one does not stop the rest."""
        end = D.phase_actions(step.instrument, "end")

        def send(i: int) -> None:
            if i >= len(end):
                return
            call = end[i]

            def got(_key: str, reply: Dict[str, Any]) -> None:
                self._post(runner, step.step_id, call, reply)
                send(i + 1)

            self._execute(f"{step.step_id}/end{i}", call, got)

        send(0)

    def _execute(self, key: str, call: Mapping[str, Any], on_reply) -> None:
        if call.get("phase") not in ("onAbort", "pause") and call.get("tool"):
            # This tool may be doing something again until it is stopped
            self._safe.discard(call["tool"])
            step_id = key.split("/", 1)[0]
            self._last_step[call["tool"]] = step_id
        driver_name = self.workcell.driver_of(call.get("tool"))
        if driver_name is None:
            on_reply(key, _not_sent(self.workcell.unknown_tool_text(call.get("tool"))))
            return
        instrument = {
            k: call[k]
            for k in ("tool", "command", "params", "timeoutSeconds")
            if call.get(k) is not None
        }
        self.drivers[driver_name].execute(key, instrument, on_reply)

    def shutdown(self) -> List[str]:
        """Stop every driver; return steps whose commands were still in flight."""
        pending: List[str] = []
        for driver in self.drivers.values():
            pending.extend(driver.shutdown())
        return sorted(set(pending))


def open_drivers(
    workcell: D.Workcell,
    names,
    *,
    live: bool = False,
    driver_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, D.InstrumentDriver]:
    """Open each named driver over its workcell tools; touches no instrument."""
    opened: Dict[str, D.InstrumentDriver] = {}
    try:
        for name in sorted(set(names)):
            opened[name] = D.open_driver(
                workcell,
                name,
                live=live,
                options=(driver_options or {}).get(name),
            )
    except (D.DriverUnavailable, D.WorkcellError) as e:
        for driver in opened.values():
            driver.shutdown()
        raise InstrumentSetupError(str(e)) from None
    return opened


def bridge_workcell(workcell: D.Workcell, drivers: Mapping[str, D.InstrumentDriver]):
    """
    What the bridge's Publisher needs of a workcell: its id and name, and per
    tool every local value (address, config) its scrubber must hide.
    """
    tools = {
        name: drivers[entry.driver].tool_view(name)
        for name, entry in workcell.tools.items()
        if entry.driver in drivers
    }
    return SimpleNamespace(id=workcell.id, name=workcell.name, tools=tools)


def open_instruments(
    program: Mapping[str, Any],
    workcell_source,
    *,
    client_factory: Optional[Callable] = None,
    live: bool = False,
    driver_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> InstrumentSession:
    """
    Load the workcell for ``program`` without contacting any tool.

    Tools run simulated unless ``live`` is true. ``driver_options`` maps a
    driver name to extra keyword arguments for it (``client_factory`` is
    galago's). Raises InstrumentSetupError when a driver's package is
    missing, the workcell is invalid, or a step names a tool the workcell
    lacks.
    """
    workcell = _load_workcell(workcell_source)
    tools = _program_tools(program)
    unknown = [t for t in tools if t not in workcell.tools]
    used = {workcell.driver_of(t) for t in tools if t in workcell.tools}
    options = {k: dict(v) for k, v in (driver_options or {}).items()}
    if client_factory is not None:
        options.setdefault(D.DEFAULT_DRIVER, {})["client_factory"] = client_factory
    # Every driver the program uses, plus the default one when the workcell
    # has its tools: a missing package or a bad tool entry is reported first.
    names = set(used)
    if any(e.driver == D.DEFAULT_DRIVER for e in workcell.tools.values()):
        names.add(D.DEFAULT_DRIVER)
    opened = open_drivers(workcell, names, live=live, driver_options=options)
    if unknown:
        for driver in opened.values():
            driver.shutdown()
        raise InstrumentSetupError(
            f"Workcell {workcell.id!r} has no tool named "
            + ", ".join(repr(t) for t in unknown)
            + f" (tools: {', '.join(sorted(workcell.tools))})"
        )
    return InstrumentSession(workcell, opened, tools, live=live)


def attach_instruments(
    runner,
    workcell_source,
    *,
    client_factory: Optional[Callable] = None,
    live: bool = False,
    driver_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> InstrumentSession:
    """
    Open, configure and attach in one go (no confirmation step): the tools
    are configured and ``runner`` sends each instrument step's command when
    the step starts.
    """
    session = open_instruments(
        runner.program,
        workcell_source,
        client_factory=client_factory,
        live=live,
        driver_options=driver_options,
    )
    try:
        session.prepare()
    except InstrumentSetupError:
        session.shutdown()
        raise
    session.attach(runner)
    return session
