"""Publish a live instrument run to rhylthyme.com and take commands from it.

``rhylthyme bridge --workcell lab.json PROGRAM`` runs a program exactly like
``rhylthyme run`` and, while it runs, keeps two rows up to date in the user's
own Supabase tables (owner-only RLS; see rhylthyme-server/sql/galago_bridges.sql
and docs/design/web-bridge.md):

- ``bridges``: this lab machine, its tools (name, type, status) and a
  heartbeat;
- ``bridge_state``: the run's steps, their status, the tool each waits on and
  any failure.

With a ``submit`` callback it also reads the user's ``bridge_commands`` rows
(pause, resume, retry, skip, abort), refuses stale, foreign or unknown ones,
lets the runner decide the rest, and writes each outcome back to its row.

Only outbound HTTPS, with the user's session. Nothing about where a tool
listens ever leaves the machine: rows are built from an allowlist of fields,
and error text is scrubbed of every private value each tool's driver names
(galago hosts and ports; LabMCP serial, TCP and VISA addresses, server URLs
and options).

Moved here from rhylthyme_galago.bridge so that every instrument driver can
use it; that module stays for compatibility.
"""

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from rhylthyme.drivers import blocking_action
from rhylthyme.instruments import InstrumentSetupError

TokenFn = Callable[[], str]

HEARTBEAT_SECONDS = 10.0
STATE_SECONDS = 1.0

#: Commands the browser may send (design note: "The command set"): steering a
#: run in progress, and starting one on an idle bridge.
STEER_KINDS = ("pause", "resume", "retry", "skip", "abort")
START_KINDS = ("start_run",)

#: A command older than this when the bridge sees it is refused, so a
#: reconnecting bridge never replays a stale retry or abort.
COMMAND_MAX_AGE_SECONDS = 30.0

#: Called with {"id", "kind", "args"}; returns {"accepted", "reason"} once the
#: runner has decided, or None if it did not answer in time.
Submit = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


class BridgeError(RuntimeError):
    """The bridge could not reach or write to rhylthyme.com."""


def user_id_from_token(token: str) -> str:
    """The ``sub`` of a Supabase JWT (the server re-checks it through RLS)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["sub"]
    except Exception as e:  # noqa: BLE001
        raise BridgeError(
            "The saved session is not a usable token; run `rhylthyme login`."
        ) from e


def bridge_id_for(workcell: Any, config_path: Path) -> str:
    """A stable id per workcell on this machine, kept in ``config_path``."""
    try:
        ids = json.loads(config_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        ids = {}
    if workcell.id not in ids:
        ids[workcell.id] = str(uuid.uuid4())
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(ids, indent=2))
    return ids[workcell.id]


def scrubber(workcell: Any) -> Callable[[Optional[str]], str]:
    """
    Replace every private value of every workcell tool in free text: each
    tool's ``secrets()`` (see drivers.ToolView), longest first so
    "10.0.0.12:50010" goes before "10.0.0.12".
    """
    pairs = [pair for tool in workcell.tools.values() for pair in tool.secrets()]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    pattern = re.compile("|".join(re.escape(p[0]) for p in pairs)) if pairs else None
    mapping = dict(pairs)

    def scrub(text: Optional[str]) -> str:
        if not text or pattern is None:
            return text or ""
        return pattern.sub(lambda m: mapping[m.group(0)], text)

    return scrub


def _status_value(status: Any) -> str:
    return getattr(status, "value", str(status))


def run_status(runner: Any) -> str:
    if getattr(runner, "program_abort_reason", None):
        return "aborted"
    if getattr(runner, "failed_steps", None):
        return "failed"
    steps = list(runner.steps.values())
    if steps and all(_status_value(s.status) == "COMPLETED" for s in steps):
        return "completed"
    if getattr(runner, "is_paused", False):
        return "paused"
    if getattr(runner, "is_running", False):
        return "running"
    return "idle"


def snapshot(runner: Any, scrub: Callable[[Optional[str]], str]) -> Dict[str, Any]:
    """The run as the web sees it: an allowlist of fields, error text scrubbed."""
    t0 = getattr(runner, "program_start_time", None)

    def rel(t: Optional[float]) -> Optional[float]:
        return round(t - t0, 2) if (t is not None and t0 is not None) else None

    steps = []
    for step in runner.steps.values():
        instrument = step.instrument or {}
        main = blocking_action(instrument) or {}
        status = _status_value(step.status)
        failure = None
        if step.failure:
            failure = {
                "code": str(step.failure.get("code") or ""),
                "errorMessage": scrub(step.failure.get("errorMessage")),
            }
        steps.append(
            {
                "stepId": step.step_id,
                "name": step.name,
                "trackId": step.track_id,
                "status": status,
                "start": rel(step.start_time),
                "end": rel(step.end_time),
                "tool": instrument.get("tool"),
                # The call the step waits on: its command or its until action
                "command": main.get("command"),
                "waitingOn": (
                    main.get("tool") if (main and status == "RUNNING") else None
                ),
                "failure": failure,
                "abortReason": scrub(step.abort_reason) if step.abort_reason else None,
            }
        )
    clock = None
    if t0 is not None:
        clock = round(runner.current_time - t0, 2)
    return {
        "clock": clock,
        "paused": bool(getattr(runner, "is_paused", False)),
        "abortReason": scrub(getattr(runner, "program_abort_reason", None)) or None,
        "steps": steps,
    }


class SupabaseRest:
    """Minimal PostgREST client acting as the signed-in user."""

    def __init__(self, url: str, anon_key: str, token: TokenFn, timeout: float = 10.0):
        self.url = url.rstrip("/") + "/rest/v1"
        self.anon_key = anon_key
        self.token = token
        self.timeout = timeout

    def select(self, table: str, query: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/{table}?{query}", None, {}) or []

    def upsert(self, table: str, row: Mapping[str, Any], on_conflict: str) -> None:
        self._request(
            "POST",
            f"/{table}?on_conflict={on_conflict}",
            row,
            {"Prefer": "resolution=merge-duplicates,return=minimal"},
        )

    def patch(self, table: str, match: str, row: Mapping[str, Any]) -> None:
        self._request("PATCH", f"/{table}?{match}", row, {"Prefer": "return=minimal"})

    def _request(self, method: str, path: str, body: Any, extra: Dict[str, str]) -> Any:
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json",
            **extra,
        }
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise BridgeError(
                f"{method} {path.split('?')[0]}: HTTP {e.code} {detail}"
            ) from e
        except urllib.error.URLError as e:
            raise BridgeError(f"Could not reach rhylthyme.com: {e.reason}") from e


class Publisher:
    """Keeps ``bridges`` and ``bridge_state`` current on a background thread.

    With a ``runner`` it publishes that run and passes steering commands to
    ``submit``. With ``runner=None`` the bridge is idle: it keeps its heartbeat,
    leaves the last run's state as it was, and passes ``start_run`` on.
    """

    def __init__(
        self,
        runner: Any,
        *,
        rest: SupabaseRest,
        bridge_id: str,
        user_id: str,
        workcell: Any,
        tools: List[Dict[str, str]],
        program: Optional[Mapping[str, Any]] = None,
        mode: str = "simulated",
        allows_live: bool,
        version: str,
        on_error: Optional[Callable[[str], None]] = None,
        submit: Optional[Submit] = None,
        clock: Callable[[], float] = time.time,
        program_id: Optional[str] = None,
    ):
        self.runner = runner
        self.rest = rest
        self.bridge_id = bridge_id
        self.user_id = user_id
        self.workcell = workcell
        self.tools = tools
        self.program = program or {}
        self.program_id = program_id
        self.mode = mode
        self.accepts = STEER_KINDS if runner is not None else START_KINDS
        self.allows_live = allows_live
        self.version = version
        self.scrub = scrubber(workcell)
        self.run_id = str(uuid.uuid4())
        self.on_error = on_error or (lambda message: None)
        self.submit = submit  # None: watch only, commands are not read
        self.clock = clock
        self._handled: set = set()
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_state: Optional[str] = None
        self._last_heartbeat = 0.0

    def bridge_row(self) -> Dict[str, Any]:
        return {
            "id": self.bridge_id,
            "user_id": self.user_id,
            "name": self.workcell.name,
            "allows_live": self.allows_live,
            "tools": self.tools,
            "version": self.version,
            "last_seen": _now_iso(),
        }

    def state_row(self) -> Optional[Dict[str, Any]]:
        if self.runner is None:
            return None  # idle: the last run's state stays as it was
        state = snapshot(self.runner, self.scrub)
        state["program"] = self.program
        return {
            "bridge_id": self.bridge_id,
            "user_id": self.user_id,
            "run_id": self.run_id,
            "program_id": self.program_id,
            "program_name": self.program.get("name"),
            "mode": self.mode,
            "status": run_status(self.runner),
            "state": state,
            "updated_at": _now_iso(),
        }

    def publish_once(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_heartbeat >= HEARTBEAT_SECONDS:
            self.rest.upsert("bridges", self.bridge_row(), "id")
            self._last_heartbeat = now
        row = self.state_row()
        if row is None:
            return
        key = json.dumps(
            {k: v for k, v in row.items() if k != "updated_at"}, sort_keys=True
        )
        if force or key != self._last_state:
            self.rest.upsert("bridge_state", row, "bridge_id")
            self._last_state = key

    def poll_commands(self) -> None:
        """Handle pending browser commands, oldest first, once each."""
        if self.submit is None:
            return
        pending = self.rest.select(
            "bridge_commands",
            f"bridge_id=eq.{self.bridge_id}&status=eq.pending&order=created_at.asc&select=*",
        )
        for command in pending:
            command_id = str(command.get("id"))
            if command_id in self._handled:
                continue
            self._handled.add(command_id)
            outcome = self._decide(command)
            self.rest.patch(
                "bridge_commands",
                f"id=eq.{command_id}&status=eq.pending",
                {
                    "status": "done" if outcome["accepted"] else "rejected",
                    "result": outcome,
                    "updated_at": _now_iso(),
                },
            )

    def _decide(self, command: Mapping[str, Any]) -> Dict[str, Any]:
        def no(reason: str) -> Dict[str, Any]:
            return {"accepted": False, "reason": reason}

        if command.get("user_id") != self.user_id:
            return no("not your bridge")
        age = self.clock() - _parse_iso(command.get("created_at"))
        if age > COMMAND_MAX_AGE_SECONDS:
            return no(f"expired ({int(age)} s old)")
        kind = command.get("kind")
        if kind not in STEER_KINDS + START_KINDS:
            return no(f"unknown command {kind!r}")
        if kind not in self.accepts:
            if kind in START_KINDS:
                return no("a run is already in progress")
            return no("no run in progress")
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        if self.submit is None:
            return no("this bridge does not take commands")
        outcome = self.submit(
            {"id": str(command.get("id")), "kind": kind, "args": args}
        )
        if outcome is None:
            return no("the runner did not answer")
        return {
            "accepted": bool(outcome.get("accepted")),
            "reason": outcome.get("reason") or "",
        }

    def start(self) -> "Publisher":
        self.publish_once(force=True)  # fail fast: bad session, missing tables
        self._thread = threading.Thread(
            target=self._loop, name="rhylthyme-bridge", daemon=True
        )
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(STATE_SECONDS):
            try:
                self.poll_commands()
                self.publish_once()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - keep running; report once
                message = str(e)
                if message != self.last_error:
                    self.on_error(message)
                self.last_error = message

    def stop(self) -> None:
        """Publish the final state, then stop."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self.publish_once(force=True)
        except Exception as e:  # noqa: BLE001
            self.on_error(str(e))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_iso(value: Any) -> float:
    """Seconds since the epoch for a Postgres/ISO timestamp (UTC if no zone)."""
    from datetime import datetime, timezone

    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0  # unparseable: treat as ancient, so it expires
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


__all__ = [
    "BridgeError",
    "COMMAND_MAX_AGE_SECONDS",
    "START_KINDS",
    "STEER_KINDS",
    "Publisher",
    "SupabaseRest",
    "bridge_id_for",
    "run_status",
    "scrubber",
    "snapshot",
    "user_id_from_token",
]


def start_bridge(
    runner,
    session,
    program: Mapping[str, Any],
    *,
    rest=None,
    token_fn: Optional[Callable[[], str]] = None,
    config_path=None,
    program_id: Optional[str] = None,
    allows_live: bool = False,
):
    """
    Start publishing this run to rhylthyme.com (``rhylthyme bridge``): the
    user's own ``bridges`` / ``bridge_state`` rows, owner-only. Needs
    ``rhylthyme login``. Returns the running Publisher; call ``stop()`` at the
    end. Raises InstrumentSetupError if the site cannot be reached or written.
    """
    from .remote import auth

    try:
        if rest is None or token_fn is None:
            creds = auth.load_credentials() or {}
            if not creds.get("supabase_url"):
                raise InstrumentSetupError(
                    "rhylthyme bridge needs you signed in: run `rhylthyme login`."
                )
            token_fn = token_fn or auth.access_token
            rest = rest or SupabaseRest(
                creds["supabase_url"], creds["supabase_anon_key"], token_fn
            )
        user_id = user_id_from_token(token_fn())
    except (auth.AuthError, BridgeError) as e:
        raise InstrumentSetupError(str(e)) from None

    workcell = session.bridge_workcell()
    bridge_id = bridge_id_for(
        workcell, config_path or (auth.config_dir() / "bridges.json")
    )
    tools = session.describe_tools()

    def on_error(message: str) -> None:
        runner.status_message = f"Bridge: {message}"

    def submit(command: Dict[str, Any], wait: float = 3.0) -> Optional[Dict[str, Any]]:
        """Hand a browser command to the runner thread; wait for its decision."""
        import json as _json
        import time as _time

        runner.command_queue.put("remote:" + _json.dumps(command))
        deadline = _time.time() + wait
        while _time.time() < deadline:
            outcome = runner.remote_outcomes.get(command["id"])
            if outcome is not None:
                return outcome
            _time.sleep(0.05)
        return None

    publisher = Publisher(
        runner,
        rest=rest,
        bridge_id=bridge_id,
        user_id=user_id,
        workcell=workcell,
        tools=tools,
        program=program,
        mode="live" if session.live else "simulated",
        allows_live=allows_live,
        version=session.version,
        on_error=on_error,
        submit=submit,
        program_id=program_id,
    )
    try:
        return publisher.start()
    except BridgeError as e:
        raise InstrumentSetupError(f"Could not publish to rhylthyme.com: {e}") from None
