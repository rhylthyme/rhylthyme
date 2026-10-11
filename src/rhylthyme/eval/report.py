"""Rendering of scorer results as JSON, Markdown and a plain-text table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .metrics import ComponentScores, SetResult

RESULTS_JSON = "results.json"
RESULTS_MD = "results.md"

COLUMNS: List[Tuple[str, str]] = [
    ("program", "program"),
    ("steps P/R/F1", "steps"),
    ("dur acc", "durations_accuracy"),
    ("res P/R", "resources"),
    ("actors", "actors_accuracy"),
    ("rel P/R/F1", "relationships"),
    ("tracks RI", "structure_rand_index"),
    ("e2e", "end_to_end_pass"),
    ("unsupp", "unsupported_rate"),
]


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _row_cells(label: str, headline: Dict[str, float]) -> List[str]:
    return [
        label,
        "/".join(
            _fmt(headline[k]) for k in ("steps_precision", "steps_recall", "steps_f1")
        ),
        _fmt(headline["durations_accuracy"]),
        "/".join(
            _fmt(headline[k]) for k in ("resources_precision", "resources_recall")
        ),
        _fmt(headline["actors_accuracy"]),
        "/".join(
            _fmt(headline[k])
            for k in (
                "relationships_precision",
                "relationships_recall",
                "relationships_f1",
            )
        ),
        _fmt(headline["structure_rand_index"]),
        _fmt(headline["end_to_end_pass"]),
        _fmt(headline["unsupported_rate"]),
    ]


def table_rows(result: SetResult) -> List[List[str]]:
    """Header row, one row per program, and a mean row."""
    rows = [[title for title, _ in COLUMNS]]
    for scores in result.programs:
        label = scores.slug + (" (missing)" if scores.error else "")
        rows.append(_row_cells(label, scores.headline()))
    rows.append(_row_cells(f"mean (n={len(result.programs)})", result.summary))
    return rows


def render_markdown(result: SetResult) -> str:
    rows = table_rows(result)
    lines = [
        "# eval-prompts results",
        "",
        f"Generated: {result.meta.get('generated_at', '')}",
        f"Gold: `{result.meta.get('gold_dir', '')}`",
        f"Predicted: `{result.meta.get('predicted_dir', '')}`",
        "",
        "| " + " | ".join(rows[0]) + " |",
        "|" + "|".join("---" for _ in rows[0]) + "|",
    ]
    for row in rows[1:-1]:
        lines.append("| " + " | ".join(row) + " |")
    lines.append("| " + " | ".join(f"**{cell}**" for cell in rows[-1]) + " |")
    lines.append("")
    js_ran = [s.slug for s in result.programs if s.end_to_end.js_status == "ran"]
    lines.append(
        "JS validator: "
        + (
            f"ran on {len(js_ran)}/{len(result.programs)} programs"
            if js_ran
            else "skipped (node or schedule.js not found)"
        )
    )
    if result.missing:
        lines.append("")
        lines.append("Missing predictions (scored zero): " + ", ".join(result.missing))
    failures = [
        (s.slug, s.end_to_end)
        for s in result.programs
        if not s.error and not s.end_to_end.passed
    ]
    if failures:
        lines.append("")
        lines.append("## End-to-end failures")
        lines.append("")
        for slug, e2e in failures:
            reasons = []
            if not e2e.python_valid:
                reasons.append(f"python validator: {'; '.join(e2e.python_errors[:3])}")
            if e2e.js_valid is False:
                reasons.append(f"js validator: {'; '.join(e2e.js_errors[:3])}")
            if not e2e.makespan_ok:
                reasons.append(
                    f"makespan {e2e.pred_makespan:.0f}s vs gold {e2e.gold_makespan:.0f}s"
                )
            if not e2e.critical_ok:
                reasons.append(f"critical path overlap {e2e.critical_overlap:.2f}")
            lines.append(f"- `{slug}`: " + "; ".join(reasons))
    lines.append("")
    return "\n".join(lines)


def render_table(result: SetResult) -> str:
    """Plain-text table with aligned columns for the terminal."""
    rows = table_rows(result)
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    out = []
    for index, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def to_json_dict(result: SetResult) -> Dict[str, Any]:
    return result.to_dict()


def stamp(result: SetResult, **meta: Any) -> SetResult:
    """Attach generation metadata (time plus whatever the caller passes)."""
    result.meta.setdefault(
        "generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    result.meta.update({k: v for k, v in meta.items() if v is not None})
    return result


def write_results(result: SetResult, out_dir: Path | str) -> Tuple[Path, Path]:
    """Write ``results.json`` and ``results.md`` to ``out_dir``; return their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / RESULTS_JSON
    md_path = out_dir / RESULTS_MD
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(to_json_dict(result), handle, indent=2)
        handle.write("\n")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def per_program_lines(scores: ComponentScores) -> List[str]:
    """Verbose breakdown for one program (used by ``--verbose``)."""
    h = scores.headline()
    lines = [f"{scores.slug}:"]
    if scores.error:
        lines.append(f"  error: {scores.error}")
        return lines
    for key in ComponentScores.HEADLINE_KEYS:
        lines.append(f"  {key:<26} {h[key]:.3f}")
    if scores.unsupported_steps:
        lines.append("  unsupported steps: " + ", ".join(scores.unsupported_steps))
    unmatched = [m["gold"] for m in scores.matching if m["predicted"] is None]
    if unmatched:
        lines.append("  unmatched gold steps: " + ", ".join(unmatched))
    return lines
