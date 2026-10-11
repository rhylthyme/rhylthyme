"""The runner's instrument paths through the driver interface, with no galago."""

import copy
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from rhylthyme import drivers as D
from rhylthyme.instance_checks import Finding
from rhylthyme.instruments import (
    InstrumentSetupError,
    attach_instruments,
    fill_instrument_durations,
    instrument_findings,
    open_instruments,
)
from rhylthyme.program_runner import ProgramRunner, StepStatus

pytestmark = pytest.mark.unit


class FakeDriver(D.InstrumentDriver):
    """
    Tools ``{"name", "driver": "fake", "type", "where"}``. ``replies`` maps a
    command to a reply dict (unscripted commands succeed); ``gate`` holds
    every command until set, ``held`` commands wait for ``release``.
    ``stops`` maps a tool name to its default stop commands.
    """

    name = "fake"
    instances: List["FakeDriver"] = []
    replies: Dict[str, Dict[str, Any]] = {}
    gate: Optional[threading.Event] = None
    held: set = set()
    release = threading.Event()
    stops: Dict[str, List[str]] = {}
    status_value = "READY"

    def __init__(self, workcell_id, tools, *, live=False, **options):
        super().__init__(workcell_id, tools, live=live)
        self.options = options
        self.tools = {}
        for i, raw in enumerate(tools or []):
            if "type" not in raw:
                raise D.WorkcellError(f"tools[{i}] is missing type")
            self.tools[raw["name"]] = raw
        self.sent: List[Dict[str, Any]] = []
        self.prepared: List[str] = []
        self.closed = False
        FakeDriver.instances.append(self)

    @classmethod
    def claims(cls, instrument):
        return instrument.get("toolType") == "fake-thing"

    @property
    def tool_names(self):
        return sorted(self.tools)

    def tool_type(self, tool):
        return self.tools[tool]["type"]

    def tool_address(self, tool):
        return self.tools[tool]["where"]

    def check(self, program):
        return [
            Finding(
                code="fake_checked",
                message=f"checked {step['stepId']}",
                where=f"step:{step['stepId']}",
                severity="warning",
            )
            for step in D.instrument_steps(program)
        ]

    def fill_durations(self, program):
        filled = copy.deepcopy(dict(program))
        found = []
        for step in D.instrument_steps(filled, ("tracks",)):
            if "duration" not in step:
                step["duration"] = {"type": "fixed", "seconds": 7}
                step.setdefault("metadata", {})["durationEstimate"] = {
                    "source": "fake",
                    "seconds": 7,
                }
                found.append(D.Estimate(step["stepId"], 7, "fake", "the fake"))
        return filled, found

    def default_stops(self, tool):
        return [{"command": c, "params": {}} for c in self.stops.get(tool, [])]

    def status(self, tool):
        return self.status_value

    def prepare(self, tools):
        self.prepared.extend(tools)
        self.checks = [
            D.ToolCheck(
                t,
                self.tool_address(t),
                self.status_value,
                self.status_value == "READY",
                "" if self.status_value == "READY" else "unplugged",
            )
            for t in tools
        ]
        return self.checks

    def execute(self, key, instrument, on_reply):
        self.sent.append(dict(instrument))
        reply = self.replies.get(
            instrument["command"],
            {"ok": True, "code": "SUCCESS", "errorMessage": "", "metadata": {}},
        )

        def run():
            if self.gate is not None:
                self.gate.wait()
            if instrument["command"] in self.held:
                self.release.wait(5)
            on_reply(key, reply)

        threading.Thread(target=run, daemon=True).start()

    def shutdown(self):
        self.closed = True
        return []


@pytest.fixture(autouse=True)
def fake_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    FakeDriver.instances = []
    FakeDriver.replies = {}
    FakeDriver.gate = None
    FakeDriver.held = set()
    FakeDriver.release = threading.Event()
    FakeDriver.stops = {}
    FakeDriver.status_value = "READY"
    D.register_driver("fake", FakeDriver)
    yield FakeDriver
    D.unregister_driver("fake")


PROGRAM = {
    "programId": "weigh",
    "name": "Weigh",
    "startTrigger": {"type": "manual"},
    "tracks": [
        {
            "trackId": "bench",
            "name": "Bench",
            "steps": [
                {
                    "stepId": "tare",
                    "name": "Tare",
                    "instrument": {"tool": "balance", "command": "tare"},
                    "startTrigger": {"type": "programStart"},
                },
                {
                    "stepId": "after",
                    "name": "After",
                    "duration": {"type": "fixed", "seconds": 1},
                    "startTrigger": {"type": "afterStep", "stepId": "tare"},
                },
            ],
        }
    ],
    "resourceConstraints": [],
}
WORKCELL = {
    "id": "bench",
    "tools": [{"name": "balance", "driver": "fake", "type": "scale", "where": "COM9"}],
}


def run_until(runner, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def started(program=PROGRAM):
    runner = ProgramRunner(copy.deepcopy(program), time_scale=100.0)
    events = []
    runner.add_event_listener(lambda kind, data: events.append((kind, data)))
    session = attach_instruments(runner, WORKCELL)
    runner.start()
    runner.command_queue.put("start_program")
    return runner, session, events


def failing(message="overload"):
    return {
        "tare": {
            "ok": False,
            "code": "TOOL_ERROR",
            "errorMessage": message,
            "metadata": {},
        }
    }


# --- Workcells -------------------------------------------------------------


def test_tools_without_a_driver_are_galago():
    wc = D.load_workcell(
        {
            "id": "w",
            "tools": [
                {"name": "a", "type": "bioshake", "host": "h", "port": 1},
                {"name": "b", "driver": "fake", "type": "scale", "where": "x"},
            ],
        }
    )
    assert wc.driver_of("a") == "galago" and wc.driver_of("b") == "fake"
    assert [e.name for e in wc.by_driver()["fake"]] == ["b"]


def test_duplicate_names_across_drivers_are_refused():
    with pytest.raises(D.WorkcellError, match="Duplicate tool name 'a'"):
        D.load_workcell(
            {
                "tools": [
                    {"name": "a", "type": "bioshake", "host": "h", "port": 1},
                    {"name": "a", "driver": "fake", "type": "scale"},
                ]
            }
        )


def test_driver_errors_name_the_tool_index_in_the_file():
    workcell = {
        "id": "w",
        "tools": [
            {"name": "a", "driver": "other", "type": "t"},
            {"name": "b", "driver": "fake", "where": "x"},
        ],
    }
    with pytest.raises(D.WorkcellError, match=r"tools\[1\] is missing type"):
        D.open_driver(D.load_workcell(workcell), "fake")


def test_unknown_driver_is_refused_by_name():
    workcell = {"id": "w", "tools": [{"name": "a", "driver": "nope", "type": "t"}]}
    with pytest.raises(InstrumentSetupError, match="Unknown instrument driver 'nope'"):
        open_instruments(
            {
                **PROGRAM,
                "tracks": [
                    {
                        "trackId": "t",
                        "steps": [
                            {
                                "stepId": "s",
                                "instrument": {"tool": "a", "command": "go"},
                            }
                        ],
                    }
                ],
            },
            workcell,
        )


def test_entry_point_drivers_are_found(monkeypatch):
    class EP:
        name = "plugged"

        def load(self):
            return FakeDriver

    monkeypatch.setattr(D, "_entry_points", lambda: {"plugged": EP()})
    assert D.driver_class("plugged") is FakeDriver
    assert "plugged" in D.known_drivers()


def test_galago_workcell_without_galago_gives_the_install_hint():
    workcell = {
        "id": "w",
        "tools": [
            {"name": "balance", "driver": "fake", "type": "scale", "where": "x"},
            {"name": "shaker", "type": "bioshake", "host": "h", "port": 1},
        ],
    }
    with pytest.raises(InstrumentSetupError, match=r"rhylthyme\[galago\]"):
        open_instruments(PROGRAM, workcell)


# --- A run -----------------------------------------------------------------


def test_opening_touches_no_tool():
    session = open_instruments(PROGRAM, WORKCELL)
    [driver] = FakeDriver.instances
    assert session.tools == ["balance"] and driver.prepared == [] and not session.live


def test_step_is_sent_and_ends_on_the_reply(fake_driver):
    fake_driver.gate = threading.Event()
    runner, session, events = started()
    try:
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.RUNNING)
        run_until(runner, lambda: False, timeout=0.5)
        assert tare.status == StepStatus.RUNNING  # waits for the instrument
        fake_driver.gate.set()
        assert run_until(runner, lambda: tare.status == StepStatus.COMPLETED)
    finally:
        session.shutdown()
    [driver] = FakeDriver.instances
    assert driver.prepared == ["balance"] and driver.closed
    assert driver.sent == [{"tool": "balance", "command": "tare", "params": {}}]
    completed = [
        d for k, d in events if k == "step_completed" and d["step_id"] == "tare"
    ]
    assert completed[0]["ended_by"] == "instrument"
    assert [d["code"] for k, d in events if k == "instrument_reply"] == ["SUCCESS"]


def test_error_reply_fails_the_step_with_the_drivers_message(fake_driver):
    fake_driver.replies = failing("overload: 220 g")
    runner, session, events = started()
    try:
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
    finally:
        session.shutdown()
    assert tare.failure == {
        "tool": "balance",
        "command": "tare",
        "code": "TOOL_ERROR",
        "errorMessage": "overload: 220 g",
    }
    assert runner.failed_steps == ["tare"]


def test_retry_resends_through_the_driver(fake_driver):
    fake_driver.replies = failing()
    runner, session, _ = started()
    try:
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
        fake_driver.replies = {}
        runner.command_queue.put("retry:tare")
        assert run_until(runner, lambda: tare.status == StepStatus.COMPLETED)
    finally:
        session.shutdown()
    [driver] = FakeDriver.instances
    assert [s["command"] for s in driver.sent] == ["tare", "tare"]
    assert tare.instrument_attempts == 2


def test_skip_continues_the_schedule(fake_driver):
    fake_driver.replies = failing()
    runner, session, events = started()
    try:
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
        runner.command_queue.put("skip:tare")
        assert run_until(
            runner, lambda: runner.steps["after"].status != StepStatus.PENDING
        )
    finally:
        session.shutdown()
    ended = [d for k, d in events if k == "step_completed" and d["step_id"] == "tare"]
    assert ended[0]["ended_by"] == "skipped"


def test_abort_ends_the_program(fake_driver):
    fake_driver.replies = failing()
    runner, session, _ = started()
    try:
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
        runner.command_queue.put("abort_program:operator")
        assert run_until(runner, lambda: runner.program_abort_reason is not None)
    finally:
        session.shutdown()
    assert tare.status == StepStatus.ABORTED


def test_not_ready_is_refused_with_the_report(fake_driver):
    fake_driver.status_value = "OFFLINE"
    runner = ProgramRunner(copy.deepcopy(PROGRAM))
    with pytest.raises(
        InstrumentSetupError, match=r"balance @ COM9: OFFLINE \[NOT READY\] unplugged"
    ):
        attach_instruments(runner, WORKCELL)
    assert FakeDriver.instances[0].closed


def test_summary_and_describe(fake_driver):
    session = open_instruments(PROGRAM, WORKCELL, live=True)
    text = "\n".join(session.summary(PROGRAM))
    assert "Workcell 'bench' (LIVE: real hardware will move)" in text
    assert "balance (scale) @ COM9: READY" in text
    assert "tare: balance.tare()" in text
    session.prepare()
    assert session.describe_tools() == [
        {"name": "balance", "type": "scale", "status": "READY"}
    ]
    view = session.bridge_workcell()
    assert view.id == "bench" and view.tools["balance"].private == ("COM9",)
    session.shutdown()


def test_driver_options_reach_the_driver():
    open_instruments(PROGRAM, WORKCELL, driver_options={"fake": {"answer": 42}})
    assert FakeDriver.instances[0].options["answer"] == 42


# --- Validation and planning -------------------------------------------------


def test_validation_goes_to_the_workcells_driver():
    findings = instrument_findings(PROGRAM, WORKCELL)
    assert [(f.code, f.where) for f in findings] == [("fake_checked", "step:tare")]


def test_validation_without_a_workcell_asks_which_driver_claims_the_step():
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][0]["instrument"]["toolType"] = "fake-thing"
    assert [f.code for f in instrument_findings(program)] == ["fake_checked"]
    # Unclaimed steps fall to galago, which is not installed here
    [finding] = instrument_findings(PROGRAM)
    assert (finding.code, finding.severity) == ("instrument_unchecked", "warning")


def test_validation_reports_tools_the_workcell_lacks():
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][0]["instrument"]["tool"] = "pump"
    [finding] = instrument_findings(program, WORKCELL)
    assert finding.code == "instrument_unknown_tool"
    assert "no tool 'pump' (tools: balance)" in finding.message


def test_fill_durations_through_the_driver():
    filled, lines = fill_instrument_durations(copy.deepcopy(PROGRAM), WORKCELL)
    tare = filled["tracks"][0]["steps"][0]
    assert tare["duration"] == {"type": "fixed", "seconds": 7}
    assert lines == ["Estimated instrument durations:", "  tare: 7 s (from the fake)"]
    assert "duration" not in PROGRAM["tracks"][0]["steps"][0]
