"""LabMCP tools in a workcell beside galago ones (phase 2 of the LabMCP plan)."""

import copy
import json
import sys
import time

import pytest

from rhylthyme.cli.tui import run_program
from rhylthyme.instruments import (
    InstrumentSetupError,
    attach_instruments,
    open_instruments,
)
from rhylthyme.program_runner import ProgramRunner, StepStatus

pytestmark = pytest.mark.unit

ADDRESS = "/dev/tty.usbserial-RHY4242"

PROGRAM = {
    "programId": "tare-and-shake",
    "name": "Tare, then shake",
    "startTrigger": {"type": "manual"},
    "tracks": [
        {
            "trackId": "bench",
            "name": "Bench",
            "steps": [
                {
                    "stepId": "tare",
                    "name": "Tare the balance",
                    "instrument": {"tool": "balance", "command": "tare"},
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
                    "startTrigger": {"type": "afterStep", "stepId": "tare"},
                },
            ],
        }
    ],
    "resourceConstraints": [],
}
WORKCELL = {
    "id": "mixed-bench",
    "tools": [
        {
            "name": "balance",
            "driver": "labmcp",
            "server": "mettler-toledo",
            "address": ADDRESS,
        },
        {"name": "shaker", "type": "bioshake", "host": "10.20.30.40", "port": 50710},
    ],
}


@pytest.fixture
def fakes():
    pytest.importorskip("rhylthyme_galago")
    labmcp = pytest.importorskip("rhylthyme_labmcp")
    from rhylthyme_galago import FakeToolClient

    server = labmcp.FakeServerClient({"tare": {"value": 0.0, "unit": "g"}})
    launched = []

    def launch(tool, simulate):
        launched.append(
            (tool.name, simulate, labmcp.server_args(tool, simulate=simulate))
        )
        return server

    server.launched = launched
    return {
        "server": server,
        "shaker": FakeToolClient(),
        "options": {"labmcp": {"client_factory": launch}},
    }


def run_until(runner, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def attach(runner, fakes, workcell=WORKCELL):
    return attach_instruments(
        runner,
        workcell,
        client_factory=lambda b: fakes["shaker"],
        driver_options=fakes["options"],
    )


def test_one_labmcp_step_and_one_galago_step_run_to_completion(fakes, tmp_path):
    from rhylthyme.history.recorder import RunRecorder
    from rhylthyme.history.store import validate_run

    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    recorder = RunRecorder(
        runner, source_program=PROGRAM, runs_dir=str(tmp_path)
    ).attach()
    session = attach(runner, fakes)
    server = fakes["server"]
    try:
        # Started at pre-flight, simulated, with the workcell's address
        [(name, simulate, args)] = server.launched
        assert (name, simulate) == ("balance", True)
        assert args[:3] == ["labmcp-mettler-toledo", "--address", ADDRESS]
        assert args[-1] == "--simulate"
        assert server.started and not server.closed
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    assert server.closed  # stopped at the end of the run
    assert [c["tool"] for c in server.calls] == ["get_connection_info", "tare"]
    assert [c["command"] for c in fakes["shaker"].executed] == ["start_shake"]

    text = open(recorder.finalize()).read()
    assert ADDRESS not in text and "10.20.30.40" not in text
    record = json.loads(text)
    assert validate_run(record) == []
    tare = {s["stepId"]: s for s in record["steps"]}["tare"]
    assert tare["endedBy"] == "instrument"
    assert tare["instrument"]["tool"] == "balance"
    assert tare["instrument"]["command"] == "tare"
    [reply] = tare["instrument"]["replies"]
    assert reply["code"] == "SUCCESS"
    assert reply["metadata"] == {"value": 0.0, "unit": "g"}


def test_a_tool_error_fails_the_step_with_the_servers_message(fakes):
    from rhylthyme_labmcp import ToolResult

    fakes["server"].replies["tare"] = ToolResult(
        "TOOL_ERROR", f"Balance at {ADDRESS} reports overload (> 220 g)"
    )
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    session = attach(runner, fakes)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
    finally:
        session.shutdown()
    assert tare.failure == {
        "tool": "balance",
        "command": "tare",
        "code": "TOOL_ERROR",
        "errorMessage": "Balance at <balance address> reports overload (> 220 g)",
    }


def test_a_server_that_does_not_start_blocks_the_run(fakes):
    fakes["server"].start_error = "uvx: package not found"
    runner = ProgramRunner(copy.deepcopy(PROGRAM))
    with pytest.raises(
        InstrumentSetupError, match=r"balance @ .*: OFFLINE \[NOT READY\] uvx"
    ):
        attach(runner, fakes)


def test_pre_flight_report_names_the_server(fakes):
    session = open_instruments(
        PROGRAM,
        WORKCELL,
        client_factory=lambda b: fakes["shaker"],
        driver_options=fakes["options"],
    )
    try:
        session.prepare()
        report = "\n".join(session.report())
        assert f"balance @ {ADDRESS}: SIMULATED [ok]" in report
        assert session.describe_tools()[0] == {
            "name": "balance",
            "type": "labmcp-mettler-toledo",
            "status": "SIMULATED",
        }
    finally:
        session.shutdown()


def test_missing_package_gives_the_labmcp_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "rhylthyme_labmcp", None)
    with pytest.raises(InstrumentSetupError, match=r"rhylthyme\[labmcp\]"):
        open_instruments(PROGRAM, WORKCELL)


def test_run_program_with_a_mixed_workcell(fakes, tmp_path, monkeypatch):
    import rhylthyme.cli.tui as pr

    real_open = pr.open_instruments
    monkeypatch.setattr(
        pr,
        "open_instruments",
        lambda program, workcell, live=False: real_open(
            program,
            workcell,
            live=live,
            client_factory=lambda b: fakes["shaker"],
            driver_options=fakes["options"],
        ),
    )
    ran = []
    monkeypatch.setattr(pr.curses, "wrapper", lambda fn: ran.append(True))
    program = tmp_path / "p.json"
    program.write_text(json.dumps(PROGRAM))
    workcell = tmp_path / "lab.json"
    workcell.write_text(json.dumps(WORKCELL))
    run_program(str(program), validate=False, record=False, workcell=str(workcell))
    assert ran and fakes["server"].closed


def test_a_failed_labmcp_step_sends_the_servers_safety_tools(fakes):
    from rhylthyme_labmcp import ToolResult

    server = fakes["server"]
    server.tools += [
        {"name": "reset_balance", "kind": "safety", "required": []},
        {"name": "set_zero_point", "kind": "safety", "required": ["value"]},
        {"name": "tare", "kind": "control", "required": []},
    ]
    server.replies["tare"] = ToolResult("TOOL_ERROR", "overload")
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    events = []
    runner.add_event_listener(lambda kind, data: events.append((kind, data)))
    session = attach(runner, fakes)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(
            runner, lambda: runner.steps["tare"].status == StepStatus.FAILED
        )
        assert run_until(runner, lambda: not runner.safe_stops_pending)
    finally:
        session.shutdown()
    # Safety tools with no required arguments, never reconnect
    assert [c["tool"] for c in server.calls][-1] == "reset_balance"
    stops = [
        d for k, d in events if k == "instrument_reply" and d.get("phase") == "onAbort"
    ]
    assert [(d["tool"], d["command"], d["code"]) for d in stops] == [
        ("balance", "reset_balance", "SUCCESS")
    ]


# --- Validation against the LabMCP catalogue (phase 5) -----------------------


def _findings(program, workcell=None):
    from rhylthyme.instruments import instrument_findings

    return [
        (f.code, f.where, f.severity) for f in instrument_findings(program, workcell)
    ]


def test_validation_checks_labmcp_steps_against_the_catalogue():
    pytest.importorskip("rhylthyme_labmcp")
    pytest.importorskip("rhylthyme_galago")
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][0]["instrument"]["command"] = "tear"
    workcell = copy.deepcopy(WORKCELL)
    workcell["tools"][0]["limits"] = {"no_such_limit": 1}
    assert _findings(program, workcell) == [
        ("workcell_unknown_limit", "workcell", "error"),
        ("instrument_invalid_command", "step:tare", "error"),
    ]


def test_without_a_workcell_labmcp_steps_are_routed_by_their_tool_type():
    pytest.importorskip("rhylthyme_labmcp")
    program = {
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "heat",
                        "instrument": {
                            "tool": "stirrer",
                            "toolType": "labmcp-ika",
                            "command": "set_temperature",
                            "params": {"temperature_c": 900},
                        },
                    }
                ],
            }
        ]
    }
    assert _findings(program) == [
        ("instrument_invalid_command", "step:heat", "error"),  # schema max 500
        ("instrument_over_limit", "step:heat", "error"),  # server default 150
    ]


def test_labmcp_steps_without_the_package_say_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "rhylthyme_labmcp", None)
    program = {
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "heat",
                        "instrument": {
                            "tool": "s",
                            "toolType": "labmcp-ika",
                            "command": "x",
                        },
                    }
                ],
            }
        ]
    }
    from rhylthyme.instruments import instrument_findings

    [finding] = instrument_findings(program)
    assert finding.code == "instrument_unchecked" and "rhylthyme[labmcp]" in finding.fix


# --- The live gate (phase 6) -----------------------------------------------------

LIVE_INFO = {
    "server": "Mettler Toledo Balance (MT-SICS)",
    "simulated": False,
    "connected": True,
    "instrument": {
        "manufacturer": "Mettler Toledo",
        "model": "XS205DU",
        "serial": "B42",
    },
    "safety_limits": {
        "max_series_duration_s": {
            "value": 300,
            "unit": "s",
            "kind": "max",
            "default": 600,
        }
    },
}


@pytest.fixture
def live_run(fakes, tmp_path, monkeypatch):
    """run_program --live on the mixed workcell; curses never opens."""
    from rhylthyme_galago import FakeToolClient

    import rhylthyme.cli.tui as pr

    server = fakes["server"]
    server.simulated = False
    server.tools += [{"name": "tare", "kind": "hazard", "required": []}]
    server.replies["get_connection_info"] = dict(LIVE_INFO)
    shaker = FakeToolClient(status="READY")
    real_open = pr.open_instruments
    monkeypatch.setattr(
        pr,
        "open_instruments",
        lambda program, workcell, live=False: real_open(
            program,
            workcell,
            live=live,
            client_factory=lambda b: shaker,
            driver_options=fakes["options"],
        ),
    )
    ran = []
    monkeypatch.setattr(pr.curses, "wrapper", lambda fn: ran.append(True))
    program = tmp_path / "p.json"
    program.write_text(json.dumps(PROGRAM))
    workcell = tmp_path / "lab.json"
    live_workcell = copy.deepcopy(WORKCELL)
    live_workcell["tools"][0]["limits"] = {"max_series_duration_s": 300}
    workcell.write_text(json.dumps(live_workcell))

    def run(**kwargs):
        return run_program(
            str(program),
            validate=False,
            record=False,
            workcell=str(workcell),
            live=True,
            **kwargs,
        )

    run.server, run.shaker, run.ran = server, shaker, ran
    return run


def _sent(server):
    return [c["tool"] for c in server.calls if c["tool"] != "get_connection_info"]


def test_live_without_confirmation_never_starts(live_run, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        live_run()
    assert "pass --confirm-live" in capsys.readouterr().out
    assert _sent(live_run.server) == [] and live_run.server.closed
    assert live_run.shaker.configured == [] and not live_run.ran


def test_live_pre_flight_shows_identity_mode_limits_and_hazards(live_run, capsys):
    live_run(confirm_live=True)
    out = capsys.readouterr().out
    assert f"balance (labmcp-mettler-toledo) @ {ADDRESS}: READY" in out
    assert (
        "instrument: Mettler Toledo XS205DU, serial B42; mode: live; "
        "limits: max_series_duration_s=300 s (workcell)"
    ) in out
    assert (
        "Hazard actions (1): these heat, move, dispense or energise\n  tare: balance.tare()"
        in out
    )
    assert live_run.ran
    # The server was started live: no --simulate
    [(_, simulate, args)] = live_run.server.launched
    assert simulate is False and "--simulate" not in args


def test_an_unreachable_instrument_refuses_a_live_start(live_run, capsys):
    live_run.server.start_error = f"could not open {ADDRESS}: no such device"
    with pytest.raises(SystemExit):
        live_run(confirm_live=True)
    out = capsys.readouterr().out
    refusal = out[out.index("Live run refused") :]
    assert (
        "balance (labmcp-mettler-toledo): OFFLINE: could not open <balance address>"
        in refusal
    )
    assert ADDRESS not in refusal
    assert live_run.shaker.configured == [] and not live_run.ran


def test_a_simulated_server_refuses_a_live_start(live_run, capsys):
    live_run.server.replies["get_connection_info"] = {**LIVE_INFO, "simulated": True}
    with pytest.raises(SystemExit):
        live_run(confirm_live=True)
    assert "WRONG_MODE: the server is not live" in capsys.readouterr().out
    assert not live_run.ran


# --- Duration estimates (phase 7) ---------------------------------------------


def test_fill_durations_estimates_labmcp_steps_and_flags_them():
    pytest.importorskip("rhylthyme_labmcp")
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme.instruments import fill_instrument_durations

    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"].insert(
        1,
        {
            "stepId": "log",
            "name": "Log weight",
            "instrument": {
                "tool": "balance",
                "until": {
                    "command": "log_weight_series",
                    "params": {"count": 31, "interval_s": 2},
                },
            },
            "startTrigger": {"type": "afterStep", "stepId": "tare"},
        },
    )
    program["tracks"][0]["steps"][2]["startTrigger"]["stepId"] = "log"
    filled, lines = fill_instrument_durations(program, WORKCELL)
    steps = {s["stepId"]: s for s in filled["tracks"][0]["steps"]}
    assert steps["tare"]["duration"] == {"type": "fixed", "seconds": 10}
    assert steps["tare"]["metadata"]["durationEstimate"] == {
        "source": "default",
        "seconds": 10,
    }
    assert steps["log"]["duration"] == {"type": "fixed", "seconds": 60}
    assert steps["log"]["metadata"]["durationEstimate"] == {
        "source": "params",
        "seconds": 60,
    }
    assert "  log: 60 s (from params.count, params.interval_s)" in lines
    # The galago step is still estimated by galago (params.duration = 5)
    assert steps["shake"]["duration"] == {"type": "fixed", "seconds": 5}


def test_without_a_workcell_labmcp_steps_are_estimated_by_their_tool_type():
    pytest.importorskip("rhylthyme_labmcp")
    from rhylthyme.instruments import fill_instrument_durations

    program = {
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {
                        "stepId": "wait",
                        "instrument": {
                            "tool": "stirrer",
                            "toolType": "labmcp-ika",
                            "until": {
                                "command": "wait_for_temperature",
                                "params": {"target_c": 60},
                            },
                        },
                    }
                ],
            }
        ]
    }
    filled, lines = fill_instrument_durations(program)
    assert filled["tracks"][0]["steps"][0]["duration"]["seconds"] == 600
    assert lines[-1] == (
        "  wait: 600 s (from params.timeout_s, tool defaults (an upper bound))"
    )


# --- One Opentrons program, two backends (phase 12) ---------------------------

OT_SCRIPT = "def run(protocol):\n    pass\n"
OT_PROGRAM = {
    "programId": "ot",
    "name": "OT",
    "startTrigger": {"type": "manual"},
    "tracks": [
        {
            "trackId": "robot",
            "name": "Robot",
            "steps": [
                {
                    "stepId": "transfer",
                    "name": "Transfer",
                    "instrument": {
                        "tool": "ot2",
                        "command": "run_program",
                        "params": {"script_content": OT_SCRIPT},
                    },
                    "startTrigger": {"type": "programStart"},
                }
            ],
        }
    ],
    "resourceConstraints": [],
}


def test_the_same_opentrons_program_validates_and_runs_on_galago(tmp_path):
    pytest.importorskip("rhylthyme_galago")
    from rhylthyme_galago import FakeToolClient

    from rhylthyme.instruments import instrument_findings

    galago = {
        "id": "ot",
        "tools": [{"name": "ot2", "type": "opentrons2", "host": "h", "port": 1}],
    }
    labmcp_wc = {
        "id": "ot",
        "tools": [{"name": "ot2", "driver": "labmcp", "server": "opentrons"}],
    }
    assert instrument_findings(OT_PROGRAM, galago) == []
    pytest.importorskip("rhylthyme_labmcp")
    assert instrument_findings(OT_PROGRAM, labmcp_wc) == []
    tool = FakeToolClient()
    runner = ProgramRunner(copy.deepcopy(OT_PROGRAM), time_scale=100.0)
    session = attach_instruments(runner, galago, client_factory=lambda b: tool)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        session.shutdown()
    assert [c["command"] for c in tool.executed] == ["run_program"]


def test_labmcp_opentrons_commands_are_checked_like_galagos():
    pytest.importorskip("rhylthyme_labmcp")
    from rhylthyme.instruments import instrument_findings

    labmcp_wc = {
        "id": "ot",
        "tools": [{"name": "ot2", "driver": "labmcp", "server": "opentrons"}],
    }
    bad = copy.deepcopy(OT_PROGRAM)
    bad["tracks"][0]["steps"][0]["instrument"]["params"] = {"script": "x"}
    messages = [f.message for f in instrument_findings(bad, labmcp_wc)]
    assert any("'script_content' is a required property" in m for m in messages)
    assert any("'script' was unexpected" in m for m in messages)


def test_pausing_the_schedule_holds_tools_that_can_be_held(fakes):
    """A running step's tool with a pause tool is paused with the schedule
    and resumed with it, and the replies are recorded under the step."""
    import threading

    server = fakes["server"]
    server.tools += [
        {"name": "pause_run", "kind": "safety", "required": []},
        {"name": "resume_run", "kind": "hazard", "required": []},
    ]
    release = threading.Event()
    server.replies["tare"] = lambda args: (release.wait(5), {})[1]
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    events = []
    runner.add_event_listener(lambda kind, data: events.append((kind, data)))
    session = attach(runner, fakes)

    def held():
        return [
            (d.get("phase"), d.get("command"), d.get("code"))
            for k, d in events
            if k == "instrument_reply" and d.get("phase") in ("pause", "resume")
        ]

    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(
            runner, lambda: runner.steps["tare"].status == StepStatus.RUNNING
        )
        runner.toggle_pause()
        assert run_until(runner, lambda: len(held()) == 1)
        runner.toggle_pause()
        assert run_until(runner, lambda: len(held()) == 2)
        release.set()
        assert run_until(runner, lambda: not runner.is_running)
    finally:
        release.set()
        session.shutdown()
    assert held() == [
        ("pause", "pause_run", "SUCCESS"),
        ("resume", "resume_run", "SUCCESS"),
    ]
