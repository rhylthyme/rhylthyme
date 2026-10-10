"""
The LabMCP driver: workcell tools with ``"driver": "labmcp"`` run on LabMCP
instrument servers (K-Dense's lab-instrument-mcps) through
``rhylthyme-labmcp``. Each tool's server is launched with uvx when the run is
prepared (``--simulate`` unless the run is live) and stopped when it ends; a
step's ``command`` is an MCP tool of that server, and the step ends on its
reply.

rhylthyme_labmcp is imported on each use rather than once, so a missing
package is noticed (and reported with the install hint) wherever it matters.
"""

import copy
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..instance_checks import Finding
from . import (
    DriverUnavailable,
    Estimate,
    InstrumentDriver,
    OnReply,
    ToolCheck,
    ToolView,
    WorkcellError,
    install_hint,
    instrument_steps,
)


def _labmcp():
    try:
        import rhylthyme_labmcp
    except ImportError:
        raise DriverUnavailable(install_hint("labmcp")) from None
    return rhylthyme_labmcp


#: Identity fields LabMCP servers report, in the order shown.
IDENTITY_KEYS = ("manufacturer", "vendor", "model", "name", "type", "serial")


def _identity(info: Mapping[str, Any]) -> str:
    """'Mettler Toledo XS205DU, serial B123456789' from get_connection_info."""
    instrument = info.get("instrument")
    if not isinstance(instrument, Mapping):
        return str(instrument or "")
    words = [
        str(instrument[k])
        for k in IDENTITY_KEYS
        if k != "serial" and instrument.get(k) not in (None, "")
    ]
    if not words:
        words = [str(v) for v in instrument.values() if isinstance(v, (str, int))][:3]
    serial = instrument.get("serial")
    return " ".join(words) + (f", serial {serial}" if serial else "")


def _limits(info: Mapping[str, Any], workcell: Mapping[str, float]) -> Tuple[str, ...]:
    """'max_temperature_c=80 °C (workcell)' for each active safety limit."""
    out = []
    for name, limit in sorted((info.get("safety_limits") or {}).items()):
        if not isinstance(limit, Mapping):
            continue
        value = limit.get("value", limit.get("default"))
        unit = f" {limit['unit']}" if limit.get("unit") else ""
        text = (
            f"{name}={value:g}{unit}"
            if isinstance(value, (int, float))
            else f"{name}={value}"
        )
        out.append(text + (" (workcell)" if name in workcell else ""))
    return tuple(out)


def _finding(issue) -> Finding:
    return Finding(
        code=issue.code,
        message=issue.message,
        where=f"step:{issue.step_id}" if issue.step_id else "workcell",
        severity=issue.severity,
    )


def _reply_dict(result) -> Dict[str, Any]:
    return {
        "ok": result.ok,
        "code": result.code,
        "errorMessage": result.error_message,
        "metadata": copy.deepcopy(dict(result.data)),
    }


class LabMCPDriver(InstrumentDriver):
    name = "labmcp"

    def __init__(
        self,
        workcell_id: str,
        tools: Optional[Sequence[Mapping[str, Any]]],
        *,
        live: bool = False,
        workcell_name: Optional[str] = None,
        client_factory: Optional[Callable] = None,
        **options: Any,
    ):
        super().__init__(workcell_id, tools, live=live)
        labmcp = _labmcp()
        self.tools: Dict[str, Any] = {}
        self.executor: Any = None
        if tools is None:
            return
        try:
            self.tools = labmcp.parse_tools(list(tools))
        except labmcp.WorkcellError as e:
            raise WorkcellError(str(e)) from None
        kwargs: Dict[str, Any] = {"simulated": not live}
        if client_factory is not None:
            kwargs["client_factory"] = client_factory
        self.executor = labmcp.LabMCPExecutor(self.tools, **kwargs)

    @classmethod
    def ensure_available(cls) -> None:
        _labmcp()

    @property
    def version(self) -> str:
        return "rhylthyme-labmcp " + getattr(_labmcp(), "__version__", "")

    # -- The workcell ------------------------------------------------------

    @property
    def tool_names(self) -> List[str]:
        return sorted(self.tools)

    def tool_type(self, tool: str) -> str:
        return self.tools[tool].kind

    def tool_address(self, tool: str) -> str:
        return self.tools[tool].location

    def tool_view(self, tool: str) -> ToolView:
        # The address in every form it may surface in (serial path, TCP
        # host and port, VISA parts, server URL host) and the options
        return ToolView(tool, private=tuple(self.tools[tool].private_values()))

    # -- Offline -----------------------------------------------------------

    @classmethod
    def claims(cls, instrument: Mapping[str, Any]) -> bool:
        # A step names its LabMCP server as toolType: "labmcp-ika"
        return str(instrument.get("toolType") or "").startswith("labmcp-")

    def check(self, program: Mapping[str, Any]) -> List[Finding]:
        # Against the vendored LabMCP catalogue: tools, params, bounds, limits
        issues = _labmcp().check_calls(program, self.tools or None)
        return [_finding(issue) for issue in issues]

    def workcell_findings(self) -> List[Finding]:
        return [_finding(issue) for issue in _labmcp().check_workcell(self.tools)]

    def fill_durations(
        self, program: Mapping[str, Any]
    ) -> Tuple[Dict[str, Any], List[Estimate]]:
        # From each call's params and the tool's defaults (rhylthyme_labmcp
        # .estimates): series lengths, run lengths, wait timeouts, else a
        # short default
        labmcp = _labmcp()
        filled = copy.deepcopy(dict(program))
        estimates: List[Estimate] = []
        for step in instrument_steps(filled, ("tracks",)):
            if "duration" in step:
                continue
            instrument = step["instrument"]
            tool = self.tools.get(instrument.get("tool"))
            package = tool.package if tool else str(instrument.get("toolType") or "")
            found = labmcp.estimates.estimate_call(
                package, str(instrument.get("command")), instrument.get("params")
            )
            seconds = (
                int(found.seconds) if found.seconds.is_integer() else found.seconds
            )
            step["duration"] = {"type": "fixed", "seconds": seconds}
            step.setdefault("metadata", {})["durationEstimate"] = {
                "source": found.source,
                "seconds": seconds,
            }
            estimates.append(
                Estimate(str(step.get("stepId")), seconds, found.source, found.detail)
            )
        return filled, estimates

    # -- A run -------------------------------------------------------------

    def status(self, tool: str) -> str:
        # A server is only started by inspect() or prepare(); asking before
        # that would start one, and connect it to the instrument.
        for check in self.checks:
            if check.tool == tool:
                return check.status
        return "STARTED" if self.executor.started(tool) else "NOT STARTED"

    def _tool_checks(self, tools: Sequence[str]) -> List[ToolCheck]:
        self.checks = [
            ToolCheck(
                tool=c.tool,
                address=self.tool_address(c.tool),
                status=c.status,
                ready=c.ready,
                detail=c.detail,
                identity=_identity(c.info),
                simulated=(
                    bool(c.info["simulated"]) if "simulated" in c.info else None
                ),
                limits=_limits(c.info, self.tools[c.tool].limits),
            )
            for c in self.executor.prepare(tools)
        ]
        return self.checks

    def inspect(self, tools: Sequence[str]) -> List[ToolCheck]:
        # Starts each server (live unless simulated) and asks what it is
        # connected to: get_connection_info reads, it never acts.
        return self._tool_checks(tools)

    def prepare(self, tools: Sequence[str]) -> List[ToolCheck]:
        return self._tool_checks(tools)

    def call_kind(self, tool: str, command: str) -> Optional[str]:
        kind = self.executor.tool_kind(tool, command) if self.executor else None
        if kind is None and tool in self.tools:
            entry = _labmcp().checks.server_entry(self.tools[tool].package) or {}
            kind = (entry.get("tools", {}).get(command) or {}).get("kind")
        return kind

    def execute(
        self, key: str, instrument: Mapping[str, Any], on_reply: OnReply
    ) -> None:
        self.executor.submit(
            key, instrument, lambda k, result: on_reply(k, _reply_dict(result))
        )

    def pause_calls(self, tool: str) -> List[Dict[str, Any]]:
        # e.g. Opentrons pause_run: the robot finishes its step and holds
        if self.executor is None:
            return []
        return [{"command": n, "params": {}} for n in self.executor.pause_tools(tool)]

    def resume_calls(self, tool: str) -> List[Dict[str, Any]]:
        if self.executor is None:
            return []
        return [{"command": n, "params": {}} for n in self.executor.resume_tools(tool)]

    def default_stops(self, tool: str) -> List[Dict[str, Any]]:
        # The server's safety tools that need no arguments (stop_all, ...)
        if self.executor is None:
            return []
        return [
            {"command": name, "params": {}}
            for name in self.executor.default_stops(tool)
        ]

    def shutdown(self) -> List[str]:
        return self.executor.shutdown() if self.executor is not None else []
