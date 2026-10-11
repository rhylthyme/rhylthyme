"""``rhylthyme login | logout | whoami | generate | publish | mcp-test``.

``generate`` is the natural-language path: it sends the request to the
hosted MCP server's ``import_text`` tool (four model turns, run and paid
for server-side, so it needs a sign-in), then publishes the resulting
program with ``visualize_schedule`` and prints the Gantt summary and the
live-timeline URL. ``--run`` hands the program straight to ``rhylthyme
run`` for the terminal runner.
"""

from __future__ import annotations

import json
import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import click

from . import auth
from .mcp_client import McpClient, McpError, ToolError, endpoint_for, extract_program

ENVIRONMENTS = ["generic", "kitchen", "lab", "events", "gym"]


def _fail(msg: str) -> None:
    raise click.ClickException(msg)


@click.command("login")
@click.option(
    "--token",
    help="Store a pasted access token instead of signing in through the browser (expires in about an hour).",
)
@click.option(
    "--no-browser", is_flag=True, help="Print the sign-in URL instead of opening it."
)
def login(token: Optional[str], no_browser: bool):
    """Sign in to rhylthyme.com so `generate` can use the hosted MCP server."""
    try:
        if token:
            auth.login_with_token(token)
            click.echo(
                f"Saved token to {auth.credentials_path()} (no refresh; run `rhylthyme login` again when it expires)."
            )
            return
        creds = auth.login_via_browser(open_browser=not no_browser, echo=click.echo)
    except auth.AuthError as e:
        _fail(str(e))
    except OSError as e:
        _fail(f"Could not reach {auth.site_url()}: {e}")
    who = creds.get("email") or "your account"
    click.echo(f"Signed in as {who}. Session saved to {auth.credentials_path()}.")


@click.command("logout")
def logout():
    """Forget the stored rhylthyme.com session."""
    if auth.clear_credentials():
        click.echo("Signed out.")
    else:
        click.echo("Not signed in.")


@click.command("whoami")
def whoami():
    """Show the stored sign-in and when it expires."""
    creds = auth.load_credentials()
    if not creds:
        click.echo("Not signed in. Run `rhylthyme login`.")
        return
    exp = creds.get("expires_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "unknown"
    renew = "renews automatically" if creds.get("refresh_token") else "no refresh token"
    click.echo(
        f"{creds.get('email') or 'signed in'} · access token expires {when} · {renew}"
    )


def _read_request(words, file) -> str:
    parts = []
    if file:
        parts.append(sys.stdin.read() if file == "-" else Path(file).read_text())
    if words:
        parts.append(" ".join(words))
    if not parts and not sys.stdin.isatty():
        parts.append(sys.stdin.read())
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    if not text:
        _fail(
            'Describe what to schedule, e.g. rhylthyme generate "roast chicken and two sides for 6, dinner at 7pm"'
        )
    return text


@click.command("generate")
@click.argument("request", nargs=-1)
@click.option(
    "-f",
    "--file",
    "file",
    type=click.Path(allow_dash=True),
    help="Read the request or source text (recipe, protocol, run sheet) from a file, or - for stdin.",
)
@click.option(
    "-e",
    "--env",
    "environment",
    type=click.Choice(ENVIRONMENTS),
    default="generic",
    show_default=True,
    help="Where it happens. Picks the timeline site and the model's vocabulary.",
)
@click.option(
    "--by",
    "deadline",
    help='When everything must be done, e.g. "19:00" or "dinner at 7pm".',
)
@click.option(
    "--with",
    "hints",
    help='Equipment and people limits, e.g. "one oven, four burners, two cooks".',
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    help="Save the program JSON here.",
)
@click.option(
    "--no-publish",
    is_flag=True,
    help="Only build the program; don't create a live timeline.",
)
@click.option(
    "--open", "open_url", is_flag=True, help="Open the live timeline in a browser."
)
@click.option(
    "--run",
    "run_after",
    is_flag=True,
    help="Run the program in the terminal runner afterwards.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print {url, program, ...} as JSON instead of the summary.",
)
@click.option("-q", "--quiet", is_flag=True, help="Print only the live-timeline URL.")
@click.pass_context
def generate(
    ctx,
    request,
    file,
    environment,
    deadline,
    hints,
    output,
    no_publish,
    open_url,
    run_after,
    json_output,
    quiet,
):
    """Turn a natural-language request into a program and a live timeline.

    \b
    Examples:
      rhylthyme generate "roast chicken, potatoes and green beans for 6" -e kitchen --by 19:00 --with "one oven"
      rhylthyme generate -e lab -f western_blot.txt -o blot.json --run
      pbpaste | rhylthyme generate -e events --by "doors at 18:30"

    Uses the hosted MCP server (mcp.rhylthyme.com); run `rhylthyme login` once first.
    """
    text = _read_request(request, file)
    try:
        token = auth.access_token()
    except auth.AuthError as e:
        _fail(str(e))

    client = McpClient(url=endpoint_for(environment))
    say = (
        (lambda m: None)
        if (quiet or json_output)
        else (lambda m: click.echo(m, err=True))
    )

    args = {"text": text, "environmentType": environment, "token": token}
    if deadline:
        args["deadline"] = deadline
    if hints:
        args["hints"] = hints
    say(
        "Building the schedule (the server reads your request in four passes; ~20-60 s)…"
    )
    try:
        imported = client.call_tool("import_text", args, timeout=240)
    except ToolError as e:
        if "login" in e.text.lower() or "token" in e.text.lower():
            _fail(f"{e.text}\n\nYour sign-in may have expired: run `rhylthyme login`.")
        _fail(e.text)
    except McpError as e:
        _fail(str(e))
    program = extract_program(imported.text)
    if not program:
        _fail("The server did not return a program.\n\n" + imported.text[:1500])

    if output:
        Path(output).write_text(json.dumps(program, indent=2) + "\n")
        say(f"Saved program to {output}")

    published = None
    if not no_publish:
        say("Publishing the live timeline…")
        try:
            published = client.call_tool(
                "visualize_schedule", {"program": program}, timeout=60
            )
        except (ToolError, McpError) as e:
            _fail(f"Built the program but could not publish it: {e}")
    url = (published.structured or {}).get("url") if published else None

    if json_output:
        click.echo(
            json.dumps(
                {
                    "url": url,
                    "imageUrl": (
                        (published.structured or {}).get("imageUrl")
                        if published
                        else None
                    ),
                    "makespanSeconds": (
                        (published.structured or {}).get("makespanSeconds")
                        if published
                        else None
                    ),
                    "program": program,
                },
                indent=2,
            )
        )
    elif quiet:
        if url:
            click.echo(url)
    else:
        click.echo(
            (published or imported)
            .text.split("\n\nCall **visualize_schedule**")[0]
            .strip()
        )
        if url:
            click.echo(f"\nLive timeline: {url}")

    if open_url and url:
        webbrowser.open(url)

    if run_after:
        from rhylthyme.cli.main import run as run_command

        path = output
        if not path:
            safe = (
                "".join(
                    c if c.isalnum() else "_"
                    for c in (program.get("name") or "program")
                )
                .strip("_")
                .lower()
            )
            path = str(Path.cwd() / f"{safe or 'program'}.json")
            Path(path).write_text(json.dumps(program, indent=2) + "\n")
            say(f"Saved program to {path}")
        ctx.invoke(run_command, program_file=path)


_MARKS = {"pass": "✓", "warn": "!", "fail": "✗", "skip": "-"}
_COLORS = {"pass": "green", "warn": "yellow", "fail": "red", "skip": "bright_black"}


@click.command("mcp-test")
@click.option(
    "--url",
    help="MCP server to test (default: $RHYLTHYME_MCP_URL or https://mcp.rhylthyme.com/mcp).",
)
@click.option(
    "-e",
    "--env",
    "endpoints",
    multiple=True,
    type=click.Choice(ENVIRONMENTS),
    help="Endpoint(s) to test; repeatable. Default: all five.",
)
@click.option(
    "-k",
    "only",
    multiple=True,
    help="Run only these checks (e.g. -k tools -k catalog); repeatable.",
)
@click.option(
    "--publish",
    is_flag=True,
    help="Also publish a test timeline per endpoint and fetch its page and PNG.",
)
@click.option(
    "--generate",
    "generate_too",
    is_flag=True,
    help="Also run the model-backed import_text once (needs `rhylthyme login`; costs a few model calls).",
)
@click.option("--strict", is_flag=True, help="Exit non-zero on warnings too.")
@click.option("--json", "json_output", is_flag=True, help="Print the report as JSON.")
@click.option("--list", "list_checks", is_flag=True, help="List the checks and exit.")
def mcp_test(
    url, endpoints, only, publish, generate_too, strict, json_output, list_checks
):
    """Smoke-test a Rhylthyme MCP server: protocol, tools, resources, prompts.

    \b
    The default run is read-only and free. Examples:
      rhylthyme mcp-test                       # all five hosted endpoints
      rhylthyme mcp-test -e lab --publish      # one endpoint, plus a real share
      rhylthyme mcp-test --url http://localhost:3000/mcp -e generic
      rhylthyme mcp-test --json --strict       # for cron / CI

    Exits 1 if any check fails.
    """
    from . import checks

    if list_checks:
        for name, fn in checks.CHECKS:
            doc = (fn.__doc__ or "").strip().splitlines()
            click.echo(f"{name:14} {doc[0] if doc else ''}")
        return
    known = {name for name, _ in checks.CHECKS}
    unknown = [k for k in only if k not in known]
    if unknown:
        _fail(
            f"Unknown check(s): {', '.join(unknown)}. See `rhylthyme mcp-test --list`."
        )

    token = None
    if generate_too:
        try:
            token = auth.access_token()
        except auth.AuthError as e:
            _fail(str(e))

    from .mcp_client import base_url

    current = {"endpoint": None}

    def show(result):
        if json_output:
            return
        if result.endpoint != current["endpoint"]:
            current["endpoint"] = result.endpoint
            click.echo(f"\n{checks.endpoint_for(result.endpoint, opts.base_url)}")
        mark = click.style(_MARKS[result.status], fg=_COLORS[result.status], bold=True)
        click.echo(f"  {mark} {result.name:14} {result.ms:>6} ms  {result.detail}")

    opts = checks.SuiteOptions(
        base_url=(url or base_url()).rstrip("/"),
        endpoints=list(endpoints) or list(checks.ENDPOINTS),
        publish=publish,
        generate=generate_too,
        token=token,
        on_result=show,
    )
    report = checks.run_suite(opts, only=only or None)

    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        c = report.to_dict()["counts"]
        click.echo(
            f"\n{c['pass']} passed, {c['warn']} warning(s), {c['fail']} failed, {c['skip']} skipped"
        )
    if not report.ok or (strict and report.count("warn")):
        ctx = click.get_current_context()
        ctx.exit(1)


MAX_PROGRAM_BYTES = 2 * 1024 * 1024


def _fetch_bytes(url: str, limit: int = 8 * 1024 * 1024) -> bytes:
    import urllib.request

    if not url.startswith(("http://", "https://")):
        raise ValueError("not an http(s) URL")
    request = urllib.request.Request(url, headers={"User-Agent": "rhylthyme-cli"})
    with urllib.request.urlopen(
        request, timeout=60
    ) as response:  # nosec B310 - scheme checked above
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"larger than {limit // (1024 * 1024)} MB")
    return data


def _read_source(source: str) -> str:
    """The text of a program given as a file path, an http(s) URL, or `-`
    for standard input."""
    if source == "-":
        return sys.stdin.read()
    if source.startswith(("http://", "https://")):
        return _fetch_bytes(source, MAX_PROGRAM_BYTES).decode("utf-8")
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError("no such file")
    return path.read_text()


def _load_program(program_file: str) -> dict:
    program = None
    try:
        text = _read_source(program_file)
        if program_file.lower().split("?")[0].endswith((".yaml", ".yml")):
            import yaml

            program = yaml.safe_load(text)
        else:
            program = json.loads(text)
    except Exception as e:  # noqa: BLE001
        _fail(f"Could not read {program_file}: {e}")
    if isinstance(program, dict) and isinstance(program.get("tools"), list):
        # A workcell (lab tool addresses) must never leave this machine.
        _fail(
            f"{program_file} is a workcell file, not a program; workcells stay "
            "on this machine and are never sent to rhylthyme.com."
        )
    if not isinstance(program, dict) or not isinstance(program.get("tracks"), list):
        _fail(f"{program_file} is not a Rhylthyme program (no `tracks` list).")
    assert isinstance(program, dict)
    return program


def _clock_to_iso(value: str, now: Optional[datetime] = None) -> str:
    """``19:00`` or ``7:30pm`` -> the next such local time, as ISO 8601 with
    an offset. Anything else is passed through for the server to parse."""
    text = value.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", text)
    if not m or (not m.group(2) and not m.group(3)):
        return value
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return value
    now = now or datetime.now().astimezone()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when.isoformat()


def _local(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return (
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%a %H:%M")
        )
    except ValueError:
        return iso


@click.command("analyze")
@click.argument("program_file", metavar="PROGRAM")
@click.option(
    "--finish-at",
    "finish_at",
    default=None,
    help="When everything must be finished: 19:00, 7:30pm or an ISO 8601 datetime. Start times are worked backwards from it.",
)
@click.option(
    "--start-at",
    "start_at",
    default=None,
    help="When the program starts (same formats). Ignored with --finish-at.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Print the full analysis as JSON."
)
@click.option(
    "--strict", is_flag=True, help="Exit non-zero when there are resource conflicts."
)
@click.option(
    "--workcell",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Estimate instrument steps without a duration from this workcell's tools.",
)
def analyze(program_file, finish_at, start_at, json_output, strict, workcell):
    """Total length, critical path, resource conflicts and clock times. No sign-in needed.

    PROGRAM is a JSON or YAML file, an http(s) URL, or - for standard input.

    \b
    Computed by the hosted MCP server (analyze_schedule); nothing is published.
      rhylthyme analyze dinner.json --finish-at 19:00
      rhylthyme analyze two-protocols.json --strict
    """
    program = _load_program(program_file)
    # Instrument steps may leave their duration to the instrument; estimate
    # them locally (the workcell itself is never sent).
    from rhylthyme.instruments import InstrumentSetupError, fill_instrument_durations

    try:
        program, estimate_lines = fill_instrument_durations(program, workcell)
    except InstrumentSetupError as e:
        _fail(str(e))
    for line in estimate_lines:
        click.echo(line, err=True)
    args: dict = {"program": program}
    if finish_at:
        args["finishAt"] = _clock_to_iso(finish_at)
    elif start_at:
        args["startAt"] = _clock_to_iso(start_at)
    client = McpClient(url=endpoint_for(None))
    result = None
    try:
        result = client.call_tool("analyze_schedule", args, timeout=90)
    except ToolError as e:
        _fail(e.text)
    except McpError as e:
        _fail(str(e))
    assert result is not None
    info = result.structured or {}
    conflicts = info.get("resourceConflicts") or []
    if json_output:
        click.echo(json.dumps(info, indent=2))
    else:
        # The server's clock lines are UTC; say them in local time instead.
        text = result.text.split("**Wall-clock itinerary:**")[0]
        lines = [ln for ln in text.splitlines() if not ln.startswith("**Wall clock:**")]
        click.echo("\n".join(lines).strip())
        clock = info.get("wallClock") or {}
        steps = [st for st in info.get("steps") or [] if st.get("startAt")]
        if clock and steps:
            click.echo(
                f"\nStart {_local(clock.get('startAt'))}, finish {_local(clock.get('finishAt'))} (local time)"
            )
            for st in sorted(steps, key=lambda x: (x.get("startSeconds") or 0)):
                click.echo(
                    f"  {_local(st['startAt'])}  {st.get('name') or st.get('stepId')}"
                )
    if strict and conflicts:
        click.get_current_context().exit(1)


ENV_BY_TYPE = {
    "kitchen": "kitchen",
    "bakery": "kitchen",
    "restaurant": "kitchen",
    "commercial-kitchen": "kitchen",
    "laboratory": "lab",
    "lab": "lab",
    "hospital": "lab",
    "event": "events",
    "events": "events",
    "gym": "gym",
    "fitness": "gym",
}


@click.command("publish")
@click.argument("program_file", metavar="PROGRAM")
@click.option(
    "-e",
    "--env",
    "environment",
    type=click.Choice(ENVIRONMENTS),
    default=None,
    help="Timeline site to publish to (default: from the program's environmentType).",
)
@click.option(
    "--open", "open_url", is_flag=True, help="Open the live timeline in a browser."
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print {url, imageUrl, makespanSeconds, warnings} as JSON.",
)
@click.option("-q", "--quiet", is_flag=True, help="Print only the live-timeline URL.")
@click.option(
    "--image",
    "image_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Also save a PNG of the timeline here.",
)
def publish(program_file, environment, open_url, json_output, quiet, image_path):
    """Publish a program as a live, shareable timeline. No sign-in needed.

    PROGRAM is a JSON or YAML file, an http(s) URL, or - for standard input.

    \b
    The hosted MCP server validates the program first and refuses an invalid
    one, so run `rhylthyme validate` locally and fix what it reports.
      rhylthyme publish dinner.json
      rhylthyme publish blot.json -e lab -q
      rhylthyme publish https://example.org/party.json --image party.png --open
    """
    program = _load_program(program_file)
    env = environment or ENV_BY_TYPE.get(
        str(program.get("environmentType") or "").lower(), "generic"
    )
    client = McpClient(url=endpoint_for(env))
    try:
        published = client.call_tool(
            "visualize_schedule", {"program": program}, timeout=90
        )
    except ToolError as e:
        _fail(e.text)
    except McpError as e:
        _fail(str(e))
    info = published.structured or {}
    url = info.get("url")
    if json_output:
        click.echo(
            json.dumps(
                {
                    k: info.get(k)
                    for k in (
                        "url",
                        "shareId",
                        "imageUrl",
                        "makespanSeconds",
                        "warnings",
                    )
                },
                indent=2,
            )
        )
    elif quiet:
        click.echo(url or "")
    else:
        click.echo(published.text.strip())
        if url:
            click.echo(f"\nLive timeline: {url}")
    if image_path and info.get("imageUrl"):
        try:
            Path(image_path).write_bytes(_fetch_bytes(info["imageUrl"]))
            if not json_output and not quiet:
                click.echo(f"Picture: {image_path}")
        except Exception as e:  # noqa: BLE001
            click.echo(f"Could not save the picture: {e}", err=True)
    if open_url and url:
        webbrowser.open(url)


def register(cli_group: click.Group) -> None:
    for command in (login, logout, whoami, generate, publish, analyze, mcp_test):
        cli_group.add_command(command)
