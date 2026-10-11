"""
``rhylthyme runs report`` — is this program's history inferential?

    rhylthyme runs report PROGRAM [--runs-dir DIR] [--since DATE]
                          [--min-runs K] [--cv-threshold X]
                          [--format table|md|json] [--out FILE]
    rhylthyme runs report --all ...

``PROGRAM`` is a programId or a path to a program file; a file also supplies the
``task`` of each step, which is what the roll-up by step type groups on. The
computation lives in :mod:`.report`; this module only renders it.

Registered onto the existing ``runs`` group by
``rhylthyme.cli.main._register_runs_commands``.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

import click

from rhylthyme.history.report import (
    DEFAULT_CV_THRESHOLD,
    DEFAULT_MIN_RUNS,
    EXECUTOR_CONTROLLED,
    LAG,
    PREDICTABLE,
    Report,
    build_report,
)
from rhylthyme.history.store import list_runs, resolve_runs_dir

VERDICT_HELP = {
    PREDICTABLE: "n >= k and CV <= threshold: history beats the author's guess",
    EXECUTOR_CONTROLLED: (
        "n >= k and CV > threshold: the person decides how long this takes"
    ),
    "insufficient": "fewer than k measured runs: no verdict yet",
    LAG: (
        "fixed duration: no verdict, only how much longer than planned the step "
        "took (measured when its successor was released)"
    ),
}


# --------------------------------------------------------------- formatting


def fmt_seconds(value: Optional[float]) -> str:
    """``h:mm:ss`` for a duration; ``-`` for None."""
    if value is None:
        return "-"
    total = int(round(abs(value)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    sign = "-" if value < 0 else ""
    return f"{sign}{hours}:{minutes:02d}:{secs:02d}"


def fmt_signed(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if int(round(value)) == 0:
        return "0:00:00"
    return ("+" if value > 0 else "-") + fmt_seconds(abs(value))


def fmt_ratio(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _pad(headers: List[str], rows: List[List[str]]) -> List[int]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    return widths


def _plain_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = _pad(headers, rows)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(line.rstrip() for line in lines)


def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


STEP_HEADERS = [
    "Step",
    "Type",
    "Task",
    "n",
    "Planned",
    "Median",
    "P10",
    "P90",
    "IQR",
    "CV",
    "Mean dev",
    "Verdict",
]


def step_rows(report: Report) -> List[List[str]]:
    rows = []
    for step in report.steps:
        if step.verdict == LAG:
            verdict = f"lag {fmt_signed(step.lagSeconds)} (n={step.lagN})"
            rows.append(
                [
                    step.stepId,
                    step.durationType or "-",
                    step.task or "-",
                    str(step.n),
                    fmt_seconds(step.plannedSeconds),
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    verdict,
                ]
            )
            continue
        rows.append(
            [
                step.stepId,
                step.durationType or "-",
                step.task or "-",
                str(step.n),
                fmt_seconds(step.plannedSeconds),
                fmt_seconds(step.median),
                fmt_seconds(step.p10),
                fmt_seconds(step.p90),
                fmt_seconds(step.iqr),
                fmt_ratio(step.cv),
                fmt_signed(step.meanDeviation),
                step.verdict,
            ]
        )
    return rows


ROLLUP_HEADERS = [
    "Group",
    "Steps",
    "n",
    "Median CV",
    "Predictable",
    "Executor",
    "Insufficient",
    "Lag",
]


def rollup_rows(groups) -> List[List[str]]:
    return [
        [
            g.key,
            str(g.steps),
            str(g.n),
            fmt_ratio(g.medianCv),
            str(g.predictable),
            str(g.executorControlled),
            str(g.insufficient),
            str(g.lag),
        ]
        for g in groups
    ]


def render_report(report: Report, fmt: str = "table") -> str:
    """Render ``report`` as ``table``, ``md`` or ``json``."""
    if fmt == "json":
        return json.dumps(report.to_dict(), indent=2)

    table = _md_table if fmt == "md" else _plain_table
    h1 = "# " if fmt == "md" else ""
    h2 = "## " if fmt == "md" else ""
    out: List[str] = []
    out.append(f"{h1}Inferentiality report: {report.programId or '(all programs)'}")
    out.append("")
    excluded = len(report.runsExcluded)
    out.append(
        f"Runs: {report.runsUsable} usable of {report.runsConsidered}"
        + (f" ({excluded} excluded)" if excluded else "")
        + f"; k={report.minRuns}, CV threshold {report.cvThreshold:.2f}"
    )
    if report.predictableFraction is None:
        out.append("Predictable steps: none measured")
    else:
        out.append(
            f"Predictable steps: {report.stepsPredictable}/{report.stepsWithVerdict}"
            f" ({report.predictableFraction * 100:.0f}% of steps with a verdict)"
        )
    out.append("")
    out.append(f"{h2}Per step")
    out.append("")
    out.append(table(STEP_HEADERS, step_rows(report)))
    out.append("")
    out.append(f"{h2}By step type (task)")
    out.append("")
    out.append(table(ROLLUP_HEADERS, rollup_rows(report.byTask)))
    out.append("")
    out.append(f"{h2}By duration kind")
    out.append("")
    out.append(table(ROLLUP_HEADERS, rollup_rows(report.byDurationKind)))
    if report.runsExcluded:
        out.append("")
        out.append(f"{h2}Excluded runs")
        out.append("")
        out.append(
            table(
                ["Run", "Reason"],
                [
                    [str(r.get("runId")), str(r.get("reason"))]
                    for r in report.runsExcluded
                ],
            )
        )
    out.append("")
    for verdict, text in VERDICT_HELP.items():
        out.append(f"{verdict}: {text}")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ command


def _load_program(program: Optional[str]) -> Optional[Dict[str, Any]]:
    if not program or not os.path.isfile(program):
        return None
    try:
        from rhylthyme.validate_program import load_program_file

        return load_program_file(program)
    except Exception:
        return None


def _program_id(
    program: Optional[str], loaded: Optional[Dict[str, Any]]
) -> Optional[str]:
    if loaded is not None:
        return str(loaded.get("programId") or program)
    return program


def _group_by_program(records) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record.get("programId")), []).append(record)
    return grouped


@click.command("report")
@click.argument("program", required=False)
@click.option(
    "--all", "all_programs", is_flag=True, help="Report on every program with runs"
)
@click.option(
    "--runs-dir",
    type=click.Path(),
    default=None,
    help="Directory holding run records (default: $RHYLTHYME_RUNS_DIR or ~/.rhylthyme/runs)",
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
    help="k: measured runs a step needs before it gets a verdict",
)
@click.option(
    "--cv-threshold",
    type=float,
    default=DEFAULT_CV_THRESHOLD,
    show_default=True,
    help="Coefficient of variation at or below which a step counts as predictable",
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
    "--out", type=click.Path(), default=None, help="Write to FILE instead of stdout"
)
def runs_report(
    program, all_programs, runs_dir, since, min_runs, cv_threshold, fmt, out
):
    """
    Per-step duration statistics and a predictability verdict over usable runs.

    Usable runs completed on a wall clock at speed 1; usable steps were ended by
    the executor and never paused. Fixed steps are never "measured" — they get a
    lag row (how much longer than planned the step took) instead of a verdict.
    """
    if not program and not all_programs:
        raise click.UsageError("Give a programId or program file, or --all.")

    loaded = _load_program(program)
    program_id = None if all_programs else _program_id(program, loaded)
    records = list_runs(runs_dir, program_id)
    if not records:
        where = resolve_runs_dir(runs_dir)
        target = f" for '{program_id}'" if program_id else ""
        click.echo(f"No runs recorded{target} in {where}", err=True)
        sys.exit(1)

    if all_programs:
        reports = [
            build_report(
                group, None, k=min_runs, cv_threshold=cv_threshold, since=since
            )
            for _pid, group in sorted(_group_by_program(records).items())
        ]
    else:
        reports = [
            build_report(
                records, loaded, k=min_runs, cv_threshold=cv_threshold, since=since
            )
        ]

    if fmt == "json":
        payload = (
            [r.to_dict() for r in reports] if all_programs else reports[0].to_dict()
        )
        text = json.dumps(payload, indent=2) + "\n"
    else:
        text = "\n".join(render_report(r, fmt) for r in reports)

    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        click.echo(f"Report written to {out}")
    else:
        click.echo(text, nl=False)


def register(group) -> None:
    """Attach ``report`` to the ``runs`` command group."""
    group.add_command(runs_report)


__all__ = ["runs_report", "register", "render_report", "fmt_seconds", "fmt_signed"]
