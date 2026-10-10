"""``rhylthyme eval-prompts`` -- score agent-authored programs against the gold set.

Two modes share one scorer and one output layout:

* ``--score-only --predicted <dir>``: compare programs already on disk.
* ``--model <id> [--patterns baseline]``: render the prompt pattern for
  each gold source, call the model, extract and validate the program,
  then score it (:mod:`rhylthyme.cli.eval.harness`).

A third, non-scoring mode is the sub-command ``eval-prompts compare``,
which diffs a fresh ``results.json`` against the committed numbers and
exits non-zero on a regression (:mod:`rhylthyme.eval.compare`).
It is what CI runs after the live run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from rhylthyme.eval.compare import DEFAULT_E2E_DROP, DEFAULT_REL_F1_DROP

DEFAULT_OUT = "./eval-results"


@click.group("eval-prompts", invoke_without_command=True)
@click.pass_context
@click.option(
    "--score-only",
    is_flag=True,
    help="Score predicted programs already on disk (no model calls).",
)
@click.option(
    "--gold",
    "gold_dir",
    type=click.Path(exists=True, file_okay=False),
    help="Gold set directory (rhylthyme-examples/gold); required unless a "
    "sub-command is given.",
)
@click.option(
    "--predicted",
    "predicted_dir",
    type=click.Path(exists=True, file_okay=False),
    help="Directory of predicted programs: <slug>.json or <slug>/program.json.",
)
@click.option(
    "--model",
    help="Model id for live runs, e.g. claude-haiku-4-5 (required unless --score-only).",
)
@click.option(
    "--patterns",
    default="baseline",
    show_default=True,
    help="Comma-separated prompt patterns to run (see --list-patterns).",
)
@click.option(
    "--list-patterns", is_flag=True, help="Print the registered patterns and exit."
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    help="Run only the first N gold programs (sorted by slug).",
)
@click.option(
    "--only",
    "only",
    help="Comma-separated gold slugs to run (applied before --limit).",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Programs to run in parallel (thread pool).",
)
@click.option(
    "--from-cache",
    is_flag=True,
    help="Never call the model; replay cached responses and fail on a miss.",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False),
    help="Where raw responses are cached (default <out>/cache).",
)
@click.option(
    "--max-fix-iterations",
    type=click.IntRange(min=0),
    default=2,
    show_default=True,
    help="How many times validator findings are sent back for a fix.",
)
@click.option(
    "--max-tokens",
    type=click.IntRange(min=256),
    default=16000,
    show_default=True,
    help="max_tokens per model call.",
)
@click.option(
    "--write-baseline",
    "baseline_path",
    type=click.Path(dir_okay=False),
    help="Also write {model, pattern, date, results} to this path (eval/baseline.json).",
)
@click.option(
    "--fake-client",
    "fake_client_path",
    type=click.Path(exists=True, dir_okay=False),
    hidden=True,
    help="JSON file of canned responses (list or {substring: text}); tests only.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False),
    default=DEFAULT_OUT,
    show_default=True,
    help="Where results.json and results.md are written.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="What to print to stdout.",
)
@click.option(
    "--skip-js",
    is_flag=True,
    help="Do not run the JavaScript validator (schedule.js) even if node is present.",
)
@click.option(
    "--threshold",
    type=float,
    default=0.5,
    show_default=True,
    help="Minimum source-span overlap for two steps to match.",
)
@click.option("--verbose", "-v", is_flag=True, help="Print a per-program breakdown.")
def eval_prompts(
    ctx,
    score_only,
    gold_dir,
    predicted_dir,
    model,
    patterns,
    list_patterns,
    limit,
    only,
    concurrency,
    from_cache,
    cache_dir,
    max_fix_iterations,
    max_tokens,
    baseline_path,
    fake_client_path,
    out_dir,
    fmt,
    skip_js,
    threshold,
    verbose,
):
    """
    Score agent-authored programs against the expert gold set.

    Compares each predicted program with the gold program of the same slug
    and reports per-component precision/recall (steps, durations,
    resources, actors, relationships), the track Rand index, the
    end-to-end pass and the unsupported-step rate. Writes results.json and
    results.md to --out.

    With --score-only the predicted programs come from --predicted. With
    --model they are produced live: each gold source is rendered through
    every pattern in --patterns, sent to the model, extracted, validated
    (with a bounded fix loop) and scored. Raw responses are cached under
    --cache-dir so --from-cache can re-score without spending.
    """
    if ctx.invoked_subcommand is not None:
        return

    if list_patterns:
        from .patterns import PATTERNS

        for name in sorted(PATTERNS):
            click.echo(name)
        return

    if not gold_dir:
        raise click.UsageError("--gold <dir> is required.")

    if score_only:
        _score_only(gold_dir, predicted_dir, out_dir, fmt, skip_js, threshold, verbose)
        return

    if not model:
        raise click.UsageError(
            "Pass --score-only with --predicted <dir>, or --model <id> for a live run."
        )
    _live(
        gold_dir=gold_dir,
        model=model,
        patterns=patterns,
        limit=limit,
        only=only,
        concurrency=concurrency,
        from_cache=from_cache,
        cache_dir=cache_dir,
        max_fix_iterations=max_fix_iterations,
        max_tokens=max_tokens,
        baseline_path=baseline_path,
        fake_client_path=fake_client_path,
        out_dir=out_dir,
        fmt=fmt,
        skip_js=skip_js,
        threshold=threshold,
        verbose=verbose,
    )


def _load_gold(gold_dir):
    from rhylthyme.eval.gold import load_gold_set

    gold_set = load_gold_set(gold_dir)
    if not gold_set:
        click.echo(f"No gold programs found under {gold_dir}", err=True)
        sys.exit(1)
    return gold_set


def _emit(result, out_dir, fmt, verbose, label: Optional[str] = None):
    from rhylthyme.eval.report import (
        per_program_lines,
        render_table,
        to_json_dict,
        write_results,
    )

    json_path, md_path = write_results(result, out_dir)
    if fmt == "json":
        click.echo(json.dumps(to_json_dict(result), indent=2))
        return json_path, md_path
    if label:
        click.echo(f"== {label} ==")
    click.echo(render_table(result))
    if verbose:
        click.echo("")
        for scores in result.programs:
            click.echo("\n".join(per_program_lines(scores)))
    click.echo("")
    click.echo(f"Wrote {json_path} and {md_path}")
    return json_path, md_path


def _score_only(gold_dir, predicted_dir, out_dir, fmt, skip_js, threshold, verbose):
    if not predicted_dir:
        raise click.UsageError("--score-only requires --predicted <dir>.")

    from rhylthyme.eval.gold import load_predicted_set
    from rhylthyme.eval.metrics import score_set
    from rhylthyme.eval.report import stamp

    gold_set = _load_gold(gold_dir)
    predicted = load_predicted_set(predicted_dir, (g.slug for g in gold_set))
    result = score_set(gold_set, predicted, threshold=threshold, js=not skip_js)
    stamp(
        result,
        mode="score-only",
        gold_dir=str(gold_dir),
        predicted_dir=str(predicted_dir),
        threshold=threshold,
    )
    _emit(result, out_dir, fmt, verbose)
    if result.missing and fmt != "json":
        click.echo(
            "Missing predictions (scored zero): " + ", ".join(result.missing),
            err=True,
        )


def _live(
    *,
    gold_dir,
    model,
    patterns,
    limit,
    only,
    concurrency,
    from_cache,
    cache_dir,
    max_fix_iterations,
    max_tokens,
    baseline_path,
    fake_client_path,
    out_dir,
    fmt,
    skip_js,
    threshold,
    verbose,
):
    from rhylthyme.eval.report import stamp

    from .harness import CacheMiss, HarnessConfig, run_harness, write_baseline
    from .llm import make_client
    from .patterns import PATTERNS

    names = [name.strip() for name in patterns.split(",") if name.strip()]
    unknown = [name for name in names if name not in PATTERNS]
    if not names or unknown:
        raise click.UsageError(
            f"Unknown pattern(s): {', '.join(unknown) or '(none given)'}; "
            f"known: {', '.join(sorted(PATTERNS))}"
        )
    if baseline_path and len(names) > 1:
        raise click.UsageError("--write-baseline takes exactly one pattern.")

    only_slugs = (
        [slug.strip() for slug in only.split(",") if slug.strip()] if only else None
    )

    fake_responses = None
    if fake_client_path:
        with open(fake_client_path, "r", encoding="utf-8") as handle:
            fake_responses = json.load(handle)
    try:
        client = make_client(fake_responses, model=model)
    except RuntimeError as exc:
        raise click.UsageError(str(exc))

    gold_set = _load_gold(gold_dir)
    out_root = Path(out_dir)
    quiet = fmt == "json"

    def progress(scores):
        if quiet:
            return
        status = f"error: {scores.error}" if scores.error else "ok"
        extras = scores.extras
        click.echo(
            f"  {scores.slug}: {status} "
            f"(calls={extras.get('calls', 0)}, fix={extras.get('fix_iterations', 0)}, "
            f"tokens={extras.get('input_tokens', 0)}/{extras.get('output_tokens', 0)})",
            err=True,
        )

    for name in names:
        pattern_out = out_root if len(names) == 1 else out_root / name
        config = HarnessConfig(
            model=model,
            pattern=name,
            out_dir=pattern_out,
            cache_dir=Path(cache_dir) if cache_dir else None,
            from_cache=from_cache,
            limit=limit,
            only=only_slugs,
            concurrency=concurrency,
            max_fix_iterations=max_fix_iterations,
            max_tokens=max_tokens,
            threshold=threshold,
            js=not skip_js,
        )
        planned = len(only_slugs) if only_slugs else len(gold_set)
        planned = min(limit or planned, planned)
        if not quiet:
            click.echo(
                f"Running pattern '{name}' on {planned} program(s) with {model}"
                + (" from cache" if from_cache else "")
                + f" (concurrency={concurrency})",
                err=True,
            )
        try:
            result = run_harness(gold_set, config, client, progress=progress)
        except CacheMiss as exc:
            raise click.ClickException(f"cache miss: {exc}")
        except KeyError as exc:
            raise click.UsageError(str(exc).strip('"'))
        stamp(result, gold_dir=str(gold_dir))
        _emit(result, pattern_out, fmt, verbose, label=None if quiet else name)
        if not quiet:
            totals = result.meta.get("totals", {})
            cost = totals.get("cost_usd")
            cost_text = (
                f"${cost:.4f}" if cost is not None else "unknown (model not priced)"
            )
            click.echo(
                f"Calls: {totals.get('calls', 0)}, tokens in/out: "
                f"{totals.get('input_tokens', 0)}/{totals.get('output_tokens', 0)}, "
                f"estimated cost: {cost_text}; cache: {config.cache_dir}"
            )
            if result.missing:
                click.echo(
                    "Scored zero (no valid program): " + ", ".join(result.missing),
                    err=True,
                )
        if baseline_path:
            path = write_baseline(result, baseline_path)
            if not quiet:
                click.echo(f"Wrote baseline {path}")


@eval_prompts.command("compare")
@click.option(
    "--baseline",
    "reference_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Stored reference: eval/four-turn.json (what ships), not baseline.json.",
)
@click.option(
    "--results",
    "results_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="results.json from the run under test (or another stored file).",
)
@click.option(
    "--rel-f1-drop",
    type=float,
    default=DEFAULT_REL_F1_DROP,
    show_default=True,
    help="Largest tolerated fall in relationships F1, in points (0.05 F1 = 5).",
)
@click.option(
    "--e2e-drop",
    type=float,
    default=DEFAULT_E2E_DROP,
    show_default=True,
    help="Largest tolerated fall in the end-to-end pass rate, in points.",
)
@click.option(
    "--allow-missing",
    is_flag=True,
    help="Do not fail when the results file is missing a program the reference has.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="What to print.",
)
@click.option("--verbose", "-v", is_flag=True, help="Show every per-program change.")
def compare_command(
    reference_path,
    results_path,
    rel_f1_drop,
    e2e_drop,
    allow_missing,
    fmt,
    verbose,
):
    """
    Fail when a run regresses against the committed numbers.

    Exits non-zero when relationships F1 falls by more than --rel-f1-drop
    points, or when the end-to-end pass rate falls at all, measured on the
    set means. The reference to gate on is ``eval/four-turn.json``: that is
    the pattern ``plan_schedule`` ships. ``eval/baseline.json`` is kept for
    the historical single-message number and is not a gate.
    """
    from rhylthyme.eval.compare import (
        compare_files,
        comparison_to_dict,
        render_comparison,
    )

    comparison = compare_files(
        reference_path,
        results_path,
        rel_f1_drop=rel_f1_drop,
        e2e_drop=e2e_drop,
        allow_missing=allow_missing,
    )
    if fmt == "json":
        click.echo(json.dumps(comparison_to_dict(comparison), indent=2))
    else:
        click.echo(render_comparison(comparison, verbose=verbose))
    if not comparison.ok:
        raise SystemExit(1)


def register(group: click.Group) -> None:
    """Attach ``eval-prompts`` to the main CLI group."""
    group.add_command(eval_prompts)
