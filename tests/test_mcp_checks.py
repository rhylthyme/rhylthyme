"""`rhylthyme mcp-test`: the checks pass against a well-behaved server and
each one fails when the behaviour it guards is broken.

A local fake MCP server answers every method the suite uses; ``broken``
switches individual behaviours off. Set RHYLTHYME_MCP_LIVE=1 to also run
the read-only suite against the real hosted server.
"""

import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from click.testing import CliRunner

from rhylthyme.cli.main import cli
from rhylthyme.cli.remote import checks

pytestmark = pytest.mark.unit

ALL_TOOLS = sorted(checks.CORE_TOOLS | set().union(*checks.VERTICAL_TOOLS.values()))


class FakeServer:
    def __init__(self, broken=()):
        self.broken = set(broken)
        self.user_agents = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, ctype, text):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                origin = f"http://{self.headers['Host']}"
                if "no-oauth" in fake.broken:
                    return self._send(404, "application/json", "{}")
                if self.path.startswith("/.well-known/oauth-protected-resource"):
                    path = (
                        self.path[len("/.well-known/oauth-protected-resource") :]
                        or "/mcp"
                    )
                    return self._send(
                        200,
                        "application/json",
                        json.dumps(
                            {
                                "resource": origin + path,
                                "authorization_servers": [origin + "/auth/v1"],
                            }
                        ),
                    )
                if self.path == "/.well-known/oauth-authorization-server/auth/v1":
                    meta = {
                        "issuer": origin + "/auth/v1",
                        "authorization_endpoint": origin + "/a",
                        "token_endpoint": origin + "/t",
                        "registration_endpoint": origin + "/r",
                        "code_challenge_methods_supported": ["S256"],
                    }
                    if "oauth-no-registration" in fake.broken:
                        meta.pop("registration_endpoint")
                    return self._send(200, "application/json", json.dumps(meta))
                return self._send(404, "application/json", "{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.user_agents.add(self.headers.get("User-Agent"))
                accept = self.headers.get("Accept", "")
                wants_sse = "text/event-stream" in accept
                if "no-json-accept" in fake.broken and not wants_sse:
                    return self._send(
                        406, "application/json", '{"error":"Not Acceptable"}'
                    )
                if "id" not in body:
                    return self._send(202, "application/json", "")
                if (
                    "oauth-401" in fake.broken
                    and body.get("method") == "tools/call"
                    and body["params"]["name"] == "import_text"
                    and not body["params"]["arguments"].get("token")
                ):
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        'Bearer resource_metadata="http://x/.well-known/oauth-protected-resource/mcp"',
                    )
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                vertical = (
                    self.path.split("/")[1] if self.path.count("/") == 2 else "generic"
                )
                reply = {"jsonrpc": "2.0", "id": body["id"]}
                try:
                    reply["result"] = fake.handle(
                        vertical, body["method"], body.get("params") or {}
                    )
                except KeyError:
                    reply["error"] = {"code": -32601, "message": "Method not found"}
                except ValueError as e:
                    reply["error"] = {"code": -32602, "message": str(e)}
                if wants_sse:
                    self._send(
                        200,
                        "text/event-stream",
                        f"event: message\ndata: {json.dumps(reply)}\n\n",
                    )
                else:
                    self._send(200, "application/json", json.dumps(reply))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- behaviour ---------------------------------------------------------

    def handle(self, vertical, method, params):
        if method == "initialize":
            caps = {"tools": {}, "resources": {}, "prompts": {}}
            if "no-prompts-capability" in self.broken:
                caps.pop("prompts")
            return {
                "protocolVersion": "2025-06-18",
                "serverInfo": {
                    "name": checks.SERVER_NAMES[vertical],
                    "version": "9.9.9",
                },
                "capabilities": caps,
                "instructions": "Use validate_program then visualize_schedule.",
            }
        if method == "tools/list":
            names = set(checks.CORE_TOOLS) | checks.VERTICAL_TOOLS.get(vertical, set())
            if "missing-tool" in self.broken:
                names.discard("analyze_schedule")
            return {
                "tools": [
                    {
                        "name": n,
                        "description": f"{n} tool"
                        + (" " + "x" * 2000 if "fat-tools" in self.broken else ""),
                        "inputSchema": {"type": "object"},
                    }
                    for n in sorted(names)
                ]
            }
        if method == "resources/list":
            return {
                "resources": [
                    {"uri": u}
                    for u in (
                        "rhylthyme://schema/program",
                        "rhylthyme://guide/authoring",
                        "rhylthyme://examples/pancakes",
                    )
                ]
            }
        if method == "resources/read":
            text = (
                json.dumps({"type": "object"})
                if params["uri"].endswith("schema/program")
                else json.dumps(checks.FIXTURE)
            )
            return {
                "contents": [
                    {"uri": params["uri"], "mimeType": "application/json", "text": text}
                ]
            }
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": "plan_schedule",
                        "arguments": [{"name": "goal", "required": True}],
                    }
                ]
            }
        if method == "prompts/get":
            goal = (
                ""
                if "prompt-ignores-goal" in self.broken
                else params["arguments"]["goal"]
            )
            return {
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": f"Plan: {goal}"},
                    }
                ]
            }
        if method == "tools/call":
            return self.call(vertical, params["name"], params.get("arguments") or {})
        raise KeyError(method)

    def call(self, vertical, name, args):
        text = lambda t, **extra: dict(
            {"content": [{"type": "text", "text": t}]}, **extra
        )  # noqa: E731
        if name == "validate_program":
            program = args["program"]
            ids = {s["stepId"] for t in program["tracks"] for s in t["steps"]}
            dangling = [
                s["stepId"]
                for t in program["tracks"]
                for s in t["steps"]
                if s["startTrigger"].get("stepId")
                and s["startTrigger"]["stepId"] not in ids
            ]
            if dangling and "accepts-dangling" not in self.broken:
                errors = [
                    {
                        "code": "dangling_step_ref",
                        "message": "x",
                        "fix": "fix the stepId",
                    }
                ]
                return text(
                    "invalid", structuredContent={"valid": False, "errors": errors}
                )
            return text(
                "ok",
                structuredContent={
                    "valid": True,
                    "errors": [],
                    "stats": {"steps": len(ids)},
                },
            )
        if name == "analyze_schedule":
            makespan = (
                999 if "wrong-makespan" in self.broken else checks.FIXTURE_MAKESPAN
            )
            return text(
                "analysis",
                structuredContent={
                    "makespanSeconds": makespan,
                    "criticalPath": ["mix", "rest", "cook"],
                    "wallClock": {"startAt": "x"},
                    "resourceConflicts": [],
                },
            )
        if name == "import_text":
            if args.get("token") or "no-login-gate" in self.broken:
                return text("done\n```json\n" + json.dumps(checks.FIXTURE) + "\n```")
            return text(
                "import_text requires the user's Rhylthyme access token. Call **login**.",
                isError=True,
            )
        if name == "login":
            return text("Open https://www.rhylthyme.com/mcp/auth")
        if name == "search_public_recipes":
            empty = "empty-catalog" in self.broken and vertical not in (
                "generic",
                "kitchen",
            )
            results = [] if empty else [{"id": "abc", "name": "Pancakes"}]
            return text(
                "results",
                structuredContent={
                    "count": len(results),
                    "environment": vertical,
                    "results": results,
                },
            )
        if name == "load_public_recipe":
            if "program_id" not in args:
                raise ValueError("program_id required")
            return text("# Pancakes\nhttps://kitchen.rhylthyme.com/?program=abc")
        raise ValueError(f"unknown tool {name}")


@pytest.fixture
def server():
    made = []

    def make(broken=()):
        s = FakeServer(broken)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def run(fake, endpoints=("generic", "lab"), **kw):
    return checks.run_suite(
        checks.SuiteOptions(base_url=fake.url, endpoints=endpoints, timeout=10, **kw)
    )


def statuses(report, name):
    return {r.endpoint: r.status for r in report.results if r.name == name}


def test_healthy_server_passes_every_default_check(server):
    fake = server()
    report = run(fake, endpoints=checks.ENDPOINTS)
    bad = [
        (r.endpoint, r.name, r.detail)
        for r in report.results
        if r.status in ("fail", "warn")
    ]
    assert bad == []
    assert report.ok and report.count("pass") == 12 * 5
    assert {r.name for r in report.results if r.status == "skip"} == {
        "publish",
        "generate",
    }
    assert all(
        "mcp-test" in ua for ua in fake.user_agents
    ), "test traffic must be labelled"


@pytest.mark.parametrize(
    "broken, check, needle",
    [
        ("no-prompts-capability", "initialize", "prompts"),
        ("missing-tool", "tools", "analyze_schedule"),
        ("accepts-dangling", "validate-bad", "reported as valid"),
        ("wrong-makespan", "analyze", "999"),
        ("prompt-ignores-goal", "prompts", "not substituted"),
        ("no-json-accept", "json-accept", "406"),
        ("no-login-gate", "login-gate", "without a token"),
    ],
)
def test_each_check_catches_its_regression(server, broken, check, needle):
    report = run(server([broken]), endpoints=("generic",))
    (result,) = [r for r in report.results if r.name == check]
    assert result.status == "fail", result
    assert needle in result.detail
    assert not report.ok


def test_empty_vertical_catalog_is_a_warning_not_a_failure(server):
    report = run(server(["empty-catalog"]))
    assert statuses(report, "catalog") == {"generic": "pass", "lab": "warn"}
    assert report.ok


def test_oversized_tool_definitions_are_a_warning(server):
    report = run(server(["fat-tools"]), endpoints=("generic",))
    assert statuses(report, "tools") == {"generic": "warn"}
    detail = next(r.detail for r in report.results if r.name == "tools")
    assert "tokens of definitions" in detail and "budget" in detail
    assert report.ok


def test_oauth_check_skips_warns_and_accepts_a_401_challenge(server):
    assert statuses(run(server(["no-oauth"]), endpoints=("generic",)), "oauth") == {
        "generic": "skip"
    }
    assert statuses(
        run(server(["oauth-no-registration"]), endpoints=("generic",)), "oauth"
    ) == {"generic": "warn"}
    assert statuses(
        run(server(["oauth-401"]), endpoints=("generic",)), "login-gate"
    ) == {"generic": "pass"}


def test_unreachable_endpoint_fails_initialize_and_skips_the_rest():
    report = checks.run_suite(
        checks.SuiteOptions(
            base_url="http://127.0.0.1:9/mcp", endpoints=("generic",), timeout=2
        )
    )
    by_name = {r.name: r.status for r in report.results}
    assert by_name["initialize"] == "fail"
    assert set(by_name.values()) == {"fail", "skip"}


def test_generate_runs_once_and_needs_a_token(server):
    fake = server()
    report = run(fake, generate=True, token="tok")
    assert statuses(report, "generate") == {"generic": "pass", "lab": "skip"}
    no_token = run(fake, endpoints=("generic",), generate=True)
    assert statuses(no_token, "generate") == {"generic": "fail"}


def test_cli_exit_codes_json_and_filters(server, monkeypatch):
    healthy, broken = server(), server(["wrong-makespan", "empty-catalog"])
    runner = CliRunner()

    ok = runner.invoke(cli, ["mcp-test", "--url", healthy.url, "-e", "generic"])
    assert ok.exit_code == 0, ok.output
    assert "12 passed, 0 warning(s), 0 failed, 2 skipped" in ok.output

    bad = runner.invoke(
        cli, ["mcp-test", "--url", broken.url, "-e", "generic", "--json"]
    )
    assert bad.exit_code == 1
    data = json.loads(bad.output)
    assert data["ok"] is False and data["counts"]["fail"] == 1
    assert [r["name"] for r in data["results"] if r["status"] == "fail"] == ["analyze"]

    only = runner.invoke(
        cli, ["mcp-test", "--url", broken.url, "-e", "lab", "-k", "catalog"]
    )
    assert only.exit_code == 0, only.output  # a warning alone passes...
    strict = runner.invoke(
        cli, ["mcp-test", "--url", broken.url, "-e", "lab", "-k", "catalog", "--strict"]
    )
    assert strict.exit_code == 1  # ...unless --strict

    assert runner.invoke(cli, ["mcp-test", "-k", "nope"]).exit_code != 0
    listing = runner.invoke(cli, ["mcp-test", "--list"])
    assert listing.exit_code == 0 and "json-accept" in listing.output


def test_fixture_is_a_valid_program():
    from rhylthyme.validate_program import perform_additional_validations

    assert perform_additional_validations(copy.deepcopy(checks.FIXTURE)) == []


@pytest.mark.skipif(
    os.environ.get("RHYLTHYME_MCP_LIVE") != "1",
    reason="set RHYLTHYME_MCP_LIVE=1 to hit the hosted server",
)
def test_live_hosted_server_has_no_failures():
    from rhylthyme.cli.remote.mcp_client import base_url

    report = checks.run_suite(checks.SuiteOptions(base_url=base_url()))
    assert [
        (r.endpoint, r.name, r.detail) for r in report.results if r.status == "fail"
    ] == []
