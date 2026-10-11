"""`rhylthyme login` / `generate`: MCP client, credentials, browser hand-off.

No network: a local HTTP server stands in for the MCP server and for the
browser's POST back to the login listener.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from click.testing import CliRunner

from rhylthyme.cli.main import cli
from rhylthyme.cli.remote import auth
from rhylthyme.cli.remote.mcp_client import (
    McpClient,
    ToolError,
    endpoint_for,
    extract_program,
    parse_rpc_body,
)

pytestmark = pytest.mark.unit

PROGRAM = {
    "schemaVersion": "0.1.0",
    "programId": "dinner",
    "name": "Roast Dinner",
    "environmentType": "kitchen",
    "tracks": [
        {
            "trackId": "oven",
            "name": "Oven",
            "steps": [
                {
                    "stepId": "roast",
                    "name": "Roast chicken",
                    "task": "oven",
                    "duration": {"type": "fixed", "seconds": 3600},
                    "startTrigger": {"type": "programStart"},
                }
            ],
        }
    ],
    "resourceConstraints": [{"task": "oven", "maxConcurrent": 1}],
}


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("RHYLTHYME_TOKEN", raising=False)
    return tmp_path


# ---- MCP client -------------------------------------------------------------


def test_parse_rpc_body_json_and_sse():
    assert parse_rpc_body('{"jsonrpc":"2.0","id":1,"result":{"a":1}}')["result"] == {
        "a": 1
    }
    sse = (
        "event: message\ndata: "
        + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"b": 2}})
        + "\n\n"
    )
    assert parse_rpc_body(sse)["result"] == {"b": 2}


def test_extract_program_takes_last_program_block():
    text = (
        'summary\n```json\n{"not": "a program"}\n```\nmore\n```json\n'
        + json.dumps(PROGRAM)
        + "\n```"
    )
    assert extract_program(text)["programId"] == "dinner"
    assert extract_program("no json here") is None


def test_endpoint_for_routes_verticals():
    assert (
        endpoint_for("kitchen", "https://mcp.rhylthyme.com/mcp")
        == "https://mcp.rhylthyme.com/kitchen/mcp"
    )
    assert (
        endpoint_for("generic", "https://mcp.rhylthyme.com/mcp")
        == "https://mcp.rhylthyme.com/mcp"
    )
    assert endpoint_for(None, "http://localhost:9/mcp") == "http://localhost:9/mcp"


class FakeMcp:
    """A stateless MCP server that answers initialize, import_text and visualize_schedule."""

    def __init__(self, import_error=None):
        self.calls = []
        self.import_error = import_error
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.path, body))
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                if body["method"] == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "fake"},
                        "capabilities": {},
                    }
                elif body["params"]["name"] == "import_text":
                    if fake.import_error:
                        result = {
                            "isError": True,
                            "content": [{"type": "text", "text": fake.import_error}],
                        }
                    else:
                        text = (
                            "# Roast Dinner\n\nCall **visualize_schedule** with this program.\n\n```json\n"
                            + json.dumps(PROGRAM)
                            + "\n```"
                        )
                        result = {"content": [{"type": "text", "text": text}]}
                elif body["params"]["name"] == "analyze_schedule":
                    clock = body["params"]["arguments"].get("finishAt")
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "**Makespan:** 1h\n**Wall clock:** utc\n"
                                "**Resource conflicts (1):**\n- oven\n\n"
                                "**Wall-clock itinerary:**\n```\nutc rows\n```",
                            }
                        ],
                        "structuredContent": {
                            "makespanSeconds": 3600,
                            "resourceConflicts": [{"task": "oven"}],
                            "wallClock": clock
                            and {
                                "startAt": "2026-11-26T22:00:00.000Z",
                                "finishAt": "2026-11-26T23:00:00.000Z",
                            },
                            "steps": [
                                {
                                    "stepId": "roast",
                                    "name": "Roast",
                                    "startSeconds": 0,
                                    "startAt": clock and "2026-11-26T22:00:00.000Z",
                                }
                            ],
                        },
                    }
                else:
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "# Roast Dinner\n\nOven  |████████|",
                            }
                        ],
                        "structuredContent": {
                            "url": "https://kitchen.rhylthyme.com?share=abc",
                            "shareId": "abc",
                            "imageUrl": None,
                            "makespanSeconds": 3600,
                            "warnings": [],
                        },
                    }
                payload = (
                    "event: message\ndata: "
                    + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
                    + "\n\n"
                )
                data = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def tool_calls(self):
        return [
            (p, b["params"]["name"], b["params"]["arguments"])
            for p, b in self.calls
            if b.get("method") == "tools/call"
        ]


@pytest.fixture
def fake_mcp(monkeypatch):
    fake = FakeMcp()
    monkeypatch.setenv("RHYLTHYME_MCP_URL", fake.url)
    yield fake
    fake.close()


def test_client_raises_tool_error():
    fake = FakeMcp(
        import_error="import_text requires the user's Rhylthyme access token."
    )
    try:
        with pytest.raises(ToolError):
            McpClient(url=fake.url).call_tool("import_text", {"text": "x"})
    finally:
        fake.close()


# ---- generate -----------------------------------------------------------------


def test_generate_builds_publishes_and_saves(
    fake_mcp, config_home, tmp_path, monkeypatch
):
    monkeypatch.setenv("RHYLTHYME_TOKEN", "tok-123")
    out = tmp_path / "dinner.json"
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "roast chicken for 6",
            "-e",
            "kitchen",
            "--by",
            "19:00",
            "--with",
            "one oven",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Live timeline: https://kitchen.rhylthyme.com?share=abc" in result.output
    assert json.loads(out.read_text())["programId"] == "dinner"

    calls = fake_mcp.tool_calls()
    assert [c[1] for c in calls] == ["import_text", "visualize_schedule"]
    path, _, args = calls[0]
    assert path == "/kitchen/mcp"
    assert args == {
        "text": "roast chicken for 6",
        "environmentType": "kitchen",
        "token": "tok-123",
        "deadline": "19:00",
        "hints": "one oven",
    }
    assert calls[1][2]["program"]["programId"] == "dinner"
    init = next(b for _, b in fake_mcp.calls if b.get("method") == "initialize")
    assert init["params"]["clientInfo"]["name"] == "rhylthyme-cli"


def test_generate_quiet_and_json_and_stdin(fake_mcp, config_home, monkeypatch):
    monkeypatch.setenv("RHYLTHYME_TOKEN", "tok")
    quiet = CliRunner().invoke(cli, ["generate", "-q", "a workout"])
    assert quiet.exit_code == 0, quiet.output
    assert quiet.output.strip() == "https://kitchen.rhylthyme.com?share=abc"

    as_json = CliRunner().invoke(
        cli,
        ["generate", "--json", "--no-publish", "-f", "-"],
        input="Western blot, overnight primary",
    )
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert data["url"] is None and data["program"]["programId"] == "dinner"
    assert fake_mcp.tool_calls()[-1][2]["text"] == "Western blot, overnight primary"


def test_generate_requires_login(fake_mcp, config_home):
    result = CliRunner().invoke(cli, ["generate", "anything"])
    assert result.exit_code != 0
    assert "rhylthyme login" in result.output
    assert fake_mcp.tool_calls() == []


def test_generate_requires_text(config_home, monkeypatch):
    monkeypatch.setenv("RHYLTHYME_TOKEN", "tok")
    result = CliRunner().invoke(cli, ["generate"], input="")
    assert result.exit_code != 0
    assert "Describe what to schedule" in result.output


# ---- credentials ------------------------------------------------------------


def _creds(**over):
    base = {
        "access_token": "old",
        "refresh_token": "r1",
        "expires_at": int(time.time()) + 3600,
        "supabase_url": "https://example.supabase.co",
        "supabase_anon_key": "anon",
    }
    base.update(over)
    return base


def test_saved_credentials_are_private(config_home):
    path = auth.save_credentials(_creds())
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert auth.load_credentials()["access_token"] == "old"
    assert auth.clear_credentials() is True
    assert auth.load_credentials() is None


def test_access_token_uses_fresh_token_without_refresh(config_home, monkeypatch):
    auth.save_credentials(_creds())
    monkeypatch.setattr(auth, "refresh", lambda c: pytest.fail("should not refresh"))
    assert auth.access_token() == "old"


def test_access_token_refreshes_near_expiry_and_saves(config_home, monkeypatch):
    auth.save_credentials(_creds(expires_at=int(time.time()) + 30))
    seen = {}

    def fake_http(url, data=None, headers=None, timeout=20):
        seen.update(url=url, data=data, headers=headers)
        return {
            "access_token": "new",
            "refresh_token": "r2",
            "expires_at": int(time.time()) + 3600,
        }

    monkeypatch.setattr(auth, "_http_json", fake_http)
    assert auth.access_token() == "new"
    assert (
        seen["url"]
        == "https://example.supabase.co/auth/v1/token?grant_type=refresh_token"
    )
    assert seen["data"] == {"refresh_token": "r1"} and seen["headers"] == {
        "apikey": "anon"
    }
    assert auth.load_credentials()["refresh_token"] == "r2"


def test_env_token_wins(config_home, monkeypatch):
    auth.save_credentials(_creds())
    monkeypatch.setenv("RHYLTHYME_TOKEN", "from-env")
    assert auth.access_token() == "from-env"


def test_expired_without_refresh_token_asks_to_log_in(config_home):
    auth.save_credentials(_creds(expires_at=int(time.time()) - 10, refresh_token=None))
    with pytest.raises(auth.AuthError, match="rhylthyme login"):
        auth.access_token()


# ---- browser hand-off -------------------------------------------------------


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=5
        ) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_login_via_browser_accepts_matching_state_only(config_home, monkeypatch):
    monkeypatch.setattr(
        auth,
        "fetch_public_config",
        lambda site=None: {
            "supabase_url": "https://x.supabase.co",
            "supabase_anon_key": "anon",
        },
    )
    opened = {}

    def fake_browser(url):
        # Play the sign-in page: first a forged post, then the real one.
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        port, state = q["cli_port"][0], q["state"][0]
        opened["url"] = url
        callback = f"http://127.0.0.1:{port}/callback"

        def browser():
            opened["forged"] = _post_form(
                callback, {"state": "wrong", "access_token": "evil"}
            )
            opened["real"] = _post_form(
                callback,
                {
                    "state": state,
                    "access_token": "acc",
                    "refresh_token": "ref",
                    "expires_at": "1900000000",
                    "email": "a@b.c",
                },
            )

        threading.Thread(target=browser, daemon=True).start()
        return True

    monkeypatch.setattr(auth.webbrowser, "open", fake_browser)
    creds = auth.login_via_browser(
        site="https://www.rhylthyme.com", echo=lambda m: None, timeout=10
    )
    assert opened["url"].startswith("https://www.rhylthyme.com/mcp/auth?cli_port=")
    assert opened["forged"] == 400 and opened["real"] == 200
    assert creds["access_token"] == "acc" and creds["refresh_token"] == "ref"
    assert creds["expires_at"] == 1900000000 and creds["email"] == "a@b.c"
    assert auth.load_credentials()["supabase_anon_key"] == "anon"


def test_generate_run_hands_the_saved_program_to_run(
    fake_mcp, config_home, tmp_path, monkeypatch
):
    import sys

    # The package re-exports the `cli` group under the module's name.
    cli_module = sys.modules["rhylthyme.cli.main"]

    seen = {}
    monkeypatch.setattr(
        cli_module.run, "callback", lambda **kwargs: seen.update(kwargs)
    )
    monkeypatch.setenv("RHYLTHYME_TOKEN", "tok")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["generate", "dinner", "--no-publish", "--run"])
    assert result.exit_code == 0, result.output
    saved = tmp_path / "roast_dinner.json"
    assert seen["program_file"] == str(saved)
    assert json.loads(saved.read_text())["programId"] == "dinner"
    assert seen["time_scale"] == 1.0  # run's own defaults are filled in


def test_publish_needs_no_login_and_routes_by_environment_type(
    fake_mcp, config_home, tmp_path
):
    lab = dict(PROGRAM, environmentType="laboratory")
    f = tmp_path / "blot.json"
    f.write_text(json.dumps(lab))
    result = CliRunner().invoke(cli, ["publish", str(f), "-q"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "https://kitchen.rhylthyme.com?share=abc"
    path, name, args = fake_mcp.tool_calls()[-1]
    assert (path, name) == ("/lab/mcp", "visualize_schedule")
    assert args["program"]["environmentType"] == "laboratory" and "token" not in args

    as_json = CliRunner().invoke(cli, ["publish", str(f), "-e", "generic", "--json"])
    assert json.loads(as_json.output)["makespanSeconds"] == 3600
    assert fake_mcp.tool_calls()[-1][0] == "/mcp"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "no tracks"}))
    refused = CliRunner().invoke(cli, ["publish", str(bad)])
    assert refused.exit_code != 0 and "not a Rhylthyme program" in refused.output


def test_analyze_reports_conflicts_and_local_clock_times(
    fake_mcp, config_home, tmp_path
):
    f = tmp_path / "dinner.json"
    f.write_text(json.dumps(PROGRAM))
    plain = CliRunner().invoke(cli, ["analyze", str(f)])
    assert plain.exit_code == 0, plain.output
    assert "Resource conflicts (1)" in plain.output
    path, name, args = fake_mcp.tool_calls()[-1]
    assert (path, name) == ("/mcp", "analyze_schedule")
    assert set(args) == {"program"}, "no token, no clock unless asked"

    timed = CliRunner().invoke(cli, ["analyze", str(f), "--finish-at", "19:00"])
    assert timed.exit_code == 0, timed.output
    sent = fake_mcp.tool_calls()[-1][2]["finishAt"]
    assert datetime.fromisoformat(sent).utcoffset() is not None
    assert datetime.fromisoformat(sent).strftime("%H:%M") == "19:00"
    assert "utc" not in timed.output, "the server's UTC lines are replaced"
    assert "(local time)" in timed.output and "Roast" in timed.output

    strict = CliRunner().invoke(cli, ["analyze", str(f), "--strict"])
    assert strict.exit_code == 1
    as_json = CliRunner().invoke(cli, ["analyze", str(f), "--json"])
    assert json.loads(as_json.output)["makespanSeconds"] == 3600


def test_clock_to_iso():
    from rhylthyme.cli.remote.cli import _clock_to_iso

    now = datetime(2026, 11, 26, 14, 0).astimezone()
    assert _clock_to_iso("19:00", now).startswith("2026-11-26T19:00:00")
    assert _clock_to_iso("7:30pm", now).startswith("2026-11-26T19:30:00")
    assert _clock_to_iso("9am", now).startswith(
        "2026-11-27T09:00:00"
    ), "already past: tomorrow"
    assert _clock_to_iso("12am", now).startswith("2026-11-27T00:00:00")
    iso = "2026-11-26T18:00:00-05:00"
    assert _clock_to_iso(iso, now) == iso
    assert _clock_to_iso("25:00", now) == "25:00", "left for the server to reject"
    assert _clock_to_iso("7", now) == "7", "a bare number is not a time"


def test_publish_and_analyze_read_a_url_or_stdin(
    fake_mcp, config_home, tmp_path, monkeypatch
):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Files(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/dinner.json":
                body, status = json.dumps(PROGRAM).encode(), 200
            elif self.path == "/timeline.png":
                body, status = b"\x89PNG fake", 200
            else:
                body, status = b"nope", 404
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        from_url = CliRunner().invoke(cli, ["publish", f"{base}/dinner.json", "-q"])
        assert from_url.exit_code == 0, from_url.output
        assert from_url.output.strip() == "https://kitchen.rhylthyme.com?share=abc"
        assert fake_mcp.tool_calls()[-1][2]["program"]["programId"] == "dinner"

        piped = CliRunner().invoke(cli, ["analyze", "-"], input=json.dumps(PROGRAM))
        assert piped.exit_code == 0, piped.output
        assert fake_mcp.tool_calls()[-1][1] == "analyze_schedule"

        missing = CliRunner().invoke(cli, ["publish", f"{base}/nope.json"])
        assert missing.exit_code != 0 and "404" in missing.output
        assert (
            CliRunner()
            .invoke(cli, ["publish", str(tmp_path / "absent.json")])
            .exit_code
            != 0
        )
    finally:
        server.shutdown()
        server.server_close()


def test_clock_and_sources_reject_other_schemes():
    from rhylthyme.cli.remote.cli import _fetch_bytes

    with pytest.raises(ValueError):
        _fetch_bytes("file:///etc/passwd")


# --- Workcells never leave the machine (galago instruments) ---------------

WORKCELL = {
    "id": "bench",
    "tools": [
        {"name": "shaker", "type": "bioshake", "host": "10.20.30.40", "port": 50710}
    ],
}


@pytest.mark.parametrize("command", ["publish", "analyze"])
def test_workcell_files_are_refused_before_any_request(
    command, fake_mcp, config_home, tmp_path
):
    f = tmp_path / "lab.json"
    f.write_text(json.dumps(WORKCELL))
    result = CliRunner().invoke(cli, [command, str(f)])
    assert result.exit_code != 0
    assert "is a workcell file" in result.output
    assert fake_mcp.calls == []


@pytest.mark.parametrize("command", ["publish", "analyze"])
def test_instrument_programs_are_sent_without_workcell_data(
    command, fake_mcp, config_home, tmp_path
):
    program = json.loads(json.dumps(PROGRAM))
    program["tracks"][0]["steps"][0]["instrument"] = {
        "tool": "shaker",
        "command": "start_shake",
    }
    f = tmp_path / "shake.json"
    f.write_text(json.dumps(program))
    (tmp_path / "lab.json").write_text(json.dumps(WORKCELL))
    CliRunner().invoke(cli, [command, str(f)])
    assert fake_mcp.calls, "the program itself should have been sent"
    sent = "".join(str(body) for _, body in fake_mcp.calls)
    assert "10.20.30.40" not in sent and "50710" not in sent


def test_analyze_sends_estimated_instrument_durations(fake_mcp, config_home, tmp_path):
    pytest.importorskip("rhylthyme_galago")
    program = json.loads(json.dumps(PROGRAM))
    step = program["tracks"][0]["steps"][0]
    step.pop("duration", None)
    step["instrument"] = {
        "tool": "shaker",
        "command": "start_shake",
        "params": {"duration": 90},
    }
    f = tmp_path / "shake.json"
    f.write_text(json.dumps(program))
    result = CliRunner().invoke(cli, ["analyze", str(f), "--json"])
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # estimate notes go to stderr, not into the JSON
    assert "from params.duration" in result.stderr
    name, args = fake_mcp.tool_calls()[-1][1:]
    sent = args["program"]["tracks"][0]["steps"][0]
    assert sent["duration"] == {"type": "fixed", "seconds": 90}
    assert sent["metadata"]["durationEstimate"]["source"] == "params"
