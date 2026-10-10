"""The web bridge with LabMCP tools, and no galago installed (phase 9)."""

import copy
import datetime
import io
import json
import sys
import time

import pytest

labmcp = pytest.importorskip("rhylthyme_labmcp")

from rhylthyme.cli.bridge import start_bridge  # noqa: E402
from rhylthyme.cli.bridge_serve import serve  # noqa: E402
from rhylthyme.cli.main import _default_schema_path  # noqa: E402
from rhylthyme.instruments import attach_instruments  # noqa: E402
from rhylthyme.program_runner import ProgramRunner, StepStatus  # noqa: E402

pytestmark = pytest.mark.unit

# Distinctive values: none may ever reach a published row or message
SECRETS = [
    "10.77.66.55",
    "47913",
    "RHYPRIV9",
    "10.88.99.11",
    "PRIVOPT-77",
    "lab-secret",
]
WORKCELL = {
    "id": "private-bench",
    "name": "Private bench",
    "tools": [
        {
            "name": "balance",
            "driver": "labmcp",
            "server": "mettler-toledo",
            "address": "tcp://10.77.66.55:47913",
            "options": {"profile": "PRIVOPT-77"},
        },
        {
            "name": "stirrer",
            "driver": "labmcp",
            "server": "ika",
            "address": "serial:///dev/tty.usbserial-RHYPRIV9?baudrate=9600",
        },
        {
            "name": "scope",
            "driver": "labmcp",
            "server": "rigol-scope",
            "address": "TCPIP0::10.88.99.11::inst0::INSTR",
        },
        {
            "name": "remote",
            "driver": "labmcp",
            "url": "http://lab-secret.example:8123/mcp",
        },
    ],
}
PROGRAM = {
    "programId": "weigh-and-stir",
    "name": "Weigh and stir",
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
                    "stepId": "stir",
                    "name": "Stir",
                    "instrument": {
                        "tool": "stirrer",
                        "start": [{"command": "start_stirring"}],
                        "until": {"command": "wait_for_temperature"},
                    },
                    "startTrigger": {"type": "afterStep", "stepId": "tare"},
                },
                {
                    "stepId": "capture",
                    "name": "Capture",
                    "instrument": {"tool": "scope", "command": "single"},
                    "startTrigger": {"type": "afterStep", "stepId": "stir"},
                },
            ],
        }
    ],
    "resourceConstraints": [],
}
STOPS = [
    {"name": "stop_all", "kind": "safety", "required": []},
    {"name": "stop_stirring", "kind": "safety", "required": []},
]


def _jwt(sub):
    import base64

    body = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    )
    return f"h.{body}.s"


class Rest:
    def __init__(self, commands=(), program=None):
        self.commands = list(commands)
        self.program = program
        self.rows = []

    def select(self, table, query):
        if table == "bridge_commands":
            rows, self.commands = self.commands, []
            return rows
        if table == "programs" and self.program is not None:
            return [
                {"id": PID, "name": self.program["name"], "program_json": self.program}
            ]
        return []

    def upsert(self, table, row, on_conflict):
        self.rows.append((table, json.loads(json.dumps(row))))

    def patch(self, table, match, row):
        self.rows.append((table, json.loads(json.dumps(row))))


PID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def servers(monkeypatch):
    """One fake LabMCP server per tool; galago is not installed."""
    monkeypatch.setitem(sys.modules, "rhylthyme_galago", None)
    made = {}

    def launch(tool, simulate):
        server = made.get(tool.name)
        if server is None:
            server = made[tool.name] = labmcp.FakeServerClient(tools=STOPS)
        return server

    return made, {"labmcp": {"client_factory": launch}}


def run_until(runner, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def leaks(text):
    return [s for s in SECRETS if s in text]


def test_a_labmcp_run_is_published_steered_and_never_leaks_an_address(
    servers, tmp_path
):
    made, options = servers
    errors = []
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    session = attach_instruments(runner, WORKCELL, driver_options=options)
    # Every reply quotes the instrument's address in some form
    made["balance"].replies["tare"] = labmcp.ToolResult(
        "TOOL_ERROR",
        "Connect call failed ('10.77.66.55', 47913) for tcp://10.77.66.55:47913",
    )
    made["stirrer"].replies["wait_for_temperature"] = labmcp.ToolResult(
        "TOOL_ERROR",
        "could not open port /dev/tty.usbserial-RHYPRIV9: [Errno 16] "
        "Resource busy: 'tty.usbserial-RHYPRIV9'",
    )
    rest = Rest()
    publisher = start_bridge(
        runner,
        session,
        PROGRAM,
        rest=rest,
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "bridges.json",
    )
    publisher.on_error = errors.append
    try:
        runner.start()
        runner.command_queue.put("start_program")
        tare = runner.steps["tare"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        publisher.publish_once(force=True)
        # Steer it the way the Bridges page does
        assert runner.apply_remote_command(
            {"kind": "retry", "args": {"step_id": "tare"}}
        )["accepted"]
        assert run_until(runner, lambda: tare.status == StepStatus.FAILED)
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        assert runner.apply_remote_command(
            {"kind": "skip", "args": {"step_id": "tare"}}
        )["accepted"]
        stir = runner.steps["stir"]
        assert run_until(runner, lambda: stir.status == StepStatus.FAILED)
        assert run_until(runner, lambda: not runner.safe_stops_pending)
        publisher.publish_once(force=True)
        reason = (
            "unplug /dev/tty.usbserial-RHYPRIV9 and 10.88.99.11; "
            "restart http://lab-secret.example:8123/mcp"
        )
        assert runner.apply_remote_command(
            {"kind": "abort", "args": {"reason": reason}}
        )["accepted"]
        session.finish(runner, timeout=5)
    finally:
        publisher.stop()
        session.shutdown()

    published = json.dumps(rest.rows) + json.dumps(errors)
    assert leaks(published) == []
    bridge = [row for t, row in rest.rows if t == "bridges"][0]
    # The run's tools, as their drivers describe them: no address
    assert bridge["tools"] == [
        {"name": "balance", "type": "labmcp-mettler-toledo", "status": "SIMULATED"},
        {"name": "scope", "type": "labmcp-rigol-scope", "status": "SIMULATED"},
        {"name": "stirrer", "type": "labmcp-ika", "status": "SIMULATED"},
    ]
    states = [row for t, row in rest.rows if t == "bridge_state"]
    failed = states[1]["state"]["steps"][0]
    assert failed["failure"] == {
        "code": "TOOL_ERROR",
        "errorMessage": "Connect call failed <balance address> for <balance address>",
    }
    final = states[-1]
    assert final["status"] == "aborted"
    assert final["state"]["abortReason"].startswith("unplug <stirrer address>")
    stir_row = {s["stepId"]: s for s in final["state"]["steps"]}["stir"]
    assert stir_row["command"] == "wait_for_temperature"  # the until call
    # Aborting from the web stopped the stirrer (the failed step's stops ran
    # when it failed; the abort found it already safe)
    assert [c["tool"] for c in made["stirrer"].calls][-2:] == [
        "stop_all",
        "stop_stirring",
    ]


def test_an_abort_from_the_web_stops_a_running_labmcp_step(servers, tmp_path):
    import threading

    made, options = servers
    runner = ProgramRunner(copy.deepcopy(PROGRAM), time_scale=100.0)
    session = attach_instruments(runner, WORKCELL, driver_options=options)
    made["stirrer"].gate = threading.Event()
    publisher = start_bridge(
        runner,
        session,
        PROGRAM,
        rest=Rest(),
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "bridges.json",
    )
    try:
        runner.start()
        runner.command_queue.put("start_program")
        assert run_until(
            runner, lambda: runner.steps["stir"].status == StepStatus.RUNNING
        )
        made["stirrer"].gate.set()  # let the stops through
        made["stirrer"].replies["wait_for_temperature"] = {}
        assert runner.apply_remote_command({"kind": "abort", "args": {}})["accepted"]
        session.finish(runner, timeout=5)
    finally:
        publisher.stop()
        session.shutdown()
    stops = [c["tool"] for c in made["stirrer"].calls if c["tool"].startswith("stop_")]
    assert stops == ["stop_all", "stop_stirring"]
    assert runner.steps["stir"].status == StepStatus.ABORTED


def test_an_idle_bridge_lists_and_starts_labmcp_runs_without_leaking(servers, tmp_path):
    made, options = servers
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    command = {
        "id": "c1",
        "user_id": "user-9",
        "kind": "start_run",
        "args": {"program_id": PID, "mode": "simulated"},
        "status": "pending",
        "created_at": now,
    }
    rest = Rest([command], program=PROGRAM)
    runs = []
    started = serve(
        WORKCELL,
        schema_file=_default_schema_path(),
        rest=rest,
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "b.json",
        run=lambda *a, **k: runs.append(k),
        max_runs=1,
        out=io.StringIO(),
        driver_options=options,
    )
    assert started == 1 and runs[0]["program_data"] == PROGRAM
    idle = [row for t, row in rest.rows if t == "bridges"][0]
    assert idle["tools"] == [
        {"name": "balance", "type": "labmcp-mettler-toledo", "status": "idle"},
        {"name": "stirrer", "type": "labmcp-ika", "status": "idle"},
        {"name": "scope", "type": "labmcp-rigol-scope", "status": "idle"},
        {"name": "remote", "type": "labmcp (http)", "status": "idle"},
    ]
    runs[0]["prepared_instruments"].shutdown()
    assert leaks(json.dumps(rest.rows)) == []


def test_a_start_refused_for_an_unready_instrument_names_no_address(servers, tmp_path):
    made, options = servers
    made["scope"] = labmcp.FakeServerClient(
        start_error="VISA resource TCPIP0::10.88.99.11::inst0::INSTR not found "
        "(is 10.88.99.11 reachable?)"
    )
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def start(command_id):
        return {
            "id": command_id,
            "user_id": "user-9",
            "kind": "start_run",
            "args": {"program_id": PID, "mode": "simulated"},
            "status": "pending",
            "created_at": now,
        }

    class Twice(Rest):
        """The first start is refused; then the scope is fixed and it starts."""

        def select(self, table, query):
            if table == "bridge_commands" and not self.commands:
                answered = [r for t, r in self.rows if t == "bridge_commands"]
                if len(answered) == 1:
                    made["scope"] = labmcp.FakeServerClient(tools=STOPS)
                    self.commands = [start("c2")]
            return super().select(table, query)

    rest = Twice([start("c1")], program=PROGRAM)
    out = io.StringIO()
    runs = []
    serve(
        WORKCELL,
        schema_file=_default_schema_path(),
        rest=rest,
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "b.json",
        run=lambda *a, **k: runs.append(k),
        max_runs=1,
        out=out,
        driver_options=options,
    )
    runs[0]["prepared_instruments"].shutdown()
    refused, accepted = [row for t, row in rest.rows if t == "bridge_commands"]
    assert refused["status"] == "rejected"
    assert refused["result"]["reason"].startswith("tools not ready:")
    assert "scope" in refused["result"]["reason"]
    assert accepted["status"] == "done"
    assert leaks(json.dumps(rest.rows) + out.getvalue()) == []
