"""
``rhylthyme runs`` — inspect recorded executions.

    rhylthyme runs list [PROGRAM] [--runs-dir DIR]   newest first
    rhylthyme runs PROGRAM                            shorthand for list
    rhylthyme runs show RUN [--runs-dir DIR]          per-step planned vs actual
    rhylthyme runs show RUN --svg OUT [--program F]   the same, as a Gantt overlay

``PROGRAM`` is a programId or a path to a program file; ``RUN`` is a runId
(or unique prefix) or a path to a record. Rendering is separated from data
(``run_table_rows``, ``run_summary``) so later phases can add ``report``
without touching the table code; ``--svg`` shells the JavaScript renderer
(see ``history.render``) with ``renderTimelineSvg(program, {run: record})``.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

import click

from rhylthyme.history.store import find_run, list_runs, load_run, resolve_runs_dir

# ---------------------------------------------------------------- formatting


def format_hms(seconds: Optional[float]) -> str:
    """``h:mm:ss`` for a non-negative number of seconds; ``-`` for None."""
    if seconds is None:
        return "-"
    total = int(round(seconds))
    hours, rem = divmod(abs(total), 3600)
    minutes, secs = divmod(rem, 60)
    sign = "-" if total < 0 else ""
    return f"{sign}{hours}:{minutes:02d}:{secs:02d}"


def format_signed(seconds: Optional[float]) -> str:
    """Signed ``+h:mm:ss`` / ``-h:mm:ss`` deviation; ``-`` for None."""
    if seconds is None:
        return "-"
    total = int(round(seconds))
    if total == 0:
        return "0:00:00"
    return ("+" if total > 0 else "-") + format_hms(abs(total))


# ---------------------------------------------------------------------- data


def planned_makespan(record: Dict[str, Any]) -> Optional[float]:
    ends = [s["planned"]["end"] for s in record.get("steps", []) if s.get("planned")]
    return max(ends) if ends else None


def actual_makespan(record: Dict[str, Any]) -> Optional[float]:
    ends = [
        s["actual"]["end"]
        for s in record.get("steps", [])
        if s.get("actual") and s["actual"].get("end") is not None
    ]
    return max(ends) if ends else None


def run_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """One-line summary fields for a record (used by ``runs list``)."""
    planned = planned_makespan(record)
    actual = actual_makespan(record)
    return {
        "runId": record.get("runId"),
        "programId": record.get("programId"),
        "startedAt": record.get("startedAt"),
        "outcome": record.get("outcome"),
        "makespanActual": actual,
        "makespanPlanned": planned,
        "deviation": (
            actual - planned if actual is not None and planned is not None else None
        ),
        "speed": record.get("runtime", {}).get("speed"),
        "path": record.get("_path"),
    }


def run_table_rows(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-step planned-vs-actual rows for a record (used by ``runs show``)."""
    rows = []
    for step in record.get("steps", []):
        planned = step.get("planned", {})
        actual = step.get("actual") or {}
        p_start, p_end = planned.get("start"), planned.get("end")
        a_start, a_end = actual.get("start"), actual.get("end")
        rows.append(
            {
                "stepId": step.get("stepId"),
                "instance": step.get("instance", 1),
                "durationType": planned.get("durationType"),
                "plannedStart": p_start,
                "plannedEnd": p_end,
                "actualStart": a_start,
                "actualEnd": a_end,
                "startDeviation": (
                    a_start - p_start
                    if a_start is not None and p_start is not None
                    else None
                ),
                "endDeviation": (
                    a_end - p_end if a_end is not None and p_end is not None else None
                ),
                "endedBy": step.get("endedBy"),
                "pausedSeconds": step.get("pausedSeconds", 0),
                "triggerFiredAt": step.get("triggerFiredAt"),
                "waitedOn": step.get("waitedOn", []),
            }
        )
    return rows


def _program_id_from_arg(program: str) -> str:
    """Accept a programId or a path to a program file."""
    if os.path.isfile(program):
        from rhylthyme.validate_program import load_program_file

        data = load_program_file(program)
        return str(data.get("programId", program))
    return program


def render_run_overlay(
    record: Dict[str, Any],
    out_path: str,
    program_path: Optional[str] = None,
    runs_dir: Optional[str] = None,
    width: Optional[int] = None,
):
    """
    Draw ``record`` as a planned-vs-actual SVG at ``out_path``.

    The program is taken from ``program_path`` when given, otherwise located
    by ``programId`` (preferring an exact ``programVersion`` match). Raises
    ``FileNotFoundError`` when no program can be found, and whatever
    ``history.render.render_run_svg`` raises when Node or the renderer is
    missing.
    """
    from rhylthyme.history.hash import load_program_for_hash
    from rhylthyme.history.store import find_program_for_record

    from .render import render_run_svg

    if program_path:
        program_file = program_path
    else:
        located = find_program_for_record(record, runs_dir)
        if located is None:
            raise FileNotFoundError(
                f"Could not find the program '{record.get('programId')}' that this run "
                "executed; pass --program with the program file"
            )
        program_file = str(located)
    program = load_program_for_hash(program_file)
    return render_run_svg(program, record, out_path, width=width)


def _render_svg(record, svg_out, program_path, runs_dir, width):
    """``render_run_overlay`` with CLI-shaped error reporting."""
    try:
        return render_run_overlay(record, svg_out, program_path, runs_dir, width)
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


def _table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


# ------------------------------------------------------------------ commands


class RunsGroup(click.Group):
    """``rhylthyme runs <program>`` falls through to ``runs list <program>``."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if args and not args[0].startswith("-"):
                return "list", self.commands["list"], args
            raise


@click.group("runs", cls=RunsGroup, invoke_without_command=True)
@click.pass_context
def runs(ctx):
    """
    Inspect recorded runs (execution history).

    Every `rhylthyme run` writes a record of planned vs actual timings to
    ~/.rhylthyme/runs/<programId>/<runId>.json (override with --runs-dir or
    RHYLTHYME_RUNS_DIR). Use `runs list` to see them and `runs show` for the
    per-step table.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@runs.command("list")
@click.argument("program", required=False)
@click.option(
    "--runs-dir",
    type=click.Path(),
    default=None,
    help="Directory holding run records (default: $RHYLTHYME_RUNS_DIR or ~/.rhylthyme/runs)",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Print JSON instead of a table"
)
def runs_list(program, runs_dir, json_output):
    """List recorded runs, newest first, for PROGRAM (a programId or program file)."""
    program_id = _program_id_from_arg(program) if program else None
    records = list_runs(runs_dir, program_id)
    summaries = [run_summary(r) for r in records]

    if json_output:
        click.echo(json.dumps(summaries, indent=2))
        return
    if not summaries:
        where = resolve_runs_dir(runs_dir)
        target = f" for '{program_id}'" if program_id else ""
        click.echo(f"No runs recorded{target} in {where}")
        return

    headers = ["Run", "Started", "Outcome", "Actual", "Planned", "Deviation"]
    if program_id is None:
        headers.insert(1, "Program")
    rows = []
    for s in summaries:
        row = [
            str(s["runId"]),
            str(s["startedAt"]),
            str(s["outcome"]),
            format_hms(s["makespanActual"]),
            format_hms(s["makespanPlanned"]),
            format_signed(s["deviation"]),
        ]
        if program_id is None:
            row.insert(1, str(s["programId"]))
        rows.append(row)
    click.echo(_table(headers, rows))


@runs.command("show")
@click.argument("run")
@click.option(
    "--runs-dir",
    type=click.Path(),
    default=None,
    help="Directory holding run records (default: $RHYLTHYME_RUNS_DIR or ~/.rhylthyme/runs)",
)
@click.option("--json", "json_output", is_flag=True, help="Print the record as JSON")
@click.option(
    "--svg",
    "svg_out",
    type=click.Path(),
    default=None,
    help="Also write a planned-vs-actual overlay SVG here (needs Node and the timeline renderer)",
)
@click.option(
    "--program",
    "program_path",
    type=click.Path(exists=True),
    default=None,
    help="Program JSON the run executed, for --svg (default: located by programId and programVersion)",
)
@click.option(
    "--width",
    type=int,
    default=None,
    help="Width of the --svg drawing in px (default 820)",
)
def runs_show(run, runs_dir, json_output, svg_out, program_path, width):
    """
    Show planned vs actual timing per step for RUN (a runId or record path).

    With --svg the same comparison is drawn: each step's actual bar with the
    plan underneath it as a thin ghost bar, outlined green when the step
    finished on time, blue when early and red when late.
    """
    path = find_run(run, runs_dir)
    if path is None:
        click.echo(
            f"Run '{run}' not found under {resolve_runs_dir(runs_dir)}", err=True
        )
        sys.exit(1)
    record = load_run(str(path))
    record.pop("_path", None)

    # Rendering first, so a missing program or renderer fails before output.
    written_svg = None
    if svg_out:
        written_svg = _render_svg(record, svg_out, program_path, runs_dir, width)

    if json_output:
        click.echo(json.dumps(record, indent=2))
        if written_svg:
            click.echo(f"Wrote {written_svg}", err=True)
        return

    runtime = record.get("runtime", {})
    summary = run_summary(record)
    click.echo(f"Run:      {record.get('runId')}")
    click.echo(f"Program:  {record.get('programId')}  ({record.get('programVersion')})")
    click.echo(
        f"Started:  {record.get('startedAt')}  ->  {record.get('endedAt')}  "
        f"[{record.get('outcome')}]"
    )
    click.echo(
        f"Runtime:  {runtime.get('kind')} {runtime.get('version')}  "
        f"clock={runtime.get('clockMode')}  speed={runtime.get('speed')}  "
        f"environment={record.get('environmentId') or '-'}"
    )
    click.echo(
        f"Makespan: actual {format_hms(summary['makespanActual'])}  "
        f"planned {format_hms(summary['makespanPlanned'])}  "
        f"deviation {format_signed(summary['deviation'])}"
    )
    click.echo("")

    headers = [
        "Step",
        "Type",
        "Planned start",
        "Planned end",
        "Actual start",
        "Actual end",
        "Deviation",
        "Ended by",
        "Paused",
    ]
    rows = []
    for r in run_table_rows(record):
        label = r["stepId"]
        if r["instance"] and r["instance"] != 1:
            label = f"{label} #{r['instance']}"
        rows.append(
            [
                label,
                str(r["durationType"] or "-"),
                format_hms(r["plannedStart"]),
                format_hms(r["plannedEnd"]),
                format_hms(r["actualStart"]),
                format_hms(r["actualEnd"]),
                format_signed(r["endDeviation"]),
                str(r["endedBy"] or "-"),
                format_hms(r["pausedSeconds"]) if r["pausedSeconds"] else "0:00:00",
            ]
        )
    click.echo(_table(headers, rows))
    if written_svg:
        click.echo("")
        click.echo(f"Wrote {written_svg}  (planned vs actual overlay)")


__all__ = [
    "runs",
    "render_run_overlay",
    "run_summary",
    "run_table_rows",
    "planned_makespan",
    "actual_makespan",
    "format_hms",
    "format_signed",
]
