"""Safe stops on failure and abort (phase 4 of the LabMCP plan)."""

import copy
import json
import threading
import time

import pytest
from test_drivers import FakeDriver

from rhylthyme import drivers as D
from rhylthyme.instruments import attach_instruments
from rhylthyme.program_runner import ProgramRunner, StepStatus

pytestmark = pytest.mark.unit

WORKCELL = {
    "id": "bench",
    "tools": [
        {"name": "stirrer", "driver": "fake", "type": "hotplate", "where": "COM4"},
        {"name": "ph", "driver": "fake", "type": "ph-probe", "where": "COM5"},
        {"name": "balance", "driver": "fake", "type": "scale", "where": "COM6"},
    ],
}
FAIL = {"ok": False, "code": "TOOL_ERROR", "errorMessage": "overheated", "metadata": {}}


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
        "programId": "stops",
        "name": "Stops",
        "startTrigger": {"type": "manual"},
        "tracks": [{"trackId": "bench", "name": "Bench", "steps": chained}],
        "resourceConstraints": [],
    }


TARE = {"stepId": "tare", "instrument": {"tool": "balance", "command": "tare"}}
HEAT = {
    "stepId": "heat",
    "instrument": {
        "tool": "stirrer",
        "start": [{"command": "start_heating"}],
        "until": {"command": "wait_for_temperature"},
    },
}
LOG = {
    "stepId": "log",
    "instrument": {
        "tool": "ph",
        "start": [{"tool": "stirrer", "command": "set_speed"}],
        "until": {"command": "log_series"},
    },
}
AFTER = {"stepId": "after", "duration": {"type": "fixed", "seconds": 1}}


@pytest.fixture(autouse=True)
def fake():
    FakeDriver.instances = []
    FakeDriver.replies = {}
    FakeDriver.gate = None
    FakeDriver.held = set()
    FakeDriver.release = threading.Event()
    FakeDriver.stops = {
        "stirrer": ["stop_heating", "stop_all"],
        "ph": [],
        "balance": ["reset"],
    }
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


def started(prog):
    runner = ProgramRunner(copy.deepcopy(prog), time_scale=100.0)
    events = []
    runner.add_event_listener(lambda kind, data: events.append((kind, data)))
    session = attach_instruments(runner, WORKCELL)
    runner.start()
    runner.command_queue.put("start_program")
    return runner, session, events


def sent():
    return [(s["tool"], s["command"]) for s in FakeDriver.instances[0].sent]


def stops_of(events, step_id=None):
    return [
        (d["tool"], d["command"], d["code"])
        for k, d in events
        if k == "instrument_reply"
        and d.get("phase") == "onAbort"
        and (step_id is None or d["step_id"] == step_id)
    ]


def test_a_failure_stops_every_tool_the_step_touched_before_the_prompt(fake):
    fake.replies = {"log_series": FAIL}
    fake.held = {"stop_heating", "stop_all"}
    runner, session, events = started(program(LOG, AFTER))
    try:
        log = runner.steps["log"]
        assert run_until(runner, lambda: log.status == StepStatus.FAILED)
        # The stops go out at once; the prompt waits for them
        assert run_until(runner, lambda: ("stirrer", "stop_heating") in sent())
        assert "stopping its instruments safely" in runner.status_message
        assert runner.safe_stops_pending == {"log": 2}
        assert not runner.retry_failed_step("log")
        assert "still being stopped" in runner.status_message
        fake.release.set()
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        assert runner.status_message.startswith("Instruments stopped. Paused new steps")
    finally:
        session.shutdown()
    # stirrer (its start action's tool) and ph (no stops), in step order
    assert stops_of(events, "log") == [
        ("stirrer", "stop_heating", "SUCCESS"),
        ("stirrer", "stop_all", "SUCCESS"),
    ]


def test_on_abort_replaces_the_default_stops(fake):
    fake.replies = {"log_series": FAIL}
    log = copy.deepcopy(LOG)
    log["instrument"]["onAbort"] = [
        {"tool": "stirrer", "command": "set_speed", "params": {"speed_rpm": 0}}
    ]
    runner, session, events = started(program(log))
    try:
        assert run_until(runner, lambda: stops_of(events))
    finally:
        session.shutdown()
    assert stops_of(events) == [("stirrer", "set_speed", "SUCCESS")]
    assert FakeDriver.instances[0].sent[-1]["params"] == {"speed_rpm": 0}


def test_retry_resends_after_the_stops_and_skip_continues(fake):
    fake.replies = {"wait_for_temperature": FAIL}
    runner, session, events = started(program(HEAT, AFTER))
    try:
        heat = runner.steps["heat"]
        assert run_until(runner, lambda: heat.status == StepStatus.FAILED)
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        assert runner.retry_failed_step("heat")
        assert run_until(runner, lambda: heat.status == StepStatus.FAILED)
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        runner.command_queue.put("skip:heat")
        assert run_until(
            runner, lambda: runner.steps["after"].status != StepStatus.PENDING
        )
    finally:
        session.shutdown()
    assert [c for _, c in sent()] == [
        "start_heating",
        "wait_for_temperature",
        "stop_heating",
        "stop_all",
        "start_heating",  # retry re-sends the step's actions
        "wait_for_temperature",
        "stop_heating",  # and a second failure stops it again
        "stop_all",
    ]


@pytest.mark.parametrize("how", ["terminal", "web"])
def test_aborting_the_run_stops_every_tool_it_touched(fake, how):
    fake.gate = None
    fake.held = {"wait_for_temperature"}
    runner, session, events = started(program(TARE, HEAT))
    try:
        heat = runner.steps["heat"]
        assert run_until(runner, lambda: ("stirrer", "wait_for_temperature") in sent())
        if how == "terminal":
            runner.command_queue.put("abort_program:operator")
        else:
            assert runner.apply_remote_command({"kind": "abort", "args": {}})[
                "accepted"
            ]
        assert run_until(runner, lambda: runner.program_abort_reason is not None)
        session.finish(runner, timeout=5)
    finally:
        fake.release.set()
        session.shutdown()
    assert heat.status == StepStatus.ABORTED
    # The running step's tool under its step; the balance (used by an
    # earlier step) under that step
    assert stops_of(events, "heat") == [
        ("stirrer", "stop_heating", "SUCCESS"),
        ("stirrer", "stop_all", "SUCCESS"),
    ]
    assert stops_of(events, "tare") == [("balance", "reset", "SUCCESS")]


def test_a_tool_already_stopped_is_not_stopped_again_on_abort(fake):
    fake.replies = {"wait_for_temperature": FAIL}
    runner, session, events = started(program(HEAT, AFTER))
    try:
        assert run_until(
            runner, lambda: runner.steps["heat"].status == StepStatus.FAILED
        )
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        runner.abort_program("operator")
        session.finish(runner, timeout=5)
    finally:
        session.shutdown()
    assert len(stops_of(events)) == 2  # only the stops after the failure


def test_closing_mid_run_stops_the_tools_and_records_it(fake, tmp_path):
    from rhylthyme.history.recorder import RunRecorder
    from rhylthyme.history.store import validate_run

    fake.held = {"wait_for_temperature"}
    prog = program(HEAT)
    runner = ProgramRunner(copy.deepcopy(prog), time_scale=100.0)
    recorder = RunRecorder(runner, source_program=prog, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(runner, WORKCELL)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: ("stirrer", "wait_for_temperature") in sent())
        session.finish(runner, timeout=5)  # what run_program does on the way out
    finally:
        fake.release.set()
        session.shutdown()
    record = json.loads(open(recorder.finalize(outcome="abandoned")).read())
    assert validate_run(record) == []
    replies = {s["stepId"]: s for s in record["steps"]}["heat"]["instrument"]["replies"]
    assert [(r["phase"], r["command"]) for r in replies] == [
        ("start", "start_heating"),
        ("onAbort", "stop_heating"),
        ("onAbort", "stop_all"),
    ]


def test_tools_without_stops_change_nothing(fake):
    fake.stops = {}
    fake.replies = {"log_series": FAIL}
    runner, session, events = started(program(LOG))
    try:
        assert run_until(
            runner, lambda: runner.steps["log"].status == StepStatus.FAILED
        )
    finally:
        session.shutdown()
    assert runner.safe_stops_pending == {}
    assert runner.status_message == runner.FAILED_PROMPT
    assert stops_of(events) == []
