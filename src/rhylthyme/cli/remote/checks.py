"""Smoke tests for a Rhylthyme MCP server, run by ``rhylthyme mcp-test``.

Each check makes real requests against one endpoint (``/mcp``,
``/kitchen/mcp``, ...) and returns a :class:`CheckResult`:

* ``pass``  the server did what the protocol and the tool contract say
* ``warn``  it works but something is off (empty catalog, needed a retry)
* ``fail``  a client would be broken by this
* ``skip``  not applicable, or an earlier check it depends on failed

The default suite is read-only and free. ``publish`` creates one shared
timeline per endpoint; ``generate`` runs the model-backed ``import_text``
once and needs a sign-in. Nothing here imports click, so the suite is
usable from pytest or a monitoring script.
"""

from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from rhylthyme import __version__ as _pkg_version

from .mcp_client import (
    VERTICALS,
    McpClient,
    McpError,
    endpoint_for,
    extract_program,
    parse_rpc_body,
)

ENDPOINTS = ("generic",) + VERTICALS

CORE_TOOLS = {
    "validate_program",
    "analyze_schedule",
    "visualize_schedule",
    "import_from_source",
    "import_text",
    "create_environment",
    "login",
    "search_public_recipes",
    "load_public_recipe",
    "save_program",
}
VERTICAL_TOOLS = {
    "kitchen": {"cook_recipe", "whats_for_dinner"},
    "lab": {"run_protocol", "random_protocol"},
    "events": {"plan_event", "random_event_template"},
    "gym": {"start_workout", "surprise_workout"},
}
SERVER_NAMES = {
    "generic": "rhylthyme-mcp",
    **{v: f"rhylthyme-{v}-mcp" for v in VERTICALS},
}
SITE_HOSTS = {
    "generic": "www.rhylthyme.com",
    **{v: f"{v}.rhylthyme.com" for v in VERTICALS},
}

#: Two tracks, one shared stove, and a compound trigger carrying the
#: ``type: "compound"`` label models like to add (the server once rejected
#: it). Makespan is 1500 s: mix 300 -> rest 600 -> cook 600.
FIXTURE_MAKESPAN = 1500
FIXTURE: Dict[str, Any] = {
    "schemaVersion": "0.1.0",
    "programId": "mcp-test-pancakes",
    "name": "MCP Test Pancakes",
    "tracks": [
        {
            "trackId": "batter",
            "name": "Batter",
            "steps": [
                {
                    "stepId": "mix",
                    "name": "Mix batter",
                    "task": "prep",
                    "duration": {"type": "fixed", "seconds": 300},
                    "startTrigger": {"type": "programStart"},
                },
                {
                    "stepId": "rest",
                    "name": "Rest batter",
                    "task": "counter",
                    "duration": {"type": "fixed", "seconds": "10m"},
                    "startTrigger": {"type": "afterStep", "stepId": "mix"},
                },
            ],
        },
        {
            "trackId": "griddle",
            "name": "Griddle",
            "steps": [
                {
                    "stepId": "heat",
                    "name": "Heat griddle",
                    "task": "stove",
                    "duration": {"type": "fixed", "seconds": 300},
                    "startTrigger": {
                        "type": "programStartOffset",
                        "offsetSeconds": 600,
                    },
                },
                {
                    "stepId": "cook",
                    "name": "Cook pancakes",
                    "task": "stove",
                    "duration": {"type": "fixed", "seconds": 600},
                    "startTrigger": {
                        "type": "compound",
                        "logic": "all",
                        "triggers": [
                            {"type": "afterStep", "stepId": "rest"},
                            {"type": "afterStep", "stepId": "heat"},
                        ],
                    },
                },
            ],
        },
    ],
    "resourceConstraints": [
        {"task": "prep", "maxConcurrent": 1},
        {"task": "counter", "maxConcurrent": 1},
        {"task": "stove", "maxConcurrent": 1},
    ],
}

GENERATE_TEXT = (
    "Toast and eggs: toast two slices of bread for 3 minutes. "
    "Meanwhile fry two eggs for 4 minutes. Plate everything together."
)


class CheckFailed(Exception):
    """Raised inside a check to fail it with a message."""


class CheckWarning(Exception):
    """Raised inside a check to pass it with a warning."""


class CheckSkipped(Exception):
    """Raised inside a check that does not apply."""


@dataclass
class CheckResult:
    endpoint: str
    name: str
    status: str  # pass | warn | fail | skip
    detail: str = ""
    ms: int = 0


@dataclass
class SuiteOptions:
    base_url: str
    endpoints: Iterable[str] = ENDPOINTS
    publish: bool = False
    generate: bool = False
    token: Optional[str] = None
    timeout: float = 60.0
    on_result: Optional[Callable[[CheckResult], None]] = None


@dataclass
class SuiteReport:
    base_url: str
    results: List[CheckResult] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def ok(self) -> bool:
        return self.count("fail") == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "baseUrl": self.base_url,
            "ok": self.ok,
            "counts": {s: self.count(s) for s in ("pass", "warn", "fail", "skip")},
            "results": [asdict(r) for r in self.results],
        }


def _need(cond: Any, message: str) -> None:
    if not cond:
        raise CheckFailed(message)


def _is_hosted(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "").endswith("rhylthyme.com")


def _http_get_text(url: str, timeout: float = 30) -> tuple:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "rhylthyme-cli/mcp-test", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                resp.headers.get("Content-Type", ""),
                resp.read().decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            e.headers.get("Content-Type", ""),
            e.read().decode("utf-8", "replace"),
        )


def _http_get(url: str, timeout: float = 30) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": "rhylthyme-cli/mcp-test"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(64)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), b""


# ---------------------------------------------------------------------------
# Checks. Each takes (client, endpoint, options, state) and returns a short
# detail string, or raises CheckFailed / CheckWarning / CheckSkipped.
# ---------------------------------------------------------------------------


def check_initialize(client, endpoint, opts, state) -> str:
    result = client.initialize()
    info = result.get("serverInfo") or {}
    _need(info.get("name"), "no serverInfo.name")
    caps = result.get("capabilities") or {}
    for cap in ("tools", "resources", "prompts"):
        _need(cap in caps, f"capability '{cap}' not advertised")
    _need((result.get("instructions") or "").strip(), "no server instructions")
    _need(result.get("protocolVersion"), "no protocolVersion")
    expected = SERVER_NAMES[endpoint]
    if _is_hosted(opts.base_url):
        _need(
            info["name"] == expected,
            f"serverInfo.name is {info['name']!r}, expected {expected!r}",
        )
    return f"{info['name']} {info.get('version', '?')}, protocol {result['protocolVersion']}"


TOOL_DEFINITION_BUDGET_CHARS = 17000


def check_tools(client, endpoint, opts, state) -> str:
    tools = client.request("tools/list").get("tools") or []
    names = {t.get("name") for t in tools}
    missing = sorted((CORE_TOOLS | VERTICAL_TOOLS.get(endpoint, set())) - names)
    _need(not missing, f"missing tools: {', '.join(missing)}")
    if endpoint == "generic":
        stray = sorted(set().union(*VERTICAL_TOOLS.values()) & names)
        _need(not stray, f"vertical tools on the generic endpoint: {', '.join(stray)}")
    for t in tools:
        _need(
            (t.get("description") or "").strip(),
            f"tool {t.get('name')} has no description",
        )
        schema = t.get("inputSchema") or {}
        _need(
            schema.get("type") == "object",
            f"tool {t.get('name')} inputSchema is not an object",
        )
    # What a host pastes into the model's context on every turn. A server
    # that costs too much to keep connected does not get used.
    seen = sum(
        len(
            json.dumps(
                {
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "inputSchema": t.get("inputSchema"),
                }
            )
        )
        for t in tools
    )
    detail = f"{len(tools)} tools, about {round(seen / 4 / 100) * 100:,} tokens of definitions"
    if seen > TOOL_DEFINITION_BUDGET_CHARS:
        raise CheckWarning(
            f"{detail}; budget is about {TOOL_DEFINITION_BUDGET_CHARS // 4:,}"
        )
    return detail


def check_validate_good(client, endpoint, opts, state) -> str:
    r = client.call_tool("validate_program", {"program": FIXTURE})
    s = r.structured or {}
    _need(
        s.get("valid") is True,
        f"valid program rejected: {json.dumps(s.get('errors'))[:200]}",
    )
    steps = (s.get("stats") or {}).get("steps")
    _need(steps == 4, f"stats.steps is {steps}, expected 4")
    return "valid, 4 steps (compound trigger label accepted)"


def check_validate_bad(client, endpoint, opts, state) -> str:
    bad = copy.deepcopy(FIXTURE)
    bad["tracks"][0]["steps"][1]["startTrigger"]["stepId"] = "no-such-step"
    r = client.call_tool("validate_program", {"program": bad}, raise_on_error=False)
    s = r.structured or {}
    _need(s.get("valid") is False, "a dangling step reference was reported as valid")
    codes = [e.get("code") for e in s.get("errors") or []]
    _need("dangling_step_ref" in codes, f"expected dangling_step_ref, got {codes}")
    first = next(e for e in s["errors"] if e.get("code") == "dangling_step_ref")
    _need(first.get("fix"), "error carries no fix hint")
    return f"rejected with {', '.join(sorted(set(codes)))}"


def check_analyze(client, endpoint, opts, state) -> str:
    r = client.call_tool(
        "analyze_schedule", {"program": FIXTURE, "finishAt": "2030-01-01T18:00:00Z"}
    )
    s = r.structured or {}
    _need(
        s.get("makespanSeconds") == FIXTURE_MAKESPAN,
        f"makespanSeconds is {s.get('makespanSeconds')}, expected {FIXTURE_MAKESPAN}",
    )
    _need(s.get("criticalPath"), "no critical path")
    _need(s.get("wallClock"), "finishAt given but no wallClock itinerary")
    _need(
        not s.get("resourceConflicts"),
        f"unexpected resource conflicts: {s.get('resourceConflicts')}",
    )
    return f"makespan {FIXTURE_MAKESPAN} s, critical path of {len(s['criticalPath'])}"


def check_resources(client, endpoint, opts, state) -> str:
    resources = client.request("resources/list").get("resources") or []
    uris = {r.get("uri") for r in resources}
    for uri in ("rhylthyme://schema/program", "rhylthyme://guide/authoring"):
        _need(uri in uris, f"resource {uri} not listed")
    _need(
        any(u.startswith("rhylthyme://examples/") for u in uris),
        "no example programs listed",
    )
    contents = (
        client.request("resources/read", {"uri": "rhylthyme://schema/program"}).get(
            "contents"
        )
        or []
    )
    _need(contents and contents[0].get("text"), "schema resource is empty")
    schema = json.loads(contents[0]["text"])
    _need(isinstance(schema, dict) and schema, "schema resource is not a JSON object")
    example_uri = sorted(u for u in uris if u.startswith("rhylthyme://examples/"))[0]
    example = (
        client.request("resources/read", {"uri": example_uri}).get("contents") or []
    )
    program = json.loads(example[0]["text"])
    _need(isinstance(program.get("tracks"), list), f"{example_uri} is not a program")
    v = client.call_tool("validate_program", {"program": program}, raise_on_error=False)
    _need(
        (v.structured or {}).get("valid") is True,
        f"bundled example {example_uri} does not validate",
    )
    return f"{len(resources)} resources; schema parses; {example_uri.rsplit('/', 1)[-1]} validates"


def check_prompts(client, endpoint, opts, state) -> str:
    prompts = client.request("prompts/list").get("prompts") or []
    plan = next((p for p in prompts if p.get("name") == "plan_schedule"), None)
    _need(plan, "plan_schedule prompt not listed")
    got = client.request(
        "prompts/get",
        {"name": "plan_schedule", "arguments": {"goal": "pancakes for four by 9am"}},
    )
    messages = got.get("messages") or []
    _need(messages, "plan_schedule returned no messages")
    text = json.dumps(messages)
    _need("pancakes for four" in text, "the goal was not substituted into the prompt")
    return f"{len(prompts)} prompt(s); plan_schedule returns {len(messages)} message(s)"


def check_json_accept(client, endpoint, opts, state) -> str:
    """Clients that don't accept SSE (curl, uptime checkers) must get JSON, not 406."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for accept in ("application/json", "*/*"):
        status, ctype, body = client.send_raw(
            payload, accept=accept, timeout=opts.timeout
        )
        _need(status == 200, f"Accept: {accept} -> HTTP {status}")
        _need(
            "application/json" in ctype, f"Accept: {accept} -> Content-Type {ctype!r}"
        )
        _need(
            parse_rpc_body(body).get("result", {}).get("tools"),
            f"Accept: {accept} -> no tools in reply",
        )
    return "application/json and */* both get a JSON reply"


def check_bad_requests(client, endpoint, opts, state) -> str:
    status, _ctype, body = client.send_raw(
        {"jsonrpc": "2.0", "id": 1, "method": "no/such-method"}, timeout=opts.timeout
    )
    _need(status < 500, f"unknown method -> HTTP {status}")
    err = parse_rpc_body(body).get("error") or {}
    _need(
        err.get("code") == -32601,
        f"unknown method -> error code {err.get('code')}, expected -32601",
    )
    status, _ctype, body = client.send_raw(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        },
        timeout=opts.timeout,
    )
    _need(status < 500, f"unknown tool -> HTTP {status}")
    msg = parse_rpc_body(body)
    _need(
        msg.get("error") or (msg.get("result") or {}).get("isError"),
        "unknown tool did not produce an error",
    )
    return "unknown method -> -32601; unknown tool -> error, no 5xx"


def check_login_gate(client, endpoint, opts, state) -> str:
    """Account tools refuse without a credential: an OAuth 401 challenge, or a login hint."""
    status, headers_ctype, body = client.send_raw(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "import_text",
                "arguments": {"text": "toast", "environmentType": "generic"},
            },
        },
        timeout=opts.timeout,
    )
    challenge = client.last_headers.get(
        "www-authenticate", ""
    )  # before the next request replaces it
    hint = client.call_tool("login", {})
    _need("/mcp/auth" in hint.text, "login (no token) does not return the sign-in URL")
    if status == 401:
        _need(
            "resource_metadata=" in challenge,
            "401 without a WWW-Authenticate resource_metadata challenge",
        )
        return "import_text answers 401 with an OAuth challenge; login still returns the sign-in URL"
    _need(status == 200, f"import_text without a credential -> HTTP {status}")
    result = parse_rpc_body(body).get("result") or {}
    text = " ".join(c.get("text", "") for c in result.get("content") or [])
    _need(result.get("isError"), "import_text ran without a token")
    _need("login" in text.lower(), "the refusal does not tell the model to call login")
    return "import_text refuses without a token; login returns the sign-in URL"


def check_oauth(client, endpoint, opts, state) -> str:
    """OAuth discovery: protected-resource metadata names an authorization server that publishes its own."""
    from urllib.parse import urlparse

    u = urlparse(client.url)
    origin = f"{u.scheme}://{u.netloc}"
    status, _ctype, raw = _http_get_text(
        f"{origin}/.well-known/oauth-protected-resource{u.path}"
    )
    if status in (404, 405, 501):
        raise CheckSkipped("this server does not advertise OAuth")
    _need(status == 200, f"protected-resource metadata -> HTTP {status}")
    meta = json.loads(raw)
    _need(
        meta.get("resource") == client.url,
        f"resource is {meta.get('resource')!r}, expected {client.url!r}",
    )
    servers = meta.get("authorization_servers") or []
    _need(servers, "no authorization_servers listed")
    iss = urlparse(servers[0])
    as_url = f"{iss.scheme}://{iss.netloc}/.well-known/oauth-authorization-server{iss.path.rstrip('/')}"
    status, _ctype, raw = _http_get_text(as_url)
    if status != 200:
        raise CheckWarning(
            f"authorization server metadata at {as_url} -> HTTP {status} (is the OAuth server enabled?)"
        )
    as_meta = json.loads(raw)
    for key in ("authorization_endpoint", "token_endpoint"):
        _need(as_meta.get(key), f"authorization server metadata has no {key}")
    if not as_meta.get("registration_endpoint"):
        raise CheckWarning(
            "no registration_endpoint: hosts such as Claude need dynamic client registration"
        )
    _need(
        "S256" in (as_meta.get("code_challenge_methods_supported") or ["S256"]),
        "PKCE S256 not supported",
    )
    return f"authorization server {iss.netloc}; registration and PKCE available"


def check_catalog(client, endpoint, opts, state) -> str:
    args = {"query": "", "limit": 3}
    retried = False
    try:
        r = client.call_tool("search_public_recipes", args)
    except McpError as first:
        retried = True
        try:
            r = client.call_tool("search_public_recipes", args)
        except McpError:
            raise CheckFailed(f"search failed twice: {str(first)[:160]}")
    s = r.structured or {}
    count = s.get("count") or 0
    if endpoint in ("generic", "kitchen"):
        _need(count >= 1, "the kitchen catalog returned nothing")
    if count:
        first_id = s["results"][0]["id"]
        loaded = client.call_tool("load_public_recipe", {"program_id": first_id})
        _need("http" in loaded.text, "load_public_recipe returned no URL")
    if retried:
        raise CheckWarning(
            f"first search failed (cold cache / statement timeout), retry returned {count}"
        )
    if not count:
        one_shot = sorted(VERTICAL_TOOLS.get(endpoint, {"?"}))[0]
        raise CheckWarning(
            f"the {s.get('environment') or endpoint} catalog is empty, so {one_shot} can never find a match"
        )
    return f"{count} result(s); first one loads with a URL"


def check_publish(client, endpoint, opts, state) -> str:
    if not opts.publish:
        raise CheckSkipped("pass --publish to create a shared timeline")
    r = client.call_tool(
        "visualize_schedule", {"program": FIXTURE}, timeout=opts.timeout
    )
    s = r.structured or {}
    url = s.get("url") or ""
    _need(
        url.startswith("http") and s.get("shareId"),
        f"no share URL in result: {json.dumps(s)[:160]}",
    )
    _need(
        s.get("makespanSeconds") == FIXTURE_MAKESPAN,
        f"makespanSeconds {s.get('makespanSeconds')}",
    )
    if _is_hosted(opts.base_url):
        host = urlparse(url).hostname
        _need(
            host == SITE_HOSTS[endpoint],
            f"URL host is {host}, expected {SITE_HOSTS[endpoint]}",
        )
    status, _ctype, _ = _http_get(url)
    _need(status == 200, f"share URL -> HTTP {status}")
    if s.get("imageUrl"):
        status, ctype, head = _http_get(s["imageUrl"])
        _need(
            status == 200 and "image/png" in ctype,
            f"timeline image -> HTTP {status} {ctype}",
        )
        _need(head.startswith(b"\x89PNG"), "timeline image is not a PNG")
    _need("Mix batter" in r.text, "the text summary does not list the steps")
    return f"{url} (page and PNG load)"


def check_generate(client, endpoint, opts, state) -> str:
    if not opts.generate:
        raise CheckSkipped("pass --generate to run the model-backed import_text")
    if state.get("generated"):
        raise CheckSkipped("already run on another endpoint (it costs model calls)")
    if not opts.token:
        raise CheckFailed("--generate needs a sign-in: run `rhylthyme login`")
    state["generated"] = True
    env = endpoint if endpoint != "generic" else "kitchen"
    r = client.call_tool(
        "import_text",
        {"text": GENERATE_TEXT, "environmentType": env, "token": opts.token},
        timeout=240,
    )
    program = extract_program(r.text)
    if not program:
        raise CheckFailed("import_text returned no program JSON")
    v = client.call_tool("validate_program", {"program": program}, raise_on_error=False)
    _need(
        (v.structured or {}).get("valid") is True,
        "the generated program does not validate",
    )
    steps: int = sum(len(t.get("steps") or []) for t in program["tracks"])
    _need(steps >= 2, f"only {steps} step(s) extracted")
    return f"{len(program['tracks'])} track(s), {steps} steps, validates"


CHECKS: List[tuple] = [
    ("initialize", check_initialize),
    ("tools", check_tools),
    ("validate-good", check_validate_good),
    ("validate-bad", check_validate_bad),
    ("analyze", check_analyze),
    ("resources", check_resources),
    ("prompts", check_prompts),
    ("json-accept", check_json_accept),
    ("bad-requests", check_bad_requests),
    ("login-gate", check_login_gate),
    ("oauth", check_oauth),
    ("catalog", check_catalog),
    ("publish", check_publish),
    ("generate", check_generate),
]


def run_suite(opts: SuiteOptions, only: Optional[Iterable[str]] = None) -> SuiteReport:
    report = SuiteReport(base_url=opts.base_url)
    wanted = set(only) if only else None
    state: Dict[str, Any] = {}
    for endpoint in opts.endpoints:
        # "mcp-test" in the User-Agent tells the server not to page anyone
        # about the errors these checks provoke on purpose.
        client = McpClient(
            url=endpoint_for(endpoint, opts.base_url),
            timeout=opts.timeout,
            user_agent=f"rhylthyme-cli/{_pkg_version} mcp-test",
        )
        reachable = True
        for name, fn in CHECKS:
            if wanted and name not in wanted and name != "initialize":
                continue
            started = time.monotonic()
            if not reachable:
                result = CheckResult(endpoint, name, "skip", "endpoint unreachable")
            else:
                try:
                    detail = fn(client, endpoint, opts, state)
                    result = CheckResult(endpoint, name, "pass", detail or "")
                except CheckSkipped as e:
                    result = CheckResult(endpoint, name, "skip", str(e))
                except CheckWarning as e:
                    result = CheckResult(endpoint, name, "warn", str(e))
                except CheckFailed as e:
                    result = CheckResult(endpoint, name, "fail", str(e))
                except McpError as e:
                    result = CheckResult(
                        endpoint, name, "fail", " ".join(str(e).split())[:300]
                    )
                    if name == "initialize":
                        reachable = False
                except (
                    Exception
                ) as e:  # noqa: BLE001 - a broken reply must not end the run
                    result = CheckResult(
                        endpoint, name, "fail", f"{type(e).__name__}: {e}"[:300]
                    )
            result.ms = int((time.monotonic() - started) * 1000)
            report.results.append(result)
            if opts.on_result:
                opts.on_result(result)
    return report
