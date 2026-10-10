"""Instrument steps (galago-tools via rhylthyme-galago) in the program runner."""

import copy
import json
import sys
import threading
import time
from typing import Any, Dict

import pytest

from rhylthyme.cli.tui import run_program
from rhylthyme.instruments import (
    INSTALL_HINT,
    InstrumentSetupError,
    attach_instruments,
    program_uses_instruments,
)
from rhylthyme.program_runner import DurationType, ProgramRunner, StepStatus

pytestmark = pytest.mark.unit

PROGRAM = {
    "programId": "shake-plate",
    "name": "Shake a plate",
    "startTrigger": {"type": "manual"},
    "tracks": [
        {
            "trackId": "bench",
            "name": "Bench",
            "steps": [
                {
                    "stepId": "load",
                    "name": "Load plate",
                    "duration": {"type": "fixed", "seconds": 1},
                    "startTrigger": {"type": "programStart"},
                },
                {
                    "stepId": "shake",
                    "name": "Shake",
                    "instrument": {
                        "tool": "shaker",
                        "command": "start_shake",
                        "params": {"speed": 1000, "duration": 5},
                    },
                    "startTrigger": {"type": "afterStep", "stepId": "load"},
                },
                {
                    "stepId": "unload",
                    "name": "Unload plate",
                    "duration": {"type": "fixed", "seconds": 1},
                    "startTrigger": {"type": "afterStep", "stepId": "shake"},
                },
            ],
        }
    ],
    "resourceConstraints": [],
}
WORKCELL = {
    "id": "bench",
    "tools": [{"name": "shaker", "type": "bioshake", "host": "h", "port": 1}],
}


def run_until(runner, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def started_runner(client, program=PROGRAM):
    pytest.importorskip("rhylthyme_galago")
    runner = ProgramRunner(copy.deepcopy(program), time_scale=100.0)
    events = []
    runner.add_event_listener(lambda kind, data: events.append((kind, data)))
    session = attach_instruments(runner, WORKCELL, client_factory=lambda b: client)
    runner.start()
    runner.command_queue.put("start_program")
    return runner, session, events


def test_program_uses_instruments():
    assert program_uses_instruments(PROGRAM)
    plain = copy.deepcopy(PROGRAM)
    del plain["tracks"][0]["steps"][1]["instrument"]
    assert not program_uses_instruments(plain)


def test_instrument_step_ends_on_the_reply_not_a_timer():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    shaker = FakeToolClient(gate=threading.Event())
    runner, session, events = started_runner(shaker)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.RUNNING)
        # At 100x the planned 60 s placeholder would long have passed; the
        # step still waits for the instrument.
        run_until(runner, lambda: False, timeout=0.8)
        assert shake.status == StepStatus.RUNNING
        assert runner.steps["unload"].status == StepStatus.PENDING
        assert shaker.executed[0]["command"] == "start_shake"
        assert shaker.configured[0].simulated is True

        shaker.gate.set()
        assert run_until(runner, lambda: shake.status == StepStatus.COMPLETED)
        assert run_until(
            runner, lambda: runner.steps["unload"].status != StepStatus.PENDING
        )
    finally:
        session.shutdown()

    completed = [
        d for k, d in events if k == "step_completed" and d["step_id"] == "shake"
    ]
    assert completed[0]["ended_by"] == "instrument"
    replies = [d for k, d in events if k == "instrument_reply"]
    assert replies[0]["ok"] and replies[0]["code"] == "SUCCESS"


def test_late_reply_for_an_aborted_step_is_ignored():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    shaker = FakeToolClient(gate=threading.Event())
    runner, session, _ = started_runner(shaker)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.RUNNING)
        runner.abort_step(shake, runner.current_time, reason="operator")
        shaker.gate.set()
        run_until(runner, lambda: False, timeout=0.3)
    finally:
        session.shutdown()
    assert shake.status == StepStatus.ABORTED
    assert shake.abort_reason == "operator"


def test_unknown_tool_is_refused_before_the_run():
    pytest.importorskip("rhylthyme_galago")
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][1]["instrument"]["tool"] = "reader"
    runner = ProgramRunner(program)
    with pytest.raises(InstrumentSetupError, match="no tool named 'reader'"):
        attach_instruments(runner, WORKCELL)


def test_tool_not_ready_is_refused_with_a_report():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient, ToolReply

    class OfflineTool(FakeToolClient):
        def configure(self, config):
            return ToolReply("UNREACHABLE", "connection refused")

    runner = ProgramRunner(copy.deepcopy(PROGRAM))
    offline = OfflineTool(status="OFFLINE")
    with pytest.raises(
        InstrumentSetupError, match="shaker @ h:1: OFFLINE \\[NOT READY\\]"
    ):
        attach_instruments(runner, WORKCELL, client_factory=lambda b: offline)


def test_missing_package_gives_the_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    runner = ProgramRunner(copy.deepcopy(PROGRAM))
    with pytest.raises(InstrumentSetupError, match="rhylthyme\\[galago\\]"):
        attach_instruments(runner, WORKCELL)
    assert "pip install" in INSTALL_HINT


def test_run_refuses_instrument_program_without_workcell(tmp_path, capsys):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(PROGRAM))
    with pytest.raises(SystemExit):
        run_program(str(path), validate=False, record=False)
    assert "pass --workcell" in capsys.readouterr().out


# --- Validation (phase 2) --------------------------------------------------


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def _with_instrument(**changes):
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][1]["instrument"].update(changes)
    return program


def _codes(findings):
    return [(f.code, f.severity) for f in findings]


def test_programs_without_instruments_need_no_package(monkeypatch):
    from rhylthyme.instruments import instrument_findings

    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    plain = copy.deepcopy(PROGRAM)
    del plain["tracks"][0]["steps"][1]["instrument"]
    assert instrument_findings(plain) == []


def test_missing_package_warns_without_workcell_and_fails_with_one(monkeypatch):
    from rhylthyme.instruments import instrument_findings

    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    [finding] = instrument_findings(PROGRAM)
    assert _codes([finding]) == [("instrument_unchecked", "warning")]
    assert "rhylthyme[galago]" in finding.fix
    [finding] = instrument_findings(PROGRAM, WORKCELL)
    assert _codes([finding]) == [("instrument_unchecked", "error")]


def test_findings_name_the_step_and_field():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.instruments import instrument_findings

    [finding] = instrument_findings(
        _with_instrument(toolType="bioshake", params={"speed": "fast"})
    )
    assert finding.code == "instrument_invalid_command"
    assert finding.where == "step:shake"
    assert finding.message == (
        "Step 'shake': shaker (bioshake) start_shake: "
        "param 'speed' must be an integer, got 'fast'"
    )
    assert _codes(instrument_findings(PROGRAM)) == [("instrument_unchecked", "warning")]
    assert instrument_findings(PROGRAM, WORKCELL) == []


def test_workcell_findings():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.instruments import instrument_findings

    assert _codes(instrument_findings(_with_instrument(tool="reader"), WORKCELL)) == [
        ("instrument_unknown_tool", "error")
    ]
    assert _codes(
        instrument_findings(_with_instrument(toolType="liconic"), WORKCELL)
    ) == [("instrument_tool_type_mismatch", "error")]
    [bad] = instrument_findings(PROGRAM, {"tools": [{"name": "shaker"}]})
    assert (bad.code, bad.where) == ("workcell_invalid", "workcell")
    assert "missing type, host, port" in bad.message


@pytest.mark.cli
def test_validate_cli_with_workcell(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from click.testing import CliRunner

    from rhylthyme.cli.main import cli

    good = _write(tmp_path, "good.json", PROGRAM)
    bad = _write(tmp_path, "bad.json", _with_instrument(command="spin"))
    workcell = _write(tmp_path, "lab.json", WORKCELL)

    ok = CliRunner().invoke(cli, ["validate", good, "--workcell", workcell])
    assert ok.exit_code == 0, ok.output
    assert "instrument_unchecked" not in ok.output

    offline = CliRunner().invoke(cli, ["validate", good])
    assert offline.exit_code == 0
    assert "instrument_unchecked" in offline.output

    failed = CliRunner().invoke(
        cli, ["validate", bad, "--workcell", workcell, "--json"]
    )
    assert failed.exit_code == 1
    result = json.loads(failed.output)
    [finding] = [f for f in result["findings"] if f["code"].startswith("instrument")]
    assert finding["where"] == "step:shake"
    assert "bioshake has no command 'spin'" in finding["message"]


def test_run_refuses_invalid_instrument_command(tmp_path, capsys):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.cli.main import _default_schema_path

    path = _write(tmp_path, "p.json", _with_instrument(params={"rpm": 5}))
    workcell = _write(tmp_path, "lab.json", WORKCELL)
    with pytest.raises(SystemExit):
        run_program(path, _default_schema_path(), record=False, workcell=workcell)
    assert "unknown param 'rpm'" in capsys.readouterr().out


# --- Pre-flight and --live (phase 3) ---------------------------------------


@pytest.fixture
def fake_tools(monkeypatch):
    """Route run_program's tools to one FakeToolClient; never open curses."""
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    import rhylthyme.cli.tui as pr

    tool = FakeToolClient()
    real_open = pr.open_instruments
    factory_calls = []

    def factory(binding):
        factory_calls.append(binding.name)
        return tool

    monkeypatch.setattr(
        pr,
        "open_instruments",
        lambda program, workcell, live=False: real_open(
            program, workcell, live=live, client_factory=factory
        ),
    )
    ran = []
    monkeypatch.setattr(pr.curses, "wrapper", lambda fn: ran.append(True))
    tool.ran = ran
    tool.factory_calls = factory_calls
    return tool


def _run(tmp_path, **kwargs):
    program = _write(tmp_path, "p.json", PROGRAM)
    workcell = _write(tmp_path, "lab.json", WORKCELL)
    return run_program(
        program, validate=False, record=False, workcell=workcell, **kwargs
    )


def test_open_instruments_contacts_no_tool():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.instruments import open_instruments

    made = []
    session = open_instruments(
        PROGRAM, WORKCELL, client_factory=lambda b: made.append(b) or None
    )
    assert session.tools == ["shaker"] and made == []
    assert not session.live


def test_live_summary_lists_tools_and_commands_without_configuring():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    from rhylthyme.instruments import open_instruments

    tool = FakeToolClient(status="READY")
    session = open_instruments(
        PROGRAM, WORKCELL, client_factory=lambda b: tool, live=True
    )
    text = "\n".join(session.summary(PROGRAM))
    assert "LIVE: real hardware will move" in text
    assert "shaker (bioshake) @ h:1: READY" in text
    assert "shake: shaker.start_shake(speed=1000, duration=5)" in text
    assert tool.configured == []


def test_default_run_configures_simulated(fake_tools, tmp_path, capsys):
    _run(tmp_path)
    assert [c.simulated for c in fake_tools.configured] == [True]
    assert fake_tools.ran
    assert "(simulated)" in capsys.readouterr().out


def test_live_without_a_terminal_needs_confirm_live(
    fake_tools, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        _run(tmp_path, live=True)
    out = capsys.readouterr().out
    assert "pass --confirm-live" in out and "nothing was sent" in out
    assert fake_tools.configured == [] and fake_tools.executed == []
    assert not fake_tools.ran


@pytest.mark.parametrize(
    "answer, proceeds", [("live", True), ("LIVE ", True), ("yes", False), ("", False)]
)
def test_live_asks_in_a_terminal(fake_tools, tmp_path, monkeypatch, answer, proceeds):
    import builtins

    fake_tools._status = "READY"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": answer)
    if proceeds:
        _run(tmp_path, live=True)
        assert [c.simulated for c in fake_tools.configured] == [False]
        assert fake_tools.ran
    else:
        with pytest.raises(SystemExit):
            _run(tmp_path, live=True)
        assert fake_tools.configured == [] and not fake_tools.ran


def test_confirm_live_skips_the_question(fake_tools, tmp_path, monkeypatch):
    fake_tools._status = "READY"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _run(tmp_path, live=True, confirm_live=True)
    assert [c.simulated for c in fake_tools.configured] == [False]


def test_live_refuses_tools_that_are_not_ready(fake_tools, tmp_path, capsys):
    fake_tools._status = "FAILED"
    with pytest.raises(SystemExit):
        _run(tmp_path, live=True, confirm_live=True)
    out = capsys.readouterr().out
    assert "shaker @ h:1: FAILED [NOT READY]" in out
    assert fake_tools.executed == [] and not fake_tools.ran


def test_run_record_holds_no_workcell_data(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    from rhylthyme.history.recorder import RunRecorder

    workcell = copy.deepcopy(WORKCELL)
    workcell["tools"][0].update(host="10.20.30.40", port=50710)
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    recorder = RunRecorder(
        runner, source_program=PROGRAM, runs_dir=str(tmp_path)
    ).attach()
    session = attach_instruments(
        runner, workcell, client_factory=lambda b: FakeToolClient()
    )
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    written = recorder.finalize()
    text = open(written).read()
    assert "10.20.30.40" not in text and "50710" not in text
    from rhylthyme.history.store import validate_run

    record = json.loads(text)
    assert validate_run(record) == []
    assert {s["stepId"]: s.get("endedBy") for s in record["steps"]}[
        "shake"
    ] == "instrument"


# --- Failure handling (phase 4) ---------------------------------------------

# Bench: load -> shake (instrument) -> unload. Side: warm (100 s, running
# while the shake fails) -> after-warm (becomes ready during the failure).
TWO_TRACKS: Dict[str, Any] = copy.deepcopy(PROGRAM)
TWO_TRACKS["tracks"].append(
    {
        "trackId": "side",
        "name": "Side",
        "steps": [
            {
                "stepId": "warm",
                "name": "Warm media",
                "duration": {"type": "fixed", "seconds": 100},
                "startTrigger": {"type": "programStart"},
            },
            {
                "stepId": "after-warm",
                "name": "Use warm media",
                "duration": {"type": "fixed", "seconds": 1},
                "startTrigger": {"type": "afterStep", "stepId": "warm"},
            },
        ],
    }
)


def _failing(code="DRIVER_ERROR", message="lid open"):
    from rhylthyme_galago import FakeToolClient, ToolReply

    return FakeToolClient({"start_shake": ToolReply(code, message)})


@pytest.mark.parametrize(
    "code",
    [
        "DRIVER_ERROR",
        "ERROR_FROM_TOOL",
        "NOT_READY",
        "INVALID_ARGUMENTS",
        "UNREACHABLE",
    ],
)
def test_error_replies_fail_the_step(code):
    pytest.importorskip("rhylthyme_galago")
    runner, session, events = started_runner(_failing(code, "boom"))
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.FAILED)
    finally:
        session.shutdown()
    assert shake.failure == {
        "tool": "shaker",
        "command": "start_shake",
        "code": code,
        "errorMessage": "boom",
    }
    assert runner.failed_steps == ["shake"]
    assert runner.failure_text(shake) == f"shake: shaker.start_shake {code} (boom)"
    assert "r retries it, x marks it done, A (twice) aborts" in runner.status_message
    failed = [d for k, d in events if k == "step_failed"]
    assert failed[0]["code"] == code


def test_timeout_fails_the_step():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][1]["instrument"]["timeoutSeconds"] = 0.2
    tool = FakeToolClient(gate=threading.Event())
    runner, session, _ = started_runner(tool, program)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.FAILED)
        assert shake.failure["code"] == "TIMEOUT"
    finally:
        tool.gate.set()
        session.shutdown()


def test_failure_holds_new_steps_but_running_ones_finish():
    pytest.importorskip("rhylthyme_galago")
    runner, session, _ = started_runner(_failing(), TWO_TRACKS)
    try:
        assert run_until(
            runner, lambda: runner.steps["shake"].status == StepStatus.FAILED
        )
        assert runner.steps["warm"].status == StepStatus.RUNNING
        assert run_until(
            runner, lambda: runner.steps["warm"].status == StepStatus.COMPLETED
        )
        run_until(runner, lambda: False, timeout=0.3)
        assert runner.steps["after-warm"].status == StepStatus.PENDING
        assert runner.steps["unload"].status == StepStatus.PENDING
        assert runner.is_running
    finally:
        session.shutdown()


def test_retry_resends_and_success_resumes():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import ToolReply

    tool = _failing()
    runner, session, events = started_runner(tool, TWO_TRACKS)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.FAILED)
        tool.replies["start_shake"] = ToolReply("SUCCESS")
        runner.command_queue.put("retry:shake")
        assert run_until(runner, lambda: shake.status == StepStatus.COMPLETED)
        assert run_until(
            runner, lambda: runner.steps["unload"].status != StepStatus.PENDING
        )
    finally:
        session.shutdown()
    assert [c["command"] for c in tool.executed] == ["start_shake", "start_shake"]
    assert shake.instrument_attempts == 2
    assert runner.failed_steps == []
    retries = [d for k, d in events if k == "step_retry"]
    assert retries[0]["attempt"] == 2


def test_retry_that_fails_again_stays_failed():
    pytest.importorskip("rhylthyme_galago")
    tool = _failing()
    runner, session, _ = started_runner(tool)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.FAILED)
        runner.command_queue.put("retry:")
        assert run_until(runner, lambda: len(tool.executed) == 2)
        assert run_until(runner, lambda: shake.status == StepStatus.FAILED)
    finally:
        session.shutdown()


def test_skip_marks_done_and_releases_dependents():
    pytest.importorskip("rhylthyme_galago")
    runner, session, events = started_runner(_failing(), TWO_TRACKS)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.FAILED)
        runner.command_queue.put("skip:shake")
        assert run_until(
            runner, lambda: runner.steps["unload"].status != StepStatus.PENDING
        )
    finally:
        session.shutdown()
    assert shake.status == StepStatus.COMPLETED
    ended = [d for k, d in events if k == "step_completed" and d["step_id"] == "shake"]
    assert ended[0]["ended_by"] == "skipped"


def test_abort_program_ends_the_run_with_a_reason(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.history.recorder import RunRecorder

    runner = ProgramRunner(copy.deepcopy(TWO_TRACKS), time_scale=100.0)
    recorder = RunRecorder(
        runner, source_program=TWO_TRACKS, runs_dir=str(tmp_path)
    ).attach()
    session = attach_instruments(runner, WORKCELL, client_factory=lambda b: _failing())
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(
            runner, lambda: runner.steps["shake"].status == StepStatus.FAILED
        )
        runner.command_queue.put("abort_program:plate cracked")
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    assert runner.steps["shake"].status == StepStatus.ABORTED
    assert runner.steps["warm"].status == StepStatus.ABORTED
    assert runner.steps["unload"].status == StepStatus.PENDING
    assert runner.program_abort_reason == "plate cracked"
    record = json.loads(open(recorder.finalize()).read())
    assert record["outcome"] == "aborted"
    assert record["context"]["abortReason"] == "plate cracked"
    from rhylthyme.history.store import validate_run

    assert validate_run(record) == []


class _Keys:
    def __init__(self, *keys):
        self.keys = list(keys)

    def getkey(self):
        if not self.keys:
            raise Exception("no input")
        return self.keys.pop(0)


def test_keys_retry_skip_and_double_press_abort():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.cli.tui import handle_input

    runner, session, _ = started_runner(_failing())
    try:
        assert run_until(runner, lambda: runner.failed_steps == ["shake"])
        handle_input(_Keys("r"), runner)
        assert runner.command_queue.get_nowait() == "retry:"
        handle_input(_Keys("x"), runner)
        assert runner.command_queue.get_nowait() == "skip:"
        handle_input(_Keys("A"), runner)
        assert runner.command_queue.empty()
        assert "Press A again" in runner.status_message
        handle_input(_Keys("A"), runner)
        assert runner.command_queue.get_nowait().startswith("abort_program:")
    finally:
        session.shutdown()


def test_ctrl_c_reports_in_flight_commands(fake_tools, tmp_path, capsys, monkeypatch):
    import rhylthyme.cli.tui as pr

    fake_tools.gate = threading.Event()
    captured = {}
    real_runner = pr.ProgramRunner

    class Capturing(real_runner):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["runner"] = self

    def wrapper(fn):
        # Run headlessly until the shake is in flight, then Ctrl-C
        r = captured["runner"]
        r.time_scale = 100.0
        r.start()
        r.command_queue.put("start_program")
        assert run_until(r, lambda: r.steps["shake"].status == StepStatus.RUNNING)
        raise KeyboardInterrupt

    monkeypatch.setattr(pr, "ProgramRunner", Capturing)
    monkeypatch.setattr(pr.curses, "wrapper", wrapper)
    started = time.time()
    _run(tmp_path)
    assert time.time() - started < 5
    out = capsys.readouterr().out
    assert "Program execution interrupted." in out
    assert "Instrument commands still running when the runner stopped: shake" in out
    fake_tools.gate.set()


# --- Tools as resources, mixed programs (phase 5) ----------------------------


def _shake_step(step_id):
    return {
        "stepId": step_id,
        "name": step_id,
        "instrument": {"tool": "shaker", "command": "start_shake"},
        "startTrigger": {"type": "programStart"},
    }


def _two_shakes(**program_fields):
    program = {
        "programId": "two-shakes",
        "name": "Two shakes",
        "startTrigger": {"type": "manual"},
        "tracks": [
            {"trackId": "a", "name": "A", "steps": [_shake_step("shake-a")]},
            {"trackId": "b", "name": "B", "steps": [_shake_step("shake-b")]},
        ],
        "resourceConstraints": [],
    }
    program.update(program_fields)
    return program


class _GatedPerCall:
    """FakeToolClient whose each execute waits for its own release."""

    def __new__(cls):
        from rhylthyme_galago import FakeToolClient

        class Tool(FakeToolClient):
            def __init__(self):
                super().__init__()
                self.releases = []

            def execute(self, command, timeout=None):
                gate = threading.Event()
                self.releases.append(gate)
                gate.wait(5)
                return super().execute(command, timeout)

        return Tool()


def test_each_tool_is_an_implicit_resource_of_one():
    pytest.importorskip("rhylthyme_galago")
    runner = ProgramRunner(_two_shakes())
    assert runner.resource_constraints["shaker"] == 1
    assert runner.implicit_tool_resources == {"shaker"}
    assert "shaker" in runner.steps["shake-a"].task_types


def test_two_steps_on_one_tool_take_turns():
    pytest.importorskip("rhylthyme_galago")
    tool = _GatedPerCall()
    runner, session, _ = started_runner(tool, _two_shakes())
    a, b = runner.steps["shake-a"], runner.steps["shake-b"]
    try:
        assert run_until(
            runner,
            lambda: a.status == StepStatus.RUNNING or b.status == StepStatus.RUNNING,
        )
        first, second = (a, b) if a.status == StepStatus.RUNNING else (b, a)
        run_until(runner, lambda: False, timeout=0.3)
        assert second.status == StepStatus.PENDING  # waiting on the tool, not failed
        assert second.trigger_fired_time is not None
        assert len(tool.releases) == 1
        tool.releases[0].set()
        assert run_until(runner, lambda: second.status == StepStatus.RUNNING)
        assert first.status == StepStatus.COMPLETED
        assert run_until(runner, lambda: len(tool.releases) == 2)
        tool.releases[1].set()
        assert run_until(runner, lambda: second.status == StepStatus.COMPLETED)
    finally:
        for gate in tool.releases:
            gate.set()
        session.shutdown()


def test_declared_constraint_overrides_the_capacity():
    pytest.importorskip("rhylthyme_galago")
    tool = _GatedPerCall()
    program = _two_shakes(
        resourceConstraints=[
            {"task": "shaker", "maxConcurrent": 2, "description": "two-deck shaker"}
        ]
    )
    runner, session, _ = started_runner(tool, program)
    try:
        assert run_until(
            runner,
            lambda: all(
                runner.steps[s].status == StepStatus.RUNNING
                for s in ("shake-a", "shake-b")
            ),
        )
        assert runner.implicit_tool_resources == set()
        assert runner.actor_requirements["shaker"] == 0.0
    finally:
        for gate in tool.releases:
            gate.set()
        session.shutdown()


def test_tool_steps_need_no_person():
    """With the only actor busy on a hand task, the shaker still starts."""
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    program = _two_shakes(
        actors=1,
        resourceConstraints=[
            {
                "task": "pipetting",
                "maxConcurrent": 1,
                "actorsRequired": 1,
                "description": "hands",
            }
        ],
    )
    program["tracks"][1]["steps"] = [
        {
            "stepId": "pipette",
            "name": "Pipette by hand",
            "task": "pipetting",
            "duration": {"type": "fixed", "seconds": 100},
            "startTrigger": {"type": "programStart"},
        }
    ]
    tool = FakeToolClient(gate=threading.Event())
    runner, session, _ = started_runner(tool, program)
    try:
        assert run_until(
            runner,
            lambda: runner.steps["pipette"].status == StepStatus.RUNNING
            and runner.steps["shake-a"].status == StepStatus.RUNNING,
        )
    finally:
        tool.gate.set()
        session.shutdown()


def test_hand_step_between_two_incubator_steps_runs_in_order():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    program = {
        "programId": "mixed",
        "name": "Incubate, check, incubate",
        "startTrigger": {"type": "manual"},
        "tracks": [
            {
                "trackId": "culture",
                "name": "Culture",
                "steps": [
                    {
                        "stepId": "fetch",
                        "name": "Fetch plate",
                        "instrument": {
                            "tool": "incubator",
                            "command": "fetch_plate",
                            "params": {"cassette": 1, "level": 2},
                        },
                        "startTrigger": {"type": "programStart"},
                    },
                    {
                        "stepId": "check",
                        "name": "Check media colour",
                        "duration": {"type": "fixed", "seconds": 60},
                        "startTrigger": {"type": "afterStep", "stepId": "fetch"},
                    },
                    {
                        "stepId": "store",
                        "name": "Store plate",
                        "instrument": {
                            "tool": "incubator",
                            "command": "store_plate",
                            "params": {"cassette": 1, "level": 2},
                        },
                        "startTrigger": {"type": "afterStep", "stepId": "check"},
                    },
                ],
            }
        ],
        "resourceConstraints": [],
    }
    workcell = {
        "id": "bench",
        "tools": [{"name": "incubator", "type": "liconic", "host": "h", "port": 2}],
    }
    tool = FakeToolClient()
    runner = ProgramRunner(program, time_scale=100.0)
    order = []
    runner.add_event_listener(
        lambda kind, data: (
            order.append((kind, data["step_id"]))
            if kind in ("step_started", "step_completed")
            else None
        )
    )
    session = attach_instruments(runner, workcell, client_factory=lambda b: tool)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    assert order == [
        ("step_started", "fetch"),
        ("step_completed", "fetch"),
        ("step_started", "check"),
        ("step_completed", "check"),
        ("step_started", "store"),
        ("step_completed", "store"),
    ]
    assert [c["command"] for c in tool.executed] == ["fetch_plate", "store_plate"]


# --- Visibility (phase 6) -----------------------------------------------------


class _Screen:
    """Just enough of a curses window for draw_ui."""

    def __init__(self, height=40, width=140):
        self.size = (height, width)
        self.cells = {}

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        self.cells[(y, x)] = text

    def clear(self):
        self.cells = {}

    def refresh(self):
        pass

    def text(self):
        return "\n".join(t for _, t in sorted(self.cells.items()))


@pytest.fixture
def screen(monkeypatch):
    import rhylthyme.cli.tui as pr

    for name in ("start_color", "use_default_colors", "init_pair"):
        monkeypatch.setattr(pr.curses, name, lambda *a: None)
    monkeypatch.setattr(pr.curses, "color_pair", lambda n: 0)
    return _Screen()


def test_display_info_badges_and_waiting_on():
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    tool = FakeToolClient(gate=threading.Event())
    runner, session, _ = started_runner(tool)
    try:
        shake = runner.steps["shake"]
        assert run_until(runner, lambda: shake.status == StepStatus.RUNNING)
        info = runner.get_step_display_info(shake)
        assert (info["instrument_tool"], info["waiting_on"]) == ("shaker", "shaker")
        load = runner.get_step_display_info(runner.steps["load"])
        assert (load["instrument_tool"], load["waiting_on"]) == (None, None)
    finally:
        tool.gate.set()
        session.shutdown()


def test_tui_shows_badge_waiting_and_failure(screen):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    from rhylthyme.cli.tui import draw_ui

    tool = FakeToolClient(gate=threading.Event())
    runner, session, _ = started_runner(tool)
    try:
        assert run_until(
            runner, lambda: runner.steps["shake"].status == StepStatus.RUNNING
        )
        draw_ui(screen, runner)
        text = screen.text()
        assert "[shaker]" in text
        assert "waiting on shaker" in text

        runner.post_instrument_reply(
            "shake",
            {
                "ok": False,
                "code": "DRIVER_ERROR",
                "errorMessage": "lid open",
                "metadata": {},
            },
        )
        runner.update()
        draw_ui(screen, runner)
        text = screen.text()
        assert "FAILED" in text and "DRIVER_ERROR" in text
        assert "FAILED shake: shaker.start_shake DRIVER_ERROR (lid open)" in text
        assert "waiting on shaker" not in text
    finally:
        tool.gate.set()
        session.shutdown()


def test_run_history_records_every_reply_with_metadata(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient, ToolReply

    from rhylthyme.history.recorder import RunRecorder
    from rhylthyme.history.store import validate_run

    readings = {
        "wells": {"A1": 0.412, "A2": 1.07},
        "unit": "OD600",
        "flags": [True, None],
    }
    replies = [
        ToolReply("DRIVER_ERROR", "lid open"),
        ToolReply("SUCCESS", metadata=readings),
    ]
    tool = FakeToolClient({"start_shake": lambda command: replies.pop(0)})
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    recorder = RunRecorder(
        runner, source_program=PROGRAM, runs_dir=str(tmp_path)
    ).attach()
    session = attach_instruments(runner, WORKCELL, client_factory=lambda b: tool)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(
            runner, lambda: runner.steps["shake"].status == StepStatus.FAILED
        )
        runner.command_queue.put("retry:shake")
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    record = json.loads(open(recorder.finalize()).read())
    assert validate_run(record) == []
    shake = {s["stepId"]: s for s in record["steps"]}["shake"]
    log = shake["instrument"]
    assert (log["tool"], log["command"]) == ("shaker", "start_shake")
    assert [(r["attempt"], r["code"]) for r in log["replies"]] == [
        (1, "DRIVER_ERROR"),
        (2, "SUCCESS"),
    ]
    assert log["replies"][0]["errorMessage"] == "lid open"
    assert log["replies"][1]["metadata"] == readings
    assert "metadata" not in log["replies"][0]
    assert 0 <= log["replies"][0]["at"] <= log["replies"][1]["at"]
    assert "instrument" not in {s["stepId"]: s for s in record["steps"]}["load"]


# --- Estimated durations (phase 7) -------------------------------------------


def test_fill_durations_needs_the_package(monkeypatch):
    from rhylthyme.instruments import fill_instrument_durations

    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    program, [note] = fill_instrument_durations(PROGRAM)
    assert program is PROGRAM
    assert (
        "1 instrument step(s) have no duration" in note and "rhylthyme[galago]" in note
    )


def test_nothing_to_fill_needs_nothing(monkeypatch):
    from rhylthyme.instruments import fill_instrument_durations

    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    plain = copy.deepcopy(PROGRAM)
    plain["tracks"][0]["steps"][1]["duration"] = 5
    assert fill_instrument_durations(plain) == (plain, [])


def test_planned_makespan_follows_triggers_and_durations():
    from rhylthyme.program_planner import planned_makespan

    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][1]["duration"] = {"type": "fixed", "seconds": 5.5}
    assert planned_makespan(program) == 7.5  # 1 + 5.5 + 1


@pytest.mark.cli
def test_plan_fills_durations_offline(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from click.testing import CliRunner

    from rhylthyme.cli.main import cli

    src = _write(tmp_path, "p.json", PROGRAM)
    out = tmp_path / "planned.json"
    result = CliRunner().invoke(cli, ["plan", src, str(out)])
    assert result.exit_code == 0, result.output
    assert "shake: 5 s (from params.duration)" in result.output
    assert "Makespan (by start triggers and durations): 7 s" in result.output
    shake = {s["stepId"]: s for s in json.loads(out.read_text())["tracks"][0]["steps"]}[
        "shake"
    ]
    assert shake["duration"] == {"type": "fixed", "seconds": 5}
    assert shake["metadata"]["durationEstimate"] == {"source": "params", "seconds": 5}


@pytest.mark.cli
def test_plan_with_workcell_asks_the_tools(tmp_path, monkeypatch):
    galago = pytest.importorskip("rhylthyme_galago")
    from click.testing import CliRunner

    from rhylthyme.cli.main import cli

    tool = galago.FakeToolClient(estimates={"start_shake": 42})
    real = galago.fill_durations
    monkeypatch.setattr(
        galago,
        "fill_durations",
        lambda p, w=None: real(p, w, client_factory=lambda b: tool),
    )
    src = _write(tmp_path, "p.json", PROGRAM)
    lab = _write(tmp_path, "lab.json", WORKCELL)
    out = tmp_path / "planned.json"
    result = CliRunner().invoke(cli, ["plan", src, str(out), "--workcell", lab])
    assert result.exit_code == 0, result.output
    assert "shake: 42 s (from shaker EstimateDuration)" in result.output
    assert "Makespan (by start triggers and durations): 44 s" in result.output
    assert tool.configured == []  # planning never configures a tool


# --- The web bridge, slice 1: watch only (phase 10) -------------------------


class _RecordingRest:
    def __init__(self):
        self.rows = []

    def upsert(self, table, row, on_conflict):
        self.rows.append((table, json.loads(json.dumps(row))))

    def patch(self, table, match, row):
        self.rows.append((table, row))


def _jwt(sub):
    import base64

    body = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    )
    return f"h.{body}.s"


def test_bridge_publishes_the_run_to_the_users_tables(
    fake_tools, tmp_path, monkeypatch
):
    import rhylthyme.cli.tui as pr

    rest = _RecordingRest()
    real_start = pr.start_bridge
    monkeypatch.setattr(
        pr,
        "start_bridge",
        lambda runner, session, program, **kw: real_start(
            runner,
            session,
            program,
            rest=rest,
            token_fn=lambda: _jwt("user-9"),
            config_path=tmp_path / "bridges.json",
            **kw,
        ),
    )

    def run_headless(fn):
        r = captured["runner"]
        r.time_scale = 100.0
        r.start()
        r.command_queue.put("start_program")
        assert run_until(r, lambda: not r.is_running)

    captured = {}
    real_runner = pr.ProgramRunner

    class Capturing(real_runner):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["runner"] = self

    monkeypatch.setattr(pr, "ProgramRunner", Capturing)
    monkeypatch.setattr(pr.curses, "wrapper", run_headless)
    workcell = copy.deepcopy(WORKCELL)
    workcell["tools"][0].update(host="10.20.30.40", port=50710)
    program = _write(tmp_path, "p.json", PROGRAM)
    run_program(
        program,
        validate=False,
        record=False,
        workcell=_write(tmp_path, "lab.json", workcell),
        bridge=True,
    )

    tables = [t for t, _ in rest.rows]
    assert tables[0] == "bridges" and "bridge_state" in tables
    bridge = rest.rows[0][1]
    assert bridge["user_id"] == "user-9" and bridge["allows_live"] is False
    assert bridge["tools"] == [
        {"name": "shaker", "type": "bioshake", "status": "SIMULATED"}
    ]
    final = [row for t, row in rest.rows if t == "bridge_state"][-1]
    assert final["status"] == "completed" and final["mode"] == "simulated"
    assert [s["status"] for s in final["state"]["steps"]] == ["COMPLETED"] * 3
    assert "10.20.30.40" not in json.dumps(rest.rows) and "50710" not in json.dumps(
        rest.rows
    )


def test_bridge_needs_a_login(fake_tools, tmp_path, monkeypatch, capsys):
    from rhylthyme.cli.remote import auth

    monkeypatch.setattr(auth, "load_credentials", lambda path=None: None)
    program = _write(tmp_path, "p.json", PROGRAM)
    with pytest.raises(SystemExit):
        run_program(
            program,
            validate=False,
            record=False,
            workcell=_write(tmp_path, "lab.json", WORKCELL),
            bridge=True,
        )
    assert "rhylthyme login" in capsys.readouterr().out
    assert not fake_tools.ran


# --- The web bridge, slice 2: steering from the browser ----------------------


def _remote(runner, kind, **args):
    return runner.apply_remote_command({"id": "x", "kind": kind, "args": args})


def test_remote_commands_only_apply_when_they_make_sense():
    pytest.importorskip("rhylthyme_galago")
    runner, session, _ = started_runner(_failing())
    try:
        assert run_until(runner, lambda: runner.failed_steps == ["shake"])
        assert _remote(runner, "resume") == {"accepted": False, "reason": "not paused"}
        assert (
            _remote(runner, "retry", step_id="load")["reason"]
            == "step 'load' has not failed"
        )
        assert _remote(runner, "reboot")["reason"] == "unknown command 'reboot'"
        assert _remote(runner, "pause")["accepted"] and runner.is_paused
        assert _remote(runner, "pause")["reason"] == "already paused"
        assert _remote(runner, "resume")["accepted"] and not runner.is_paused
        assert _remote(runner, "skip", step_id="shake")["accepted"]
        assert runner.steps["shake"].status == StepStatus.COMPLETED
        assert "(from the web)" in runner.status_message
        assert _remote(runner, "abort", reason="lid cracked")["accepted"]
        assert runner.program_abort_reason == "lid cracked"
        assert _remote(runner, "pause")["reason"] == "the program is not running"
    finally:
        session.shutdown()


def test_remote_commands_go_into_the_run_record(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.history.recorder import RunRecorder
    from rhylthyme.history.store import validate_run

    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    recorder = RunRecorder(
        runner, source_program=PROGRAM, runs_dir=str(tmp_path)
    ).attach()
    session = attach_instruments(runner, WORKCELL, client_factory=lambda b: _failing())
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: runner.failed_steps == ["shake"])
        runner.command_queue.put(
            'remote:{"id": "c9", "kind": "skip", "args": {"step_id": "shake"}}'
        )
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    assert runner.remote_outcomes["c9"] == {"accepted": True, "reason": ""}
    record = json.loads(open(recorder.finalize()).read())
    [cmd] = record["context"]["remoteCommands"]
    assert (cmd["id"], cmd["kind"], cmd["args"], cmd["accepted"]) == (
        "c9",
        "skip",
        {"step_id": "shake"},
        True,
    )
    assert validate_run(record) == []


def test_a_browser_retry_reaches_the_runner_through_the_bridge(tmp_path):
    """The full path: bridge_commands row -> bridge -> runner -> row answered."""
    pytest.importorskip("rhylthyme_galago")
    import datetime

    from rhylthyme_galago import FakeToolClient, ToolReply

    from rhylthyme.cli.bridge import start_bridge

    tool = FakeToolClient({"start_shake": ToolReply("DRIVER_ERROR", "lid open")})

    class Rest(_RecordingRest):
        def __init__(self):
            super().__init__()
            self.pending = []

        def select(self, table, query):
            rows, self.pending = self.pending, []
            return rows

        def patch(self, table, match, row):
            self.rows.append((table, dict(row, match=match)))

    rest = Rest()
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    session = attach_instruments(runner, WORKCELL, client_factory=lambda b: tool)
    publisher = start_bridge(
        runner,
        session,
        runner.program,
        rest=rest,
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "b.json",
    )
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: runner.failed_steps == ["shake"])
        tool.replies["start_shake"] = ToolReply("SUCCESS")
        rest.pending = [
            {
                "id": "c1",
                "bridge_id": publisher.bridge_id,
                "user_id": "user-9",
                "kind": "retry",
                "args": {"step_id": "shake"},
                "status": "pending",
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        ]
        assert run_until(runner, lambda: not runner.is_running, timeout=10)
    finally:
        publisher.stop()
        session.shutdown()
    answered = [row for t, row in rest.rows if t == "bridge_commands"]
    assert answered[0]["match"] == "id=eq.c1&status=eq.pending"
    assert answered[0]["status"] == "done"
    assert runner.steps["shake"].status == StepStatus.COMPLETED
    assert [c["command"] for c in tool.executed] == ["start_shake", "start_shake"]


def test_main_loop_leaves_by_itself_after_a_web_started_run(screen, monkeypatch):
    import rhylthyme.cli.tui as pr

    monkeypatch.setattr(pr.curses, "curs_set", lambda n: None)

    class Scr(_Screen):
        def timeout(self, ms):
            pass

        def getkey(self):
            raise Exception("no input")

    runner = ProgramRunner(
        copy.deepcopy(TWO_TRACKS), time_scale=1000.0, auto_start=True
    )
    runner.steps["warm"].duration_seconds = 1
    for step in list(runner.steps.values()):
        if step.instrument:
            step.instrument = None  # hand steps only: the run finishes on timers
            step.duration_type = DurationType.FIXED
            step.duration_seconds = 1
    assert not pr.run_finished(runner)
    started = time.time()
    pr.main_loop(Scr(), runner, exit_when_done=True)
    assert pr.run_finished(runner)
    assert time.time() - started < 15
