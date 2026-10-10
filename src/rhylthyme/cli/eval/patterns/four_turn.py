"""``four-turn`` pattern: the four-message ``plan_schedule`` prompt.

The turn templates are a byte-identical copy of
``rhylthyme-server/mcp-api/prompts.js`` -- the same strings the remote MCP
server sends -- so the prompt under test is the prompt that ships.
``tests/test_eval_harness.py::TestJsParity`` extracts the JS exports with
``node`` and fails when the two drift; regenerate this file's copy with::

    python -m rhylthyme.cli.eval.patterns.four_turn --check

The structure follows Almuntashiri, Ibanez & Chapman (ProvenanceWeek '25):

    T1 read-back      confirm the whole source was read
    T2 model check    restate the target data model, name the constraints
    T3 extraction     scenario pattern; steps + source spans, no relations
    T4 relationships  tracks, triggers, question refinement, then the program

T1 and T2 are scored 2/1/0 as the paper does: 2 when the reply carries the
expected JSON shape first time, 1 when a single retry turn recovers it, 0
when it never does. A failing run can then be attributed to reading,
modelling, extraction or relationships.

The system prompt is deliberately the *same* as the ``baseline`` pattern's
(harness preamble + the frozen Phase-2 server instructions + the authoring
guide), apart from the preamble sentence that tells the model the turns
arrive one at a time, so the only variable between the two patterns is the
shape of the user turns.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from rhylthyme.eval.gold import GoldProgram, Program

from . import Pattern, PatternResult, Turn, finish_with_fix_loop
from .baseline import (
    ONE_SHOT,
    authoring_guide,
    constraints_from_context,
    render_server_instructions,
    vertical_for,
)
from .extract import _candidates as _json_candidates
from .extract import extract_program

# ==========================================================================
# BEGIN copy of rhylthyme-server/mcp-api/prompts.js -- do not edit by hand.
# Edit the JS, then re-run the parity check. Everything between the BEGIN
# and END markers is the same text, in Python spelling.
# ==========================================================================

#: A slot marker is ``{name}``; JSON braces in the templates never match.
SLOT_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


def slots_in(template: str) -> List[str]:
    """The slot names a template asks for, in first-appearance order."""
    names: List[str] = []
    for match in SLOT_RE.finditer(template):
        if match.group(1) not in names:
            names.append(match.group(1))
    return names


def render(template: str, slots: Optional[Dict[str, Any]] = None) -> str:
    """Fill ``{slot}`` markers; raise on an unknown or a missing slot."""
    values = dict(slots or {})
    wanted = slots_in(template)
    unknown = [key for key in values if key not in wanted]
    if unknown:
        raise KeyError(
            "render: unknown slot(s) "
            + ", ".join(unknown)
            + "; template takes "
            + (", ".join(wanted) or "no slots")
        )
    missing = [key for key in wanted if key not in values]
    if missing:
        raise KeyError("render: missing slot(s) " + ", ".join(missing))
    return SLOT_RE.sub(lambda m: str(values[m.group(1)]), template)


EXPECTED_T1 = """{
  "summary": "one paragraph: what is being made, for how many, by when, under what constraints",
  "servesOrScale": "8 people",
  "deadline": "2026-11-26T18:00:00",
  "constraints": ["one oven", "two burners", "two cooks"]
}"""

EXPECTED_T2 = """{
  "acknowledgement": "tracks are sequential lines of work; one duration kind per step; triggers link steps; every task needs a resourceConstraint; stepIds are global",
  "resourceConstraints": [
    { "task": "oven", "maxConcurrent": 1 },
    { "task": "burner", "maxConcurrent": 2 },
    { "task": "prep", "maxConcurrent": 2 }
  ],
  "actors": 2
}"""

EXPECTED_T3 = """{
  "steps": [
    {
      "stepId": "roast-turkey",
      "name": "Roast the turkey",
      "task": "oven",
      "duration": { "type": "fixed", "seconds": 10800 },
      "sourceSpan": { "quote": "Roast the turkey for 3 hours", "occurrence": 1 },
      "inferred": false
    },
    {
      "stepId": "rest-turkey",
      "name": "Rest the turkey",
      "task": "counter",
      "duration": { "type": "variable", "minSeconds": 1200, "maxSeconds": 2400, "defaultSeconds": 1800 },
      "sourceSpan": { "quote": "let it rest", "occurrence": 1 },
      "inferred": false
    },
    {
      "stepId": "preheat-oven",
      "name": "Preheat the oven",
      "task": "oven",
      "duration": { "type": "fixed", "seconds": 900 },
      "sourceSpan": null,
      "inferred": true
    }
  ]
}"""

EXPECTED_T4 = """{
  "schemaVersion": "0.3.0-alpha",
  "programId": "kebab-case-id",
  "name": "Human title",
  "environmentType": "kitchen",
  "actors": 2,
  "tracks": [
    {
      "trackId": "turkey",
      "name": "Turkey",
      "steps": [
        {
          "stepId": "preheat-oven",
          "name": "Preheat the oven",
          "task": "oven",
          "duration": { "type": "fixed", "seconds": 900 },
          "startTrigger": { "type": "programStart" },
          "metadata": { "sourceSpan": null, "inferred": true }
        },
        {
          "stepId": "roast-turkey",
          "name": "Roast the turkey",
          "task": "oven",
          "duration": { "type": "fixed", "seconds": 10800 },
          "startTrigger": { "type": "afterStep", "stepId": "preheat-oven" },
          "metadata": { "sourceSpan": { "quote": "Roast the turkey for 3 hours", "occurrence": 1 }, "inferred": false }
        }
      ]
    }
  ],
  "resourceConstraints": [
    { "task": "oven", "maxConcurrent": 1 },
    { "task": "counter", "maxConcurrent": 2 }
  ],
  "metadata": { "serves": "8" }
}"""

EXPECTED_OUTPUT: Dict[str, str] = {
    "T1": EXPECTED_T1,
    "T2": EXPECTED_T2,
    "T3": EXPECTED_T3,
    "T4": EXPECTED_T4,
}

T1_READBACK = """Turn 1 of 4 — read-back. Do not write any program JSON in this turn.

You are going to turn a {kind} into a Rhylthyme program: parallel **tracks** of sequential **steps**, each with a duration and a start trigger, that one person follows against a live clock.

Plan this as a Rhylthyme {kind} and deliver a live timeline: {goal}
{constraintsLine}
{deadlineLine}

{source}

Read all of it before you answer. Then say back, in one paragraph and in your own words: what is being made, for how many (or at what scale), by when, and under what constraints — equipment that exists only once, how many people are working, anything that has to be held, rested or ready at a particular moment. If the text you were given looks truncated, or points at a section that is not here, say so instead of filling the gap.

End your reply with the same read-back as JSON in a single ```json fenced block:

```json
{
  "summary": "one paragraph: what is being made, for how many, by when, under what constraints",
  "servesOrScale": "8 people",
  "deadline": "2026-11-26T18:00:00",
  "constraints": ["one oven", "two burners", "two cooks"]
}
```"""

T2_MODEL_CHECK = """Turn 2 of 4 — model check. Still no program JSON.

Before extracting anything, restate the target data model in your own words, in five or six sentences:

- a **track** is one sequential line of work; steps in the same track never overlap, and anything that happens while something else happens belongs in a different track;
- a **step** has exactly one duration kind: `fixed` (`seconds`), `variable` (`minSeconds`, `maxSeconds`, `defaultSeconds`) or `indefinite` (runs until the person ends it);
- a **startTrigger** ties a step to the clock or to another step — `programStart`, `programStartOffset`, `afterStep`, `afterStepWithBuffer`, `manual`, `onAbort`, `compound`;
- `stepId`s are unique across the WHOLE program, not per track;
- every `task` a step occupies needs a matching `resourceConstraints` entry, and that entry's `maxConcurrent` is what stops two steps sharing one oven, one thermocycler or one PA;
- work repeated n times (n trays, n samples, n aircraft) is `replicates: {count: n, mode: "serial" | "parallel" | "stagger"}` on ONE step — never n hand-copied steps or tracks. Chain what follows each repetition with `{"type":"afterStep","stepId":"<step>","instances":"each"}`, give the step that waits for all of them `{"instances":"all"}`, and when the holding area between them only fits k at a time (a rack that holds two trays, a rotor that holds six tubes, a taxiway that holds two aircraft) set `replicates.maxInFlight: k` so the upstream step is held back instead of stranding the downstream one.

`rhylthyme://guide/authoring` is the full cheat-sheet, and `rhylthyme://schema/program` the schema, if you want to check a rule.

Then list the resource constraints you expect this {kind} to need — one `task` per line with the `maxConcurrent` you intend to declare and how many people are working — and end with that list as JSON in a single ```json fenced block:

```json
{
  "acknowledgement": "tracks are sequential lines of work; one duration kind per step; triggers link steps; every task needs a resourceConstraint; stepIds are global",
  "resourceConstraints": [
    { "task": "oven", "maxConcurrent": 1 },
    { "task": "burner", "maxConcurrent": 2 },
    { "task": "prep", "maxConcurrent": 2 }
  ],
  "actors": 2
}
```"""

T3_EXTRACTION = """Turn 3 of 4 — extraction. Steps only: no tracks, no triggers, no program JSON.

Imagine you have to carry out this {kind} yourself, in {environment}, and everything must be ready at {deadline}. You have only the text below. Read all of it.

{source}

List every timed activity you would have to perform, in the order the text gives it, with:

- a short kebab-case `stepId` and a human `name`;
- `task`: the resource it occupies while it runs — the oven, a burner, the thermocycler, the PA, the bench, your own hands. Use the same name every time for the same resource;
- how long it takes: exact (`fixed`), a range (`variable`), or "until I decide" (`indefinite`);
- `sourceSpan`: the exact words in the text you took it from, as `{"quote": "...", "occurrence": n}`, where n says which occurrence of those words you mean (1 for the first). Quote the text verbatim — do not paraphrase it;
- `inferred`: `true` for a step the text never states but the work needs anyway (preheating, thawing, resting, cooling, labelling, sound-check), with `"sourceSpan": null`. Keep these few and obvious.

Split anything the text bundles into one sentence when the parts occupy different resources or different stretches of time ("brown the meat, then simmer for 40 minutes" is two activities). Do not decide yet which activities can overlap, which track they belong to, or what waits for what.

Reply with the step list as JSON in a single ```json fenced block:

```json
{
  "steps": [
    {
      "stepId": "roast-turkey",
      "name": "Roast the turkey",
      "task": "oven",
      "duration": { "type": "fixed", "seconds": 10800 },
      "sourceSpan": { "quote": "Roast the turkey for 3 hours", "occurrence": 1 },
      "inferred": false
    },
    {
      "stepId": "rest-turkey",
      "name": "Rest the turkey",
      "task": "counter",
      "duration": { "type": "variable", "minSeconds": 1200, "maxSeconds": 2400, "defaultSeconds": 1800 },
      "sourceSpan": { "quote": "let it rest", "occurrence": 1 },
      "inferred": false
    },
    {
      "stepId": "preheat-oven",
      "name": "Preheat the oven",
      "task": "oven",
      "duration": { "type": "fixed", "seconds": 900 },
      "sourceSpan": null,
      "inferred": true
    }
  ]
}
```"""

T4_RELATIONSHIPS = """Turn 4 of 4 — relationships, then the program.

Take the step list from turn 3 exactly as it stands.

**First, tracks.** Put each step in a track: one track per parallel line of work — per dish, per station, per instrument, per performer, per sample group. Steps in one track run one after another and must not overlap; anything that runs while something else runs belongs in its own track.

**Second, triggers.** Give every step a `startTrigger` from this vocabulary and no other:

- `{"type":"programStart"}` — at t = 0.
- `{"type":"programStartOffset","offsetSeconds":n}` — n seconds after the start; this is how a short track is delayed so it finishes with the others.
- `{"type":"afterStep","stepId":"x"}` — when x ends. Add `"offsetSeconds"` for a gap, `"event":"start"` to hang off x's start instead of its end, `"instances"` when x is replicated.
- `{"type":"afterStepWithBuffer","stepId":"x","bufferSeconds":n}` — n seconds after x ends.
- `{"type":"manual"}` — the person taps to start it: a gate that no clock can predict (guests seated, dough looks right, the surgeon is ready).
- `{"type":"compound","logic":"all"|"any","triggers":[...]}` — wait for all of, or the first of, several triggers.

**Third, refine the question before you answer it.** Within the scope of scheduling these activities for one person to follow, suggest a better version of the question "which activities depend on which, and which can run at the same time?" — one that would expose dependencies this text implies but does not state: things that must cool, rest, preheat, thaw, proof, be held warm, or be ready at the same moment as something else. Write your improved question out, answer it, and revise the triggers accordingly. A step the improved question reveals is added here, marked `"inferred": true`.

**Then emit the complete program JSON** in a single ```json fenced block: `schemaVersion`, `programId`, `name`, `environmentType`, `actors`, `tracks`, and a `resourceConstraints` entry for every `task` any step uses. Carry turn 3's provenance onto each step as `"metadata": {"sourceSpan": ..., "inferred": ...}`.

```json
{
  "schemaVersion": "0.3.0-alpha",
  "programId": "kebab-case-id",
  "name": "Human title",
  "environmentType": "kitchen",
  "actors": 2,
  "tracks": [
    {
      "trackId": "turkey",
      "name": "Turkey",
      "steps": [
        {
          "stepId": "preheat-oven",
          "name": "Preheat the oven",
          "task": "oven",
          "duration": { "type": "fixed", "seconds": 900 },
          "startTrigger": { "type": "programStart" },
          "metadata": { "sourceSpan": null, "inferred": true }
        },
        {
          "stepId": "roast-turkey",
          "name": "Roast the turkey",
          "task": "oven",
          "duration": { "type": "fixed", "seconds": 10800 },
          "startTrigger": { "type": "afterStep", "stepId": "preheat-oven" },
          "metadata": { "sourceSpan": { "quote": "Roast the turkey for 3 hours", "occurrence": 1 }, "inferred": false }
        }
      ]
    }
  ],
  "resourceConstraints": [
    { "task": "oven", "maxConcurrent": 1 },
    { "task": "counter", "maxConcurrent": 2 }
  ],
  "metadata": { "serves": "8" }
}
```

Then finish the job with the tools:
1. If the public catalog already covers this ({catalogTools}), load that instead and stop — its result already has the live URL.
2. Otherwise keep the program you just built; `rhylthyme://guide/authoring` is the cheat-sheet and `rhylthyme://examples/*` are complete programs to pattern-match.
3. Express the limits as constraints, not as copied JSON: the `resourceConstraints`, `replicates`, `instances` and `maxInFlight` you confirmed in turn 2.
4. Run validate_program and fix every error it reports.
5. Run analyze_schedule{finishAtArg} to check the makespan, critical path and resource conflicts; adjust offsets so tracks finish together. Its `bindingConstraints` say which limit is actually gating the makespan.
6. Call visualize_schedule and give the user the live URL plus the Gantt/itinerary from the result. Do not describe the schedule in prose."""

FOUR_TURNS: List[Dict[str, Any]] = [
    {
        "key": "T1",
        "name": "read-back",
        "purpose": "Confirm the whole source was read and the goal understood, before any JSON exists.",
        "template": T1_READBACK,
        "expected": EXPECTED_T1,
        "slots": slots_in(T1_READBACK),
    },
    {
        "key": "T2",
        "name": "model check",
        "purpose": "Restate the program model in the model's own words and name the resource constraints it expects to declare.",
        "template": T2_MODEL_CHECK,
        "expected": EXPECTED_T2,
        "slots": slots_in(T2_MODEL_CHECK),
    },
    {
        "key": "T3",
        "name": "extraction",
        "purpose": "Scenario pattern: list every timed activity with its duration, the resource it occupies and the source span it came from. No relationships yet.",
        "template": T3_EXTRACTION,
        "expected": EXPECTED_T3,
        "slots": slots_in(T3_EXTRACTION),
    },
    {
        "key": "T4",
        "name": "relationships",
        "purpose": "Assign tracks and triggers, apply the question-refinement pattern to the dependency question, then emit the program and run the tool workflow.",
        "template": T4_RELATIONSHIPS,
        "expected": EXPECTED_T4,
        "slots": slots_in(T4_RELATIONSHIPS),
    },
]

KINDS: Dict[str, str] = {
    "generic": "process",
    "kitchen": "recipe",
    "lab": "protocol",
    "events": "run-of-show",
    "gym": "workout",
}

PLACES: Dict[str, str] = {
    "generic": "the workspace the text assumes",
    "kitchen": "a kitchen with one oven and two burners",
    "lab": "a lab with one of each shared instrument",
    "events": "a venue with one stage and one PA system",
    "gym": "a gym with one set of each piece of equipment",
}

PLACE_NAMES: Dict[str, str] = {
    "generic": "workspace",
    "kitchen": "kitchen",
    "lab": "lab",
    "events": "venue",
    "gym": "gym",
}


def kind_for(vertical: str) -> str:
    return KINDS.get(vertical, KINDS["generic"])


def environment_phrase(vertical: str, constraints: Optional[str] = None) -> str:
    if constraints and str(constraints).strip():
        return (
            "a "
            + PLACE_NAMES.get(vertical, PLACE_NAMES["generic"])
            + " where you have: "
            + str(constraints).strip()
        )
    return PLACES.get(vertical, PLACES["generic"])


def deadline_phrase(finish_at: Optional[str]) -> str:
    if finish_at and str(finish_at).strip():
        return str(finish_at).strip()
    return "the earliest time the work allows"


def constraints_line(constraints: Optional[str]) -> str:
    if constraints and str(constraints).strip():
        return "Resource limits: " + str(constraints).strip() + "."
    return (
        "Resource limits: none were given — infer them from the text and say "
        "which you assumed."
    )


def deadline_line(finish_at: Optional[str]) -> str:
    if finish_at and str(finish_at).strip():
        return "Everything must be finished by " + str(finish_at).strip() + "."
    return "No finishing time was given; finish as early as the work allows."


def source_block(source_text: Optional[str]) -> str:
    text = "" if source_text is None else str(source_text).strip()
    if not text:
        return (
            "No source text was supplied: the goal above is all you have, so work "
            "from what it states and from what the work itself requires."
        )
    return "Source text (read all of it):\n<<<\n" + text + "\n>>>"


def catalog_tools(one_shot: Optional[str]) -> str:
    return (
        "search_public_recipes or " + one_shot if one_shot else "search_public_recipes"
    )


def finish_at_arg(finish_at: Optional[str]) -> str:
    if finish_at and str(finish_at).strip():
        return ' with finishAt="' + str(finish_at).strip() + '"'
    return ""


def turn_slots(
    *,
    goal: str = "",
    finish_at: Optional[str] = None,
    constraints: Optional[str] = None,
    source_text: Optional[str] = None,
    vertical: str = "generic",
    one_shot: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """Per-turn slot maps for one set of ``plan_schedule`` arguments."""
    kind = kind_for(vertical)
    source = source_block(source_text)
    return {
        "T1": {
            "kind": kind,
            "goal": goal or "",
            "constraintsLine": constraints_line(constraints),
            "deadlineLine": deadline_line(finish_at),
            "source": source,
        },
        "T2": {"kind": kind},
        "T3": {
            "kind": kind,
            "environment": environment_phrase(vertical, constraints),
            "deadline": deadline_phrase(finish_at),
            "source": source,
        },
        "T4": {
            "catalogTools": catalog_tools(one_shot),
            "finishAtArg": finish_at_arg(finish_at),
        },
    }


def render_four_turns(**kwargs: Any) -> List[Dict[str, str]]:
    """The four rendered messages, in order: ``[{key, name, text}, ...]``."""
    slots = turn_slots(**kwargs)
    return [
        {
            "key": t["key"],
            "name": t["name"],
            "text": render(t["template"], slots[t["key"]]),
        }
        for t in FOUR_TURNS
    ]


# ==========================================================================
# END copy of prompts.js
# ==========================================================================

#: Sent instead of the baseline preamble: same "the tools are not here"
#: framing, but saying the turns arrive one at a time.
FOUR_TURN_PREAMBLE = (
    "You are being evaluated offline. The Rhylthyme MCP tools named below "
    "(search_public_recipes and the one-shot tools, validate_program, "
    "analyze_schedule, visualize_schedule) are NOT available in this session, "
    "so build everything yourself and expect no live URL or Gantt. You will be "
    "given four turns, one at a time: a read-back, a model check, a step "
    "extraction and finally the relationships plus the complete program JSON. "
    "Answer only the turn you were given — do not run ahead to the program — "
    "and end every turn with the JSON shape that turn asks for, in a single "
    "```json fenced block. The server instructions and the content of "
    "rhylthyme://guide/authoring follow."
)

RETRY_REQUEST = (
    "Your reply did not carry the JSON object this turn asks for. Reply again "
    "with nothing but a single ```json fenced block, filled in for this "
    "{kind}, in exactly this shape:\n\n```json\n{expected}\n```"
)


def goal_for(gold: GoldProgram) -> str:
    """One-line goal from ``context.json`` (the source arrives in T1 and T3)."""
    context = gold.context or {}
    title = context.get("title") or gold.program.get("name") or gold.slug
    scale = context.get("servesOrScale")
    return f"{title} for {scale}" if scale else str(title)


def render_system(vertical: str) -> str:
    """Four-turn preamble + the frozen server instructions + authoring guide."""
    return "\n\n".join(
        [FOUR_TURN_PREAMBLE, render_server_instructions(vertical), authoring_guide()]
    )


def _json_objects(text: str) -> List[Any]:
    """Every JSON value in a reply, fenced blocks first (see extract.py)."""
    return [value for value in _json_candidates(text or "")]


def check_t1(reply: str) -> Optional[Dict[str, Any]]:
    """T1's read-back shape: ``{summary, servesOrScale, deadline?, constraints[]}``."""
    for value in _json_objects(reply):
        if (
            isinstance(value, dict)
            and isinstance(value.get("summary"), str)
            and value["summary"].strip()
            and "servesOrScale" in value
            and isinstance(value.get("constraints"), list)
        ):
            return value
    return None


def check_t2(reply: str) -> Optional[Dict[str, Any]]:
    """T2's shape: an acknowledgement plus the resource constraints expected."""
    for value in _json_objects(reply):
        if not isinstance(value, dict):
            continue
        acknowledgement = value.get("acknowledgement")
        constraints = value.get("resourceConstraints")
        if (
            isinstance(acknowledgement, str)
            and acknowledgement.strip()
            and isinstance(constraints, list)
            and constraints
            and all(
                isinstance(item, dict) and isinstance(item.get("task"), str)
                for item in constraints
            )
        ):
            return value
    return None


def t3_steps(reply: str) -> List[Dict[str, Any]]:
    """The flat step list T3 asked for, or ``[]``."""
    for value in _json_objects(reply):
        steps = None
        if isinstance(value, dict) and isinstance(value.get("steps"), list):
            steps = value["steps"]
        elif isinstance(value, list):
            steps = value
        if steps and all(isinstance(step, dict) for step in steps):
            if any("stepId" in step or "name" in step for step in steps):
                return steps
    return []


def unsupported_step_ids(steps: Sequence[Dict[str, Any]]) -> List[str]:
    """Step ids the model itself flagged as not supported by the source.

    A step with ``inferred: true`` or without a ``sourceSpan``. This is the
    model's own claim, not the scorer's: ``ComponentScores.unsupported_steps``
    is the measured rate against gold.
    """
    ids = []
    for index, step in enumerate(steps):
        if step.get("inferred") is True or not step.get("sourceSpan"):
            ids.append(str(step.get("stepId") or step.get("name") or index))
    return ids


class FourTurnPattern(Pattern):
    """Read-back, model check, extraction, relationships -- then the fix loop."""

    name = "four-turn"

    #: Turn name -> shape check. Only T1 and T2 are scored 2/1/0.
    CHECKS: Dict[str, Callable[[str], Optional[Dict[str, Any]]]] = {
        "T1 read-back": check_t1,
        "T2 model check": check_t2,
    }
    SCORE_KEYS = {"T1 read-back": "t1_score", "T2 model check": "t2_score"}

    def render(self, gold: GoldProgram) -> List[Turn]:
        context = gold.context or {}
        vertical = vertical_for(context.get("environmentType"))
        args: Dict[str, Any] = dict(
            goal=goal_for(gold),
            finish_at=context.get("deadline") or None,
            constraints=constraints_from_context(gold),
            source_text=gold.source_text,
            vertical=vertical,
            one_shot=ONE_SHOT.get(vertical),
        )
        slots = turn_slots(**args)
        system = render_system(vertical)
        recorded = {
            "goal": args["goal"],
            "finish_at": args["finish_at"],
            "constraints": args["constraints"],
            "vertical": vertical,
        }
        turns = []
        for spec in FOUR_TURNS:
            turns.append(
                Turn(
                    name=f"{spec['key']} {spec['name']}",
                    content=render(spec["template"], slots[spec["key"]]),
                    system=system,
                    slots=dict(recorded, turn=spec["key"], **slots[spec["key"]]),
                )
            )
        return turns

    def parse(self, turn_outputs: Sequence[str]) -> Optional[Program]:
        """T4's reply carries the program; earlier turns never do."""
        outputs = list(turn_outputs)
        return extract_program(outputs[-1]) if outputs else None

    def run(self, gold: GoldProgram, session) -> PatternResult:
        turns = self.render(gold)
        history: List[Dict[str, Any]] = []
        outputs: List[str] = []
        extras: Dict[str, Any] = {"retries": 0}
        system = turns[-1].system
        for index, turn in enumerate(turns):
            history.append({"role": "user", "content": turn.text(outputs)})
            reply = session.complete(history, system=turn.system).text
            check = self.CHECKS.get(turn.name)
            if check is not None:
                score = 2 if check(reply) is not None else 0
                if score == 0:
                    # One retry turn, then 1 if it lands and 0 if it does not.
                    history.append({"role": "assistant", "content": reply})
                    history.append(
                        {
                            "role": "user",
                            "content": render(
                                RETRY_REQUEST,
                                {
                                    "kind": kind_for(
                                        turn.slots.get("vertical", "generic")
                                    ),
                                    "expected": FOUR_TURNS[index]["expected"],
                                },
                            ),
                        }
                    )
                    reply = session.complete(history, system=turn.system).text
                    score = 1 if check(reply) is not None else 0
                    extras["retries"] += 1
                extras[self.SCORE_KEYS[turn.name]] = score
            if turn.name.startswith("T3"):
                steps = t3_steps(reply)
                extras["t3_step_count"] = len(steps)
                extras["unsupported_step_ids"] = unsupported_step_ids(steps)
            outputs.append(reply)
            if index < len(turns) - 1:
                history.append({"role": "assistant", "content": reply})
        result = finish_with_fix_loop(
            session,
            history,
            outputs[-1],
            system=system,
            parse=lambda reply: self.parse(outputs[:-1] + [reply]),
        )
        extras.setdefault("t1_score", 0)
        extras.setdefault("t2_score", 0)
        extras.setdefault("unsupported_step_ids", [])
        extras.setdefault("turns", len(turns))
        extras.setdefault(
            "turn_slots", {turn.name: turn.slots for turn in turns if turn.slots}
        )
        result.extras.update(extras)
        return result


# --------------------------------------------------------------------------
# Parity check helper: `python -m rhylthyme.cli.eval.patterns.four_turn`
# --------------------------------------------------------------------------


def js_exports(prompts_js: Optional[str] = None) -> Dict[str, Any]:
    """Dump ``prompts.js``'s templates with node (``None`` when unavailable)."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    path = Path(prompts_js) if prompts_js else find_prompts_js()
    if node is None or path is None:
        return {}
    script = (
        "const P = require(process.argv[1]);"
        "process.stdout.write(JSON.stringify({"
        "T1_READBACK: P.T1_READBACK, T2_MODEL_CHECK: P.T2_MODEL_CHECK,"
        "T3_EXTRACTION: P.T3_EXTRACTION, T4_RELATIONSHIPS: P.T4_RELATIONSHIPS,"
        "EXPECTED_OUTPUT: P.EXPECTED_OUTPUT,"
        "FOUR_TURNS: P.FOUR_TURNS.map((t) => ({key: t.key, name: t.name,"
        " purpose: t.purpose, slots: t.slots})),"
        "samples: [{goal: 'Thanksgiving for 8', finishAt: '18:00',"
        " constraints: 'one oven, four burners', sourceText: 'Roast it.',"
        " vertical: 'kitchen', oneShot: 'cook_recipe'},"
        "{goal: 'bare goal'}].map((a) => P.renderFourTurns(a))"
        "}));"
    )
    out = subprocess.run(
        [node, "-e", script, str(path)], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:
        raise RuntimeError(f"node failed reading {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def find_prompts_js():
    """Locate ``rhylthyme-server/mcp-api/prompts.js`` in the monorepo."""
    import os
    from pathlib import Path

    override = os.environ.get("RHYLTHYME_PROMPTS_JS")
    if override:
        return Path(override) if Path(override).exists() else None
    here = Path(__file__).resolve()
    for root in here.parents:
        candidate = root / "rhylthyme-server" / "mcp-api" / "prompts.js"
        if candidate.exists():
            return candidate
    return None


def parity_differences() -> List[str]:
    """Names of the templates whose Python copy differs from the JS export."""
    exports = js_exports()
    if not exports:
        return []
    differences = []
    ours = {
        "T1_READBACK": T1_READBACK,
        "T2_MODEL_CHECK": T2_MODEL_CHECK,
        "T3_EXTRACTION": T3_EXTRACTION,
        "T4_RELATIONSHIPS": T4_RELATIONSHIPS,
    }
    for key, value in ours.items():
        if exports.get(key) != value:
            differences.append(key)
    for key, value in EXPECTED_OUTPUT.items():
        if exports.get("EXPECTED_OUTPUT", {}).get(key) != value:
            differences.append(f"EXPECTED_OUTPUT[{key}]")
    for js_turn, py_turn in zip(exports.get("FOUR_TURNS", []), FOUR_TURNS):
        for field in ("key", "name", "purpose", "slots"):
            if js_turn.get(field) != py_turn[field]:
                differences.append(f"FOUR_TURNS[{py_turn['key']}].{field}")
    samples = exports.get("samples") or []
    if samples:
        rendered = [
            render_four_turns(
                goal="Thanksgiving for 8",
                finish_at="18:00",
                constraints="one oven, four burners",
                source_text="Roast it.",
                vertical="kitchen",
                one_shot="cook_recipe",
            ),
            render_four_turns(goal="bare goal"),
        ]
        for index, (js_sample, py_sample) in enumerate(zip(samples, rendered)):
            for js_turn, py_turn in zip(js_sample, py_sample):
                if js_turn["text"] != py_turn["text"]:
                    differences.append(f"renderFourTurns[{index}].{py_turn['key']}")
    return differences


if __name__ == "__main__":  # pragma: no cover - parity helper
    drift = parity_differences()
    if drift:
        raise SystemExit(
            "four_turn.py has drifted from prompts.js: " + ", ".join(drift)
        )
    print("four_turn.py matches rhylthyme-server/mcp-api/prompts.js")
