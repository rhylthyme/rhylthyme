"""
``rhylthyme runs evaluate`` — does prediction beat the author's guess?

    rhylthyme runs evaluate PROGRAM [--runs-dir DIR] [--since DATE]
                            [--holdout 0.2] [--min-runs K]
                            [--verdict-min-runs K] [--cv-threshold X]
                            [--format table|md|json] [--out FILE]
    rhylthyme runs evaluate --all ...

``PROGRAM`` is a programId or a path to a program file; a file also supplies
the ``task`` of each step, which is what the roll-up by step type groups on.
The computation lives in :mod:`.evaluate`; this module only renders it.

Registered onto the existing ``runs`` group by
``rhylthyme.cli.main._register_runs_commands``.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

import click

from rhylthyme.history.evaluate import (
    DEFAULT_HOLDOUT,
    DEFAULT_MIN_TOTAL_RUNS,
    Evaluation,
    evaluate_program,
    group_by_program,
)
from rhylthyme.history.report import DEFAULT_CV_THRESHOLD, DEFAULT_MIN_RUNS
from rhylthyme.history.store import list_runs, resolve_runs_dir

from .report_cli import _md_table, _plain_table, fmt_seconds

REASON_HELP = {
    "no-usable-runs": (
        "no recorded run is a measurement (see `runs report` for why each was "
        "excluded)"
    ),
    "too-few-runs": "fewer usable runs than --min-runs: nothing to hold out yet",
    "no-measured-steps-held-out": (
        "the held-out runs contain no measured step, so there is nothing to score"
    ),
}


# --------------------------------------------------------------- formatting


def fmt_pct(value: Optional[float]) -> str:
    """A signed percentage; ``-`` for None. Positive means prediction won."""
    if value is None:
        return "-"
    return f"{value * 100:+.1f}%"


def fmt_basis(counts: Optional[Dict[str, int]]) -> str:
    """``identical/model/none`` counts as ``3i 1m 0n``; ``-`` when empty."""
    if not counts:
        return "-"
    parts = [
        f"{counts.get(key, 0)}{key[0]}"
        for key in ("identical", "model", "none")
        if counts.get(key)
    ]
    return " ".join(parts) if parts else "-"


STEP_HEADERS = [
    "Step",
    "Type",
    "Task",
    "Verdict",
    "n",
    "Planned",
    "Median actual",
    "MAE predicted",
    "MAE planned",
    "Improvement",
    "Basis",
]


def step_rows(evaluation: Evaluation) -> List[List[str]]:
    rows = []
    for step in evaluation.steps:
        mark = "*" if step.verdict == "predictable" else " "
        rows.append(
            [
                mark + step.stepId,
                step.durationType or "-",
                step.task or "-",
                step.verdict,
                str(step.n),
                fmt_seconds(step.plannedSeconds),
                fmt_seconds(step.medianActual),
                fmt_seconds(step.maePredicted),
                fmt_seconds(step.maePlanned),
                fmt_pct(step.improvement),
                fmt_basis(step.basisCounts),
            ]
        )
    return rows


GROUP_HEADERS = [
    "Group",
    "Steps",
    "n",
    "MAE predicted",
    "MAE planned",
    "Improvement",
    "Predictable steps",
    "Predictable n",
    "Predictable improvement",
]


def group_rows(groups) -> List[List[str]]:
    return [
        [
            g.key,
            str(g.steps),
            str(g.n),
            fmt_seconds(g.maePredicted),
            fmt_seconds(g.maePlanned),
            fmt_pct(g.improvement),
            str(g.predictableSteps),
            str(g.predictableN),
            fmt_pct(g.predictableImprovement),
        ]
        for g in groups
    ]


def render_evaluation(evaluation: Evaluation, fmt: str = "table") -> str:
    """Render ``evaluation`` as ``table``, ``md`` or ``json``."""
    if fmt == "json":
        return json.dumps(evaluation.to_dict(), indent=2, sort_keys=True)

    table = _md_table if fmt == "md" else _plain_table
    h1 = "# " if fmt == "md" else ""
    h2 = "## " if fmt == "md" else ""
    out: List[str] = []
    out.append(
        f"{h1}Held-out duration evaluation: "
        f"{evaluation.programId or '(all programs)'}"
    )
    out.append("")
    out.append(
        f"Runs: {evaluation.runsUsable} usable of {evaluation.runsConsidered}; "
        f"trained on {evaluation.runsTrain}, held out {evaluation.runsHeldOut} "
        f"(latest {evaluation.holdout * 100:.0f}%)"
    )
    if evaluation.reason:
        out.append("")
        out.append(
            f"Not evaluated: {REASON_HELP.get(evaluation.reason, evaluation.reason)}"
        )
        return "\n".join(out) + "\n"
    out.append(
        f"Scored {evaluation.n} of {evaluation.observations} held-out "
        f"measurements ({fmt_basis(evaluation.basisCounts)})"
    )
    out.append(
        f"Overall MAE: predicted {fmt_seconds(evaluation.maePredicted)} vs "
        f"planned {fmt_seconds(evaluation.maePlanned)} "
        f"({fmt_pct(evaluation.improvement)})"
    )
    claim = evaluation.claim or {}
    holds = claim.get("holds")
    if holds is None:
        verdict = "untested: no predictable step has a held-out measurement"
    elif holds:
        verdict = "holds for " + ", ".join(claim.get("stepTypes") or [])
    else:
        verdict = "FAILS for " + ", ".join(claim.get("failures") or [])
    out.append(
        f"Claim (predicted beats planned on every predictable step type): {verdict}"
    )
    out.append("")
    out.append(f"{h2}Per step")
    out.append("")
    out.append(table(STEP_HEADERS, step_rows(evaluation)))
    out.append("")
    out.append(f"{h2}By step type (task)")
    out.append("")
    out.append(table(GROUP_HEADERS, group_rows(evaluation.byTask)))
    out.append("")
    out.append(f"{h2}By duration kind")
    out.append("")
    out.append(table(GROUP_HEADERS, group_rows(evaluation.byDurationKind)))
    out.append("")
    out.append(
        "Improvement is 1 - MAE(predicted)/MAE(planned): positive means the "
        "forecast beat the author's number."
    )
    out.append(
        "A step marked * was called `predictable` by `runs report` on the "
        "training runs; the acceptance claim is read off those rows only."
    )
    out.append(
        "n counts held-out measurements that had BOTH a prediction and a "
        "planned value, so the two MAE columns are over the same sample; "
        "Basis says which lookup branch answered (i=identical, m=model, "
        "n=none)."
    )
    out.append(
        "Fixed steps never appear: their observed duration only confirms "
        "their timer, so there is nothing to forecast."
    )
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


@click.command("evaluate")
@click.argument("program", required=False)
@click.option(
    "--all", "all_programs", is_flag=True, help="Evaluate every program with runs"
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
    "--holdout",
    type=float,
    default=DEFAULT_HOLDOUT,
    show_default=True,
    help="Share of the latest runs held out of training and predicted",
)
@click.option(
    "--min-runs",
    "min_runs",
    type=int,
    default=DEFAULT_MIN_TOTAL_RUNS,
    show_default=True,
    help="K: usable runs a program needs before it is evaluated at all",
)
@click.option(
    "--verdict-min-runs",
    type=int,
    default=DEFAULT_MIN_RUNS,
    show_default=True,
    help="Measured runs a step needs before `runs report` gives it a verdict",
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
def runs_evaluate(
    program,
    all_programs,
    runs_dir,
    since,
    holdout,
    min_runs,
    verdict_min_runs,
    cv_threshold,
    fmt,
    out,
):
    """
    Hold out the latest runs, predict them from the rest, compare with the plan.

    For every step measured in a held-out run, the mean absolute error of the
    history-based prediction is set beside the mean absolute error of the
    author's planned duration. Positive improvement means the forecast was
    closer to what happened than the number in the program; the rows marked *
    are the steps `runs report` called predictable, and the acceptance claim
    is read off those.
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

    options = dict(
        holdout=holdout,
        min_runs=min_runs,
        verdict_min_runs=verdict_min_runs,
        cv_threshold=cv_threshold,
        since=since,
    )
    if all_programs:
        evaluations = [
            evaluate_program(group, None, **options)
            for _pid, group in sorted(group_by_program(records).items())
        ]
    else:
        evaluations = [evaluate_program(records, loaded, **options)]

    if fmt == "json":
        payload = (
            [e.to_dict() for e in evaluations]
            if all_programs
            else evaluations[0].to_dict()
        )
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    else:
        text = "\n".join(render_evaluation(e, fmt) for e in evaluations)

    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        click.echo(f"Evaluation written to {out}")
    else:
        click.echo(text, nl=False)


def register(group) -> None:
    """Attach ``evaluate`` to the ``runs`` command group."""
    group.add_command(runs_evaluate)


__all__ = [
    "runs_evaluate",
    "register",
    "render_evaluation",
    "fmt_pct",
    "fmt_basis",
]
