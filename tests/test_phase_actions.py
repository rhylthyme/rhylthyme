"""Instrument phase actions: start, until, end (phase 3 of the LabMCP plan)."""

import copy
import json
import threading
import time

import pytest
from test_drivers import FakeDriver

from rhylthyme import drivers as D
from rhylthyme.instruments import attach_instruments, instrument_findings
from rhylthyme.program_runner import ProgramRunner, StepStatus

pytestmark = pytest.mark.unit

WORKCELL = {
    "id": "titration-bench",
    "tools": [
        {"name": "stirrer", "driver": "fake", "type": "hotplate", "where": "COM4"},
        {"name": "ph", "driver": "fake", "type": "ph-probe", "where": "COM5"},
    ],
}
OK = {"ok": True, "code": "SUCCESS", "errorMessage": "", "metadata": {}}


def program(*steps):
    chained = []
    for i, step in enumerate(steps):
        trigger = (
            {"type": "programStart"}
            if i == 0
            else {"type": "afterStep", "stepId": steps[i - 1]["stepId"]}
        )
        chained.append({"name": step["stepId"], "startTrigger": trigger, **step})
    return {
        "programId": "titration",
        "name": "Titration",
        "startTrigger": {"type": "manual"},
        "tracks": [{"trackId": "bench", "name": "Bench", "steps": chained}],
        "resourceConstraints": [],
    }


HEAT = {
    "stepId": "heat",
    "duration": {"type": "fixed", "seconds": 1},
    "instrument": {
        "tool": "stirrer",
        "start": [
            {"command": "set_temperature", "params": {"temperature_c": 40}},
            {"command": "start_heating"},
        ],
        "end": [{"command": "stop_heating"}],
    },
}
PH = {
    "stepId": "ph",
    "instrument": {
        "tool": "ph",
        "start": [
            {"tool": "stirrer", "command": "set_speed", "params": {"speed_rpm": 150}}
        ],
        "until": {"command": "log_series", "params": {"count": 10, "interval_s": 3}},
        "end": [{"tool": "stirrer", "command": "stop_all"}],
    },
}
AFTER = {"stepId": "after", "duration": {"type": "fixed", "seconds": 1}}


@pytest.fixture(autouse=True)
def fake(monkeypatch):
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


def run_until(runner, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def started(prog, time_scale=100.0):
    runner = ProgramRunner(copy.deepcopy(prog), time_scale=time_scale)
    events = []
    runner.add_event_listener(lambda kind, data: events.append((kind, data)))
    session = attach_instruments(runner, WORKCELL)
    runner.start()
    runner.command_queue.put("start_program")
    return runner, session, events


def sent(driver):
    return [(s["tool"], s["command"]) for s in driver.sent]


def ended_by(events, step_id):
    return [
        d["ended_by"]
        for k, d in events
        if k == "step_completed" and d["step_id"] == step_id
    ]


def test_start_and_end_actions_around_the_timer():
    runner, session, events = started(program(HEAT, AFTER))
    try:
        assert run_until(runner, lambda: not runner.is_running)
        run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 3, 2)
    finally:
        session.shutdown()
    [driver] = FakeDriver.instances
    assert sent(driver) == [
        ("stirrer", "set_temperature"),
        ("stirrer", "start_heating"),
        ("stirrer", "stop_heating"),
    ]
    assert driver.sent[0]["params"] == {"temperature_c": 40}
    assert ended_by(events, "heat") == ["timer"]


def test_until_ends_the_step_and_actions_may_name_another_tool(fake):
    fake.gate = threading.Event()
    runner, session, events = started(program(PH, AFTER))
    try:
        ph = runner.steps["ph"]
        assert run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 1)
        run_until(runner, lambda: False, timeout=0.3)
        # The start action is in flight: nothing else has gone out
        assert sent(FakeDriver.instances[0]) == [("stirrer", "set_speed")]
        assert ph.status == StepStatus.RUNNING
        fake.gate.set()
        assert run_until(runner, lambda: ph.status == StepStatus.COMPLETED)
        run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 3, 2)
    finally:
        session.shutdown()
    assert sent(FakeDriver.instances[0]) == [
        ("stirrer", "set_speed"),
        ("ph", "log_series"),
        ("stirrer", "stop_all"),
    ]
    assert ended_by(events, "ph") == ["instrument"]
    # The step holds every tool its actions name
    assert set(runner.steps["ph"].task_types) >= {"ph", "stirrer"}


def test_end_actions_follow_an_operator_end():
    step = {k: v for k, v in HEAT.items() if k != "duration"}
    runner, session, events = started(program(step, AFTER))
    try:
        heat = runner.steps["heat"]
        assert run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 2)
        run_until(runner, lambda: False, timeout=0.3)
        assert heat.status == StepStatus.RUNNING  # no duration: waits for a person
        runner.complete_step(heat, runner.current_time, ended_by="executor")
        assert run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 3)
    finally:
        session.shutdown()
    assert sent(FakeDriver.instances[0])[-1] == ("stirrer", "stop_heating")


def test_a_failed_start_action_fails_the_step_and_sends_no_more(fake):
    fake.replies = {
        "set_speed": {
            "ok": False,
            "code": "TOOL_ERROR",
            "errorMessage": "speed above limit",
            "metadata": {},
        }
    }
    runner, session, _ = started(program(PH, AFTER))
    try:
        ph = runner.steps["ph"]
        assert run_until(runner, lambda: ph.status == StepStatus.FAILED)
        run_until(runner, lambda: False, timeout=0.3)
    finally:
        session.shutdown()
    assert sent(FakeDriver.instances[0]) == [("stirrer", "set_speed")]
    assert ph.failure == {
        "tool": "stirrer",
        "command": "set_speed",
        "code": "TOOL_ERROR",
        "errorMessage": "speed above limit",
    }


def test_retry_resends_the_start_actions(fake):
    fake.replies = {"log_series": {**OK, "ok": False, "code": "TOOL_ERROR"}}
    runner, session, _ = started(program(PH, AFTER))
    try:
        ph = runner.steps["ph"]
        assert run_until(runner, lambda: ph.status == StepStatus.FAILED)
        fake.replies = {}
        runner.command_queue.put("retry:ph")
        assert run_until(runner, lambda: ph.status == StepStatus.COMPLETED)
        run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 5, 2)
    finally:
        session.shutdown()
    assert [c for _, c in sent(FakeDriver.instances[0])] == [
        "set_speed",
        "log_series",
        "set_speed",
        "log_series",
        "stop_all",
    ]


def test_the_run_record_keeps_every_call_with_its_phase(tmp_path):
    from rhylthyme.history.recorder import RunRecorder
    from rhylthyme.history.store import validate_run

    prog = program(HEAT, PH)
    runner = ProgramRunner(copy.deepcopy(prog), time_scale=100.0)
    recorder = RunRecorder(runner, source_program=prog, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(runner, WORKCELL)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: not runner.is_running)
        # The last end action replies after the program completes
        run_until(runner, lambda: len(FakeDriver.instances[0].sent) == 6, 2)
        runner.update()
    finally:
        session.shutdown()
    text = open(recorder.finalize()).read()
    assert "COM4" not in text and "COM5" not in text
    record = json.loads(text)
    assert validate_run(record) == []
    steps = {s["stepId"]: s for s in record["steps"]}
    heat = steps["heat"]["instrument"]
    assert (heat["tool"], heat["command"]) == ("stirrer", "")
    assert [(r["phase"], r["command"]) for r in heat["replies"]] == [
        ("start", "set_temperature"),
        ("start", "start_heating"),
        ("end", "stop_heating"),
    ]
    ph = steps["ph"]["instrument"]
    assert ph["command"] == "log_series"
    assert [(r["phase"], r["tool"], r["command"]) for r in ph["replies"]] == [
        ("start", "stirrer", "set_speed"),
        ("until", "ph", "log_series"),
        ("end", "stirrer", "stop_all"),
    ]


def test_validation_routes_every_call_to_its_tools_driver():
    findings = instrument_findings(program(HEAT, PH), WORKCELL)
    checked = [f.message for f in findings if f.code == "fake_checked"]
    assert checked == ["checked heat", "checked ph"]


def test_action_tools_must_be_in_the_workcell():
    ph = copy.deepcopy(PH)
    ph["instrument"]["end"] = [{"tool": "pump", "command": "stop"}]
    [finding] = [
        f
        for f in instrument_findings(program(ph), WORKCELL)
        if f.code == "instrument_unknown_tool"
    ]
    assert "no tool 'pump'" in finding.message and finding.where == "step:ph"


def test_steps_with_only_start_actions_and_no_duration_warn():
    step = {k: v for k, v in HEAT.items() if k != "duration"}
    codes = [f.code for f in instrument_findings(program(step), WORKCELL)]
    assert "instrument_no_end" in codes


def test_only_blocking_steps_get_estimated_durations():
    from rhylthyme.instruments import fill_instrument_durations

    step = {k: v for k, v in HEAT.items() if k != "duration"}
    filled, lines = fill_instrument_durations(program(step, PH), WORKCELL)
    by_id = {s["stepId"]: s for s in filled["tracks"][0]["steps"]}
    assert "duration" not in by_id["heat"]
    assert by_id["ph"]["duration"] == {"type": "fixed", "seconds": 7}
    assert lines[-1] == "  ph: 7 s (from the fake)"


# --- The schema amendment ------------------------------------------------------

SCHEMAS = ["0.2.0-alpha", "0.3.0-alpha"]


def _schema_errors(prog, version):
    import jsonschema
    from rhylthyme_spec import get_program_schema_path

    with open(get_program_schema_path(version)) as fh:
        schema = json.load(fh)
    return [e.message for e in jsonschema.Draft7Validator(schema).iter_errors(prog)]


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_accepts_phase_actions(name):
    assert _schema_errors(program(HEAT, PH, AFTER), name) == []


@pytest.mark.parametrize("name", SCHEMAS)
@pytest.mark.parametrize(
    "instrument",
    [
        {"tool": "ph"},  # nothing to do
        {"tool": "ph", "command": "a", "until": {"command": "b"}},  # both
        {"tool": "ph", "start": []},
        {"tool": "ph", "start": [{"params": {}}]},  # action without command
        {"tool": "ph", "end": [{"command": "a", "phase": "x"}]},
        {"tool": "ph", "until": {"command": "a"}, "params": {"x": 1}, "speed": 3},
    ],
)
def test_schema_refuses_malformed_actions(name, instrument):
    step = {"stepId": "s", "name": "s", "startTrigger": {"type": "programStart"}}
    assert _schema_errors(program({**step, "instrument": instrument}), name)
