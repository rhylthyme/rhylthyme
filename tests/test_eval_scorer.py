"""
Unit tests for the offline prompt-evaluation scorer (rhylthyme.eval).

Everything here is pure: hand-built programs, plus the gold set in
rhylthyme-examples/gold scored against itself and against a perturbed copy.
"""

import copy
import json
from pathlib import Path

import pytest

from rhylthyme.eval import metrics as M
from rhylthyme.eval.gold import (
    GoldProgram,
    count_occurrences,
    load_gold_set,
    load_predicted_set,
    locate_quote,
    locate_quote_fuzzy,
)
from rhylthyme.eval.matcher import (
    flatten_steps,
    match_steps,
    normalize_name,
    overlap_ratio,
)
from rhylthyme.eval.report import render_markdown, render_table, write_results

pytestmark = pytest.mark.unit

GOLD_DIR = Path(__file__).resolve().parent.parent.parent / "rhylthyme-examples" / "gold"
GOLD_SLUGS = (
    sorted(p.name for p in GOLD_DIR.iterdir() if (p / "program.json").exists())
    if GOLD_DIR.exists()
    else []
)

TEXT = (
    "Boil the pasta for 10 minutes. Drain. Toss with sauce for 2 minutes. "
    "Boil the pasta for 10 minutes again."
)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def mk_step(step_id, name, quote=None, occ=1, duration=600, trigger=None, task="stove"):
    step = {
        "stepId": step_id,
        "name": name,
        "startTrigger": trigger or {"type": "programStart"},
        "metadata": {},
    }
    if task:
        step["task"] = task
    if duration is not None:
        step["duration"] = (
            {"type": "fixed", "seconds": duration}
            if isinstance(duration, (int, float))
            else duration
        )
    if quote is not None:
        step["metadata"]["sourceSpan"] = {"quote": quote, "occurrence": occ}
    return step


def mk_program(tracks, resources=("stove",), actors=1):
    """tracks: {track_id: [steps]}"""
    return {
        "programId": "test",
        "name": "Test",
        "environmentType": "kitchen",
        "actors": actors,
        "resourceConstraints": [{"task": t, "maxConcurrent": 1} for t in resources],
        "tracks": [
            {"trackId": tid, "name": tid, "steps": steps}
            for tid, steps in tracks.items()
        ],
    }


def after(step_id, **extra):
    return {"type": "afterStep", "stepId": step_id, **extra}


def three_step_gold():
    return mk_program(
        {
            "main": [
                mk_step("boil", "Boil pasta", "Boil the pasta for 10 minutes"),
                mk_step(
                    "drain", "Drain pasta", "Drain.", duration=30, trigger=after("boil")
                ),
                mk_step(
                    "toss",
                    "Toss with sauce",
                    "Toss with sauce for 2 minutes",
                    duration=120,
                    trigger=after("drain"),
                ),
            ]
        }
    )


def score(gold_program, pred_program, text=TEXT, **kw):
    kw.setdefault("js", False)
    return M.score_program(gold_program, pred_program, source_text=text, **kw)


# --------------------------------------------------------------------------
# Span location
# --------------------------------------------------------------------------


class TestLocateQuote:
    def test_first_and_second_occurrence(self):
        quote = "Boil the pasta for 10 minutes"
        assert locate_quote(TEXT, quote, 1) == (0, len(quote))
        second = TEXT.index(quote, 1)
        assert locate_quote(TEXT, quote, 2) == (second, second + len(quote))
        assert locate_quote(TEXT, quote, 3) is None
        assert count_occurrences(TEXT, quote) == 2

    def test_bad_inputs(self):
        assert locate_quote(TEXT, "", 1) is None
        assert locate_quote(TEXT, "Boil", 0) is None
        assert locate_quote(TEXT, "not in text", 1) is None

    def test_non_overlapping_count(self):
        assert locate_quote("aaaa", "aa", 1) == (0, 2)
        assert locate_quote("aaaa", "aa", 2) == (2, 4)
        assert locate_quote("aaaa", "aa", 3) is None

    def test_fuzzy_tolerates_case_and_whitespace(self):
        assert locate_quote(TEXT, "boil the  pasta for 10 minutes", 1) is None
        assert locate_quote_fuzzy(TEXT, "boil the  pasta for 10 minutes", 1) == (0, 29)
        assert locate_quote_fuzzy(TEXT, "boil the  pasta for 10 minutes", 2) == (
            TEXT.index("Boil", 1),
            TEXT.index("Boil", 1) + 29,
        )
        # Exact matches still win when present.
        assert locate_quote_fuzzy(TEXT, "Drain.", 1) == locate_quote(TEXT, "Drain.", 1)


# --------------------------------------------------------------------------
# Names and matching
# --------------------------------------------------------------------------


class TestMatcher:
    def test_normalize_name(self):
        assert normalize_name("Roast the Turkey!") == "roast turkey"
        assert normalize_name("roast-turkey") == "roast turkey"
        assert normalize_name("  Bake, until golden ") == "bake golden"
        assert normalize_name(None) == ""

    def test_overlap_ratio(self):
        assert overlap_ratio((0, 10), (5, 15)) == 0.5
        assert overlap_ratio((0, 10), (10, 20)) == 0.0
        assert overlap_ratio((0, 100), (40, 50)) == 1.0
        assert overlap_ratio(None, (0, 1)) == 0.0

    def test_span_overlap_threshold(self):
        gold = flatten_steps(
            mk_program(
                {"t": [mk_step("g", "Boil pasta", "Boil the pasta for 10 minutes")]}
            ),
            TEXT,
        )
        pred = flatten_steps(
            mk_program(
                {
                    "t": [
                        mk_step(
                            "p", "Something else", "10 minutes. Drain. Toss with sauce"
                        )
                    ]
                }
            ),
            TEXT,
        )
        # 10 shared chars / 29 (shorter span) = 0.34
        assert overlap_ratio(gold[0].span, pred[0].span) == pytest.approx(10 / 29)
        assert match_steps(gold, pred).pairs == []
        low = match_steps(gold, pred, threshold=0.3)
        assert len(low.pairs) == 1 and low.how["g"] == "span"

    def test_occurrence_index_disambiguates(self):
        quote = "Boil the pasta for 10 minutes"
        gold = flatten_steps(
            mk_program({"t": [mk_step("g", "Second boil", quote, occ=2)]}), TEXT
        )
        wrong = flatten_steps(
            mk_program({"t": [mk_step("p", "Boil again", quote, occ=1)]}), TEXT
        )
        right = flatten_steps(
            mk_program({"t": [mk_step("p", "Boil again", quote, occ=2)]}), TEXT
        )
        assert match_steps(gold, wrong).pairs == []
        assert len(match_steps(gold, right).pairs) == 1

    def test_name_fallback_when_no_spans(self):
        gold = flatten_steps(
            mk_program({"t": [mk_step("g", "Roast the turkey")]}), TEXT
        )
        pred = flatten_steps(mk_program({"t": [mk_step("p", "roast turkey")]}), TEXT)
        result = match_steps(gold, pred)
        assert len(result.pairs) == 1 and result.how["g"] == "name"

    def test_id_matches_name(self):
        gold = flatten_steps(
            mk_program({"t": [mk_step("roast-turkey", "Big bird")]}), ""
        )
        pred = flatten_steps(mk_program({"t": [mk_step("x", "Roast Turkey")]}), "")
        assert len(match_steps(gold, pred).pairs) == 1

    def test_one_to_one(self):
        quote = "Toss with sauce for 2 minutes"
        gold = flatten_steps(
            mk_program({"t": [mk_step("g1", "A", quote), mk_step("g2", "B", quote)]}),
            TEXT,
        )
        pred = flatten_steps(mk_program({"t": [mk_step("p1", "C", quote)]}), TEXT)
        result = match_steps(gold, pred)
        assert len(result.pairs) == 1
        assert result.pairs[0][0].step_id == "g1"
        assert [g.step_id for g in result.unmatched_gold] == ["g2"]
        assert result.unmatched_pred == []


# --------------------------------------------------------------------------
# Durations, resources, actors
# --------------------------------------------------------------------------


def _dur(kind, *values):
    if kind == "fixed":
        return {"type": "fixed", "seconds": values[0]}
    if kind == "variable":
        return {"type": "variable", "minSeconds": values[0], "maxSeconds": values[1]}
    if kind == "indefinite":
        return {"type": "indefinite", "defaultSeconds": values[0]}
    return None


@pytest.mark.parametrize(
    "gold,pred,expected",
    [
        (_dur("fixed", 600), _dur("fixed", 700), True),  # 16.7 % off
        (_dur("fixed", 600), _dur("fixed", 720), True),  # exactly 20 %
        (_dur("fixed", 600), _dur("fixed", 800), False),
        (_dur("fixed", 600), "10m", True),  # bare string is fixed
        (_dur("fixed", 600), _dur("variable", 500, 700), False),  # kind differs
        (_dur("variable", 600, 1200), _dur("variable", 1000, 2000), True),
        (_dur("variable", 600, 900), _dur("variable", 1000, 1200), False),
        (_dur("indefinite", 60), _dur("indefinite", 500), True),
        (None, _dur("fixed", 60), False),
        (_dur("fixed", 0), _dur("fixed", 0), True),
        (_dur("fixed", 0), _dur("fixed", 10), False),
    ],
)
def test_durations_match(gold, pred, expected):
    g = {"duration": gold} if gold is not None else {}
    p = {"duration": pred} if pred is not None else {}
    assert M.durations_match(g, p) is expected


def test_duration_accuracy_over_matched_steps():
    gold = three_step_gold()
    pred = copy.deepcopy(gold)
    pred["tracks"][0]["steps"][0]["duration"]["seconds"] = 1200  # boil 10 -> 20 min
    scores = score(gold, pred)
    assert scores.durations.total == 3
    assert scores.durations.correct == 2
    assert scores.durations.accuracy == pytest.approx(2 / 3)


def test_resources_precision_recall():
    gold = mk_program({}, resources=("oven", "stovetop-burner"))
    pred = mk_program({}, resources=("Oven", "burner"))
    prf = M.score_resources(gold, pred)
    assert (prf.tp, prf.n_gold, prf.n_pred) == (1, 2, 2)
    assert prf.precision == 0.5 and prf.recall == 0.5
    empty = M.score_resources(
        mk_program({}, resources=()), mk_program({}, resources=())
    )
    assert empty.precision == 1.0 and empty.recall == 1.0


def test_actors_accuracy():
    assert M.score_actors({"actors": 2}, {"actors": 2}).accuracy == 1.0
    assert M.score_actors({"actors": 2}, {}).accuracy == 0.0
    assert M.score_actors({}, {"actors": 1}).accuracy == 1.0  # schema default


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def relation_gold():
    return mk_program(
        {
            "one": [
                mk_step("A", "A", "Boil the pasta for 10 minutes", duration=100),
                mk_step("B", "B", "Drain.", duration=200, trigger=after("A")),
                mk_step(
                    "C",
                    "C",
                    "Toss with sauce",
                    duration=50,
                    trigger=after("B", offsetSeconds=100),
                ),
            ],
            "two": [
                mk_step(
                    "D",
                    "D",
                    "Boil the pasta for 10 minutes again",
                    duration=30,
                    trigger={"type": "programStartOffset", "offsetSeconds": 300},
                ),
                mk_step(
                    "E",
                    "E",
                    "sauce for 2 minutes",
                    duration=10,
                    trigger={"logic": "all", "triggers": [after("C"), after("D")]},
                ),
            ],
        }
    )


def _rename_ids(program, suffix="2"):
    """Same program with different step ids: forces matching by span."""
    program = copy.deepcopy(program)
    ids = {s["stepId"] for t in program["tracks"] for s in t["steps"]}
    for track in program["tracks"]:
        for step in track["steps"]:
            step["stepId"] = step["stepId"] + suffix
            trig = step["startTrigger"]
            subs = trig.get("triggers", [trig])
            for sub in subs:
                if sub.get("stepId") in ids:
                    sub["stepId"] = sub["stepId"] + suffix
    return program


class TestRelationships:
    def test_flattening_counts_compound_triggers(self):
        rels = M.relations(flatten_steps(relation_gold()))
        assert len(rels) == 6
        owners = [r.owner for r in rels]
        assert owners.count("E") == 2

    def test_identical_under_renaming(self):
        gold = relation_gold()
        scores = score(gold, _rename_ids(gold))
        assert scores.steps.f1 == 1.0
        assert scores.relationships.f1 == 1.0
        assert all(m["by"] == "span" for m in scores.matching)

    def test_offset_tolerance(self):
        gold = relation_gold()
        ok = copy.deepcopy(gold)
        ok["tracks"][0]["steps"][2]["startTrigger"]["offsetSeconds"] = 105  # 5 %
        assert score(gold, ok).relationships.recall == 1.0
        bad = copy.deepcopy(gold)
        bad["tracks"][0]["steps"][2]["startTrigger"]["offsetSeconds"] = 125  # 25 %
        prf = score(gold, bad).relationships
        assert (prf.tp, prf.n_gold, prf.n_pred) == (5, 6, 6)

    def test_retarget_type_and_event_mismatches(self):
        gold = relation_gold()
        retarget = copy.deepcopy(gold)
        retarget["tracks"][0]["steps"][1]["startTrigger"] = after("D")
        assert score(gold, retarget).relationships.tp == 5

        buffered = copy.deepcopy(gold)
        buffered["tracks"][0]["steps"][1]["startTrigger"] = {
            "type": "afterStepWithBuffer",
            "stepId": "A",
            "bufferSeconds": 0,
        }
        assert score(gold, buffered).relationships.tp == 5

        event = copy.deepcopy(gold)
        event["tracks"][0]["steps"][1]["startTrigger"] = after("A", event="start")
        assert score(gold, event).relationships.tp == 5

    def test_relation_on_unmatched_step_cannot_match(self):
        gold = relation_gold()
        pred = copy.deepcopy(gold)
        pred["tracks"][0]["steps"][1]["name"] = "Something unrelated"
        del pred["tracks"][0]["steps"][1]["metadata"]["sourceSpan"]
        pred["tracks"][0]["steps"][1]["stepId"] = "Bx"
        pred["tracks"][0]["steps"][2]["startTrigger"]["stepId"] = "Bx"
        scores = score(gold, pred)
        # Bx is unsupported (no span, no match): excluded from precision, and
        # B's and C's gold relations are lost.
        assert scores.unsupported_steps == ["Bx"]
        assert scores.steps.precision == 1.0
        assert scores.steps.recall == pytest.approx(4 / 5)
        assert scores.relationships.n_pred == 5
        assert scores.relationships.tp == 4


# --------------------------------------------------------------------------
# Steps, unsupported rate, structure
# --------------------------------------------------------------------------


def test_unsupported_rate_excludes_from_precision():
    gold = three_step_gold()
    pred = copy.deepcopy(gold)
    pred["tracks"][0]["steps"].insert(
        0, mk_step("preheat", "Preheat oven", duration=300)
    )
    scores = score(gold, pred)
    assert scores.unsupported_steps == ["preheat"]
    assert scores.unsupported_rate == pytest.approx(0.25)
    assert scores.steps.precision == 1.0 and scores.steps.recall == 1.0


def test_supported_but_wrong_step_costs_precision():
    gold = three_step_gold()
    pred = copy.deepcopy(gold)
    pred["tracks"][0]["steps"].append(
        mk_step("rinse", "Rinse", "again.", duration=30, trigger=after("toss"))
    )
    scores = score(gold, pred)
    assert scores.unsupported_rate == 0.0
    assert scores.steps.precision == pytest.approx(3 / 4)
    assert scores.steps.recall == 1.0


class TestRandIndex:
    def test_cases(self):
        assert M.rand_index([1, 1, 2, 2], [1, 1, 2, 2]).value == 1.0
        assert M.rand_index([1, 1, 2, 2], ["a", "a", "b", "b"]).value == 1.0
        assert M.rand_index([1, 1, 2, 2], [1, 2, 1, 2]).value == pytest.approx(2 / 6)
        assert M.rand_index([1, 1, 1], [1, 2, 3]).value == 0.0
        assert M.rand_index([1], [2]).value == 1.0
        assert M.rand_index([], []).value == 0.0

    def test_structure_from_matching(self):
        gold = relation_gold()  # A,B,C in one track; D,E in another
        flat = copy.deepcopy(gold)
        steps = [s for t in flat["tracks"] for s in t["steps"]]
        flat["tracks"] = [{"trackId": "only", "name": "only", "steps": steps}]
        ri = score(gold, flat).structure
        assert ri.n_steps == 5 and ri.pairs == 10
        # Gold has 4 same-track pairs; the flat prediction says all 10 are.
        assert ri.value == pytest.approx(4 / 10)
        assert score(gold, gold).structure.value == 1.0


# --------------------------------------------------------------------------
# Timeline and end-to-end
# --------------------------------------------------------------------------


class TestTimeline:
    def test_makespan_and_critical_path(self):
        program = relation_gold()
        # A 0-100, B 100-300, C 400-450, D 300-330, E max(450,330)=450-460
        assert M.makespan(program) == 460
        assert M.critical_path(program) == ["A", "B", "C", "E"]

    def test_manual_trigger_follows_track_predecessor(self):
        program = mk_program(
            {
                "t": [
                    mk_step("A", "A", duration=100),
                    mk_step("Mn", "Manual", duration=50, trigger={"type": "manual"}),
                ]
            }
        )
        assert M.compute_timeline(program)["Mn"] == (100, 150)
        assert M.critical_path(program) == ["A", "Mn"]

    def test_end_to_end_self_and_failures(self):
        gold = three_step_gold()
        e2e = score(gold, gold).end_to_end
        assert e2e.python_valid, e2e.python_errors
        assert e2e.passed and e2e.js_status == "skipped"

        slow = copy.deepcopy(gold)
        slow["tracks"][0]["steps"][0]["duration"]["seconds"] = 1200  # +80 % makespan
        e2e = score(gold, slow).end_to_end
        assert not e2e.makespan_ok and not e2e.passed

        invalid = copy.deepcopy(gold)
        invalid["tracks"][0]["steps"][1]["startTrigger"]["stepId"] = "nope"
        e2e = score(gold, invalid).end_to_end
        assert not e2e.python_valid and not e2e.passed

    def test_none_prediction_scores_zero(self):
        scores = score(three_step_gold(), None)
        assert scores.error
        assert all(v == 0.0 for v in scores.headline().values()), scores.headline()
        assert scores.steps.n_gold == 3 and scores.relationships.n_gold == 3


# --------------------------------------------------------------------------
# Gold set
# --------------------------------------------------------------------------

needs_gold = pytest.mark.skipif(
    not GOLD_SLUGS, reason="rhylthyme-examples/gold not found"
)


@needs_gold
def test_gold_set_loads():
    gold_set = load_gold_set(GOLD_DIR)
    assert [g.slug for g in gold_set] == GOLD_SLUGS
    assert len(gold_set) >= 6
    for gold in gold_set:
        assert gold.source_text.strip()
        assert gold.context.get("domain")


@needs_gold
@pytest.mark.parametrize("slug", GOLD_SLUGS)
def test_gold_scores_itself_perfectly(slug):
    gold = next(g for g in load_gold_set(GOLD_DIR) if g.slug == slug)
    scores = M.score_program(gold, gold.program)
    headline = scores.headline()
    for key, value in headline.items():
        expected = 0.0 if key == "unsupported_rate" else 1.0
        assert value == expected, (key, value, scores.end_to_end.to_dict())
    assert scores.end_to_end.js_status in ("ran", "skipped")
    if scores.end_to_end.js_status == "ran":
        assert scores.end_to_end.js_makespan == pytest.approx(
            scores.end_to_end.pred_makespan
        )


@needs_gold
def test_perturbation_drops_relationships_and_recall():
    gold = next(
        g for g in load_gold_set(GOLD_DIR) if g.slug == "kitchen-thanksgiving-one-oven"
    )
    n_relations = len(M.relations(flatten_steps(gold.program, gold.source_text)))
    n_steps = len(flatten_steps(gold.program))

    pred = copy.deepcopy(gold.program)
    by_id = {s["stepId"]: s for t in pred["tracks"] for s in t["steps"]}
    # Retarget one cross-track trigger and delete one step nothing depends on.
    assert by_id["bake-casserole"]["startTrigger"]["stepId"] == "bake-stuffing"
    by_id["bake-casserole"]["startTrigger"]["stepId"] = "roast-turkey"
    for track in pred["tracks"]:
        track["steps"] = [s for s in track["steps"] if s["stepId"] != "cube-bread"]

    scores = M.score_program(gold, pred, js=False)
    assert scores.steps.recall == pytest.approx((n_steps - 1) / n_steps)
    assert scores.steps.precision == 1.0
    assert scores.relationships.recall == pytest.approx((n_relations - 2) / n_relations)
    assert scores.relationships.precision == pytest.approx(
        (n_relations - 2) / (n_relations - 1)
    )
    assert scores.relationships.f1 < 1.0
    assert scores.structure.value == 1.0
    assert scores.durations.accuracy == 1.0


@needs_gold
def test_score_set_reports_missing_predictions():
    gold_set = load_gold_set(GOLD_DIR)[:2]
    result = M.score_set(gold_set, {gold_set[0].slug: gold_set[0].program}, js=False)
    assert result.missing == [gold_set[1].slug]
    assert result.programs[1].error
    assert result.summary["steps_f1"] == pytest.approx(0.5)
    assert result.summary["end_to_end_pass"] == pytest.approx(0.5)


def test_load_predicted_set_layouts(tmp_path):
    program = three_step_gold()
    (tmp_path / "flat.json").write_text(json.dumps(program))
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "program.json").write_text(json.dumps({"program": program}))
    loaded = load_predicted_set(tmp_path, ["flat", "nested", "absent"])
    assert loaded["flat"] == program
    assert loaded["nested"] == program
    assert loaded["absent"] is None


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def test_report_rendering_and_files(tmp_path):
    gold = GoldProgram(slug="demo", program=three_step_gold(), source_text=TEXT)
    result = M.score_set([gold], {"demo": gold.program}, js=False)
    md = render_markdown(result)
    assert "| demo |" in md and "mean (n=1)" in md
    table = render_table(result)
    assert table.splitlines()[0].startswith("program")
    json_path, md_path = write_results(result, tmp_path / "out")
    data = json.loads(json_path.read_text())
    assert data["summary"]["steps_f1"] == 1.0
    assert data["programs"][0]["slug"] == "demo"
    assert md_path.read_text() == md
