#!/usr/bin/env python3
"""Tests for the fold_view module (cookingforengineers.com-style fold view)."""

import json
from pathlib import Path

import pytest

from rhylthyme.fold_view import FoldViewError, program_to_fold

# The examples repo is checked out beside this one.
WORKSPACE = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(step_id, name, trigger, **kwargs):
    step = {"stepId": step_id, "name": name, "startTrigger": trigger}
    step.update(kwargs)
    return step


def _after(step_id):
    return {"type": "afterStep", "stepId": step_id}


def _all_of(*step_ids):
    return {"logic": "all", "triggers": [_after(sid) for sid in step_ids]}


def _brownies_program():
    """A linear-chain recipe shaped exactly like the cookingforengineers.com
    Espresso Brownies example: melt -> mix -> mix -> fold in -> bake, each
    op cumulatively spanning everything incorporated so far."""
    return {
        "name": "Espresso Brownies",
        "metadata": {
            "ingredients": [
                {"name": "unsalted butter", "measure": "4 oz (115 g)"},
                {"name": "sugar", "measure": "1 cup (200 g)"},
                {"name": "vanilla extract", "measure": "1/4 tsp. (2.5 mL)"},
                {"name": "espresso", "measure": "1 shot (60 mL)"},
                {"name": "eggs", "measure": "2 large (100 g)"},
                {"name": "all-purpose flour", "measure": "1/2 cup (80 g)"},
                {"name": "cocoa powder", "measure": "1/3 cup (80 g)"},
                {"name": "baking soda", "measure": "1/4 tsp. (1.3 g)"},
                {"name": "table salt", "measure": "1/4 tsp. (1.5 g)"},
            ]
        },
        "tracks": [
            {
                "trackId": "main",
                "name": "Main",
                "steps": [
                    _step(
                        "pan",
                        "Butter and flour an 8x8-in pan",
                        {"type": "programStart"},
                    ),
                    _step("preheat", "Preheat oven to 350F", {"type": "programStart"}),
                    _step(
                        "melt",
                        "Melt butter",
                        _after("pan"),
                        description="Melt the unsalted butter in a saucepan",
                    ),
                    _step(
                        "mix1",
                        "Mix in sugar vanilla espresso",
                        _after("melt"),
                        description="Mix in the sugar, vanilla extract, and espresso",
                    ),
                    _step(
                        "mix2",
                        "Mix in eggs",
                        _after("mix1"),
                        description="Mix in the eggs",
                    ),
                    _step(
                        "foldin",
                        "Fold in dry ingredients",
                        _after("mix2"),
                        description="Fold in the all-purpose flour, cocoa powder, "
                        "baking soda, and table salt",
                    ),
                    _step(
                        "bake",
                        "Bake",
                        _after("foldin"),
                        description="Bake at 350°F (170°C)",
                        duration={
                            "type": "variable",
                            "minSeconds": 1800,
                            "maxSeconds": 2400,
                        },
                    ),
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Recipe mode (has metadata.ingredients)
# ---------------------------------------------------------------------------


def test_brownies_linear_chain_cumulative_spans():
    fold = program_to_fold(_brownies_program())

    assert fold["title"] == "Espresso Brownies"
    assert fold["prepNotes"] == [
        "Butter and flour an 8x8-in pan",
        "Preheat oven to 350F",
    ]
    assert [r["id"] for r in fold["rows"]] == [
        "unsalted-butter",
        "sugar",
        "vanilla-extract",
        "espresso",
        "eggs",
        "all-purpose-flour",
        "cocoa-powder",
        "baking-soda",
        "table-salt",
    ]

    spans = fold["rowSpans"]
    assert spans["op-melt"] == [0, 0]
    assert spans["op-mix1"] == [0, 3]
    assert spans["op-mix2"] == [0, 4]
    assert spans["op-foldin"] == [0, 8]
    assert spans["op-bake"] == [0, 8]


def test_brownies_op_labels_and_confidence():
    fold = program_to_fold(_brownies_program())
    labels = {op["id"]: op for op in fold["ops"]}

    assert labels["op-melt"]["label"] == "melt"
    assert labels["op-melt"]["labelConfidence"] == "high"
    assert labels["op-foldin"]["label"] == "fold in"
    # Duration range + temperature both get appended to a matched verb.
    assert labels["op-bake"]["label"] == "bake 350°F (170°C) 30 to 40 min"
    assert labels["op-bake"]["labelConfidence"] == "high"


def test_label_prefers_leftmost_verb_over_priority_order():
    """A step's OWN leading action verb must win over a later, coincidental
    mention of an earlier-priority verb elsewhere in the text. A real lab
    protocol step was found where "Cut Membrane at ~82kD ... cutting the
    membrane ... will help reduce non-specific binding" picked "reduce"
    (a verb that only appears deep in an explanatory note) over "cut"
    (the step's actual, leading action) — purely because "reduce" happens
    to sit earlier in the curated verb list. The fix is to pick whichever
    verb occurs earliest in the step's own text, not whichever is
    earliest in the list."""
    program = _brownies_program()
    step = program["tracks"][0]["steps"][2]  # "melt" step
    step["name"] = "Cut the block"
    step["description"] = (
        "Cut the block of unsalted butter into pieces. Cutting it first "
        "will help reduce the melting time."
    )
    fold = program_to_fold(program)
    op = next(o for o in fold["ops"] if o["id"] == "op-melt")
    assert op["label"] == "cut"


def test_unrecognized_verb_falls_back_to_low_confidence():
    program = _brownies_program()
    # Replace the melt step's text with something no curated verb matches.
    step = program["tracks"][0]["steps"][2]
    step["name"] = "Do the butter thing"
    step["description"] = "Do the butter thing to the unsalted butter"

    fold = program_to_fold(program)
    op = next(o for o in fold["ops"] if o["id"] == "op-melt")
    assert op["labelConfidence"] == "low"
    assert op["label"] == "Do the butter thing"


def test_fold_label_metadata_override_wins():
    program = _brownies_program()
    step = program["tracks"][0]["steps"][2]
    step["metadata"] = {"foldLabel": "gently liquefy"}

    fold = program_to_fold(program)
    op = next(o for o in fold["ops"] if o["id"] == "op-melt")
    assert op["label"] == "gently liquefy"
    assert op["labelConfidence"] == "high"


def test_fold_prep_metadata_override_forces_prep_note():
    program = _brownies_program()
    # Mark "melt" as a forced prep-note even though it references an
    # ingredient and has a downstream consumer.
    program["tracks"][0]["steps"][2]["metadata"] = {"fold": "prep"}

    fold = program_to_fold(program)
    # prep-note text prefers description over name, matching the parser's
    # general text-selection precedence.
    assert "Melt the unsalted butter in a saucepan" in fold["prepNotes"]
    assert not any(op["id"] == "op-melt" for op in fold["ops"])


def test_unmentioned_ingredient_still_gets_a_row():
    program = _brownies_program()
    program["metadata"]["ingredients"].append(
        {"name": "sprinkles", "measure": "1 tbsp"}
    )
    fold = program_to_fold(program)
    row_ids = [r["id"] for r in fold["rows"]]
    assert "sprinkles" in row_ids
    # It's appended at the end since no step ever mentions it.
    assert row_ids[-1] == "sprinkles"
    # It also isn't left as a bracket-less leftover — swept into the
    # final op, same as every other never-matched ingredient.
    last_op = fold["ops"][-1]
    assert "sprinkles" in last_op["inputs"]


def test_never_matched_ingredients_swept_into_final_op():
    """A real saved recipe (Crispin Original Vinaigrette) was found where
    over half the ingredient list never got claimed by any step's own
    text — an abbreviation ("EVOO" for olive oil, "S&P" for salt and
    pepper), a comma-separated list naming ingredients by a shortened
    form ("Dijon" for "Dijon Mustard"), and duplicated ingredient entries
    from a messy import all defeat literal-text matching. Rather than
    leaving those as plain rows no bracket ever reaches, they get swept
    into the LAST op — by the end of a recipe everything really has gone
    in, so this is a more honest default than an orphaned row."""
    program = _brownies_program()
    program["metadata"]["ingredients"].append(
        {"name": "cocktail umbrella", "measure": "1"}
    )
    fold = program_to_fold(program)
    last_op = next(o for o in fold["ops"] if o["id"] == "op-bake")
    assert "cocktail-umbrella" in last_op["inputs"]
    assert fold["rowSpans"]["op-bake"] == [0, 9]


# ---------------------------------------------------------------------------
# Generic mode (no metadata.ingredients) — real cross-track merges and
# genuine fan-out, using the shape of advanced_dependencies_demo.json.
# ---------------------------------------------------------------------------


def _merge_and_fanout_program():
    return {
        "name": "Process Demo",
        "tracks": [
            {
                "trackId": "a",
                "name": "A",
                "steps": [
                    _step("start-a", "Start A", {"type": "programStart"}),
                ],
            },
            {
                "trackId": "b",
                "name": "B",
                "steps": [
                    _step("start-b", "Start B", {"type": "programStart"}),
                ],
            },
            {
                "trackId": "main",
                "name": "Main",
                "steps": [
                    _step("merge", "Merge A and B", _all_of("start-a", "start-b")),
                    _step("branch1", "Branch 1", _after("merge")),
                    _step("branch2", "Branch 2", _after("merge")),
                ],
            },
        ],
    }


def test_generic_mode_real_merge_spans_both_roots():
    fold = program_to_fold(_merge_and_fanout_program())
    # "merge" has two downstream consumers (branch1, branch2), so its
    # entire subtree — including start-a/start-b — gets deep-cloned for
    # the second branch: no single row ordering can make both branches'
    # eventual spans contiguous while sharing physical rows once they
    # diverge further (see _apply_fanout_cloning's docstring). The
    # *first* (kept, unduplicated) merge still spans both original roots.
    row_ids = {r["id"] for r in fold["rows"]}
    assert row_ids == {"start-a", "start-b", "start-a__dup1", "start-b__dup1"}
    assert fold["rowSpans"]["op-merge"] == [0, 1]


def test_generic_mode_fanout_clones_shared_ancestor():
    fold = program_to_fold(_merge_and_fanout_program())
    op_ids = {op["id"] for op in fold["ops"]}
    # branch1 keeps the original merge id; branch2 gets a fully
    # independent deep clone (its own merge op AND its own start-a/
    # start-b row clones), not just a shallow duplicate of "merge" alone.
    branch1 = next(o for o in fold["ops"] if o["id"] == "op-branch1")
    branch2 = next(o for o in fold["ops"] if o["id"] == "op-branch2")
    assert branch1["inputs"] == ["op-merge"]
    assert branch2["inputs"] != ["op-merge"]
    assert branch2["inputs"][0] in op_ids
    clone_merge = next(o for o in fold["ops"] if o["id"] == branch2["inputs"][0])
    assert clone_merge["inputs"] == ["start-a__dup1", "start-b__dup1"]
    # Both branches' spans are the same *width* (2 rows) but occupy
    # disjoint row ranges — branch2's clone owns its own physical rows.
    span1, span2 = (
        fold["rowSpans"]["op-branch1"],
        fold["rowSpans"][branch2["inputs"][0]],
    )
    assert (span1[1] - span1[0]) == (span2[1] - span2[0])
    assert span1 != span2


def test_generic_mode_dead_end_root_becomes_prep_note():
    program = _merge_and_fanout_program()
    program["tracks"].append(
        {
            "trackId": "isolated",
            "name": "Isolated",
            "steps": [
                _step("standalone", "Preheat the oven", {"type": "programStart"}),
            ],
        }
    )
    fold = program_to_fold(program)
    assert "Preheat the oven" in fold["prepNotes"]
    assert not any(r["id"] == "standalone" for r in fold["rows"])


def test_chained_prep_step_still_becomes_prep_note_not_dropped():
    """A second prep step chained after the first via afterStep (as the
    CookLang importer's document-order fallback does — 'preheat oven'
    ends up depending on 'butter the pan' even though neither touches an
    ingredient) is not root, but must still surface as its own prep-note
    banner rather than silently disappearing."""
    program = _brownies_program()
    steps = program["tracks"][0]["steps"]
    # Chain "preheat" after "pan" instead of both being independent
    # programStart roots, mirroring real CookLang import output.
    steps[1]["startTrigger"] = _after("pan")

    fold = program_to_fold(program)
    assert fold["prepNotes"] == [
        "Butter and flour an 8x8-in pan",
        "Preheat oven to 350F",
    ]


def test_forced_prep_step_ancestry_passes_through_to_downstream():
    """A step forced to a prep-note via metadata.fold, sitting mid-chain
    between a real op and a pure-continuation step that introduces no new
    ingredient, must not sever the chain. Without ancestry passing through
    the prep-note, the continuation step would see an empty upstream and
    (having no ingredient mention of its own either) would itself default
    to another prep-note — cascading every step after it into banner text
    instead of a real merge tree."""
    program = _brownies_program()
    steps = program["tracks"][0]["steps"]
    steps[1]["startTrigger"] = _after("melt")
    steps[1]["metadata"] = {"fold": "prep"}
    steps[1]["description"] = "Check the temperature"
    # "mix1" introduces no new ingredient mention here; it only continues
    # the chain started by "melt", through the forced prep-note.
    steps[3]["startTrigger"] = _after(steps[1]["stepId"])
    steps[3]["description"] = "Continue mixing"

    fold = program_to_fold(program)
    assert "Check the temperature" in fold["prepNotes"]
    op_ids = {op["id"] for op in fold["ops"]}
    assert "op-mix1" in op_ids
    mix1 = next(op for op in fold["ops"] if op["id"] == "op-mix1")
    assert "op-melt" in mix1["inputs"]


def test_leading_count_word_stripped_for_matching():
    """An ingredient name with a per-unit count word baked into the name
    field instead of kept separate in measure (a real saved recipe was
    found with name: "clove garlic", measure: "1/3 cup") must still match
    a step that says just "garlic" with no "the" — the real recipe's step
    read "s & p, and garlic", not "the garlic" or "clove garlic"
    verbatim. This is a bare, un-"the"'d match, but safe: it only strips
    a small curated set of unit words, not an arbitrary first word — a
    bare *last*-word fallback was already tried and rejected elsewhere in
    this module for causing false positives."""
    program = _brownies_program()
    program["metadata"]["ingredients"].append(
        {"name": "clove garlic", "measure": "1/3 cup"}
    )
    steps = program["tracks"][0]["steps"]
    steps[3]["description"] += ", and garlic"

    fold = program_to_fold(program)
    op = next(o for o in fold["ops"] if o["id"] == "op-mix1")
    assert "clove-garlic" in op["inputs"]


def test_isolated_listing_step_does_not_hoard_ingredient_rows():
    """A step with no dependency ties to the rest of the recipe (root,
    nothing downstream depends on it) that mentions most of the ingredient
    list at once — e.g. an importer-generated 'Gather your ingredients'
    step on its own disconnected track — must not claim those rows for
    itself, leaving every row free for whichever real step actually
    combines them first. It also gets no visual representation at all
    (no bracket, no banner) — every ingredient it names is already shown
    as its own row, so repeating them as a prep-note would be pure
    duplication. A genuine environment step like 'Preheat the oven',
    which doesn't just list ingredients, still shows as a banner."""
    program = {
        "name": "Meatloaf-shaped recipe",
        "metadata": {
            "ingredients": [
                {"name": "ground beef", "measure": "3 lb"},
                {"name": "tomato sauce", "measure": "1 cup"},
                {"name": "celery", "measure": "1 cup"},
                {"name": "onion", "measure": "1 cup"},
                {"name": "garlic", "measure": "2 clove"},
            ]
        },
        "tracks": [
            {
                "trackId": "prep",
                "name": "Preparation",
                "steps": [
                    _step(
                        "gather",
                        "Gather and prepare ingredients",
                        {"type": "programStart"},
                        description=(
                            "Gather: 3 lb ground beef, 1 cup tomato sauce, "
                            "1 cup celery, 1 cup onion, 2 clove garlic"
                        ),
                    ),
                ],
            },
            {
                "trackId": "cooking",
                "name": "Cooking Steps",
                "steps": [
                    _step(
                        "preheat",
                        "Preheat oven",
                        {"type": "programStart"},
                        description="Preheat the oven to 350 degrees F",
                    ),
                    _step(
                        "combine",
                        "Combine ingredients",
                        _after("preheat"),
                        description="Combine the ground beef, tomato sauce, "
                        "celery, onion, and garlic in a bowl",
                    ),
                    _step(
                        "pack",
                        "Pack into pan",
                        _after("combine"),
                        description="Pack firmly into the prepared pan",
                    ),
                ],
            },
        ],
    }

    fold = program_to_fold(program)
    assert not any(p.startswith("Gather:") for p in fold["prepNotes"])
    assert "Preheat the oven to 350 degrees F" in fold["prepNotes"]
    op_ids = {op["id"] for op in fold["ops"]}
    assert "op-gather" not in op_ids
    assert "op-combine" in op_ids
    assert "op-pack" in op_ids
    combine = next(op for op in fold["ops"] if op["id"] == "op-combine")
    assert set(combine["inputs"]) == {
        "ground-beef",
        "tomato-sauce",
        "celery",
        "onion",
        "garlic",
    }


def test_collective_dry_ingredients_excludes_wet_ones():
    """A step that refers to ingredients collectively ('mix all the dry
    ingredients') rather than by name must still be recognized as a real
    combining op, not fall through to a prep-note. Ingredient names that
    bake in quantities/qualifiers (a real-world import artifact — e.g.
    '260g self-raising flour plus extra for dusting') almost never appear
    verbatim in prose, so plain per-name literal matching finds nothing
    and every step collapses into banner text.

    Critically, 'the dry ingredients' must NOT sweep up a wet ingredient
    like oil just because nothing else has claimed it yet — a real recipe
    was observed doing exactly this (mixing flour/salt/sugar/baking powder
    while the oil isn't used until frying later). The oil should instead
    show up for the first time at the step that actually references it,
    via a narrow 'the <noun>' fallback (e.g. 'Heat the oil') — deliberately
    NOT a bare last-significant-word match, which was tried and reverted
    because it wrongly matched 'Butter and flour an 8x8-in pan' (no "the"
    before either word) against an 'all-purpose flour' ingredient."""
    program = {
        "name": "Jamaican fried dumplings",
        "metadata": {
            "ingredients": [
                {
                    "name": "260g self-raising flour plus extra for dusting",
                    "measure": "",
                },
                {"name": "8g salt", "measure": ""},
                {"name": "30g caster sugar", "measure": ""},
                {"name": "5g baking powder", "measure": ""},
                {"name": "vegetable oil for shaping and frying", "measure": ""},
            ]
        },
        "tracks": [
            {
                "trackId": "prep",
                "name": "Preparation",
                "steps": [
                    _step(
                        "gather",
                        "Gather and prepare ingredients",
                        {"type": "programStart"},
                        description=(
                            "Gather: 260g self-raising flour plus extra for "
                            "dusting, 8g salt, 30g caster sugar, 5g baking "
                            "powder, vegetable oil for shaping and frying"
                        ),
                    ),
                ],
            },
            {
                "trackId": "cooking",
                "name": "Cooking Steps",
                "steps": [
                    _step(
                        "mix",
                        "Dust a baking tray with flour",
                        {"type": "programStart"},
                        description="Mix all the dry ingredients in a bowl, "
                        "then gradually pour in 150ml water.",
                    ),
                    _step(
                        "fry",
                        "Fry the dumplings",
                        _after("mix"),
                        description="Heat the oil and fry the dumplings " "for 6 mins.",
                    ),
                ],
            },
        ],
    }

    fold = program_to_fold(program)
    op_ids = {op["id"] for op in fold["ops"]}
    assert "op-gather" not in op_ids
    assert "op-mix" in op_ids
    assert "op-fry" in op_ids
    mix = next(op for op in fold["ops"] if op["id"] == "op-mix")
    assert set(mix["inputs"]) == {
        "260g-self-raising-flour-plus-extra-for-dusting",
        "8g-salt",
        "30g-caster-sugar",
        "5g-baking-powder",
    }
    fry = next(op for op in fold["ops"] if op["id"] == "op-fry")
    assert "vegetable-oil-for-shaping-and-frying" in fry["inputs"]


def test_collective_reference_does_not_leak_into_unrelated_prep_step():
    """A prep step that happens to contain a word overlapping an
    ingredient's name (e.g. 'flour' in 'Butter and flour the pan') must
    stay a prep-note — the collective-phrase detector only fires on an
    explicit 'the/all ... ingredients' phrase, never on incidental word
    overlap."""
    fold = program_to_fold(_brownies_program())
    assert "Butter and flour an 8x8-in pan" in fold["prepNotes"]
    assert not any(op["id"] == "op-pan" for op in fold["ops"])


def test_mentioned_ingredients_metadata_overrides_text_matching():
    """Some importers (protocols.io) can tag exactly which ingredients a
    step uses, straight from the source author's own markup, via
    step.metadata.mentionedIngredients — an authoritative signal that
    should be trusted over every text-matching heuristic, not just used
    as one more fallback. Verify it wins even when the step's own text
    would otherwise match nothing (or match the wrong thing)."""
    program = _brownies_program()
    steps = program["tracks"][0]["steps"]
    melt_step = steps[2]
    melt_step["description"] = "Do the thing to get things going"
    melt_step["metadata"] = {"mentionedIngredients": ["unsalted butter"]}

    fold = program_to_fold(program)
    op = next(o for o in fold["ops"] if o["id"] == "op-melt")
    assert "unsalted-butter" in op["inputs"]


def test_deep_clone_required_when_branches_diverge_past_shared_ancestor():
    """Regression test for a real bug found by sweeping every example
    program: a shallow clone of just the shared node isn't enough when
    the branches sharing it need DIFFERENT additional rows past the
    shared point (not just the same leaf set again). Mirrors
    stir_fry_with_choice.json's shape: "prep veggies" feeds both "choose
    protein" (which further branches 3 ways, each adding its own unique
    ingredient) and "make sauce" (which adds 5 different ingredients) —
    no single row ordering can make both branches' spans contiguous
    without fully duplicating the shared "prep veggies" ingredients per
    branch."""
    program = {
        "name": "Fork Then Diverge",
        "tracks": [
            {
                "trackId": "main",
                "name": "Main",
                "steps": [
                    _step("prep-veg", "Prep shared veggies", {"type": "programStart"}),
                    _step("branch-a", "Branch A", _after("prep-veg")),
                    _step("extra-a", "Extra A ingredient", {"type": "programStart"}),
                    _step("combine-a", "Combine A", _all_of("branch-a", "extra-a")),
                    _step("branch-b", "Branch B", _after("prep-veg")),
                    _step("extra-b1", "Extra B1", {"type": "programStart"}),
                    _step("extra-b2", "Extra B2", {"type": "programStart"}),
                    _step(
                        "combine-b",
                        "Combine B",
                        _all_of("branch-b", "extra-b1", "extra-b2"),
                    ),
                ],
            }
        ],
    }

    fold = program_to_fold(program)  # must not raise FoldViewError

    combine_a = next(o for o in fold["ops"] if o["id"] == "op-combine-a")
    combine_b_candidates = [
        o for o in fold["ops"] if o["id"].startswith("op-combine-b")
    ]
    assert len(combine_b_candidates) == 1
    combine_b = combine_b_candidates[0]

    span_a = fold["rowSpans"][combine_a["id"]]
    span_b = fold["rowSpans"][combine_b["id"]]
    assert (span_a[1] - span_a[0] + 1) == 2  # prep-veg + extra-a
    assert (span_b[1] - span_b[0] + 1) == 3  # prep-veg (cloned) + extra-b1 + extra-b2
    # Every op's span is contiguous by construction (would have raised
    # FoldViewError otherwise) — assert nothing else silently broke.
    assert len(fold["rowSpans"]) == len(fold["ops"])


def test_advanced_dependencies_demo_fixture_converts_without_error():
    """End-to-end smoke test against a real example program shipped in the
    repo, which has both a genuine two-track merge and a genuine fan-out
    (unload-machine-a feeds both secondary-process and initial-qa)."""
    path = (
        WORKSPACE
        / "rhylthyme-examples"
        / "programs"
        / "advanced_dependencies_demo.json"
    )
    if not path.exists():
        pytest.skip("rhylthyme-examples not checked out beside this repo")
    with open(path) as f:
        program = json.load(f)

    fold = program_to_fold(program)

    assert fold["title"] == "Advanced Dependencies & Features Demo"
    # Two independent starting rows (system-startup, material-inspection),
    # each duplicated once by the deep-clone triggered at the
    # unload-machine-a fan-out (secondary-process vs initial-qa branch).
    assert len(fold["rows"]) == 4
    dup_ops = [op for op in fold["ops"] if "__dup" in op["id"]]
    assert len(dup_ops) == 5  # unload-machine-a's whole upstream chain, cloned
    # Every op's span is contiguous (would have raised FoldViewError otherwise).
    assert len(fold["rowSpans"]) == len(fold["ops"])
    # The final merge still correctly spans all 4 rows (both original and
    # cloned), confirming the two branches recombine without gaps.
    assert fold["rowSpans"]["op-final-packaging"] == [0, 3]


# ---------------------------------------------------------------------------
# Title fallback
# ---------------------------------------------------------------------------


def test_title_falls_back_to_program_id_then_untitled():
    program = _merge_and_fanout_program()
    del program["name"]
    program["programId"] = "process-demo-1"
    assert program_to_fold(program)["title"] == "process-demo-1"

    del program["programId"]
    assert program_to_fold(program)["title"] == "Untitled"
