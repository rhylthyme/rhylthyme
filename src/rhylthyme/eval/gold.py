"""Gold-set loading and source-span location.

A gold program lives in ``<gold>/<slug>/`` as three files:

* ``source.txt`` -- the text the program was extracted from,
* ``context.json`` -- ``{environmentType, deadline?, constraints[], domain}``,
* ``program.json`` -- a validated program whose every step carries
  ``metadata.sourceSpan = {quote, occurrence}``.

Spans are quoted substrings plus a 1-based occurrence index (PRD open
question 3). ``locate_quote`` resolves one to a character range; the
matcher scores predicted steps by character-range overlap against gold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

Program = Dict[str, Any]
Span = Tuple[int, int]

SOURCE_FILE = "source.txt"
CONTEXT_FILE = "context.json"
PROGRAM_FILE = "program.json"


@dataclass
class GoldProgram:
    """One gold triple: program, the text it came from and its context."""

    slug: str
    program: Program
    source_text: str
    context: Dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_gold_program(directory: Path | str) -> GoldProgram:
    """Load one ``<slug>/`` directory. Missing context or source are tolerated."""
    directory = Path(directory)
    program = _read_json(directory / PROGRAM_FILE)
    source_path = directory / SOURCE_FILE
    source_text = (
        source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    )
    context_path = directory / CONTEXT_FILE
    context = _read_json(context_path) if context_path.exists() else {}
    return GoldProgram(
        slug=directory.name,
        program=program,
        source_text=source_text,
        context=context,
        path=directory,
    )


def load_gold_set(gold_dir: Path | str) -> List[GoldProgram]:
    """Load every ``<slug>/program.json`` under ``gold_dir``, sorted by slug."""
    gold_dir = Path(gold_dir)
    if not gold_dir.is_dir():
        raise FileNotFoundError(f"Gold directory not found: {gold_dir}")
    programs = []
    for child in sorted(gold_dir.iterdir()):
        if child.is_dir() and (child / PROGRAM_FILE).exists():
            programs.append(load_gold_program(child))
    return programs


def predicted_program_path(predicted_dir: Path | str, slug: str) -> Optional[Path]:
    """Resolve ``<predicted>/<slug>.json`` or ``<predicted>/<slug>/program.json``."""
    predicted_dir = Path(predicted_dir)
    for candidate in (
        predicted_dir / f"{slug}.json",
        predicted_dir / slug / PROGRAM_FILE,
    ):
        if candidate.exists():
            return candidate
    return None


def load_predicted_program(predicted_dir: Path | str, slug: str) -> Optional[Program]:
    """Load the predicted program for ``slug`` or return ``None`` when absent."""
    path = predicted_program_path(predicted_dir, slug)
    if path is None:
        return None
    data = _read_json(path)
    # Tolerate wrappers such as {"program": {...}} produced by tool results.
    if isinstance(data, dict) and "tracks" not in data and "program" in data:
        data = data["program"]
    return data


def load_predicted_set(
    predicted_dir: Path | str, slugs: Iterable[str]
) -> Dict[str, Optional[Program]]:
    """Map each slug to its predicted program (``None`` when missing)."""
    return {slug: load_predicted_program(predicted_dir, slug) for slug in slugs}


# --------------------------------------------------------------------------
# Span location
# --------------------------------------------------------------------------


def count_occurrences(text: str, quote: str) -> int:
    """Count non-overlapping occurrences of ``quote`` in ``text``."""
    if not quote:
        return 0
    return text.count(quote)


def locate_quote(text: str, quote: str, occurrence: int = 1) -> Optional[Span]:
    """Return the ``[start, end)`` range of the n-th exact occurrence of ``quote``.

    Occurrences are counted left to right without overlap, the same way
    ``str.count`` does. Returns ``None`` when the quote is empty, the
    occurrence index is below 1, or there are fewer occurrences than asked.
    """
    if not quote or occurrence < 1:
        return None
    position = 0
    found = 0
    while True:
        index = text.find(quote, position)
        if index < 0:
            return None
        found += 1
        if found == occurrence:
            return (index, index + len(quote))
        position = index + len(quote)


_WHITESPACE = re.compile(r"\s+")


def _collapse(text: str) -> Tuple[str, List[int]]:
    """Lower-case and collapse whitespace, keeping a map back to source offsets."""
    out: List[str] = []
    index_map: List[int] = []
    previous_space = False
    for i, char in enumerate(text):
        if char.isspace():
            if previous_space:
                continue
            previous_space = True
            out.append(" ")
        else:
            previous_space = False
            out.append(char.lower())
        index_map.append(i)
    return "".join(out), index_map


def locate_quote_fuzzy(text: str, quote: str, occurrence: int = 1) -> Optional[Span]:
    """Like :func:`locate_quote` but case- and whitespace-insensitive.

    Exact matches are tried first. Used for *predicted* spans, whose quotes a
    model may have re-wrapped; gold spans must always match exactly.
    """
    exact = locate_quote(text, quote, occurrence)
    if exact is not None:
        return exact
    if not quote or occurrence < 1:
        return None
    collapsed_text, index_map = _collapse(text)
    collapsed_quote = _WHITESPACE.sub(" ", quote.strip()).lower()
    span = locate_quote(collapsed_text, collapsed_quote, occurrence)
    if span is None:
        return None
    start, end = span
    return (index_map[start], index_map[end - 1] + 1)


def step_source_span(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the ``metadata.sourceSpan`` object of a step, or ``None``."""
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    span = metadata.get("sourceSpan")
    if not isinstance(span, dict) or not isinstance(span.get("quote"), str):
        return None
    return span


def step_span(step: Dict[str, Any], text: str, fuzzy: bool = False) -> Optional[Span]:
    """Locate a step's ``sourceSpan`` in ``text`` as a character range."""
    span = step_source_span(step)
    if span is None or not text:
        return None
    occurrence = span.get("occurrence", 1)
    try:
        occurrence = int(occurrence)
    except (TypeError, ValueError):
        occurrence = 1
    locate = locate_quote_fuzzy if fuzzy else locate_quote
    return locate(text, span["quote"], occurrence)


def is_inferred(step: Dict[str, Any]) -> bool:
    """True when the step is tagged ``metadata.inferred: true``."""
    metadata = step.get("metadata")
    return isinstance(metadata, dict) and bool(metadata.get("inferred"))
