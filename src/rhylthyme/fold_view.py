#!/usr/bin/env python3
"""
Fold View — cookingforengineers.com-style recipe/process visualization.

Converts a Rhylthyme program into a compact "FoldRecipe" structure: input
rows (ingredients, or generic source steps) on one axis, and a
left-to-right chain of merge operations on the other, each spanning
exactly the rows it has incorporated by that point.

FoldRecipe shape::

    {
        "title": str,
        "prepNotes": [str, ...],
        "rows": [{"id": str, "text": str}, ...],
        "ops": [
            {"id": str, "label": str, "labelConfidence": "high"|"low",
             "inputs": [str, ...]},
            ...
        ],
        "rowSpans": {op_id: [start_index, end_index], ...},
    }

``ops[].inputs`` holds ids that reference either a row or a prior op.
Resolving an op's inputs recursively gives its full transitive leaf-row
set — i.e. its vertical span in the rendered table. Chained/cumulative
merging (an op depending on a prior op *and* a new row) inherits the
prior op's coverage automatically, with no special-casing.

``rowSpans`` is a server-computed convenience (0-based inclusive
``[start, end]`` index pairs into ``rows``) fully derivable from
``inputs`` + ``rows`` order — the format stays meaningful hand-authored
without it.

Row/prep-note/op classification (priority order):

1. Explicit ``step.metadata.fold == "prep"`` or ``step.metadata.foldLabel``
   always wins (author override).
2. A step with no upstream dependency ("root" step — its ``startTrigger``
   references no other step) that at least one other step depends on
   becomes a row (an ingredient/input entering the merge tree).
3. A step that mentions no ingredient and has no real upstream op becomes
   a prep-note (a dead-end action like "preheat oven" or "butter the
   pan") — regardless of whether it's root. A CookLang-imported recipe's
   document-order fallback often chains consecutive prep steps together
   (e.g. "preheat oven" after "butter the pan"), so the second one isn't
   root by the time it reaches here; without this rule it would silently
   vanish instead of getting its own banner row.
4. Everything else becomes an op.

When ``program.metadata.ingredients`` is present, rows are drawn from
that list (recipe mode) and ops are steps that mention an ingredient by
name or continue a prior op. Otherwise (generic mode) rows are drawn
directly from qualifying root steps.

If a row/op is consumed by more than one downstream op (e.g. "divide the
dough in half"), it is cloned into synthetic duplicate nodes — one per
consumer branch — so every op's leaf-row-set stays a contiguous interval
in the final ``rows`` order, which is required for a single rectangle to
render each op's bracket correctly.
"""

import re
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple


class FoldViewError(ValueError):
    """Raised when a program cannot be converted into a valid FoldRecipe."""


# Ordered roughly by specificity — first match against a step's text wins.
# Despite the name, not cooking-only: lab-protocol verbs are appended
# below, since generic-mode fold views (used for protocols.io imports,
# which have no cooking-specific structure) were falling back to bare,
# uninformative "Step N" labels for almost every step — none of their
# real actions ("Transfer", "Probe", "Rinse", "Centrifuge", ...) were
# recognized, even though the exact same wetlab verb pattern shows up
# across virtually every protocol on protocols.io. A curated verb list
# stays deliberately small and explicit — the alternative (fuzzy/NLP verb
# detection) was already tried and rejected elsewhere in this module for
# introducing false positives.
_COOKING_VERBS = [
    "fold in",
    "whisk",
    "melt",
    "mix",
    "stir",
    "combine",
    "beat",
    "blend",
    "knead",
    "chill",
    "simmer",
    "boil",
    "saute",
    "sauté",
    "deep-fry",
    "fry",
    "sear",
    "roast",
    "grill",
    "bake",
    "toast",
    "reduce",
    "drain",
    "strain",
    "cool",
    "rest",
    "marinate",
    "season",
    "cream",
    "sift",
    "whip",
    "toss",
    "coat",
    "layer",
    "spread",
    "brush",
    "pour",
    "sprinkle",
    "garnish",
    # Lab-protocol verbs (wetlab/molecular biology — mirrors the task
    # keyword groups already curated in rhylthyme_importers.protocolsio
    # for task-type classification, reused here for label derivation).
    "centrifuge",
    "incubate",
    "vortex",
    "sonicate",
    "lyse",
    "wash",
    "rinse",
    "block",
    "probe",
    "elute",
    "transfer",
    "resuspend",
    "precipitate",
    "denature",
    "equilibrate",
    "quantify",
    "weigh",
    "stain",
    "fix",
    "permeabilize",
    "image",
    "scan",
    "develop",
    "expose",
    "strip",
    "sequence",
    "amplify",
    "digest",
    "ligate",
    "transform",
    "plate",
    "streak",
    "culture",
    "harvest",
    "extract",
    "purify",
    "filter",
    "sterilize",
    "autoclave",
    "excise",
    "cut",
    "dilute",
    "aliquot",
    "pipette",
    "activate",
    "visualize",
    "load",
    "run",
    "add",
]

_TEMP_RE = re.compile(r"\d+\s?°[FC](?:\s*\(\d+\s?°[FC]\))?")
_ROOT_TRIGGER_TYPES = {"programStart", "programStartOffset", "manual"}
_REF_TRIGGER_TYPES = {"afterStep", "afterStepWithBuffer", "onAbort"}

# Recipe prose often refers to ingredients collectively rather than by
# name ("mix all the dry ingredients", "pour in the remaining
# ingredients") — especially when ingredient names have quantities and
# qualifiers baked in (e.g. "260g self-raising flour plus extra for
# dusting") that never appear verbatim in an instruction. A step whose
# text contains one of these explicit collective phrases is treated as
# mentioning every ingredient not yet claimed by an earlier step. This is
# a small, curated phrase list rather than generic fuzzy word-matching —
# deliberately narrow, since loosening _mentions() itself to match on a
# single significant word was tried and rejected: it produced false
# positives (e.g. "Butter and flour an 8x8-in pan" wrongly matching an
# "all-purpose flour" ingredient via its last word, even though that step
# never actually uses flour as an ingredient).
_COLLECTIVE_INGREDIENTS_RE = re.compile(
    r"\b(?:all(?:\s+the)?|the)\s+"
    r"(?:dry\s+|wet\s+|remaining\s+|rest\s+of\s+the\s+)?ingredients\b",
    re.IGNORECASE,
)

# Used to resolve "the dry ingredients" / "the wet ingredients" against a
# specific subset rather than the whole list — e.g. oil shouldn't be swept
# into a "mix the dry ingredients" step just because nothing else claimed
# it yet. Deliberately small and liquid-focused; a false negative here
# just leaves an ingredient unclaimed (falls back to a plain leftover row)
# rather than misplacing it, so this list doesn't need to be exhaustive.
_WET_INGREDIENT_RE = re.compile(
    r"\b(?:oil|water|milk|juice|vinegar|cream|buttermilk|yogurt|yoghurt|"
    r"wine|beer|broth|stock|egg|eggs)\b",
    re.IGNORECASE,
)


def _is_wet_ingredient(name: str) -> bool:
    return _WET_INGREDIENT_RE.search(name) is not None


# Strips a trailing qualifier clause ("plus extra for dusting", "for
# serving", ", to taste", "(new every time)", "<- contains other
# reagents") off an ingredient/material name so _core_noun below finds the
# actual noun rather than the last word of the qualifier, and so the
# qualifier-stripped full name can itself be matched (see _mentions).
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s*(?:,| plus | for | to serve\b| to taste\b| to garnish\b|\(|<-).*$",
    re.IGNORECASE,
)


def _core_noun(name: str) -> Optional[str]:
    core = _TRAILING_QUALIFIER_RE.sub("", name).strip()
    words = re.findall(r"[a-zA-Z][a-zA-Z'-]*", core)
    return words[-1].lower() if words else None


# Strips a leading per-unit count word ("clove garlic" -> "garlic") that
# ended up baked into an ingredient's own name field instead of its
# separate measure field — a real import was found with exactly this
# shape (name: "clove garlic", measure: "1/3 cup"). Deliberately a small,
# curated word list rather than a generic "drop the first word" rule —
# the same false-positive risk that ruled out a bare last-word fallback
# applies to a bare first-word one; scoping this to actual count/unit
# words keeps it safe.
_LEADING_COUNT_WORD_RE = re.compile(
    r"^(?:clove|cloves|sprig|sprigs|stalk|stalks|head|heads|bunch|bunches|"
    r"piece|pieces|slice|slices|leaf|leaves|stick|sticks|can|cans|"
    r"package|packages|pinch|pinches)\s+",
    re.IGNORECASE,
)


def program_to_fold(program: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Rhylthyme program into a FoldRecipe dict. See module docstring."""
    steps_by_id = _step_lookup(program)
    referenced_by_step = {
        step_id: _collect_referenced_step_ids(step.get("startTrigger"))
        for step_id, step in steps_by_id.items()
    }
    consumers_by_step: Dict[str, List[str]] = defaultdict(list)
    for step_id, refs in referenced_by_step.items():
        for ref in refs:
            consumers_by_step[ref].append(step_id)

    order = _topo_order(steps_by_id, referenced_by_step)

    ingredients = (program.get("metadata") or {}).get("ingredients") or []
    if ingredients:
        prep_notes, rows_by_id, _consumers, ops = _build_recipe_mode(
            steps_by_id, referenced_by_step, consumers_by_step, order, ingredients
        )
    else:
        prep_notes, rows_by_id, _consumers, ops = _build_generic_mode(
            steps_by_id, referenced_by_step, consumers_by_step, order
        )

    ops, rows_by_id = _apply_fanout_cloning(ops, rows_by_id)
    ordered_row_ids, row_spans = _order_rows_and_compute_spans(ops, rows_by_id)

    return {
        "title": program.get("name") or program.get("programId") or "Untitled",
        "prepNotes": prep_notes,
        "rows": [{"id": rid, "text": rows_by_id[rid]} for rid in ordered_row_ids],
        "ops": [
            {
                "id": op["id"],
                "label": op["label"],
                "labelConfidence": op["labelConfidence"],
                "inputs": op["inputs"],
            }
            for op in ops
        ],
        "rowSpans": row_spans,
    }


# --------------------------------------------------------------------------
# Dependency-graph helpers (mirrors the D3 visualizer's trigger-walking
# logic in spirit, kept self-contained here since this module must not
# depend on rhylthyme-web or any other sibling package).
# --------------------------------------------------------------------------


def _all_steps(program: Dict[str, Any]):
    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            yield step


def _step_lookup(program: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {step["stepId"]: step for step in _all_steps(program) if step.get("stepId")}


def _collect_referenced_step_ids(trigger: Optional[Dict[str, Any]]) -> List[str]:
    """Walk a (possibly compound) startTrigger and return every stepId it
    references, in encounter order. Compound triggers (logic:"all"/"any")
    recurse over their `triggers` list."""
    if not trigger:
        return []
    if "logic" in trigger:
        ids: List[str] = []
        for sub in trigger.get("triggers", []):
            ids.extend(_collect_referenced_step_ids(sub))
        return ids
    if trigger.get("type") in _REF_TRIGGER_TYPES:
        step_id = trigger.get("stepId")
        return [step_id] if step_id else []
    return []


def _topo_order(
    steps_by_id: Dict[str, Dict[str, Any]], referenced_by_step: Dict[str, List[str]]
) -> List[str]:
    """Kahn's algorithm over the startTrigger dependency graph. Falls back
    to program (tracks/steps) order for any step whose dependencies can't
    be resolved (e.g. a dangling stepId reference), so a malformed program
    still produces a best-effort ordering rather than raising."""
    indegree = {sid: 0 for sid in steps_by_id}
    forward: Dict[str, List[str]] = defaultdict(list)
    for sid, refs in referenced_by_step.items():
        for ref in refs:
            if ref in steps_by_id:
                forward[ref].append(sid)
                indegree[sid] += 1

    queue = deque(sid for sid in steps_by_id if indegree[sid] == 0)
    order: List[str] = []
    seen = set()
    while queue:
        sid = queue.popleft()
        if sid in seen:
            continue
        seen.add(sid)
        order.append(sid)
        for nxt in forward[sid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    # Anything left over is part of a cycle or unreachable — append in
    # original program order rather than dropping it.
    for sid in steps_by_id:
        if sid not in seen:
            order.append(sid)
    return order


def _is_root(refs: List[str]) -> bool:
    return len(refs) == 0


# --------------------------------------------------------------------------
# Recipe mode: program.metadata.ingredients is present.
# --------------------------------------------------------------------------


def _build_recipe_mode(
    steps_by_id,
    referenced_by_step,
    consumers_by_step,
    order,
    ingredients: List[Dict[str, Any]],
):
    rows_by_id: Dict[str, str] = {}
    ingredient_names: List[Tuple[str, str]] = []  # (row_id, name) for text matching
    used_ids: set = set()
    for ing in ingredients:
        name = (ing.get("name") or "").strip()
        if not name:
            continue
        measure = (ing.get("measure") or "").strip()
        text = f"{measure} {name}".strip() if measure else name
        row_id = _unique_slug(name, used_ids)
        rows_by_id[row_id] = text
        ingredient_names.append((row_id, name))

    prep_notes: List[str] = []
    ops: List[Dict[str, Any]] = []
    # For every step (op or prep-note), the set of "real op" ancestor ids
    # reachable by walking its references — skipping straight through any
    # prep-notes in between, since prep-notes render as banners but must
    # not sever the dependency chain. Without this, a step chained after
    # a prep-note (e.g. "combine ingredients" after "preheat oven") would
    # see an empty upstream and, if it also happened to introduce no NEW
    # ingredient (a pure continuation step like "pack into pan"), would
    # itself default to another prep-note — cascading into every
    # downstream step losing its op ancestry and the whole chain
    # collapsing into banner text with no real merge tree at all.
    op_ancestors_of: Dict[str, List[str]] = {}
    assigned_rows = set()

    for step_id in order:
        step = steps_by_id[step_id]
        refs = referenced_by_step[step_id]
        upstream_op_ids: List[str] = []
        seen_upstream = set()
        for r in refs:
            for anc in op_ancestors_of.get(r, []):
                if anc not in seen_upstream:
                    seen_upstream.add(anc)
                    upstream_op_ids.append(anc)

        text = f"{step.get('name', '')} {step.get('description', '')}"
        meta = step.get("metadata") or {}
        mentioned_ingredients_meta = meta.get("mentionedIngredients")
        collective_match = (
            None
            if mentioned_ingredients_meta
            else _COLLECTIVE_INGREDIENTS_RE.search(text)
        )
        if mentioned_ingredients_meta:
            # Authoritative override: some importers (protocols.io) can
            # tag exactly which ingredients a step uses, straight from
            # the source author's own markup, rather than requiring this
            # module to guess from prose. When present, trust it over
            # every text-matching heuristic below — it's not a guess.
            mentioned_set = {m.strip().lower() for m in mentioned_ingredients_meta}
            candidates = [
                rid
                for rid, name in ingredient_names
                if rid not in assigned_rows and name.strip().lower() in mentioned_set
            ]
        elif collective_match:
            phrase = collective_match.group(0).lower()
            if "dry" in phrase:
                candidates = [
                    rid
                    for rid, name in ingredient_names
                    if rid not in assigned_rows and not _is_wet_ingredient(name)
                ]
            elif "wet" in phrase:
                candidates = [
                    rid
                    for rid, name in ingredient_names
                    if rid not in assigned_rows and _is_wet_ingredient(name)
                ]
            else:
                candidates = [
                    rid for rid, _name in ingredient_names if rid not in assigned_rows
                ]
        else:
            candidates = [
                rid
                for rid, name in ingredient_names
                if rid not in assigned_rows and _mentions(text, name)
            ]

        # A step with no ties to the rest of the recipe (no real upstream
        # op, and nothing downstream depends on it) that mentions most of
        # the ingredient list at once isn't actually combining them — it's
        # an informational listing (e.g. a "Gather your ingredients" step
        # some importers emit as its own disconnected track). Letting it
        # claim every row it names would starve the real combining steps
        # later in the recipe of anything left to claim, collapsing the
        # entire rest of the chain into prep-notes. Such a step should
        # claim nothing (falling through to the omission below) rather
        # than hoarding rows it never structurally acts on.
        is_isolated_listing = (
            _is_root(refs)
            and not consumers_by_step.get(step_id)
            and len(ingredient_names) >= 4
            and len(candidates) >= max(4, int(len(ingredient_names) * 0.6))
        )
        mentioned = [] if is_isolated_listing else candidates

        forced_prep = meta.get("fold") == "prep"

        # An ingredient-listing step doesn't warrant a banner any more
        # than a bracket — every ingredient it names is already shown as
        # its own row, so repeating them as a prep-note is pure
        # duplication. Omit it entirely rather than showing it at all.
        if is_isolated_listing and not forced_prep:
            op_ancestors_of[step_id] = upstream_op_ids
            continue

        # A step becomes a prep-note whenever it has nothing to anchor a
        # bracket to — no ingredient mention, no real upstream op — even
        # when it's not root: a recipe's document-order fallback chains
        # consecutive prep steps together (e.g. "preheat oven" after
        # "butter the pan"), so a second prep step is rarely root by the
        # time it reaches here. Without this, such a step would silently
        # vanish from the fold view instead of showing as its own banner
        # row.
        if forced_prep or (not mentioned and not upstream_op_ids):
            prep_notes.append(step.get("description") or step.get("name") or step_id)
            # Transparent pass-through: downstream steps see straight
            # through this prep-note to whatever real op ancestry it had.
            op_ancestors_of[step_id] = upstream_op_ids
            continue

        for rid in mentioned:
            assigned_rows.add(rid)
        label, confidence = _derive_label(step)
        op_id = f"op-{step_id}"
        op_ancestors_of[step_id] = [op_id]
        ops.append(
            {
                "id": op_id,
                "label": label,
                "labelConfidence": confidence,
                "inputs": upstream_op_ids + mentioned,
                "_source_step_id": step_id,
            }
        )

    # Ingredients no step's own text ever matched still need a home —
    # swept into the LAST op in the chain rather than left as a bracket-
    # less leftover row. A real recipe was found (Crispin Original
    # Vinaigrette) where over half the ingredient list never got claimed
    # this way (abbreviations like "EVOO"/"S&P", a comma-separated list
    # naming ingredients by a shortened form, and genuinely duplicated
    # ingredient entries from a messy import all defeat literal-text
    # matching) — by the end of a recipe everything really has gone in,
    # so attaching them to the final op is a far more honest default than
    # a plain unclaimed row that no bracket ever reaches.
    unclaimed = [rid for rid, _name in ingredient_names if rid not in assigned_rows]
    if unclaimed and ops:
        ops[-1]["inputs"] = ops[-1]["inputs"] + unclaimed
    for rid in unclaimed:
        assigned_rows.add(rid)

    return prep_notes, rows_by_id, consumers_by_step, ops


def _mentions(haystack: str, name: str) -> bool:
    if not name:
        return False
    pattern = r"\b" + re.escape(name) + r"\b"
    if re.search(pattern, haystack, re.IGNORECASE):
        return True
    # Fallback: the qualifier-stripped full name (e.g. "100% ethanol" for
    # an ingredient named "100% ethanol (new every time)") — a step often
    # names an ingredient exactly except for a trailing aside the
    # ingredient list carries but prose doesn't. Only tried when there
    # actually was a qualifier to strip, so a name with none (like
    # "all-purpose flour") behaves exactly as before.
    stripped = _TRAILING_QUALIFIER_RE.sub("", name).strip()
    if stripped and stripped != name:
        if re.search(r"\b" + re.escape(stripped) + r"\b", haystack, re.IGNORECASE):
            return True
    # Fallback: the same, but for a leading count word (e.g. "garlic" for
    # an ingredient named "clove garlic") — this is a bare, un-"the"'d
    # match, safe specifically because it only strips a curated unit
    # word, not an arbitrary first word (see _LEADING_COUNT_WORD_RE).
    leading_stripped = _LEADING_COUNT_WORD_RE.sub("", name).strip()
    if leading_stripped and leading_stripped != name:
        if re.search(
            r"\b" + re.escape(leading_stripped) + r"\b", haystack, re.IGNORECASE
        ):
            return True
    # Fallback: a step referring back to an ingredient via "the <noun>"
    # rather than its full descriptive name (e.g. "Heat the oil to 170C"
    # for an ingredient named "vegetable oil for shaping and frying").
    # Requiring the definite article is what keeps this safe — it was
    # deliberately NOT made a bare last-word match, since that wrongly
    # matched "Butter and flour an 8x8-in pan" (no "the" before either
    # word) against an "all-purpose flour" ingredient.
    core = _core_noun(name)
    if core and len(core) > 2:
        definite_pattern = r"\bthe\s+" + re.escape(core) + r"\b"
        if re.search(definite_pattern, haystack, re.IGNORECASE):
            return True
    return False


def _unique_slug(text: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "row"
    slug = base
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    return slug


# --------------------------------------------------------------------------
# Generic mode: no ingredient metadata — rows come from root steps that
# feed into something; everything else is an op.
# --------------------------------------------------------------------------


def _build_generic_mode(steps_by_id, referenced_by_step, consumers_by_step, order):
    rows_by_id: Dict[str, str] = {}
    prep_notes: List[str] = []
    ops: List[Dict[str, Any]] = []
    op_id_for_step: Dict[str, str] = {}
    row_id_for_step: Dict[str, str] = {}
    used_ids: set = set()

    for step_id in order:
        step = steps_by_id[step_id]
        refs = referenced_by_step[step_id]
        meta = step.get("metadata") or {}
        forced_prep = meta.get("fold") == "prep"

        if _is_root(refs):
            has_consumer = bool(consumers_by_step.get(step_id))
            if forced_prep or not has_consumer:
                prep_notes.append(
                    step.get("description") or step.get("name") or step_id
                )
                continue
            row_id = _unique_slug(step.get("name") or step_id, used_ids)
            rows_by_id[row_id] = step.get("name") or step_id
            row_id_for_step[step_id] = row_id
            continue

        inputs = []
        for ref in refs:
            if ref in op_id_for_step:
                inputs.append(op_id_for_step[ref])
            elif ref in row_id_for_step:
                inputs.append(row_id_for_step[ref])
        if not inputs:
            # References only prep-notes/unresolved steps — nothing to
            # anchor a bracket to.
            continue

        label, confidence = _derive_label(step)
        op_id = f"op-{step_id}"
        op_id_for_step[step_id] = op_id
        ops.append(
            {
                "id": op_id,
                "label": label,
                "labelConfidence": confidence,
                "inputs": inputs,
                "_source_step_id": step_id,
            }
        )

    return prep_notes, rows_by_id, consumers_by_step, ops


# --------------------------------------------------------------------------
# Fan-out cloning: a row OR op consumed by >1 downstream op must be
# duplicated so each downstream op gets its own contiguous span.
#
# A shallow clone (duplicate just the shared node, keep pointing at the
# same underlying rows) is enough when the two consumers ultimately need
# the *same* leaf-row set. It is NOT enough when the two consumers need
# overlapping-but-different sets built on top of the shared node — e.g.
# a shared "prep veggies" step whose output separately feeds "make sauce"
# (which also pulls in 5 sauce ingredients) and "choose protein" (which
# separately branches again into chicken/tofu/shrimp, each pulling in
# their own extra ingredient). No single linear row ordering can make
# both {veggies, cornstarch} and {veggies, sauce×5} simultaneously
# contiguous while sharing the same physical "veggies" rows — this is a
# classic consecutive-ones-property violation. The general fix is to
# clone the ENTIRE transitive subgraph (not just the top node) for every
# usage past the first, so the graph becomes a true tree — a tree can
# always be laid out with contiguous intervals via a single DFS. The
# cost is visual: an ingredient needed by several independent branches
# gets its own duplicate row per branch, rather than one shared row.
# --------------------------------------------------------------------------


def _apply_fanout_cloning(ops: List[Dict[str, Any]], rows_by_id: Dict[str, str]):
    ops_by_id = {op["id"]: op for op in ops}
    rows_by_id = dict(rows_by_id)  # copy — we add clone entries below
    usage_count: Dict[str, int] = defaultdict(int)
    extra_ops: Dict[str, Dict[str, Any]] = {}
    clone_op_counter = [0]

    def deep_clone(node_id: str, suffix: str, memo: Dict[str, str]) -> str:
        """Return the id of a fully independent copy of node_id's whole
        subgraph. `memo` is scoped to one deep_clone() call tree so a
        diamond *within* the cloned subgraph still collapses to a single
        clone instance, while `suffix` is unique per top-level clone
        operation so unrelated clone operations never collide."""
        if node_id in memo:
            return memo[node_id]
        if node_id in rows_by_id:
            clone_id = f"{node_id}{suffix}"
            rows_by_id[clone_id] = rows_by_id[node_id]
            memo[node_id] = clone_id
            return clone_id
        original = ops_by_id[node_id]
        clone_id = f"{node_id}{suffix}"
        memo[node_id] = clone_id
        cloned_inputs = [deep_clone(i, suffix, memo) for i in original["inputs"]]
        extra_ops[clone_id] = {
            "id": clone_id,
            "label": original["label"],
            "labelConfidence": original["labelConfidence"],
            "inputs": cloned_inputs,
            "_source_step_id": original.get("_source_step_id"),
        }
        return clone_id

    def resolve(input_id: str) -> str:
        usage_count[input_id] += 1
        n = usage_count[input_id]
        if n == 1:
            return input_id
        clone_op_counter[0] += 1
        suffix = f"__dup{clone_op_counter[0]}"
        return deep_clone(input_id, suffix, {})

    # Clones are appended together as one contiguous block, in clone-
    # creation order, rather than topologically interleaved step-by-step
    # with the main chain. A step-by-step interleave was tried and
    # rejected: when a cloned branch mirrors the main chain one step at a
    # time (e.g. "fold 1", "fold 1 (cloned)", "fold 2", "fold 2 (cloned)",
    # ...), Kahn's algorithm naturally alternates between them, producing
    # many small shrink/grow oscillations through the middle of the
    # table instead of one clean transition. Grouping clones together
    # gives exactly one "reset" — from the end of the kept sequence to
    # the start of each clone's own independently-growing sequence —
    # which reads far more clearly than an interleaved, choppy column
    # order. For a branch needing several of its own sequential steps
    # before reconverging, that single reset is an unavoidable property
    # of laying out a DAG as one left-to-right sequence, not a bug.
    result: List[Dict[str, Any]] = []
    for op in ops:
        new_inputs = [resolve(i) for i in op["inputs"]]
        result.append({**op, "inputs": new_inputs})
    result.extend(extra_ops.values())
    return result, rows_by_id


# --------------------------------------------------------------------------
# Row ordering (dendrogram-style DFS) + span computation.
# --------------------------------------------------------------------------


def _order_rows_and_compute_spans(
    ops: List[Dict[str, Any]], rows_by_id: Dict[str, str]
):
    ops_by_id = {op["id"]: op for op in ops}
    consumed = set()
    for op in ops:
        for i in op["inputs"]:
            if i in ops_by_id:
                consumed.add(i)
    sinks = [op["id"] for op in ops if op["id"] not in consumed]

    ordered_row_ids: List[str] = []
    seen_rows = set()

    def visit(node_id: str):
        if node_id in rows_by_id:
            if node_id not in seen_rows:
                seen_rows.add(node_id)
                ordered_row_ids.append(node_id)
            return
        op = ops_by_id.get(node_id)
        if op is None:
            return
        for i in op["inputs"]:
            visit(i)

    for sink in sinks:
        visit(sink)
    # Rows never reached from any op (unmentioned ingredients) go last,
    # in their original dict order, so nothing silently disappears.
    for rid in rows_by_id:
        if rid not in seen_rows:
            seen_rows.add(rid)
            ordered_row_ids.append(rid)

    index_of = {rid: i for i, rid in enumerate(ordered_row_ids)}

    leaf_cache: Dict[str, set] = {}

    def leaves_of(node_id: str) -> set:
        if node_id in rows_by_id:
            return {node_id}
        if node_id in leaf_cache:
            return leaf_cache[node_id]
        op = ops_by_id.get(node_id)
        if op is None:
            return set()
        result: set = set()
        for i in op["inputs"]:
            result |= leaves_of(i)
        leaf_cache[node_id] = result
        return result

    row_spans: Dict[str, List[int]] = {}
    for op in ops:
        leaves = leaves_of(op["id"])
        if not leaves:
            continue
        indices = sorted(index_of[r] for r in leaves if r in index_of)
        if not indices:
            continue
        start, end = indices[0], indices[-1]
        if len(indices) != (end - start + 1):
            raise FoldViewError(
                f"Op {op['id']!r} has a non-contiguous row span "
                f"{indices} — the merge structure is not a tree even "
                f"after fan-out cloning; this indicates a bug in "
                f"program_to_fold rather than the input program."
            )
        row_spans[op["id"]] = [start, end]

    return ordered_row_ids, row_spans


# --------------------------------------------------------------------------
# Label + duration formatting.
# --------------------------------------------------------------------------


def _derive_label(step: Dict[str, Any]) -> Tuple[str, str]:
    meta = step.get("metadata") or {}
    if meta.get("foldLabel"):
        return meta["foldLabel"], "high"

    name = step.get("name", "") or ""
    description = step.get("description", "") or ""
    haystack = f"{name} {description}".lower()

    # Word-boundary match, not plain substring — "fry" as a bare substring
    # check wrongly matched inside "frying" (an ingredient's own name, e.g.
    # "vegetable oil for shaping and frying"), and the same class of bug
    # applies to "add"/"addition", "rest"/"restaurant", etc.
    #
    # Pick whichever verb occurs EARLIEST in the text, not whichever is
    # earliest in _COOKING_VERBS's own priority ordering. A real step was
    # found ("Cut Membrane at ~82kD ... cutting the membrane ... will
    # help reduce non-specific binding") where the actual action, "cut",
    # opens the step, but "reduce" — a coincidental mention buried in an
    # explanatory note — outranked it purely because it's earlier in the
    # verb list. Scanning by text position instead means whichever verb
    # the step's own text leads with wins, matching how a human would
    # read it.
    best: Optional[Tuple[int, str]] = None
    for v in _COOKING_VERBS:
        m = re.search(r"\b" + re.escape(v) + r"\b", haystack)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), v)
    verb = best[1] if best else None
    if verb is None:
        return name or step.get("stepId", ""), "low"

    label = verb
    # Prefer description over name for temperature extraction — name is
    # often a shortened derivative of description (e.g. truncated at a
    # fixed length), so searching "name description" concatenated can
    # match a truncated "350°F" in name and stop there, missing a fuller
    # "350°F (170°C)" immediately following in description.
    temp_match = _TEMP_RE.search(description) or _TEMP_RE.search(name)
    if temp_match:
        label += " " + temp_match.group(0)
    dur_text = _format_duration_range(step.get("duration"))
    if dur_text:
        label += " " + dur_text
    return label, "high"


def _to_seconds(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+)\s*(h|m|s)", s):
        matched = True
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total if matched else None


def _seconds_to_minutes_text(seconds: float) -> str:
    minutes = seconds / 60.0
    if minutes == int(minutes):
        return f"{int(minutes)} min"
    return f"{minutes:.1f} min"


def _format_duration_range(duration: Optional[Dict[str, Any]]) -> Optional[str]:
    if not duration:
        return None
    dtype = duration.get("type")
    if dtype == "fixed":
        seconds = _to_seconds(duration.get("seconds"))
        if seconds is None:
            return None
        return _seconds_to_minutes_text(seconds)
    if dtype == "variable":
        lo = _to_seconds(duration.get("minSeconds"))
        hi = _to_seconds(duration.get("maxSeconds"))
        if lo is None or hi is None:
            return None
        return f"{int(lo // 60)} to {int(hi // 60)} min"
    # "indefinite" durations have no fixed length worth printing.
    return None
