"""Per-component scoring of a predicted program against a gold program.

Implements the metrics table of ``prd/prd-agent-prompt-structure.md`` (5.2):

===============  ======================  ===========================================
component        unit                    rule
===============  ======================  ===========================================
steps            steps                   span overlap >= 0.5 or normalised name; P/R/F1
durations        matched steps           kind equal; fixed within 20 %; variable
                                         intervals overlap; accuracy
resources        resourceConstraints     normalised ``task`` names; P/R
actors           ``actors``              count equal; accuracy
relationships    triggers                matched owner and anchor, same type, offset
                                         within 10 %; P/R/F1
structure        tracks                  Rand index of step->track partitions over
                                         matched steps
end-to-end       whole program           both validators pass, makespan within 10 %,
                                         critical path overlap >= 50 %; pass rate
unsupported      predicted steps         no gold match and no locatable span; rate
===============  ======================  ===========================================

Unsupported steps are reported as a rate and excluded from the precision
denominators (PRD open question 4); they are never counted as errors.

Everything here is a pure function of two program dicts (plus the gold
source text). The only I/O is the optional ``node`` subprocess used to run
the JavaScript validator, and that is skipped when node or ``schedule.js``
cannot be found.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..validate_program import (
    _signed_seconds,
    calculate_step_start_time,
    parse_duration_to_seconds,
    perform_additional_validations,
    validate_program,
)
from .gold import GoldProgram, Program
from .matcher import (
    DEFAULT_OVERLAP_THRESHOLD,
    StepMatching,
    StepRef,
    flatten_steps,
    match_steps,
    normalize_name,
)

DURATION_TOLERANCE = 0.20
OFFSET_TOLERANCE = 0.10
MAKESPAN_TOLERANCE = 0.10
CRITICAL_PATH_MIN_OVERLAP = 0.50

SCHEMA_RESOURCE = "schemas/program_schema_0.2.0-alpha.json"
SCHEDULE_JS_ENV = "RHYLTHYME_SCHEDULE_JS"


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


def _safe_div(numerator: float, denominator: float, empty: float = 0.0) -> float:
    return numerator / denominator if denominator else empty


@dataclass
class PRF:
    """Precision / recall / F1 with the counts that produced them."""

    tp: int = 0
    n_gold: int = 0
    n_pred: int = 0

    @property
    def precision(self) -> float:
        if self.n_pred == 0:
            return 1.0 if self.n_gold == 0 else 0.0
        return self.tp / self.n_pred

    @property
    def recall(self) -> float:
        if self.n_gold == 0:
            return 1.0 if self.n_pred == 0 else 0.0
        return self.tp / self.n_gold

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return _safe_div(2 * p * r, p + r)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "tp": self.tp,
            "n_gold": self.n_gold,
            "n_pred": self.n_pred,
        }


@dataclass
class Accuracy:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return _safe_div(self.correct, self.total)

    def to_dict(self) -> Dict[str, Any]:
        return {"accuracy": self.accuracy, "correct": self.correct, "total": self.total}


@dataclass
class RandIndex:
    agreements: int = 0
    pairs: int = 0
    n_steps: int = 0

    @property
    def value(self) -> float:
        if self.pairs == 0:
            return 1.0 if self.n_steps == 1 else 0.0
        return self.agreements / self.pairs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rand_index": self.value,
            "agreements": self.agreements,
            "pairs": self.pairs,
            "n_steps": self.n_steps,
        }


@dataclass
class EndToEnd:
    python_valid: bool = False
    python_errors: List[str] = field(default_factory=list)
    js_status: str = "skipped"  # "ran" | "skipped"
    js_valid: Optional[bool] = None
    js_errors: List[str] = field(default_factory=list)
    gold_makespan: float = 0.0
    pred_makespan: float = 0.0
    makespan_ok: bool = False
    gold_critical_path: List[str] = field(default_factory=list)
    pred_critical_path: List[str] = field(default_factory=list)
    js_critical_path: Optional[List[str]] = None
    js_makespan: Optional[float] = None
    critical_overlap: float = 0.0
    critical_ok: bool = False

    @property
    def passed(self) -> bool:
        validators_ok = self.python_valid and (self.js_valid is not False)
        return validators_ok and self.makespan_ok and self.critical_ok

    @property
    def pass_rate(self) -> float:
        return 1.0 if self.passed else 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data


@dataclass
class ComponentScores:
    """All component scores for one gold/predicted pair."""

    slug: str
    steps: PRF = field(default_factory=PRF)
    durations: Accuracy = field(default_factory=Accuracy)
    resources: PRF = field(default_factory=PRF)
    actors: Accuracy = field(default_factory=Accuracy)
    relationships: PRF = field(default_factory=PRF)
    structure: RandIndex = field(default_factory=RandIndex)
    end_to_end: EndToEnd = field(default_factory=EndToEnd)
    unsupported_rate: float = 0.0
    unsupported_steps: List[str] = field(default_factory=list)
    matching: List[Dict[str, Any]] = field(default_factory=list)
    # Free-form slot for later phases (T1/T2 2/1/0 turn scores, cost, model).
    extras: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    HEADLINE_KEYS = (
        "steps_precision",
        "steps_recall",
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

    def headline(self) -> Dict[str, float]:
        """Flat dict of the numbers that go in the summary table."""
        return {
            "steps_precision": self.steps.precision,
            "steps_recall": self.steps.recall,
            "steps_f1": self.steps.f1,
            "durations_accuracy": self.durations.accuracy,
            "resources_precision": self.resources.precision,
            "resources_recall": self.resources.recall,
            "actors_accuracy": self.actors.accuracy,
            "relationships_precision": self.relationships.precision,
            "relationships_recall": self.relationships.recall,
            "relationships_f1": self.relationships.f1,
            "structure_rand_index": self.structure.value,
            "end_to_end_pass": self.end_to_end.pass_rate,
            "unsupported_rate": self.unsupported_rate,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "headline": self.headline(),
            "steps": self.steps.to_dict(),
            "durations": self.durations.to_dict(),
            "resources": self.resources.to_dict(),
            "actors": self.actors.to_dict(),
            "relationships": self.relationships.to_dict(),
            "structure": self.structure.to_dict(),
            "end_to_end": self.end_to_end.to_dict(),
            "unsupported_rate": self.unsupported_rate,
            "unsupported_steps": list(self.unsupported_steps),
            "matching": list(self.matching),
            "extras": dict(self.extras),
            "error": self.error,
        }

    @classmethod
    def zero(
        cls, slug: str, error: str, gold: Optional[Program] = None
    ) -> "ComponentScores":
        """A scored zero for a run that produced no usable program.

        The gold counts are filled in so every precision/recall reads 0.0
        (an empty prediction against a non-empty gold), not the 1.0 that
        empty-versus-empty would give.
        """
        scores = cls(slug=slug, error=error)
        scores.actors = Accuracy(correct=0, total=1)
        if gold is not None:
            gold_steps = flatten_steps(gold)
            scores.steps = PRF(tp=0, n_gold=len(gold_steps), n_pred=0)
            scores.durations = Accuracy(correct=0, total=len(gold_steps))
            scores.resources = PRF(tp=0, n_gold=len(resource_names(gold)), n_pred=0)
            scores.relationships = PRF(
                tp=0, n_gold=len(relations(gold_steps)), n_pred=0
            )
        return scores


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------


def duration_info(step: Dict[str, Any]) -> Tuple[str, Tuple[float, ...]]:
    """Return ``(kind, values)`` for a step's duration.

    ``fixed`` -> (seconds,), ``variable`` -> (min, max), ``indefinite`` ->
    (default,), missing or malformed -> ("none", ()).
    """
    duration = step.get("duration")
    if isinstance(duration, (int, float, str)):
        return "fixed", (float(parse_duration_to_seconds(duration)),)
    if not isinstance(duration, dict):
        return "none", ()
    kind = duration.get("type")
    try:
        if kind == "fixed":
            value = duration.get("seconds", duration.get("timeString"))
            return "fixed", (float(parse_duration_to_seconds(value)),)
        if kind == "variable":
            return "variable", (
                float(parse_duration_to_seconds(duration.get("minSeconds", 0))),
                float(parse_duration_to_seconds(duration.get("maxSeconds", 0))),
            )
        if kind == "indefinite":
            return "indefinite", (
                float(parse_duration_to_seconds(duration.get("defaultSeconds", 0))),
            )
    except Exception:  # noqa: BLE001 - malformed value is a scoring miss
        return "none", ()
    return "none", ()


def _within(pred: float, gold: float, tolerance: float) -> bool:
    if gold == 0:
        return pred == 0
    return abs(pred - gold) <= tolerance * abs(gold)


def durations_match(gold_step: Dict[str, Any], pred_step: Dict[str, Any]) -> bool:
    """Kind equal and value within 20 % (fixed) / intervals overlap (variable)."""
    gold_kind, gold_values = duration_info(gold_step)
    pred_kind, pred_values = duration_info(pred_step)
    if gold_kind != pred_kind:
        return False
    if gold_kind == "fixed":
        return _within(pred_values[0], gold_values[0], DURATION_TOLERANCE)
    if gold_kind == "variable":
        g_min, g_max = gold_values
        p_min, p_max = pred_values
        return max(g_min, p_min) <= min(g_max, p_max)
    # indefinite (or none): the kind is the signal; defaults are guesses.
    return True


def score_durations(matching: StepMatching) -> Accuracy:
    correct = sum(1 for g, p in matching.pairs if durations_match(g.step, p.step))
    return Accuracy(correct=correct, total=len(matching.pairs))


# --------------------------------------------------------------------------
# Resources and actors
# --------------------------------------------------------------------------


def resource_names(program: Program) -> set:
    names = set()
    for constraint in program.get("resourceConstraints", []) or []:
        if isinstance(constraint, dict):
            name = normalize_name(constraint.get("task"))
            if name:
                names.add(name)
    return names


def score_resources(gold: Program, pred: Program) -> PRF:
    gold_names, pred_names = resource_names(gold), resource_names(pred)
    return PRF(
        tp=len(gold_names & pred_names), n_gold=len(gold_names), n_pred=len(pred_names)
    )


def actor_count(program: Program) -> int:
    value = program.get("actors", 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def score_actors(gold: Program, pred: Program) -> Accuracy:
    return Accuracy(correct=int(actor_count(gold) == actor_count(pred)), total=1)


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    """One trigger, flattened: who waits, on what, how, and by how much."""

    owner: str
    type: str
    anchor: Optional[str]
    offset: float
    event: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _trigger_relations(owner: str, trigger: Any) -> List[Relation]:
    if not isinstance(trigger, dict):
        return [Relation(owner, "programStart", None, 0.0, "end")]
    if "logic" in trigger and isinstance(trigger.get("triggers"), list):
        relations: List[Relation] = []
        for sub in trigger["triggers"]:
            relations.extend(_trigger_relations(owner, sub))
        return relations
    kind = str(trigger.get("type", "programStart"))
    offset = _signed_seconds(trigger.get("offsetSeconds", 0))
    if kind == "afterStepWithBuffer":
        offset += _signed_seconds(trigger.get("bufferSeconds", 0))
    anchor = trigger.get("stepId") if kind in ANCHORED_TRIGGERS else None
    event = str(trigger.get("event", "end")) if anchor else "end"
    return [Relation(owner, kind, anchor, float(offset), event)]


ANCHORED_TRIGGERS = frozenset({"afterStep", "afterStepWithBuffer", "onAbort"})


def relations(steps: Sequence[StepRef]) -> List[Relation]:
    """Every trigger in a program as a flat relation list (compound flattened)."""
    result: List[Relation] = []
    for ref in steps:
        result.extend(_trigger_relations(ref.step_id, ref.step.get("startTrigger")))
    return result


def relation_matches(
    gold: Relation, pred: Relation, gold_to_pred: Mapping[str, str]
) -> bool:
    """Same owner (via matching), same type, same anchor (via matching),
    same event, offset within 10 %."""
    if gold_to_pred.get(gold.owner) != pred.owner:
        return False
    if gold.type != pred.type or gold.event != pred.event:
        return False
    if gold.anchor is None:
        if pred.anchor is not None:
            return False
    elif gold_to_pred.get(gold.anchor) != pred.anchor:
        return False
    return _within(pred.offset, gold.offset, OFFSET_TOLERANCE)


def score_relationships(
    gold_steps: Sequence[StepRef],
    pred_steps: Sequence[StepRef],
    matching: StepMatching,
    unsupported: Iterable[str] = (),
) -> PRF:
    gold_relations = relations(gold_steps)
    unsupported_ids = set(unsupported)
    pred_relations = relations(pred_steps)
    counted_pred = [r for r in pred_relations if r.owner not in unsupported_ids]
    gold_to_pred = matching.gold_to_pred
    used: set = set()
    tp = 0
    for gold_relation in gold_relations:
        for index, pred_relation in enumerate(counted_pred):
            if index in used:
                continue
            if relation_matches(gold_relation, pred_relation, gold_to_pred):
                used.add(index)
                tp += 1
                break
    return PRF(tp=tp, n_gold=len(gold_relations), n_pred=len(counted_pred))


# --------------------------------------------------------------------------
# Structure (Rand index)
# --------------------------------------------------------------------------


def rand_index(labels_a: Sequence[Any], labels_b: Sequence[Any]) -> RandIndex:
    """Rand index between two labelings of the same items."""
    if len(labels_a) != len(labels_b):
        raise ValueError("labelings must have the same length")
    n = len(labels_a)
    agreements = 0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            pairs += 1
            same_a = labels_a[i] == labels_a[j]
            same_b = labels_b[i] == labels_b[j]
            if same_a == same_b:
                agreements += 1
    return RandIndex(agreements=agreements, pairs=pairs, n_steps=n)


def score_structure(matching: StepMatching) -> RandIndex:
    gold_tracks = [g.track_id for g, _ in matching.pairs]
    pred_tracks = [p.track_id for _, p in matching.pairs]
    return rand_index(gold_tracks, pred_tracks)


# --------------------------------------------------------------------------
# Steps and unsupported rate
# --------------------------------------------------------------------------


def unsupported_step_ids(matching: StepMatching) -> List[str]:
    """Predicted steps with no gold match and no span located in the source."""
    return [p.step_id for p in matching.unmatched_pred if p.span is None]


def score_steps(matching: StepMatching, unsupported: Iterable[str] = ()) -> PRF:
    n_unsupported = len(set(unsupported))
    n_pred = len(matching.pairs) + len(matching.unmatched_pred) - n_unsupported
    return PRF(
        tp=len(matching.pairs),
        n_gold=len(matching.pairs) + len(matching.unmatched_gold),
        n_pred=n_pred,
    )


# --------------------------------------------------------------------------
# Timeline: makespan and critical path with the Python resolver
# --------------------------------------------------------------------------


def compute_timeline(program: Program) -> Dict[str, Tuple[float, float]]:
    """``{stepId: (start, end)}`` in seconds from program start."""
    timeline: Dict[str, Tuple[float, float]] = {}
    for track in program.get("tracks", []) or []:
        steps = track.get("steps", []) or []
        for step in steps:
            if not isinstance(step, dict) or "stepId" not in step:
                continue
            try:
                start = float(calculate_step_start_time(step, steps, program))
                duration = float(parse_duration_to_seconds(step.get("duration", "0s")))
            except Exception:  # noqa: BLE001 - unresolvable step counts as t=0
                start, duration = 0.0, 0.0
            timeline[step["stepId"]] = (start, start + duration)
    return timeline


def makespan(program: Program) -> float:
    timeline = compute_timeline(program)
    return max((end for _, end in timeline.values()), default=0.0)


def _binding_predecessor(
    step: Dict[str, Any],
    track_steps: List[Dict[str, Any]],
    program: Program,
    trigger: Any = None,
) -> Optional[str]:
    """The step whose timing determines this step's start, if any."""
    trigger = step.get("startTrigger", {}) if trigger is None else trigger
    if not isinstance(trigger, dict):
        return None
    if "logic" in trigger and isinstance(trigger.get("triggers"), list):
        best: Optional[Tuple[float, Any]] = None
        pick_max = trigger.get("logic") != "any"
        for sub in trigger["triggers"]:
            time = calculate_step_start_time(
                {"startTrigger": sub}, track_steps, program
            )
            if best is None or (time > best[0] if pick_max else time < best[0]):
                best = (time, sub)
        return (
            _binding_predecessor(step, track_steps, program, best[1]) if best else None
        )
    kind = trigger.get("type", "programStart")
    if kind in ANCHORED_TRIGGERS:
        return trigger.get("stepId")
    if kind in ("manual", "previousStepComplete"):
        previous = None
        for candidate in track_steps:
            if candidate.get("stepId") == step.get("stepId"):
                return previous
            previous = candidate.get("stepId")
    return None


def critical_path(program: Program) -> List[str]:
    """Chain of step ids that determines the makespan, in chronological order.

    Starts at the step that ends last and walks back through each step's
    binding predecessor (trigger anchor, or previous step in track for
    manual triggers) until a step anchored to program start.
    """
    timeline = compute_timeline(program)
    if not timeline:
        return []
    by_id: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
    for track in program.get("tracks", []) or []:
        steps = track.get("steps", []) or []
        for step in steps:
            if isinstance(step, dict) and "stepId" in step:
                by_id[step["stepId"]] = (step, steps)
    last_id = max(timeline, key=lambda sid: timeline[sid][1])
    chain: List[str] = []
    seen: set = set()
    current: Optional[str] = last_id
    while current is not None and current in by_id and current not in seen:
        seen.add(current)
        chain.append(current)
        step, track_steps = by_id[current]
        current = _binding_predecessor(step, track_steps, program)
    chain.reverse()
    return chain


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def _repo_root_candidates() -> List[Path]:
    here = Path(__file__).resolve()
    roots = [parent for parent in here.parents]
    roots.append(Path.cwd())
    roots.append(Path.cwd().parent)
    return roots


@lru_cache(maxsize=1)
def default_schema_path() -> Optional[Path]:
    """The 0.2.0-alpha schema from ``rhylthyme_spec`` or the monorepo."""
    try:
        from importlib import resources

        candidate = Path(
            str(resources.files("rhylthyme_spec").joinpath(SCHEMA_RESOURCE))
        )
        if candidate.exists():
            return candidate
    except Exception:  # noqa: BLE001 - fall through to the repo layout
        pass
    for root in _repo_root_candidates():
        candidate = root / "rhylthyme-spec" / "src" / "rhylthyme_spec" / SCHEMA_RESOURCE
        if candidate.exists():
            return candidate
    return None


@lru_cache(maxsize=4)
def load_schema(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    schema_path = Path(path) if path else default_schema_path()
    if schema_path is None or not schema_path.exists():
        return None
    with open(schema_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_python(
    program: Program, schema: Optional[Dict[str, Any]] = None
) -> Tuple[bool, List[str]]:
    """Schema plus logic validation with the cli-runner's validator."""
    if schema is None:
        schema = load_schema()
    errors: List[str] = []
    if schema is None:
        errors.append("schema not found")
        return False, errors
    try:
        ok, schema_errors = validate_program(program, schema)
    except Exception as exc:  # noqa: BLE001
        return False, [f"schema validation raised: {exc}"]
    if not ok:
        errors.extend(schema_errors)
    try:
        errors.extend(perform_additional_validations(program))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"logic validation raised: {exc}")
    return not errors, errors


@lru_cache(maxsize=1)
def find_schedule_js() -> Optional[Path]:
    """Locate ``rhylthyme-server/mcp-api/schedule.js`` (or ``$RHYLTHYME_SCHEDULE_JS``)."""
    env = os.environ.get(SCHEDULE_JS_ENV)
    if env:
        path = Path(env)
        return path if path.exists() else None
    for root in _repo_root_candidates():
        candidate = root / "rhylthyme-server" / "mcp-api" / "schedule.js"
        if candidate.exists():
            return candidate.resolve()
    return None


@lru_cache(maxsize=1)
def node_executable() -> Optional[str]:
    return shutil.which("node")


_NODE_SCRIPT = r"""
const s = require(process.argv[1]);
let data = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => { data += d; });
process.stdin.on("end", () => {
  let out = { valid: false, errors: [], makespanSeconds: null, criticalPath: null };
  try {
    const program = JSON.parse(data);
    const v = s.validateProgram(program);
    out.valid = !!v.valid;
    out.errors = (v.errors || []).map((e) => e.message || String(e));
    if (out.valid) {
      const a = s.analyzeSchedule(program);
      out.makespanSeconds = a.makespanSeconds;
      out.criticalPath = a.criticalPath;
    }
  } catch (e) {
    out.errors.push(String(e && e.message ? e.message : e));
  }
  process.stdout.write(JSON.stringify(out));
});
"""


@dataclass
class JsResult:
    valid: bool
    errors: List[str]
    makespan: Optional[float]
    critical_path: Optional[List[str]]


def validate_js(program: Program, timeout: float = 30.0) -> Optional[JsResult]:
    """Run ``schedule.js`` on the program. ``None`` when node or the file is absent."""
    node = node_executable()
    schedule = find_schedule_js()
    if node is None or schedule is None:
        return None
    try:
        completed = subprocess.run(
            [node, "-e", _NODE_SCRIPT, str(schedule)],
            input=json.dumps(program),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        payload = json.loads(completed.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return JsResult(
            valid=False,
            errors=[f"node failed: {exc}"],
            makespan=None,
            critical_path=None,
        )
    return JsResult(
        valid=bool(payload.get("valid")),
        errors=list(payload.get("errors") or []),
        makespan=payload.get("makespanSeconds"),
        critical_path=payload.get("criticalPath"),
    )


def score_end_to_end(
    gold: Program,
    pred: Program,
    matching: StepMatching,
    schema: Optional[Dict[str, Any]] = None,
    js: bool = True,
) -> EndToEnd:
    result = EndToEnd()
    result.python_valid, result.python_errors = validate_python(pred, schema)
    if js:
        js_result = validate_js(pred)
        if js_result is not None:
            result.js_status = "ran"
            result.js_valid = js_result.valid
            result.js_errors = js_result.errors
            result.js_makespan = js_result.makespan
            result.js_critical_path = js_result.critical_path
    result.gold_makespan = makespan(gold)
    result.pred_makespan = makespan(pred)
    result.makespan_ok = _within(
        result.pred_makespan, result.gold_makespan, MAKESPAN_TOLERANCE
    )
    result.gold_critical_path = critical_path(gold)
    result.pred_critical_path = critical_path(pred)
    pred_to_gold = matching.pred_to_gold
    mapped = {pred_to_gold.get(sid) for sid in result.pred_critical_path} - {None}
    gold_set = set(result.gold_critical_path)
    result.critical_overlap = _safe_div(len(gold_set & mapped), len(gold_set), 1.0)
    result.critical_ok = result.critical_overlap >= CRITICAL_PATH_MIN_OVERLAP
    return result


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def score_program(
    gold: GoldProgram | Program,
    predicted: Optional[Program],
    *,
    source_text: Optional[str] = None,
    slug: Optional[str] = None,
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    js: bool = True,
    schema: Optional[Dict[str, Any]] = None,
) -> ComponentScores:
    """Score one predicted program against one gold program.

    ``gold`` may be a :class:`GoldProgram` or a raw program dict (then pass
    ``source_text``). A ``None`` prediction scores zero on every component.
    """
    if isinstance(gold, GoldProgram):
        gold_program = gold.program
        text = gold.source_text if source_text is None else source_text
        slug = slug or gold.slug
    else:
        gold_program = gold
        text = source_text or ""
        slug = slug or str(gold_program.get("programId", "program"))

    if not isinstance(predicted, dict):
        return ComponentScores.zero(slug, "no predicted program", gold_program)

    gold_steps = flatten_steps(gold_program, text)
    pred_steps = flatten_steps(predicted, text, fuzzy_spans=True)
    matching = match_steps(gold_steps, pred_steps, threshold=threshold)
    unsupported = unsupported_step_ids(matching)

    scores = ComponentScores(slug=slug)
    scores.steps = score_steps(matching, unsupported)
    scores.durations = score_durations(matching)
    scores.resources = score_resources(gold_program, predicted)
    scores.actors = score_actors(gold_program, predicted)
    scores.relationships = score_relationships(
        gold_steps, pred_steps, matching, unsupported
    )
    scores.structure = score_structure(matching)
    scores.end_to_end = score_end_to_end(
        gold_program, predicted, matching, schema=schema, js=js
    )
    scores.unsupported_steps = unsupported
    scores.unsupported_rate = _safe_div(len(unsupported), len(pred_steps))
    scores.matching = matching.as_records()
    return scores


@dataclass
class SetResult:
    """Scores for a whole gold set plus their mean."""

    programs: List[ComponentScores] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> Dict[str, float]:
        return summarize(self.programs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meta": dict(self.meta),
            "summary": self.summary,
            "programs": [scores.to_dict() for scores in self.programs],
            "missing": list(self.missing),
        }


def summarize(scores: Iterable[ComponentScores]) -> Dict[str, float]:
    """Mean of every headline metric over the given programs."""
    rows = [s.headline() for s in scores]
    if not rows:
        return {key: 0.0 for key in ComponentScores.HEADLINE_KEYS}
    return {
        key: sum(row[key] for row in rows) / len(rows)
        for key in ComponentScores.HEADLINE_KEYS
    }


def score_set(
    gold_set: Iterable[GoldProgram],
    predicted: Mapping[str, Optional[Program]],
    *,
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    js: bool = True,
    schema: Optional[Dict[str, Any]] = None,
) -> SetResult:
    """Score every gold program against ``predicted[slug]``.

    A missing prediction is scored as zero and listed in ``missing`` so a
    failed run drags the summary down instead of silently shrinking it.
    """
    result = SetResult()
    for gold in gold_set:
        program = predicted.get(gold.slug)
        if program is None:
            result.missing.append(gold.slug)
        result.programs.append(
            score_program(gold, program, threshold=threshold, js=js, schema=schema)
        )
    return result
