"""rhylthyme bridge with no program: runs started from the web (slice 3)."""

import copy
import datetime
import io
import json

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip("rhylthyme_galago")

from rhylthyme_galago import FakeToolClient, ToolReply  # noqa: E402

from rhylthyme.cli.bridge_serve import check_start, serve  # noqa: E402
from rhylthyme.cli.main import _default_schema_path  # noqa: E402
from rhylthyme.validate_program import load_program_file  # noqa: E402

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
            ],
        }
    ],
    "resourceConstraints": [],
}
WORKCELL = {
    "id": "bench",
    "tools": [{"name": "shaker", "type": "bioshake", "host": "h", "port": 1}],
}


def _jwt(sub):
    import base64

    body = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    )
    return f"h.{body}.s"


PID = "11111111-2222-3333-4444-555555555555"
SCHEMA = load_program_file(_default_schema_path())


def fetch(program):
    return lambda pid: {"id": pid, "program_json": program} if pid == PID else None


def test_a_valid_saved_program_is_accepted():
    outcome, program = check_start(
        {"program_id": PID}, fetch(PROGRAM), WORKCELL, SCHEMA
    )
    assert outcome == {"accepted": True, "reason": ""}
    assert program == PROGRAM and program is not PROGRAM


@pytest.mark.parametrize(
    "args, program, reason",
    [
        (
            {"program_id": PID, "mode": "live", "confirm": "live"},
            PROGRAM,
            "this bridge does not accept live runs (start it with --allow-live)",
        ),
        (
            {"program_id": "../etc"},
            PROGRAM,
            "program_id must be the id of a program in your library",
        ),
        (
            {"program_id": "99999999-2222-3333-4444-555555555555"},
            PROGRAM,
            "no such program in your library",
        ),
    ],
)
def test_refusals(args, program, reason):
    outcome, got = check_start(args, fetch(program), WORKCELL, SCHEMA)
    assert (outcome["accepted"], outcome["reason"], got) == (False, reason, None)


def test_code_blocks_are_never_started_from_the_web():
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][0]["codeBlock"] = {
        "type": "shell",
        "code": "rm -rf ~",
    }
    outcome, _ = check_start({"program_id": PID}, fetch(program), WORKCELL, SCHEMA)
    assert (
        outcome["reason"] == "programs with code blocks cannot be started from the web"
    )


def test_a_program_that_does_not_fit_the_workcell_is_refused():
    program = copy.deepcopy(PROGRAM)
    program["tracks"][0]["steps"][1]["instrument"]["tool"] = "reader"
    outcome, _ = check_start({"program_id": PID}, fetch(program), WORKCELL, SCHEMA)
    assert outcome["reason"].startswith("the program is not valid on this workcell:")
    assert "no tool 'reader'" in outcome["reason"]


class Rest:
    def __init__(self, commands):
        self.commands = commands
        self.rows = []

    def select(self, table, query):
        if table == "bridge_commands":
            rows, self.commands = self.commands, []
            return rows
        if table == "programs":
            assert f"id=eq.{PID}" in query and "user_id=eq.user-9" in query
            return [{"id": PID, "name": PROGRAM["name"], "program_json": PROGRAM}]
        return []

    def upsert(self, table, row, on_conflict):
        self.rows.append((table, json.loads(json.dumps(row))))

    def patch(self, table, match, row):
        self.rows.append((table, dict(row, match=match)))


def test_serve_waits_starts_the_run_and_answers(tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rest = Rest(
        [
            {
                "id": "c1",
                "bridge_id": None,
                "user_id": "user-9",
                "kind": "start_run",
                "args": {"program_id": PID, "mode": "simulated"},
                "status": "pending",
                "created_at": now,
            }
        ]
    )
    runs = []
    out = io.StringIO()
    started = serve(
        WORKCELL,
        schema_file=_default_schema_path(),
        rest=rest,
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "b.json",
        run=lambda *a, **k: runs.append((a, k)),
        max_runs=1,
        out=out,
        client_factory=lambda binding: FakeToolClient(),
    )
    assert started == 1
    [(args, kw)] = runs
    # Positional (file, schema, time_scale, validate, auto_start, environment):
    # a web-started run must not wait for someone to press 's'.
    assert args[4] is False
    assert kw["program_data"] == PROGRAM and kw["program_id"] == PID
    assert kw["bridge"] and kw["exit_when_done"] and kw["workcell"] == WORKCELL
    tables = [t for t, _ in rest.rows]
    assert (
        tables[0] == "bridges" and "bridge_state" not in tables
    )  # idle: heartbeat only
    answered = [row for t, row in rest.rows if t == "bridge_commands"]
    assert (
        answered[0]["status"] == "done"
        and answered[0]["match"] == "id=eq.c1&status=eq.pending"
    )
    assert "Starting 'Shake a plate' from the web (simulated)." in out.getvalue()


def test_live_needs_both_keys():
    no_confirm, _ = check_start(
        {"program_id": PID, "mode": "live"},
        fetch(PROGRAM),
        WORKCELL,
        SCHEMA,
        allow_live=True,
    )
    assert no_confirm["reason"] == "a live run needs the typed confirmation"
    both, program = check_start(
        {"program_id": PID, "mode": "live", "confirm": "live"},
        fetch(PROGRAM),
        WORKCELL,
        SCHEMA,
        allow_live=True,
    )
    assert both["accepted"] and program == PROGRAM


def _start_command(cid="c1", **args):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "id": cid,
        "bridge_id": None,
        "user_id": "user-9",
        "kind": "start_run",
        "args": dict({"program_id": PID}, **args),
        "status": "pending",
        "created_at": now,
    }


def _serve(tmp_path, rest, tool, **kw):
    runs, out = [], io.StringIO()
    serve(
        WORKCELL,
        schema_file=_default_schema_path(),
        rest=rest,
        token_fn=lambda: _jwt("user-9"),
        config_path=tmp_path / "b.json",
        run=lambda *a, **k: runs.append(k),
        max_runs=1,
        out=out,
        client_factory=lambda binding: tool,
        **kw,
    )
    return runs, out.getvalue()


def test_a_live_start_configures_real_hardware_and_says_so(tmp_path):
    rest = Rest([_start_command(mode="live", confirm="live")])
    tool = FakeToolClient(status="READY")
    runs, out = _serve(tmp_path, rest, tool, allow_live=True)
    [kw] = runs
    assert kw["live"] is True and kw["bridge_allows_live"] is True
    assert kw["prepared_instruments"] is not None
    assert [c.simulated for c in tool.configured] == [False]
    assert "LIVE run started from the web" in out
    assert rest.rows[0][1]["allows_live"] is True


def test_tools_that_are_not_ready_refuse_the_start_without_addresses(tmp_path):
    import threading

    class Offline(FakeToolClient):
        def configure(self, config):
            return ToolReply("UNREACHABLE", "failed to connect to h:1")

    rest = Rest([_start_command(mode="live", confirm="live")])
    tools = [Offline(status="OFFLINE")]
    runs, out = [], io.StringIO()

    def later():
        # A second, simulated start on a ready tool lets serve() return.
        tools.append(FakeToolClient())
        rest.commands.append(_start_command("c2"))  # a new id: c1 is handled once

    timer = threading.Timer(2.5, later)
    timer.start()
    try:
        serve(
            WORKCELL,
            schema_file=_default_schema_path(),
            rest=rest,
            token_fn=lambda: _jwt("user-9"),
            config_path=tmp_path / "b.json",
            run=lambda *a, **k: runs.append(k),
            max_runs=1,
            out=out,
            client_factory=lambda binding: tools[-1],
            allow_live=True,
        )
    finally:
        timer.cancel()
    answered = [row for t, row in rest.rows if t == "bridge_commands"]
    assert answered[0]["status"] == "rejected"
    reason = answered[0]["result"]["reason"]
    assert reason.startswith("tools not ready:") and "OFFLINE" in reason
    assert "h:1" not in reason
    assert answered[1]["status"] == "done" and runs[0]["live"] is False
