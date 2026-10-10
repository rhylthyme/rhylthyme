"""``baseline`` pattern: the single-message ``plan_schedule`` prompt.

FROZEN as of Phase 2 (2026-09-14). "Baseline" means *the one-message
prompt the server sent before the four-turn rewrite*, so the numbers in
``eval/baseline.json`` stay reproducible from the committed cache: the
cache key covers the rendered prompt, and a single changed character
would miss every entry. Phase 3 replaced ``registerPrompts`` in
``index.js`` with the four turns of ``mcp-api/prompts.js`` (mirrored in
``patterns/four_turn.py``, which is parity-checked against the JS); this
module is no longer in step with ``index.js`` and is not meant to be.
Leave it alone unless you are deliberately re-baselining, which means
re-running the baseline and replacing both the cache and
``eval/baseline.json``.

The prompt text is a faithful copy of what the remote MCP server rendered
at that commit, taken from ``rhylthyme-server/mcp-api/index.js``:

* ``registerPrompts(server, vertical)`` -- the ``nouns`` map and the
  ``plan_schedule`` message lines, including the conditional
  ``Resource limits`` / ``Everything must be finished by`` lines and the
  one-shot tool name (``cook_recipe``, ``run_protocol``, ``plan_event``,
  ``start_workout``) spliced into step 1 per vertical.
* ``serverInstructions(vertical)`` -- the server-level ``instructions``
  a host receives at ``initialize``, sent here as the system prompt
  because a host sends both.
* ``AUTHORING_GUIDE`` -- the ``rhylthyme://guide/authoring`` resource that
  step 2 tells the model to read, copied verbatim into
  ``patterns/authoring_guide.md`` and appended to the system prompt
  because the harness serves no MCP resources.

``patterns/baseline.md`` is the rendered copy of all three for one worked
example; ``tests/test_eval_harness.py`` asserts it stays in step with
this module.

The harness adds two things the host would otherwise supply: a short
system preamble saying the tools are unavailable and the reply must carry
the program JSON, and the source text appended under the prompt as the
thing to schedule. Both are recorded in the transcript.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rhylthyme.eval.gold import GoldProgram

from . import Pattern, Turn

GUIDE_FILE = Path(__file__).with_name("authoring_guide.md")
BASELINE_MD = Path(__file__).with_name("baseline.md")

# ---------------------------------------------------------------------------
# index.js registerPrompts: nouns map and one-shot names.
# ---------------------------------------------------------------------------

NOUNS: Dict[str, Dict[str, str]] = {
    "generic": {
        "thing": "schedule",
        "example": "Thanksgiving dinner for 8 with one oven, ready at 6pm",
    },
    "kitchen": {
        "thing": "meal",
        "example": "Thanksgiving dinner for 8 with one oven, ready at 6pm",
    },
    "lab": {
        "thing": "protocol",
        "example": "Western blot for 12 samples with one transfer apparatus",
    },
    "events": {
        "thing": "run-of-show",
        "example": "wedding ceremony at 2pm, reception at 5pm, one PA system",
    },
    "gym": {"thing": "workout", "example": "45-minute upper-body superset session"},
}

# index.js VERTICALS[vertical].oneShot.name / .title.
ONE_SHOT: Dict[str, Optional[str]] = {
    "generic": None,
    "kitchen": "cook_recipe",
    "lab": "run_protocol",
    "events": "plan_event",
    "gym": "start_workout",
}

TITLES: Dict[str, str] = {
    "generic": "Rhylthyme",
    "kitchen": "Rhylthyme Kitchen",
    "lab": "Rhylthyme Lab",
    "events": "Rhylthyme Events",
    "gym": "Rhylthyme Gym",
}

# index.js serverInstructions: the `domain` map.
DOMAINS: Dict[str, str] = {
    "generic": (
        "any real-time, multi-track process — cooking, lab protocols, event "
        "run-of-shows, workouts, manufacturing, turnarounds"
    ),
    "kitchen": "cooking and meal coordination",
    "lab": "laboratory protocols and bench work",
    "events": "event run-of-shows and cue sheets",
    "gym": "workouts and training sessions",
}

# Gold context ``environmentType`` -> MCP vertical (VERTICALS keys).
ENVIRONMENT_TO_VERTICAL: Dict[str, str] = {
    "kitchen": "kitchen",
    "laboratory": "lab",
    "lab": "lab",
    "event": "events",
    "events": "events",
    "fitness": "gym",
    "gym": "gym",
}


def vertical_for(environment_type: Optional[str]) -> str:
    return ENVIRONMENT_TO_VERTICAL.get((environment_type or "").lower(), "generic")


def authoring_guide() -> str:
    """``rhylthyme://guide/authoring``, copied verbatim from index.js."""
    return GUIDE_FILE.read_text(encoding="utf-8").rstrip("\n")


def render_server_instructions(vertical: str = "generic") -> str:
    """Render ``serverInstructions(vertical)`` exactly as index.js does."""
    title = TITLES.get(vertical, TITLES["generic"])
    domain = DOMAINS.get(vertical, "real-time scheduling")
    one_shot = ONE_SHOT.get(vertical)
    lines: List[str] = [
        f'{title} schedules {domain}. A "program" is JSON: parallel **tracks** of '
        "sequential **steps**, each with a duration and a startTrigger "
        "(programStart / afterStep / programStartOffset / afterStepWithBuffer / "
        "manual), plus **resourceConstraints** (e.g. one oven) that the live "
        "runner enforces.",
        "",
        "Workflow:",
        "- Existing content: **search_public_recipes** → **load_public_recipe** "
        "(or the one-shot tool). The result already includes the live URL; no "
        "further call is needed.",
        (
            f"- Fast path: **{one_shot}** takes a keyword and returns the top "
            "catalog match as a live timeline in one call."
            if one_shot
            else ""
        ),
        "- New content: build the program → **validate_program** (fix every error "
        "it reports) → optionally **analyze_schedule** (makespan, critical path, "
        "resource conflicts, wall-clock itinerary when you pass finishAt/startAt) "
        "→ **visualize_schedule** to get the shareable live timeline. "
        "visualize_schedule validates too and refuses invalid programs.",
        "- Never describe a schedule in prose when a timeline is possible; the URL "
        "is the deliverable. Quote the ASCII Gantt / itinerary from the tool "
        "result when summarizing.",
        "- **login** is only needed for the user's private library "
        "(list_my_programs, load_program, save_program) and for imports "
        "(import_from_source with action=import/random). Public catalog tools "
        "need no token.",
        "- Read `rhylthyme://guide/authoring` for the authoring cheat-sheet and "
        "`rhylthyme://schema/program` for the full JSON schema; "
        "`rhylthyme://examples/*` are complete, valid programs to pattern-match.",
        "",
        "Authoring rules: stepIds unique across the whole program; steps in one "
        "track never overlap (chain with afterStep); every `task` used by a step "
        "has a matching resourceConstraint; durations in seconds (numbers) or "
        'time strings ("5m", "1h30m"); to make everything finish together, delay '
        "short tracks with programStartOffset or afterStep, and pass finishAt to "
        "analyze_schedule to get wall-clock start times.",
        "- Repeated work (n trays / samples / aircraft) is `replicates` on one "
        'step, never copied steps: add `instances: "each"` to chain the next step '
        'per instance, `instances: "all"` for the step that waits for every '
        "instance, and `replicates.maxInFlight: k` when a holding area (rack, "
        'rotor, taxiway) only fits k at a time. See the "Repeating work" section '
        "of `rhylthyme://guide/authoring`.",
    ]
    # JS: .filter((l) => l !== "").join("\n") -- drops the unused one-shot line
    # but keeps the deliberate "" spacers... which are themselves dropped, so
    # the rendered text has no blank lines.
    return "\n".join(line for line in lines if line != "")


def render_plan_schedule(
    goal: str,
    *,
    finish_at: Optional[str] = None,
    constraints: Optional[str] = None,
    vertical: str = "generic",
) -> str:
    """Render the ``plan_schedule`` message exactly as index.js does."""
    nouns = NOUNS.get(vertical) or {
        "thing": "schedule",
        "example": "a multi-step process",
    }
    one_shot = ONE_SHOT.get(vertical)
    lines: List[str] = [
        f"Plan this as a Rhylthyme {nouns['thing']} and deliver a live timeline: {goal}",
        f"Resource limits: {constraints}." if constraints else "",
        f"Everything must be finished by {finish_at}." if finish_at else "",
        "",
        "Steps:",
        "1. Check the public catalog first (search_public_recipes"
        + (f" or {one_shot}" if one_shot else "")
        + "). If a good match exists, use it and stop — its result already has the live URL.",  # noqa: E501
        "2. Otherwise read rhylthyme://guide/authoring and build a program: one track per parallel line of work, sequential steps chained with afterStep, realistic durations, and a resourceConstraint for every task.",  # noqa: E501
        '3. Express the limits as constraints, not as copied JSON. A machine that runs one job at a time is `resourceConstraints[].maxConcurrent`. Work repeated n times (n trays, n samples, n aircraft) is `replicates: {count: n, mode: "serial" | "parallel" | "stagger"}` on ONE step — never n hand-copied steps or tracks. Chain what follows each repetition with `{"type":"afterStep","stepId":"<step>","instances":"each"}`, and give the step that waits for all of them `"instances":"all"`. When a holding area between them only fits k at a time (a rack that holds two trays, a rotor that holds six tubes, a taxiway that holds two aircraft), set `replicates.maxInFlight: k` so the upstream step is held back instead of stranding the downstream one.',  # noqa: E501
        "4. Run validate_program and fix every error it reports.",
        "5. Run analyze_schedule"
        + (f' with finishAt="{finish_at}"' if finish_at else "")
        + " to check the makespan, critical path and resource conflicts; adjust offsets so tracks finish together. Its `bindingConstraints` say which limit is actually gating the makespan.",  # noqa: E501
        "6. Call visualize_schedule and give the user the live URL plus the Gantt/itinerary from the result. Do not describe the schedule in prose.",  # noqa: E501
    ]
    # JS: .filter(Boolean).join("\n") -- drops every empty string, the
    # unused conditional lines and the "" spacer alike.
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Harness-only framing (not part of the shipped prompt; recorded as such).
# ---------------------------------------------------------------------------

HARNESS_PREAMBLE = (
    "You are being evaluated offline. The Rhylthyme MCP tools named below "
    "(search_public_recipes and the one-shot tools, validate_program, "
    "analyze_schedule, visualize_schedule) are NOT available in this session, "
    "so: skip the catalog check, build the program yourself from the source "
    "text, check it against the rules below, and reply with the complete "
    "program JSON in a single ```json fenced block. No live URL or Gantt is "
    "expected. The server instructions and the content of "
    "rhylthyme://guide/authoring follow."
)

SOURCE_HEADER = "\n\nSource text to schedule (read all of it):\n<<<\n"
SOURCE_FOOTER = "\n>>>"


def goal_from_context(gold: GoldProgram) -> str:
    """One-line goal built from ``context.json``, pointing at the source text."""
    context = gold.context or {}
    title = context.get("title") or gold.program.get("name") or gold.slug
    scale = context.get("servesOrScale")
    goal = f"{title} for {scale}" if scale else str(title)
    return goal + ", following the source text below"


def constraints_from_context(gold: GoldProgram) -> Optional[str]:
    constraints = (gold.context or {}).get("constraints")
    if isinstance(constraints, list) and constraints:
        return ", ".join(str(item) for item in constraints)
    if isinstance(constraints, str) and constraints.strip():
        return constraints.strip()
    return None


def render_system(vertical: str) -> str:
    """Harness preamble + serverInstructions + the authoring guide."""
    return "\n\n".join(
        [HARNESS_PREAMBLE, render_server_instructions(vertical), authoring_guide()]
    )


def render_baseline(gold: GoldProgram) -> Tuple[str, str, Dict[str, Any]]:
    """``(system, user_message, slots)`` for one gold program."""
    context = gold.context or {}
    vertical = vertical_for(context.get("environmentType"))
    slots: Dict[str, Any] = {
        "goal": goal_from_context(gold),
        "finish_at": context.get("deadline") or None,
        "constraints": constraints_from_context(gold),
        "vertical": vertical,
    }
    prompt = render_plan_schedule(
        slots["goal"],
        finish_at=slots["finish_at"],
        constraints=slots["constraints"],
        vertical=vertical,
    )
    user = prompt + SOURCE_HEADER + gold.source_text.rstrip("\n") + SOURCE_FOOTER
    return render_system(vertical), user, slots


class BaselinePattern(Pattern):
    """Single ``plan_schedule`` message, then the validator fix loop."""

    name = "baseline"

    def render(self, gold: GoldProgram) -> List[Turn]:
        system, user, slots = render_baseline(gold)
        return [Turn(name="plan_schedule", content=user, system=system, slots=slots)]


# ---------------------------------------------------------------------------
# patterns/baseline.md: the checked-in rendered copy of the prompt.
# ---------------------------------------------------------------------------

MD_EXAMPLE = {
    "goal": "Thanksgiving for Eight with One Oven for 8 people, "
    "following the source text below",
    "finish_at": "18:00",
    "constraints": "one oven, four burners, two cooks, all dishes finish together",
    "vertical": "kitchen",
}

MD_HEADER = """<!--
GENERATED FILE -- do not edit by hand.

A rendered copy of the prompt the `baseline` pattern sends: today's
single-message `plan_schedule` prompt plus the system prompt a host would
have supplied. Regenerate with:

    python -m rhylthyme.cli.eval.patterns.baseline

Source of every line below, in rhylthyme-server/mcp-api/index.js:
  * serverInstructions(vertical)             -> "System prompt" below
  * AUTHORING_GUIDE (rhylthyme://guide/authoring)
  * registerPrompts(server, vertical)        -> "User message" below

Slots are filled the way the JS fills them, here for the
kitchen vertical of the `kitchen-thanksgiving-one-oven` gold program:
goal={goal!r}, finishAt={finish_at!r}, constraints={constraints!r}.
Lines whose slot is empty are dropped (`.filter(Boolean)`), so a gold
program with no deadline renders without the "Everything must be
finished by" line and with a bare "5. Run analyze_schedule to check ...".
-->
"""


def render_markdown() -> str:
    """The content of ``patterns/baseline.md``."""
    vertical = MD_EXAMPLE["vertical"]
    user = render_plan_schedule(
        MD_EXAMPLE["goal"],
        finish_at=MD_EXAMPLE["finish_at"],
        constraints=MD_EXAMPLE["constraints"],
        vertical=vertical,
    )
    return "\n".join(
        [
            MD_HEADER.format(**MD_EXAMPLE),
            "# baseline pattern prompt",
            "",
            "## System prompt (harness preamble)",
            "",
            HARNESS_PREAMBLE,
            "",
            "## System prompt (serverInstructions, vertical `kitchen`)",
            "",
            render_server_instructions(vertical),
            "",
            "## System prompt (rhylthyme://guide/authoring)",
            "",
            authoring_guide(),
            "",
            "## User message (plan_schedule)",
            "",
            user,
            "",
            "## User message (source text appended by the harness)",
            "",
            SOURCE_HEADER.strip("\n")
            + "<the gold program's source.txt>"
            + SOURCE_FOOTER,
            "",
        ]
    )


def write_markdown(path: Optional[Path] = None) -> Path:
    path = Path(path) if path else BASELINE_MD
    path.write_text(render_markdown(), encoding="utf-8")
    return path


if __name__ == "__main__":  # pragma: no cover - regeneration helper
    print(f"Wrote {write_markdown()}")
