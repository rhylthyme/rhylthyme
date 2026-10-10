"""
Instrument drivers: the backends that run a program's ``instrument`` steps.

A workcell file names each tool's driver, ``galago`` when it names none::

    {"id": "bench-1", "name": "Bench 1",
     "tools": [{"name": "shaker", "type": "bioshake",
                "host": "10.0.0.12", "port": 50010},
               {"name": "balance", "driver": "labmcp", ...}]}

The runner splits the workcell by driver and talks to every backend through
:class:`InstrumentDriver`, so a run, its pre-flight, validation, duration
estimates and the web bridge treat all instruments alike. galago-tools
(``rhylthyme-galago``) and LabMCP (``rhylthyme-labmcp``) are built in;
other packages register a driver class under the
``rhylthyme.instrument_drivers`` entry-point group, and tests can
``register_driver`` one directly.

Workcells stay on the lab machine: a driver's tool addresses are for local
pre-flight output only and are scrubbed from anything published.
"""

import copy
import importlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from ..instance_checks import Finding

ENTRY_POINT_GROUP = "rhylthyme.instrument_drivers"

#: The driver of a workcell tool that names none, and of a step checked
#: without a workcell that no other driver claims.
DEFAULT_DRIVER = "galago"

#: Reply to a step: {"ok": bool, "code": str, "errorMessage": str, "metadata": dict}
Reply = Dict[str, Any]
OnReply = Callable[[str, Reply], None]

#: Built-in drivers: module:class, and what to install when it is missing.
_BUILTIN: Dict[str, Tuple[str, str]] = {
    "galago": (
        "rhylthyme.drivers.galago:GalagoDriver",
        "Instrument steps need rhylthyme-galago: pip install 'rhylthyme[galago]'",
    ),
    "labmcp": (
        "rhylthyme.drivers.labmcp:LabMCPDriver",
        "LabMCP instrument tools need rhylthyme-labmcp: "
        "pip install 'rhylthyme[labmcp]'",
    ),
}

_registered: Dict[str, Type["InstrumentDriver"]] = {}


class WorkcellError(ValueError):
    """A workcell file that cannot be used."""


class DriverUnavailable(RuntimeError):
    """A workcell or step needs a driver whose package is not installed."""


@dataclass(frozen=True)
class ToolCheck:
    """
    One tool's readiness before a run. ``address`` is for local output only.
    ``identity`` (the instrument's own name, model, serial), ``simulated``
    (the mode it reports, None if unknown) and ``limits`` (active safety
    limits, as text) are shown in the live pre-flight when a driver knows them.
    """

    tool: str
    address: str
    status: str
    ready: bool
    detail: str = ""
    identity: str = ""
    simulated: Optional[bool] = None
    limits: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Estimate:
    """A planning duration for an instrument step that has none."""

    step_id: str
    seconds: float
    source: str  # "tool" | "params" | "default", or a driver's own
    detail: str = ""


@dataclass(frozen=True)
class ToolView:
    """
    A tool as the bridge's scrubber sees it: every local value to hide.
    ``host``/``port``/``config`` are galago's (host and host:port become the
    tool's name, config values ``<config>``); ``private`` values, any
    driver's, become ``<name address>``.
    """

    name: str
    host: str = ""
    port: Any = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    private: Tuple[str, ...] = ()

    def secrets(self) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        if self.host and self.port not in ("", None):
            pairs.append((f"{self.host}:{self.port}", self.name))
        if self.host:
            pairs.append((self.host, self.name))
        for value in (self.config or {}).values():
            if isinstance(value, str) and value:
                pairs.append((value, "<config>"))
        for value in self.private:
            if value:
                pairs.append((value, f"<{self.name} address>"))
        return pairs


class InstrumentDriver(ABC):
    """
    One instrument backend, holding the workcell tools that name it.

    Constructing a driver touches no instrument. ``tools`` is None when there
    is no workcell (validation and planning offline); then only ``claims``,
    ``check`` and ``fill_durations`` are used.
    """

    #: The ``driver`` value workcell tools use for this backend.
    name: ClassVar[str] = ""

    def __init__(
        self,
        workcell_id: str,
        tools: Optional[Sequence[Mapping[str, Any]]],
        *,
        live: bool = False,
        **options: Any,
    ):
        self.workcell_id = workcell_id
        self.live = live
        self.checks: List[ToolCheck] = []

    @classmethod
    def ensure_available(cls) -> None:
        """Raise DriverUnavailable when the backend's package is missing."""

    @classmethod
    def claims(cls, instrument: Mapping[str, Any]) -> bool:
        """Without a workcell: is this step's ``instrument`` for this driver?"""
        return False

    # -- The workcell ------------------------------------------------------

    @property
    @abstractmethod
    def tool_names(self) -> List[str]: ...

    @abstractmethod
    def tool_type(self, tool: str) -> str:
        """What kind of instrument ``tool`` is, for summaries and the bridge."""

    @abstractmethod
    def tool_address(self, tool: str) -> str:
        """Where ``tool`` is reached, for local pre-flight output only."""

    def tool_view(self, tool: str) -> ToolView:
        """What the bridge must scrub for ``tool``."""
        return ToolView(tool, private=(self.tool_address(tool),))

    # -- Offline: validation and planning ----------------------------------

    @abstractmethod
    def check(self, program: Mapping[str, Any]) -> List[Finding]:
        """Findings for ``program``'s instrument steps, all of them this driver's."""

    def workcell_findings(self) -> List[Finding]:
        """Findings about this driver's workcell entries themselves."""
        return []

    @abstractmethod
    def fill_durations(
        self, program: Mapping[str, Any]
    ) -> Tuple[Dict[str, Any], List[Estimate]]:
        """
        A copy of ``program`` whose instrument steps (all this driver's) have
        a duration: authored ones untouched, the rest estimated and flagged in
        ``metadata.durationEstimate``.
        """

    # -- A run -------------------------------------------------------------

    @abstractmethod
    def status(self, tool: str) -> str:
        """Read ``tool``'s status without changing anything."""

    def inspect(self, tools: Sequence[str]) -> List[ToolCheck]:
        """
        Before a live run is confirmed: what each tool is, without changing
        any setting or moving anything. A check that is not ready refuses the
        live run before the operator is asked. By default only the status is
        read, and readiness is left to ``prepare``.
        """
        return [ToolCheck(t, self.tool_address(t), self.status(t), True) for t in tools]

    def pause_calls(self, tool: str) -> List[Dict[str, Any]]:
        """
        Calls (``{command, params}``) that hold ``tool``'s work when the
        schedule is paused, so it can resume; none by default (the tool
        carries on while the schedule's clock is stopped).
        """
        return []

    def resume_calls(self, tool: str) -> List[Dict[str, Any]]:
        """Calls that continue what ``pause_calls`` held."""
        return []

    def call_kind(self, tool: str, command: str) -> Optional[str]:
        """``read``, ``control``, ``hazard`` or ``safety`` if the driver knows."""
        return None

    @abstractmethod
    def prepare(self, tools: Sequence[str]) -> List[ToolCheck]:
        """
        Connect to and ready ``tools`` (simulated unless live); one check per
        tool, ready only when it can run in this mode.
        """

    @abstractmethod
    def execute(
        self, key: str, instrument: Mapping[str, Any], on_reply: OnReply
    ) -> None:
        """
        Send one step's ``instrument`` without blocking; call
        ``on_reply(key, reply)`` exactly once, from any thread, when it ends.
        """

    def default_stops(self, tool: str) -> List[Dict[str, Any]]:
        """
        The calls (``{command, params}``) that put ``tool`` in a safe state
        when a step using it fails or the run is aborted and the step names
        no ``onAbort`` actions. Known once the tool is prepared; none by
        default.
        """
        return []

    def describe(self) -> List[Dict[str, str]]:
        """The prepared tools for the bridge: name, type and status, no address."""
        return [
            {"name": c.tool, "type": self.tool_type(c.tool), "status": c.status}
            for c in self.checks
        ]

    @abstractmethod
    def shutdown(self) -> List[str]:
        """Release every tool; return keys whose commands never replied."""

    @property
    def version(self) -> str:
        return ""


# -- Registry ----------------------------------------------------------------


def register_driver(name: str, cls: Type[InstrumentDriver]) -> None:
    """Make ``cls`` the driver for workcell tools with ``"driver": name``."""
    _registered[name] = cls


def unregister_driver(name: str) -> None:
    _registered.pop(name, None)


def install_hint(name: str) -> str:
    if name in _BUILTIN:
        return _BUILTIN[name][1]
    return (
        f"Instrument steps on {name} tools need rhylthyme-{name}: "
        f"pip install 'rhylthyme[{name}]'"
    )


def _load(target: str) -> Type[InstrumentDriver]:
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def _entry_points() -> Dict[str, Any]:
    from importlib.metadata import entry_points

    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def known_drivers() -> List[str]:
    return sorted(set(_registered) | set(_BUILTIN) | set(_entry_points()))


def driver_class(name: str) -> Type[InstrumentDriver]:
    """The installed driver ``name``; DriverUnavailable if it cannot be used."""
    try:
        if name in _registered:
            cls = _registered[name]
        elif name in _BUILTIN:
            cls = _load(_BUILTIN[name][0])
        else:
            ep = _entry_points().get(name)
            if ep is None:
                raise DriverUnavailable(
                    f"Unknown instrument driver {name!r} "
                    f"(known: {', '.join(known_drivers())}). {install_hint(name)}"
                )
            cls = ep.load()
        cls.ensure_available()
    except ImportError:
        raise DriverUnavailable(install_hint(name)) from None
    return cls


def claiming_driver(instrument: Mapping[str, Any]) -> str:
    """Without a workcell: the driver a step's ``instrument`` belongs to."""
    for name, cls in list(_registered.items()):
        if name != DEFAULT_DRIVER and cls.claims(instrument):
            return name
    for name, (target, _hint) in _BUILTIN.items():
        if name == DEFAULT_DRIVER or name in _registered:
            continue
        # The adapter class loads without its package; claims() needs none
        if _load(target).claims(instrument):
            return name
    for name, ep in _entry_points().items():
        if name in _registered or name == DEFAULT_DRIVER:
            continue
        try:
            if ep.load().claims(instrument):
                return name
        except ImportError:
            continue
    return DEFAULT_DRIVER


# -- Workcells ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolEntry:
    name: str
    driver: str
    index: int
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class Workcell:
    """A workcell split by driver. Holds raw tool entries; opens nothing."""

    id: str
    name: str
    tools: Mapping[str, ToolEntry]

    def driver_of(self, tool: Any) -> Optional[str]:
        entry = self.tools.get(tool)
        return entry.driver if entry is not None else None

    def by_driver(self) -> Dict[str, List[ToolEntry]]:
        groups: Dict[str, List[ToolEntry]] = {}
        for entry in sorted(self.tools.values(), key=lambda e: e.index):
            groups.setdefault(entry.driver, []).append(entry)
        return groups

    def unknown_tool_text(self, tool: Any) -> str:
        known = ", ".join(sorted(self.tools)) or "none"
        return f"Workcell {self.id!r} has no tool named {tool!r} (tools: {known})"


def parse_workcell(data: Any) -> Workcell:
    if not isinstance(data, dict):
        raise WorkcellError("A workcell must be a JSON object")
    raw_tools = data.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise WorkcellError("A workcell needs a non-empty 'tools' list")
    tools: Dict[str, ToolEntry] = {}
    unnamed = 0
    for i, raw in enumerate(raw_tools):
        if not isinstance(raw, dict):
            raise WorkcellError(f"tools[{i}] must be an object")
        driver = raw.get("driver", DEFAULT_DRIVER)
        if not isinstance(driver, str) or not driver:
            raise WorkcellError(f"tools[{i}] driver must be a non-empty string")
        name = raw.get("name")
        if name in (None, ""):
            # The driver reports what else is missing, with this index
            name = f"\0unnamed-{unnamed}"
            unnamed += 1
        name = str(name)
        if name in tools:
            raise WorkcellError(f"Duplicate tool name {name!r}")
        tools[name] = ToolEntry(name, driver, i, raw)
    workcell_id = str(data.get("id") or data.get("name") or "workcell")
    return Workcell(workcell_id, str(data.get("name", workcell_id)), tools)


def load_workcell(source: Union[str, Path, Mapping[str, Any], Workcell]) -> Workcell:
    """Load a workcell from a path or an already-parsed dict."""
    if isinstance(source, Workcell):
        return source
    if isinstance(source, Mapping):
        return parse_workcell(dict(source))
    path = Path(source)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise WorkcellError(f"No such workcell file: {path}") from None
    except json.JSONDecodeError as e:
        raise WorkcellError(f"{path} is not valid JSON: {e}") from None
    return parse_workcell(data)


_INDEX_RE = re.compile(r"tools\[(\d+)\]")


def open_driver(
    workcell: Optional[Workcell],
    name: str,
    *,
    live: bool = False,
    options: Optional[Mapping[str, Any]] = None,
) -> InstrumentDriver:
    """
    The driver ``name`` over its tools in ``workcell`` (offline when there
    are none).
    Raises DriverUnavailable or WorkcellError (indices as in the file).
    """
    cls = driver_class(name)
    entries = workcell.by_driver().get(name, []) if workcell is not None else []
    if workcell is None or not entries:
        return cls("", None, live=live, **dict(options or {}))
    try:
        return cls(
            workcell.id,
            [e.raw for e in entries],
            live=live,
            workcell_name=workcell.name,
            **dict(options or {}),
        )
    except WorkcellError as e:
        # The driver numbered its own tools; report the file's numbering
        text = _INDEX_RE.sub(
            lambda m: (
                f"tools[{entries[int(m.group(1))].index}]"
                if int(m.group(1)) < len(entries)
                else m.group(0)
            ),
            str(e),
        )
        raise WorkcellError(text) from None


# -- Programs ----------------------------------------------------------------


def instrument_steps(
    program: Mapping[str, Any], groups: Sequence[str] = ("tracks", "trackTemplates")
) -> Iterator[Dict[str, Any]]:
    for group in groups:
        for track in program.get(group) or []:
            for step in track.get("steps") or []:
                # Program JSON: steps are dicts
                if isinstance(step, dict) and isinstance(
                    step.get("instrument"), Mapping
                ):
                    yield step


#: When a step's actions are sent: ``start`` in order at step start, then the
#: blocking ``call`` (``command``) or ``until``; ``end`` in order when the step
#: ends; ``onAbort`` when it fails or the run is aborted.
PHASES = ("start", "call", "until", "end", "onAbort")


def _action(
    instrument: Mapping[str, Any], raw: Mapping[str, Any], phase: str
) -> Dict[str, Any]:
    """One action with its tool resolved (the step's tool by default)."""
    tool = raw.get("tool") or instrument.get("tool")
    action: Dict[str, Any] = {
        "phase": phase,
        "tool": tool,
        "command": raw.get("command"),
        "params": dict(raw.get("params") or {}),
    }
    if raw.get("timeoutSeconds") is not None:
        action["timeoutSeconds"] = raw["timeoutSeconds"]
    if instrument.get("toolType") and tool == instrument.get("tool"):
        action["toolType"] = instrument["toolType"]
    return action


def blocking_action(instrument: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The call whose reply ends the step (``command`` or ``until``), if any."""
    if not isinstance(instrument, Mapping):
        return None
    if instrument.get("command"):
        return _action(instrument, instrument, "call")
    until = instrument.get("until")
    if isinstance(until, Mapping) and until.get("command"):
        return _action(instrument, until, "until")
    return None


def phase_actions(instrument: Mapping[str, Any], phase: str) -> List[Dict[str, Any]]:
    """The ``start``, ``end`` or ``onAbort`` actions, tools resolved."""
    raw = instrument.get(phase) if isinstance(instrument, Mapping) else None
    if not isinstance(raw, list):
        return []
    return [_action(instrument, a, phase) for a in raw if isinstance(a, Mapping)]


def step_calls(instrument: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every call a step may send, in phase order."""
    calls = phase_actions(instrument, "start")
    main = blocking_action(instrument)
    if main is not None:
        calls.append(main)
    return (
        calls + phase_actions(instrument, "end") + phase_actions(instrument, "onAbort")
    )


def step_tools(instrument: Mapping[str, Any]) -> List[str]:
    """Every workcell tool a step touches: its own and its actions'."""
    tools = [instrument.get("tool")] if isinstance(instrument, Mapping) else []
    tools += [c["tool"] for c in step_calls(instrument)]
    return sorted({t for t in tools if t})


def call_program(
    program: Mapping[str, Any],
    calls: Sequence[Tuple[str, Mapping[str, Any]]],
) -> Dict[str, Any]:
    """
    A program of one-call steps, ``(step_id, call)`` each, for drivers that
    check and estimate single commands: every call a driver should see,
    whatever phase it belongs to, looks like a plain instrument step. Its
    ``instrument.phase`` says which phase, and its ``duration`` (when the
    call carries ``stepSeconds``) how long the real step may run.
    """
    steps = []
    for step_id, call in calls:
        instrument = {
            k: call[k]
            for k in (
                "tool",
                "command",
                "params",
                "toolType",
                "timeoutSeconds",
                "phase",
            )
            if call.get(k) not in (None, "")
        }
        step: Dict[str, Any] = {"stepId": step_id, "instrument": instrument}
        if call.get("stepSeconds") is not None:
            # How long the call's step may run, for drivers that check timing
            step["duration"] = {"type": "fixed", "seconds": call["stepSeconds"]}
        steps.append(step)
    return {
        "programId": program.get("programId", ""),
        "tracks": [{"trackId": "calls", "steps": steps}],
    }


def step_order(program: Mapping[str, Any]) -> Dict[str, int]:
    return {
        str(step.get("stepId")): i for i, step in enumerate(instrument_steps(program))
    }


def only_steps(program: Mapping[str, Any], step_ids: Sequence[str]) -> Dict[str, Any]:
    """A copy of ``program`` keeping, of its instrument steps, only ``step_ids``."""
    keep = set(step_ids)
    out = copy.deepcopy(dict(program))
    for group in ("tracks", "trackTemplates"):
        for track in out.get(group) or []:
            track["steps"] = [
                step
                for step in track.get("steps") or []
                if not (isinstance(step, Mapping) and step.get("instrument"))
                or str(step.get("stepId")) in keep
            ]
    return out


__all__ = [
    "DEFAULT_DRIVER",
    "DriverUnavailable",
    "ENTRY_POINT_GROUP",
    "Estimate",
    "InstrumentDriver",
    "PHASES",
    "OnReply",
    "Reply",
    "ToolCheck",
    "ToolEntry",
    "ToolView",
    "Workcell",
    "WorkcellError",
    "blocking_action",
    "call_program",
    "claiming_driver",
    "driver_class",
    "install_hint",
    "instrument_steps",
    "known_drivers",
    "load_workcell",
    "only_steps",
    "open_driver",
    "parse_workcell",
    "phase_actions",
    "register_driver",
    "step_calls",
    "step_order",
    "step_tools",
    "unregister_driver",
]
