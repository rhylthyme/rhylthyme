"""
``rhylthyme calibrate`` — propose durations from recorded runs.

    rhylthyme calibrate PROGRAM [--runs-dir DIR] [--runs N] [--since DATE]
                        [--min-runs K] [--low-pct P] [--high-pct P]
                        [--indefinite-range]
                        [--format table|md|json] [--out FILE]
                        [--accept stepId,…|all] [--write PATH | --in-place]

``PROGRAM`` is a path to the program file; its ``programId`` selects the runs.
The command prints the per-step diff (current → proposed with the evidence) and
the makespan/critical-path effect if the whole proposal were accepted.

**Nothing is written without ``--accept``.** With ``--accept`` you must also say
where the calibrated program goes: ``--write PATH`` (which refuses to overwrite
``PROGRAM``) or ``--in-place``. The computation lives in :mod:`.calibrate`;
this module only renders it and does the file I/O, so the MCP tool and the web
editor can wrap the same proposal without going through the terminal.

Registered as a top-level command by
``rhylthyme.cli.main._register_calibrate_command``.
"""

import json
import os
import sys
from typing import Any, Dict, List, Sequence

import click

from rhylthyme.history.calibrate import (
    DEFAULT_HIGH_PCT,
    DEFAULT_LOW_PCT,
    DEFAULT_MIN_RUNS,
    NOTE,
    PROPOSED,
    CalibrationProposal,
    StepProposal,
    apply_calibration,
    propose_calibration,
)
from rhylthyme.history.store import list_runs, resolve_runs_dir

from .report_cli import _md_table, _plain_table, fmt_seconds, fmt_signed

REASON_HELP = {
    "insufficient-runs": "fewer than k measured runs",
    "fixed-duration": "fixed duration: history only confirms the timer",
    "no-measurements": "no measurements in the recorded runs",
    "not-in-program": "measured, but no such step in the program any more",
    "not-a-duration-object": "duration is a bare number or time string",
}


# ---------------------------------------------------------------- formatting


def describe_values(values: Dict[str, Any]) -> str:
    """
    A duration as one cell: ``min–default–max`` for a range, else the number.

    ``-`` when there is nothing to show.
    """
    if not values:
        return "-"
    kind = values.get("type")
    if kind == "fixed" or "seconds" in values:
        return fmt_seconds(values.get("seconds"))
    low, default, high = (
        values.get("minSeconds"),
        values.get("defaultSeconds"),
        values.get("maxSeconds"),
    )
    if low is None and high is None:
        return fmt_seconds(default)
    return "–".join(fmt_seconds(v) for v in (low, default, high))


def step_note(row: StepProposal) -> str:
    if row.status == NOTE:
        return f"{row.note} (lag {fmt_signed(row.evidence.get('lagSeconds'))})"
    if row.status == PROPOSED:
        return row.verdict or ""
    return REASON_HELP.get(row.reason or "", row.reason or "")


STEP_HEADERS = [
    "Step",
    "Type",
    "n",
    "Current",
    "Proposed",
    "Median",
    "IQR",
    "Delta",
    "Note",
]


def step_rows(
    proposal: CalibrationProposal, changed_only: bool = False
) -> List[List[str]]:
    rows = []
    for row in proposal.steps:
        if changed_only and row.status == "skipped":
            continue
        rows.append(
            [
                row.stepId,
                row.durationType or "-",
                str(row.n),
                describe_values(row.current),
                describe_values(row.proposed) if row.status == PROPOSED else "-",
                fmt_seconds(row.evidence.get("median")),
                fmt_seconds(row.evidence.get("iqr")),
                fmt_signed(row.delta_seconds) if row.status == PROPOSED else "-",
                step_note(row),
            ]
        )
    return rows


def effect_line(proposal: CalibrationProposal) -> str:
    """One line: what accepting the whole proposal does to the plan."""
    effect = proposal.effect or {}
    if not effect:
        return "Effect if accepted: (not computed)"
    before = effect.get("makespanBeforeSeconds")
    after = effect.get("makespanAfterSeconds")
    delta = effect.get("makespanDeltaSeconds")
    if not effect.get("acceptedSteps"):
        return f"Effect if accepted: nothing to accept; makespan stays {fmt_seconds(before)}"
    path = (
        "critical path changes to " + " > ".join(effect.get("criticalPathAfter") or [])
        if effect.get("criticalPathChanged")
        else "critical path unchanged"
    )
    return (
        f"Effect if accepted: makespan {fmt_seconds(before)} -> {fmt_seconds(after)}"
        f" ({fmt_signed(delta)}); {path}"
    )


def render_proposal(proposal: CalibrationProposal, fmt: str = "table") -> str:
    """Render ``proposal`` as ``table``, ``md`` or ``json``."""
    if fmt == "json":
        return json.dumps(proposal.to_dict(), indent=2) + "\n"

    table = _md_table if fmt == "md" else _plain_table
    h1 = "# " if fmt == "md" else ""
    h2 = "## " if fmt == "md" else ""
    out: List[str] = []
    out.append(f"{h1}Calibration proposal: {proposal.programId or '(unknown program)'}")
    out.append("")
    excluded = len(proposal.runsExcluded)
    out.append(
        f"Runs: {proposal.runsUsable} usable of {proposal.runsConsidered}"
        + (f" ({excluded} excluded)" if excluded else "")
        + f"; k={proposal.minRuns}, range from P{proposal.lowPercentile:g}/"
        f"P{proposal.highPercentile:g} widened to the author's"
    )
    proposed = proposal.proposed_steps()
    notes = proposal.notes()
    out.append(
        f"Proposed: {len(proposed)} step(s)"
        + (f"; notes on {len(notes)} fixed step(s)" if notes else "")
        + f"; as of {proposal.asOf}"
    )
    out.append("")
    out.append(f"{h2}Per step")
    out.append("")
    out.append(table(STEP_HEADERS, step_rows(proposal)))
    out.append("")
    out.append(effect_line(proposal))
    out.append("")
    if proposed:
        out.append(
            "Nothing has been written. Accept with"
            " --accept " + ",".join(r.stepId for r in proposed) + " --write FILE"
            " (or --accept all --in-place)."
        )
    else:
        out.append("Nothing to accept.")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------- helpers


def parse_accept(values: Sequence[str]) -> Any:
    """``("all",)`` -> ``"all"``; ``("a,b", "c")`` -> ``["a", "b", "c"]``."""
    ids: List[str] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if part == "all":
                return "all"
            if part not in ids:
                ids.append(part)
    return ids


def _load(path: str) -> Dict[str, Any]:
    from rhylthyme.validate_program import load_program_file

    program = load_program_file(path)
    if not isinstance(program, dict) or not isinstance(program.get("tracks"), list):
        raise click.UsageError(f"{path} does not look like a program (no tracks)")
    return program


# ------------------------------------------------------------------ command


@click.command("calibrate")
@click.argument("program", type=click.Path(exists=True))
@click.option(
    "--runs-dir",
    type=click.Path(),
    default=None,
    help="Directory holding run records (default: $RHYLTHYME_RUNS_DIR or ~/.rhylthyme/runs)",
)
@click.option(
    "--runs",
    "runs_limit",
    type=int,
    default=None,
    help="Use only the newest N runs",
)
@click.option(
    "--since", default=None, help="Only runs started on or after this date (YYYY-MM-DD)"
)
@click.option(
    "--min-runs",
    "min_runs",
    type=int,
    default=DEFAULT_MIN_RUNS,
    show_default=True,
    help="k: measurements a step needs before it gets a proposal",
)
@click.option(
    "--low-pct",
    type=float,
    default=DEFAULT_LOW_PCT,
    show_default=True,
    help="Percentile proposed as minSeconds (before widening)",
)
@click.option(
    "--high-pct",
    type=float,
    default=DEFAULT_HIGH_PCT,
    show_default=True,
    help="Percentile proposed as maxSeconds (before widening)",
)
@click.option(
    "--indefinite-range",
    is_flag=True,
    help="Also propose minSeconds/maxSeconds for indefinite steps",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "md", "json"]),
    default="table",
    show_default=True,
    help="Output format",
)
@click.option(
    "--out",
    type=click.Path(),
    default=None,
    help="Write the proposal to FILE instead of stdout",
)
@click.option(
    "--accept",
    "accept_values",
    multiple=True,
    help="Accept these step ids (comma-separated, repeatable) or 'all'",
)
@click.option(
    "--write",
    "write_path",
    type=click.Path(),
    default=None,
    help="Write the calibrated program to PATH (requires --accept)",
)
@click.option(
    "--in-place",
    is_flag=True,
    help="Write the calibrated program back over PROGRAM (requires --accept)",
)
def calibrate(
    program,
    runs_dir,
    runs_limit,
    since,
    min_runs,
    low_pct,
    high_pct,
    indefinite_range,
    fmt,
    out,
    accept_values,
    write_path,
    in_place,
):
    """
    Propose durations for PROGRAM from its recorded runs, with the evidence.

    For every non-fixed step with at least k measurements: the observed median
    as defaultSeconds, and P10/P90 as the range, widened so the author's range
    is never narrowed. Fixed steps are never changed; one that consistently
    overruns is flagged with its lag as a candidate for `variable`.

    This reads and prints; it writes only what --accept names, and only to
    --write or --in-place.
    """
    accept = parse_accept(accept_values)
    if not accept and (write_path or in_place):
        raise click.UsageError(
            "--write/--in-place needs --accept to say what to write."
        )
    if accept and not (write_path or in_place):
        raise click.UsageError(
            "--accept needs --write PATH or --in-place to say where the "
            "calibrated program goes."
        )
    if write_path and in_place:
        raise click.UsageError("Give either --write PATH or --in-place, not both.")

    loaded = _load(program)
    program_id = loaded.get("programId")
    records = list_runs(runs_dir, str(program_id) if program_id else None)
    if not records:
        where = resolve_runs_dir(runs_dir)
        click.echo(f"No runs recorded for '{program_id}' in {where}", err=True)
        sys.exit(1)
    if runs_limit is not None and runs_limit > 0:
        records = records[: int(runs_limit)]

    proposal = propose_calibration(
        loaded,
        records,
        k=min_runs,
        since=since,
        low_pct=low_pct,
        high_pct=high_pct,
        range_for_indefinite=indefinite_range,
    )

    text = render_proposal(proposal, fmt)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        click.echo(f"Proposal written to {out}")
    else:
        click.echo(text, nl=False)

    if not accept:
        return

    target = os.path.abspath(program) if in_place else os.path.abspath(write_path)
    if not in_place and target == os.path.abspath(program):
        raise click.UsageError(
            "--write would overwrite PROGRAM; use --in-place if that is what you mean."
        )
    try:
        calibrated = apply_calibration(loaded, proposal, accept)
    except ValueError as exc:
        raise click.UsageError(str(exc))

    accepted = (
        [r.stepId for r in proposal.proposed_steps()]
        if accept == "all"
        else list(accept)
    )
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(calibrated, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    click.echo(
        f"Calibrated {len(accepted)} step(s) ({', '.join(accepted) or 'none'}) -> {target}"
    )


def register(group) -> None:
    """Attach ``calibrate`` as a top-level command."""
    group.add_command(calibrate)


__all__ = [
    "calibrate",
    "register",
    "render_proposal",
    "effect_line",
    "describe_values",
    "parse_accept",
    "STEP_HEADERS",
]
