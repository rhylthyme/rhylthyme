"""
``rhylthyme bridge --workcell lab.json`` with no program: wait for runs started
from rhylthyme.com (the web bridge, slice 3; design: rhylthyme-galago
docs/design/web-bridge.md).

The bridge stays listed as online on the Bridges page. A ``start_run`` command
names a program saved in the user's own library; this machine loads it,
checks it against the local workcell and refuses it unless it is safe to run
here, then runs it exactly as ``rhylthyme bridge PROGRAM`` would, and goes
back to waiting when the run completes or is aborted.

Live runs need two keys: the bridge must have been started with
``--allow-live`` (a decision made at the lab machine), and the browser must
send the ``live`` the user typed after seeing the pre-flight summary. Before
a run is accepted, in either mode, its tools are configured and must report
ready; if not, the start is refused with the pre-flight report (tool addresses
scrubbed).
"""

import json
import queue
import re
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from rhylthyme.instruments import InstrumentSetupError

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

Outcome = Dict[str, Any]


def _no(reason: str) -> Outcome:
    return {"accepted": False, "reason": reason}


def _has_code_blocks(program: Mapping[str, Any]) -> bool:
    for group in ("tracks", "trackTemplates"):
        for track in program.get(group) or []:
            for step in track.get("steps") or []:
                if isinstance(step, Mapping) and step.get("codeBlock"):
                    return True
    return False


def check_start(
    args: Mapping[str, Any],
    fetch_program: Callable[[str], Optional[Mapping[str, Any]]],
    workcell_source: Any,
    schema: Mapping[str, Any],
    allow_live: bool = False,
) -> Tuple[Outcome, Optional[Dict[str, Any]]]:
    """
    Decide whether a ``start_run`` may run here. Returns (outcome, program):
    the program when accepted, None otherwise.
    """
    from rhylthyme.validate_program import (
        perform_additional_validations,
        validate_program,
    )

    mode = args.get("mode") or "simulated"
    if mode not in ("simulated", "live"):
        return _no(f"unknown mode {mode!r}"), None
    if mode == "live":
        if not allow_live:
            return (
                _no(
                    "this bridge does not accept live runs (start it with --allow-live)"
                ),
                None,
            )
        if args.get("confirm") != "live":
            return _no("a live run needs the typed confirmation"), None
    program_id = str(args.get("program_id") or "")
    if not UUID_RE.match(program_id):
        return _no("program_id must be the id of a program in your library"), None
    row = fetch_program(program_id)
    if not row or not isinstance(row.get("program_json"), dict):
        return _no("no such program in your library"), None
    program = json.loads(json.dumps(row["program_json"]))
    if _has_code_blocks(program):
        return _no("programs with code blocks cannot be started from the web"), None
    is_valid, schema_errors = validate_program(program, dict(schema))
    errors = list(schema_errors) + perform_additional_validations(
        program, workcell=workcell_source
    )
    if not is_valid or errors:
        shown = "; ".join(str(e) for e in errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        return _no(f"the program is not valid on this workcell: {shown}{more}"), None
    return {"accepted": True, "reason": ""}, program


def serve(
    workcell_source: Any,
    *,
    schema_file: str,
    time_scale: float = 1.0,
    record: bool = True,
    rest=None,
    token_fn: Optional[Callable[[], str]] = None,
    config_path=None,
    run: Optional[Callable[..., Any]] = None,
    max_runs: Optional[int] = None,
    out=None,
    allow_live: bool = False,
    client_factory: Optional[Callable] = None,
    driver_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> int:
    """Wait for runs from the web until Ctrl-C (or ``max_runs``). Returns runs started."""
    out = out or sys.stdout
    from rhylthyme.validate_program import load_program_file

    from . import bridge as B
    from .remote import auth

    if run is None:
        from rhylthyme.cli.tui import run_program

        run = run_program
    run_fn: Callable[..., Any] = run

    from rhylthyme.drivers import DriverUnavailable, WorkcellError, load_workcell
    from rhylthyme.instruments import bridge_workcell, open_drivers

    try:
        workcell = load_workcell(workcell_source)
    except (WorkcellError, DriverUnavailable) as e:
        raise InstrumentSetupError(str(e)) from None
    # Opened only to describe the tools and what to scrub; touches nothing
    described = open_drivers(workcell, workcell.by_driver())
    for driver in described.values():
        driver.shutdown()
    view = bridge_workcell(workcell, described)
    version = ", ".join(v for v in (d.version for d in described.values()) if v)
    try:
        if rest is None or token_fn is None:
            creds = auth.load_credentials() or {}
            if not creds.get("supabase_url"):
                raise InstrumentSetupError(
                    "rhylthyme bridge needs you signed in: run `rhylthyme login`."
                )
            token_fn = token_fn or auth.access_token
            rest = rest or B.SupabaseRest(
                creds["supabase_url"], creds["supabase_anon_key"], token_fn
            )
        user_id = B.user_id_from_token(token_fn())
    except (auth.AuthError, B.BridgeError) as e:
        raise InstrumentSetupError(str(e)) from None

    bridge_id = B.bridge_id_for(
        view, config_path or (auth.config_dir() / "bridges.json")
    )
    schema = load_program_file(schema_file)
    tools: List[Dict[str, str]] = [
        {
            "name": e.name,
            "type": described[e.driver].tool_type(e.name),
            "status": "idle",
        }
        for e in sorted(workcell.tools.values(), key=lambda e: e.index)
    ]
    starts: "queue.Queue[Tuple[Dict[str, Any], str, Any, bool]]" = queue.Queue()
    scrub = B.scrubber(view)

    def fetch_program(program_id: str) -> Optional[Mapping[str, Any]]:
        rows = rest.select(
            "programs",
            f"id=eq.{program_id}&user_id=eq.{user_id}&select=id,name,program_json",
        )
        return rows[0] if rows else None

    def submit(command: Dict[str, Any]) -> Outcome:
        if not starts.empty():
            return _no("a run is already starting")
        args = command.get("args") or {}
        try:
            outcome, program = check_start(
                args, fetch_program, workcell_source, schema, allow_live=allow_live
            )
        except B.BridgeError as e:
            return _no(f"could not load the program: {e}")
        if not outcome["accepted"] or program is None:
            print(f"Refused a run from the web: {outcome['reason']}", file=out)
            return outcome
        live = args.get("mode") == "live"
        # Configure the tools now, so a tool that is not ready is a refusal
        # the browser sees rather than a run that never starts.
        from rhylthyme.instruments import open_instruments

        session = None
        try:
            kwargs: Dict[str, Any] = {"live": live, "driver_options": driver_options}
            if client_factory is not None:
                kwargs["client_factory"] = client_factory
            session = open_instruments(program, workcell_source, **kwargs)
            session.prepare()
        except InstrumentSetupError as e:
            if session is not None:
                session.shutdown()
            reason = "tools not ready: " + scrub(str(e)).replace("\n", "; ")
            print(f"Refused a run from the web: {reason}", file=out)
            return _no(reason)
        starts.put((program, args["program_id"], session, live))
        if live:
            print(
                "\n*** LIVE run started from the web: real hardware will move. "
                "Ctrl-C stops it. ***",
                file=out,
            )
        print(
            f"Starting {program.get('name')!r} from the web "
            f"({'LIVE' if live else 'simulated'}).",
            file=out,
        )
        return {"accepted": True, "reason": ""}

    runs = 0
    while max_runs is None or runs < max_runs:
        idle = B.Publisher(
            None,
            rest=rest,
            bridge_id=bridge_id,
            user_id=user_id,
            workcell=view,
            tools=tools,
            allows_live=allow_live,
            version=version,
            on_error=lambda message: print(f"Bridge: {message}", file=out),
            submit=submit,
        )
        try:
            idle.start()
        except B.BridgeError as e:
            raise InstrumentSetupError(
                f"Could not publish to rhylthyme.com: {e}"
            ) from None
        print(
            f"Bridge {workcell.name!r} is waiting for runs from rhylthyme.com "
            "(Bridges page). Ctrl-C stops it.",
            file=out,
        )
        if allow_live:
            print(
                "LIVE runs may be started from the web (--allow-live); each needs "
                "the user to type 'live' after the pre-flight summary.",
                file=out,
            )
        try:
            while True:
                try:
                    program, program_id, session, live = starts.get(timeout=0.5)
                    break
                except queue.Empty:
                    continue
        finally:
            idle.stop()
        try:
            run_fn(
                None,
                schema_file,
                time_scale,
                False,  # validated in check_start
                # auto_start=False: the clock runs as soon as the runner opens.
                # (auto_start=True honours a program's manual start trigger and
                # would wait for someone to press 's' at the lab machine.)
                False,
                None,
                record=record,
                factor_prompt=False,
                workcell=workcell_source,
                bridge=True,
                program_data=program,
                program_id=program_id,
                exit_when_done=True,
                live=live,
                prepared_instruments=session,
                bridge_allows_live=allow_live,
            )
        except SystemExit:
            print("The run could not start; waiting for the next one.", file=out)
        runs += 1
    return runs
