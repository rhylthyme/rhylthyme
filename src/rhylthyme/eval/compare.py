"""Regression gate: compare a fresh ``results.json`` with the stored numbers.

The committed reference is ``eval/four-turn.json`` -- the pattern
``plan_schedule`` actually ships. ``eval/baseline.json`` is kept as the
historical single-message number and is never the gate.

Two conditions fail the comparison (plan, "Architectural decisions", CI
bullet):

* relationships F1 drops by more than ``rel_f1_drop`` **points**
  (default 5, i.e. 0.05 of F1), and
* the end-to-end pass rate drops at all (``e2e_drop`` default 0).

Both are read off the set-level means. Per-program rows are computed too,
so the printed table says *which* program moved. A program present in one
file and not the other is reported rather than silently dropped; missing
coverage fails unless ``allow_missing`` is set, because a program that was
not run cannot be said not to have regressed.

Pure data in, pure data out: no click, no I/O beyond :func:`load_scores`,
so :mod:`rhylthyme.cli.eval.cli` and the tests share one
implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Metrics shown in the summary diff, in print order.
SUMMARY_METRICS: Tuple[str, ...] = (
    "steps_f1",
    "durations_accuracy",
    "resources_precision",
    "resources_recall",
    "actors_accuracy",
    "relationships_precision",
    "relationships_recall",
    "relationships_f1",
    "structure_rand_index",
    "end_to_end_pass",
    "unsupported_rate",
)

#: Metrics the gate is computed from, and how far each may fall (in points).
GATED_METRICS: Tuple[str, ...] = ("relationships_f1", "end_to_end_pass")

DEFAULT_REL_F1_DROP = 5.0
DEFAULT_E2E_DROP = 0.0


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@dataclass
class Scores:
    """One side of a comparison: the set mean plus the per-program means."""

    summary: Dict[str, float] = field(default_factory=dict)
    programs: Dict[str, Dict[str, float]] = field(default_factory=dict)
    model: Optional[str] = None
    pattern: Optional[str] = None
    date: Optional[str] = None
    path: Optional[str] = None

    @property
    def label(self) -> str:
        bits = [b for b in (self.pattern, self.model, self.date) if b]
        return ", ".join(bits) or (self.path or "unknown")


def _results_block(payload: Any) -> Dict[str, Any]:
    """Accept a ``results.json`` or a committed ``{model, ..., results}`` file."""
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    if "results" in payload and isinstance(payload["results"], dict):
        return payload["results"]
    return payload


def parse_scores(payload: Any, *, path: Optional[str] = None) -> Scores:
    """Read either file shape into a :class:`Scores`."""
    block = _results_block(payload)
    summary = block.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{path or 'payload'}: no 'summary' object")
    raw_meta = block.get("meta")
    meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    outer: Dict[str, Any] = payload if isinstance(payload, dict) else {}
    programs: Dict[str, Dict[str, float]] = {}
    for entry in block.get("programs") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        headline = entry.get("headline")
        if isinstance(slug, str) and isinstance(headline, dict):
            programs[slug] = {k: float(v) for k, v in headline.items()}
    return Scores(
        summary={k: float(v) for k, v in summary.items()},
        programs=programs,
        model=outer.get("model") or meta.get("model"),
        pattern=outer.get("pattern") or meta.get("pattern"),
        date=outer.get("date") or (meta.get("generated_at") or "")[:10] or None,
        path=path,
    )


def load_scores(path: Path | str) -> Scores:
    """Load ``eval/four-turn.json`` or a run's ``results.json``."""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as handle:
        return parse_scores(json.load(handle), path=str(path))


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass
class MetricDelta:
    metric: str
    reference: float
    results: float
    allowed_drop_points: Optional[float] = None

    @property
    def delta(self) -> float:
        return self.results - self.reference

    @property
    def points(self) -> float:
        """Change in percentage points (F1 0.50 -> 0.45 is -5.0 points)."""
        return self.delta * 100.0

    @property
    def regressed(self) -> bool:
        if self.allowed_drop_points is None:
            return False
        # A drop of exactly the allowance is tolerated; more is a regression.
        return (-self.points) > self.allowed_drop_points + 1e-9


@dataclass
class Comparison:
    reference: Scores
    results: Scores
    deltas: List[MetricDelta] = field(default_factory=list)
    program_deltas: Dict[str, List[MetricDelta]] = field(default_factory=dict)
    only_in_reference: List[str] = field(default_factory=list)
    only_in_results: List[str] = field(default_factory=list)
    model_mismatch: Optional[Tuple[Optional[str], Optional[str]]] = None
    allow_missing: bool = False

    @property
    def regressions(self) -> List[MetricDelta]:
        return [d for d in self.deltas if d.regressed]

    @property
    def failures(self) -> List[str]:
        """Human-readable reasons the gate fails; empty means pass."""
        reasons = [
            f"{d.metric}: {d.reference:.3f} -> {d.results:.3f} "
            f"({d.points:+.1f} points, allowed -{d.allowed_drop_points:g})"
            for d in self.regressions
        ]
        if self.only_in_reference and not self.allow_missing:
            reasons.append(
                "not run in this results file: " + ", ".join(self.only_in_reference)
            )
        return reasons

    @property
    def ok(self) -> bool:
        return not self.failures


def compare_scores(
    reference: Scores,
    results: Scores,
    *,
    rel_f1_drop: float = DEFAULT_REL_F1_DROP,
    e2e_drop: float = DEFAULT_E2E_DROP,
    allow_missing: bool = False,
) -> Comparison:
    """Diff two score sets and decide whether the gate passes."""
    allowances = {
        "relationships_f1": float(rel_f1_drop),
        "end_to_end_pass": float(e2e_drop),
    }
    deltas = [
        MetricDelta(
            metric=metric,
            reference=float(reference.summary.get(metric, 0.0)),
            results=float(results.summary.get(metric, 0.0)),
            allowed_drop_points=allowances.get(metric),
        )
        for metric in SUMMARY_METRICS
        if metric in reference.summary or metric in results.summary
    ]

    program_deltas: Dict[str, List[MetricDelta]] = {}
    for slug in sorted(set(reference.programs) & set(results.programs)):
        program_deltas[slug] = [
            MetricDelta(
                metric=metric,
                reference=float(reference.programs[slug].get(metric, 0.0)),
                results=float(results.programs[slug].get(metric, 0.0)),
            )
            for metric in GATED_METRICS
        ]

    mismatch = None
    if reference.model and results.model and reference.model != results.model:
        mismatch = (reference.model, results.model)

    return Comparison(
        reference=reference,
        results=results,
        deltas=deltas,
        program_deltas=program_deltas,
        only_in_reference=sorted(set(reference.programs) - set(results.programs)),
        only_in_results=sorted(set(results.programs) - set(reference.programs)),
        model_mismatch=mismatch,
        allow_missing=allow_missing,
    )


def compare_files(
    reference_path: Path | str,
    results_path: Path | str,
    *,
    rel_f1_drop: float = DEFAULT_REL_F1_DROP,
    e2e_drop: float = DEFAULT_E2E_DROP,
    allow_missing: bool = False,
) -> Comparison:
    return compare_scores(
        load_scores(reference_path),
        load_scores(results_path),
        rel_f1_drop=rel_f1_drop,
        e2e_drop=e2e_drop,
        allow_missing=allow_missing,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _table(rows: List[List[str]]) -> List[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    out = []
    for index, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            out.append("  ".join("-" * w for w in widths))
    return out


def render_comparison(comparison: Comparison, *, verbose: bool = False) -> str:
    """The diff table plus a verdict line."""
    lines = [
        f"reference: {comparison.reference.label}"
        + (f"  [{comparison.reference.path}]" if comparison.reference.path else ""),
        f"results:   {comparison.results.label}"
        + (f"  [{comparison.results.path}]" if comparison.results.path else ""),
        "",
    ]
    rows: List[List[str]] = [["metric", "reference", "results", "delta", "gate"]]
    for delta in comparison.deltas:
        if delta.allowed_drop_points is None:
            gate = ""
        elif delta.regressed:
            gate = f"FAIL (max -{delta.allowed_drop_points:g} pts)"
        else:
            gate = f"ok (max -{delta.allowed_drop_points:g} pts)"
        rows.append(
            [
                delta.metric,
                f"{delta.reference:.3f}",
                f"{delta.results:.3f}",
                f"{delta.points:+.1f} pts",
                gate,
            ]
        )
    lines.extend(_table(rows))

    moved = {
        slug: deltas
        for slug, deltas in comparison.program_deltas.items()
        if any(abs(d.delta) > 1e-9 for d in deltas)
    }
    if moved and (verbose or any(d.delta < 0 for ds in moved.values() for d in ds)):
        lines.append("")
        lines.append("Per-program changes:")
        prows: List[List[str]] = [["program"] + list(GATED_METRICS)]
        for slug, deltas in sorted(moved.items()):
            prows.append(
                [slug]
                + [
                    f"{d.reference:.2f}->{d.results:.2f} ({d.points:+.1f})"
                    for d in deltas
                ]
            )
        lines.extend(_table(prows))

    if comparison.only_in_reference:
        lines.append("")
        lines.append(
            "In the reference but not in these results: "
            + ", ".join(comparison.only_in_reference)
        )
    if comparison.only_in_results:
        lines.append("")
        lines.append(
            "New since the reference (not gated): "
            + ", ".join(comparison.only_in_results)
        )
    if comparison.model_mismatch:
        lines.append("")
        lines.append(
            "WARNING: model pin differs — reference "
            f"{comparison.model_mismatch[0]} vs results "
            f"{comparison.model_mismatch[1]}; a pin change is a re-baseline, "
            "not a regression."
        )

    lines.append("")
    if comparison.ok:
        lines.append("PASS: no gated regression.")
    else:
        lines.append("FAIL:")
        lines.extend(f"  - {reason}" for reason in comparison.failures)
    return "\n".join(lines)


def comparison_to_dict(comparison: Comparison) -> Dict[str, Any]:
    return {
        "ok": comparison.ok,
        "failures": comparison.failures,
        "reference": {
            "path": comparison.reference.path,
            "model": comparison.reference.model,
            "pattern": comparison.reference.pattern,
            "date": comparison.reference.date,
        },
        "results": {
            "path": comparison.results.path,
            "model": comparison.results.model,
            "pattern": comparison.results.pattern,
            "date": comparison.results.date,
        },
        "summary": {
            d.metric: {
                "reference": d.reference,
                "results": d.results,
                "delta_points": d.points,
                "allowed_drop_points": d.allowed_drop_points,
                "regressed": d.regressed,
            }
            for d in comparison.deltas
        },
        "only_in_reference": list(comparison.only_in_reference),
        "only_in_results": list(comparison.only_in_results),
        "model_mismatch": (
            list(comparison.model_mismatch) if comparison.model_mismatch else None
        ),
    }
