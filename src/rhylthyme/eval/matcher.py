"""Step matching between a gold program and a predicted program.

Two steps match when their source spans overlap by at least ``threshold``
(default 0.5, measured as the overlapping character count divided by the
length of the shorter span), or, failing that, when their normalised names
are equal. Matching is one-to-one and greedy: the best span overlaps are
assigned first, then names fill in for whatever is left.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .gold import Program, Span, step_span

DEFAULT_OVERLAP_THRESHOLD = 0.5

# Words dropped before comparing step names. Kept short on purpose: the
# goal is to make "Roast the turkey" and "roast turkey" equal, not to do
# semantic matching.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "in",
        "into",
        "of",
        "on",
        "the",
        "then",
        "to",
        "until",
        "with",
        "your",
    }
)

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_name(name: Any) -> str:
    """Lower-case, strip punctuation and stopwords, collapse whitespace."""
    if name is None:
        return ""
    text = _NON_ALNUM.sub(" ", str(name).lower())
    words = [word for word in text.split() if word not in STOPWORDS]
    return " ".join(words)


@dataclass(frozen=True)
class StepRef:
    """A step together with where it sits in the program and in the source."""

    step_id: str
    name: str
    track_id: str
    track_index: int
    order: int
    step: Dict[str, Any] = field(compare=False, hash=False, repr=False)
    span: Optional[Span] = None

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)

    @property
    def normalized_id(self) -> str:
        return normalize_name(self.step_id)


def flatten_steps(
    program: Program, source_text: str = "", fuzzy_spans: bool = False
) -> List[StepRef]:
    """List every step of a program in track order with its located span."""
    refs: List[StepRef] = []
    order = 0
    for track_index, track in enumerate(program.get("tracks", []) or []):
        track_id = str(track.get("trackId", f"track-{track_index}"))
        for step in track.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("stepId", f"step-{order}"))
            span = step_span(step, source_text, fuzzy=fuzzy_spans)
            refs.append(
                StepRef(
                    step_id=step_id,
                    name=str(step.get("name", step_id)),
                    track_id=track_id,
                    track_index=track_index,
                    order=order,
                    step=step,
                    span=span,
                )
            )
            order += 1
    return refs


def overlap_chars(a: Span, b: Span) -> int:
    """Number of characters shared by two ``[start, end)`` ranges."""
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def overlap_ratio(a: Optional[Span], b: Optional[Span]) -> float:
    """Shared characters divided by the length of the shorter span (0..1)."""
    if a is None or b is None:
        return 0.0
    shorter = min(a[1] - a[0], b[1] - b[0])
    if shorter <= 0:
        return 0.0
    return overlap_chars(a, b) / shorter


@dataclass
class StepMatching:
    """Result of :func:`match_steps`."""

    pairs: List[Tuple[StepRef, StepRef]] = field(default_factory=list)
    unmatched_gold: List[StepRef] = field(default_factory=list)
    unmatched_pred: List[StepRef] = field(default_factory=list)
    how: Dict[str, str] = field(default_factory=dict)  # gold id -> span|name

    @property
    def gold_to_pred(self) -> Dict[str, str]:
        return {g.step_id: p.step_id for g, p in self.pairs}

    @property
    def pred_to_gold(self) -> Dict[str, str]:
        return {p.step_id: g.step_id for g, p in self.pairs}

    def as_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = [
            {"gold": g.step_id, "predicted": p.step_id, "by": self.how[g.step_id]}
            for g, p in self.pairs
        ]
        records += [
            {"gold": g.step_id, "predicted": None, "by": None}
            for g in self.unmatched_gold
        ]
        records += [
            {"gold": None, "predicted": p.step_id, "by": None}
            for p in self.unmatched_pred
        ]
        return records


def _match_by_span(
    gold_steps: List[StepRef],
    pred_steps: List[StepRef],
    threshold: float,
    result: StepMatching,
    used_gold: set,
    used_pred: set,
) -> None:
    """Pass 1: assign the best span overlaps first, one-to-one."""
    candidates = []
    for gi, gold in enumerate(gold_steps):
        if gold.span is None:
            continue
        for pi, pred in enumerate(pred_steps):
            if pred.span is None:
                continue
            ratio = overlap_ratio(gold.span, pred.span)
            if ratio >= threshold:
                shared = overlap_chars(gold.span, pred.span)
                candidates.append((-ratio, -shared, gi, pi))
    for _ratio, _shared, gi, pi in sorted(candidates):
        if gi in used_gold or pi in used_pred:
            continue
        used_gold.add(gi)
        used_pred.add(pi)
        result.pairs.append((gold_steps[gi], pred_steps[pi]))
        result.how[gold_steps[gi].step_id] = "span"


def _match_by_name(
    gold_steps: List[StepRef],
    pred_steps: List[StepRef],
    result: StepMatching,
    used_gold: set,
    used_pred: set,
) -> None:
    """Pass 2: normalised name (or id) equality for whatever is left."""
    for gi, gold in enumerate(gold_steps):
        if gi in used_gold:
            continue
        keys = {gold.normalized_name, gold.normalized_id} - {""}
        for pi, pred in enumerate(pred_steps):
            if pi in used_pred:
                continue
            if keys & ({pred.normalized_name, pred.normalized_id} - {""}):
                used_gold.add(gi)
                used_pred.add(pi)
                result.pairs.append((gold, pred))
                result.how[gold.step_id] = "name"
                break


def match_steps(
    gold_steps: List[StepRef],
    pred_steps: List[StepRef],
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> StepMatching:
    """Pair gold and predicted steps one-to-one by span overlap, then by name."""
    result = StepMatching()
    used_gold: set = set()
    used_pred: set = set()
    _match_by_span(gold_steps, pred_steps, threshold, result, used_gold, used_pred)
    _match_by_name(gold_steps, pred_steps, result, used_gold, used_pred)
    result.pairs.sort(key=lambda pair: pair[0].order)
    result.unmatched_gold = [g for i, g in enumerate(gold_steps) if i not in used_gold]
    result.unmatched_pred = [p for i, p in enumerate(pred_steps) if i not in used_pred]
    return result
