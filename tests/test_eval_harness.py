"""
Tests for the live prompt-evaluation harness (rhylthyme.cli.eval.harness).

Everything runs against FakeClient; the one real-API test is marked ``llm``
and skipped unless RHYLTHYME_EVAL_LIVE=1.
"""

import copy
import json
import os
import re
import time
from pathlib import Path

import pytest

from rhylthyme.cli.eval.harness import (
    CacheMiss,
    HarnessConfig,
    baseline_payload,
    cache_key,
    git_note,
    run_harness,
    run_program,
    write_baseline,
)
from rhylthyme.cli.eval.llm import Completion, FakeClient, estimate_cost, price_for
from rhylthyme.cli.eval.patterns import (
    FIX_REQUEST,
    NO_JSON_FINDING,
    PATTERNS,
    Pattern,
    PatternResult,
    Turn,
    extract_program,
    get_pattern,
)
from rhylthyme.cli.eval.patterns.baseline import (
    BASELINE_MD,
    BaselinePattern,
    authoring_guide,
    render_baseline,
    render_markdown,
    render_plan_schedule,
    render_server_instructions,
)
from rhylthyme.cli.eval.patterns.four_turn import (
    FOUR_TURN_PREAMBLE,
    FOUR_TURNS,
    FourTurnPattern,
    check_t1,
    check_t2,
    find_prompts_js,
    parity_differences,
    render,
    render_four_turns,
    slots_in,
    t3_steps,
    unsupported_step_ids,
)
from rhylthyme.eval.gold import GoldProgram, load_gold_set

pytestmark = pytest.mark.unit

GOLD_DIR = Path(__file__).resolve().parent.parent.parent / "rhylthyme-examples" / "gold"

SOURCE = (
    "Boil the pasta for 10 minutes. Meanwhile warm the sauce for 5 minutes. "
    "Toss together for 2 minutes."
)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def mk_program(valid=True):
    program = {
        "schemaVersion": "0.2.0",
        "programId": "pasta",
        "name": "Pasta night",
        "environmentType": "kitchen",
        "actors": 1,
        "resourceConstraints": [{"task": "burner", "maxConcurrent": 2}],
        "tracks": [
            {
                "trackId": "pasta",
                "name": "Pasta",
                "steps": [
                    {
                        "stepId": "boil",
                        "name": "Boil the pasta",
                        "task": "burner",
                        "duration": {"type": "fixed", "seconds": 600},
                        "startTrigger": {"type": "programStart"},
                        "metadata": {
                            "sourceSpan": {
                                "quote": "Boil the pasta for 10 minutes",
                                "occurrence": 1,
                            }
                        },
                    },
                    {
                        "stepId": "toss",
                        "name": "Toss together",
                        "task": "burner",
                        "duration": {"type": "fixed", "seconds": 120},
                        "startTrigger": {"type": "afterStep", "stepId": "warm"},
                        "metadata": {
                            "sourceSpan": {
                                "quote": "Toss together for 2 minutes",
                                "occurrence": 1,
                            }
                        },
                    },
                ],
            },
            {
                "trackId": "sauce",
                "name": "Sauce",
                "steps": [
                    {
                        "stepId": "warm",
                        "name": "Warm the sauce",
                        "task": "burner",
                        "duration": {"type": "fixed", "seconds": 300},
                        "startTrigger": {
                            "type": "afterStep",
                            "stepId": "boil",
                            "event": "start",
                            "offsetSeconds": 300,
                        },
                        "metadata": {
                            "sourceSpan": {
                                "quote": "warm the sauce for 5 minutes",
                                "occurrence": 1,
                            }
                        },
                    }
                ],
            },
        ],
    }
    if not valid:
        # Dangling afterStep reference: the logic validator rejects it.
        program["tracks"][0]["steps"][1]["startTrigger"]["stepId"] = "nope"
    return program


def mk_gold(slug="pasta", deadline="19:00"):
    context = {
        "title": "Pasta night",
        "environmentType": "kitchen",
        "domain": "kitchen",
        "servesOrScale": "2 people",
        "constraints": ["two burners", "one cook"],
    }
    if deadline:
        context["deadline"] = deadline
    return GoldProgram(
        slug=slug, program=mk_program(), source_text=SOURCE, context=context
    )


def fenced(program, prose="Here is the program:\n\n", tail="\n\nEnjoy."):
    return prose + "```json\n" + json.dumps(program, indent=1) + "\n```" + tail


def config(tmp_path, **overrides):
    defaults = dict(
        model="fake-model",
        pattern="baseline",
        out_dir=tmp_path / "out",
        js=False,
        max_fix_iterations=2,
    )
    defaults.update(overrides)
    return HarnessConfig(**defaults)


class RaisingClient:
    """A client that must never be called (for --from-cache tests)."""

    calls = 0

    def complete(self, messages, *, system=None, model, max_tokens):
        RaisingClient.calls += 1
        raise AssertionError("model called despite --from-cache")


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


class TestRendering:
    def test_plan_schedule_matches_frozen_phase2_prompt(self):
        """The frozen single-message prompt (index.js before Phase 3)."""
        text = render_plan_schedule(
            "Brunch for 4",
            finish_at="10:00",
            constraints="one oven, two burners",
            vertical="kitchen",
        )
        lines = text.split("\n")
        # index.js registerPrompts: header, two conditional lines, "Steps:",
        # then six numbered steps. The "" spacer is dropped by filter(Boolean).
        assert len(lines) == 10
        assert lines[0] == (
            "Plan this as a Rhylthyme meal and deliver a live timeline: Brunch for 4"
        )
        assert lines[1] == "Resource limits: one oven, two burners."
        assert lines[2] == "Everything must be finished by 10:00."
        assert lines[3] == "Steps:"
        assert lines[4].startswith(
            "1. Check the public catalog first (search_public_recipes or cook_recipe)."
        )
        assert lines[5].startswith(
            "2. Otherwise read rhylthyme://guide/authoring and build a program:"
        )
        assert lines[6].startswith("3. Express the limits as constraints")
        assert 'mode: "serial" | "parallel" | "stagger"' in lines[6]
        assert '"instances":"each"' in lines[6]
        assert "`replicates.maxInFlight: k`" in lines[6]
        assert lines[7] == "4. Run validate_program and fix every error it reports."
        assert lines[8] == (
            '5. Run analyze_schedule with finishAt="10:00" to check the makespan, '
            "critical path and resource conflicts; adjust offsets so tracks finish "
            "together. Its `bindingConstraints` say which limit is actually gating "
            "the makespan."
        )
        assert lines[9] == (
            "6. Call visualize_schedule and give the user the live URL plus the "
            "Gantt/itinerary from the result. Do not describe the schedule in prose."
        )

    def test_generic_vertical_without_optional_slots(self):
        text = render_plan_schedule("Something")
        assert text.startswith(
            "Plan this as a Rhylthyme schedule and deliver a live timeline: Something\nSteps:"
        )
        assert "Resource limits" not in text
        assert "finished by" not in text
        assert "(search_public_recipes)." in text
        assert "5. Run analyze_schedule to check" in text

    @pytest.mark.parametrize(
        "environment,thing,one_shot",
        [
            ("laboratory", "protocol", "run_protocol"),
            ("event", "run-of-show", "plan_event"),
            ("fitness", "workout", "start_workout"),
            ("manufacturing", "schedule", None),
        ],
    )
    def test_vertical_nouns(self, environment, thing, one_shot):
        gold = mk_gold()
        gold.context["environmentType"] = environment
        _, user, slots = render_baseline(gold)
        assert f"Plan this as a Rhylthyme {thing}" in user
        if one_shot:
            assert f"search_public_recipes or {one_shot})" in user
        else:
            assert "(search_public_recipes)." in user

    def test_baseline_fills_slots_and_embeds_source(self):
        system, user, slots = render_baseline(mk_gold())
        assert slots["goal"].startswith("Pasta night for 2 people")
        assert slots["finish_at"] == "19:00"
        assert slots["constraints"] == "two burners, one cook"
        assert slots["vertical"] == "kitchen"
        assert "Resource limits: two burners, one cook." in user
        assert "Everything must be finished by 19:00." in user
        assert SOURCE in user
        assert user.index("Steps:") < user.index(SOURCE)
        assert authoring_guide() in system
        assert render_server_instructions("kitchen") in system
        assert "NOT available" in system

    def test_baseline_without_deadline(self):
        _, user, slots = render_baseline(mk_gold(deadline=None))
        assert slots["finish_at"] is None
        assert "finished by" not in user
        assert "5. Run analyze_schedule to check" in user

    def test_server_instructions_match_frozen_phase2_text(self):
        """serverInstructions as of Phase 2; Phase 3 shortened the JS copy."""
        text = render_server_instructions("kitchen")
        lines = text.split("\n")
        assert lines[0].startswith("Rhylthyme Kitchen schedules cooking and meal")
        assert "Workflow:" in lines
        assert any(line.startswith("- Fast path: **cook_recipe**") for line in lines)
        assert any(line.startswith("- Repeated work") for line in lines)
        assert "" not in lines  # JS filters every empty entry
        generic = render_server_instructions("generic")
        assert generic.startswith("Rhylthyme schedules any real-time, multi-track")
        assert "Fast path" not in generic
        assert render_server_instructions("lab").startswith("Rhylthyme Lab schedules")

    def test_baseline_md_is_current(self):
        """patterns/baseline.md is the checked-in rendered copy of the prompt."""
        assert (
            BASELINE_MD.exists()
        ), "run `python -m rhylthyme.cli.eval.patterns.baseline`"
        assert BASELINE_MD.read_text(encoding="utf-8") == render_markdown(), (
            "patterns/baseline.md is stale; regenerate with "
            "`python -m rhylthyme.cli.eval.patterns.baseline`"
        )

    def test_pattern_render_returns_turns(self):
        turns = BaselinePattern().render(mk_gold())
        assert len(turns) == 1
        turn = turns[0]
        assert isinstance(turn, Turn)
        assert turn.name == "plan_schedule"
        assert SOURCE in turn.text()
        assert turn.slots["vertical"] == "kitchen"
        assert turn.system and authoring_guide() in turn.system

    def test_turn_content_can_consume_prior_outputs(self):
        turn = Turn(name="T4", content=lambda prior: f"steps were: {prior[-1]}")
        assert turn.text(["T1 reply", "T3 reply"]) == "steps were: T3 reply"

    def test_pattern_parse_takes_last_program(self):
        other = copy.deepcopy(mk_program())
        other["programId"] = "second"
        parsed = BaselinePattern().parse([fenced(mk_program()), fenced(other)])
        assert parsed["programId"] == "second"
        assert BaselinePattern().parse(["no json at all"]) is None


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------


class TestExtract:
    def test_fenced_in_prose(self):
        assert extract_program(fenced(mk_program())) == mk_program()

    def test_bare_json(self):
        assert extract_program(json.dumps(mk_program())) == mk_program()

    def test_bare_with_trailing_text(self):
        text = (
            "Sure.\n" + json.dumps(mk_program()) + "\nLet me know if you want changes."
        )
        assert extract_program(text) == mk_program()

    def test_wrapper_object(self):
        text = json.dumps({"program": mk_program(), "note": "x"})
        assert extract_program(text) == mk_program()

    def test_skips_non_program_objects(self):
        text = (
            '```json\n{"summary": "not a program"}\n```\nthen {"nope": 1} and '
            + json.dumps(mk_program())
        )
        assert extract_program(text) == mk_program()

    def test_unfenced_language_tag(self):
        text = "```\n" + json.dumps(mk_program()) + "\n```"
        assert extract_program(text) == mk_program()

    @pytest.mark.parametrize("text", ["", "no json here", "{broken", '{"tracks": 3}'])
    def test_none_when_absent(self, text):
        assert extract_program(text) is None


# --------------------------------------------------------------------------
# run_program: scoring, zeroes, fix loop
# --------------------------------------------------------------------------


class TestRunProgram:
    def test_valid_program_scores_and_records_extras(self, tmp_path):
        client = FakeClient(
            [fenced(mk_program())], input_tokens=1000, output_tokens=500
        )
        cfg = config(tmp_path, model="claude-haiku-4-5")
        scores = run_program(mk_gold(), BaselinePattern(), client, cfg)

        assert scores.error is None
        assert scores.headline()["steps_f1"] == 1.0
        assert scores.headline()["relationships_f1"] == 1.0
        extras = scores.extras
        assert extras["calls"] == 1 and extras["fix_iterations"] == 0
        assert extras["input_tokens"] == 1000 and extras["output_tokens"] == 500
        assert extras["cost_usd"] == pytest.approx((1000 * 1.0 + 500 * 5.0) / 1e6)
        assert len(extras["cache_keys"]) == 1
        assert Path(extras["response_path"]).exists()
        assert json.loads(Path(extras["predicted_path"]).read_text()) == mk_program()
        assert extras["turns"] == 1
        assert extras["turn_slots"]["plan_schedule"]["vertical"] == "kitchen"
        transcript = json.loads(Path(extras["response_path"]).read_text())["calls"]
        assert transcript[0]["messages"][0]["role"] == "user"
        assert SOURCE in transcript[0]["messages"][0]["content"]

    def test_no_json_is_a_scored_zero(self, tmp_path):
        client = FakeClient(["I would need the tools to do that."])
        cfg = config(tmp_path, max_fix_iterations=0)
        gold = mk_gold()
        scores = run_program(gold, BaselinePattern(), client, cfg)
        assert scores.error == NO_JSON_FINDING
        assert scores.headline()["steps_recall"] == 0.0
        assert scores.steps.n_gold == 3
        assert scores.extras["predicted_path"] is None
        assert scores.extras["calls"] == 1

    def test_invalid_program_is_a_scored_zero(self, tmp_path):
        client = FakeClient([fenced(mk_program(valid=False))])
        cfg = config(tmp_path, max_fix_iterations=0)
        scores = run_program(mk_gold(), BaselinePattern(), client, cfg)
        assert scores.error and "failed validation" in scores.error
        assert scores.extras["validation_errors"]
        assert all(v == 0.0 for v in scores.headline().values())
        # The invalid program is still written so it can be inspected.
        assert scores.extras["predicted_path"] is not None

    def test_client_exception_is_a_scored_zero(self, tmp_path):
        class Boom:
            def complete(self, messages, *, system=None, model, max_tokens):
                raise RuntimeError("rate limited")

        scores = run_program(mk_gold(), BaselinePattern(), Boom(), config(tmp_path))
        assert scores.error == "RuntimeError: rate limited"
        assert scores.extras["calls"] == 0

    def test_fix_loop_sends_findings_and_recovers(self, tmp_path):
        client = FakeClient(
            [
                fenced(mk_program(valid=False)),
                "Oops, still: " + fenced(mk_program(valid=False)),
                fenced(mk_program()),
            ]
        )
        cfg = config(tmp_path, max_fix_iterations=2)
        scores = run_program(mk_gold(), BaselinePattern(), client, cfg)

        assert scores.error is None
        assert scores.extras["fix_iterations"] == 2
        assert len(client.calls) == 3
        second = client.calls[1].messages
        assert [m["role"] for m in second] == ["user", "assistant", "user"]
        assert second[2]["content"].startswith("validate_program reported 2 error(s):")
        assert "nope" in second[2]["content"]
        assert second[1]["content"] == fenced(mk_program(valid=False))
        third = client.calls[2].messages
        assert len(third) == 5 and third[4]["content"].startswith(
            "validate_program reported"
        )
        # Every call carries the same system prompt.
        assert len({call.system for call in client.calls}) == 1

    def test_fix_loop_stops_at_cap(self, tmp_path):
        client = FakeClient([fenced(mk_program(valid=False))])
        cfg = config(tmp_path, max_fix_iterations=1)
        scores = run_program(mk_gold(), BaselinePattern(), client, cfg)
        assert len(client.calls) == 2
        assert scores.extras["fix_iterations"] == 1
        assert "after 1 fix iteration" in scores.error

    def test_fix_loop_handles_missing_json_then_program(self, tmp_path):
        client = FakeClient(["Let me think.", fenced(mk_program())])
        scores = run_program(mk_gold(), BaselinePattern(), client, config(tmp_path))
        assert scores.error is None
        request = client.calls[1].messages[-1]["content"]
        assert NO_JSON_FINDING in request
        assert request.startswith(FIX_REQUEST.split("{")[0])


# --------------------------------------------------------------------------
# run_harness: cache, limit, concurrency, ordering
# --------------------------------------------------------------------------


def _gold_set(n=6):
    """n gold programs with distinct sources (so their cache keys differ)."""
    golds = []
    for i in range(n):
        gold = mk_gold(slug=f"p{i:02d}")
        gold.program = copy.deepcopy(gold.program)
        gold.source_text = SOURCE + f" ({gold.slug})"
        golds.append(gold)
    return golds


class TestRunHarness:
    def test_cache_write_then_from_cache_reproduces(self, tmp_path):
        golds = _gold_set(3)
        client = FakeClient({"*": fenced(mk_program())})
        first = run_harness(golds, config(tmp_path), client)
        assert len(client.calls) == 3
        cache_files = list((tmp_path / "out" / "cache").glob("*.json"))
        assert len(cache_files) == 3
        recorded = json.loads(cache_files[0].read_text())
        assert {
            "key",
            "model",
            "pattern",
            "slug",
            "system_sha256",
            "messages",
            "completion",
        } <= set(recorded)
        # The system prompt and the provider's raw body are not stored: the
        # cache is committed, so it is kept small.
        assert "system" not in recorded
        assert "raw" not in recorded["completion"]

        second = run_harness(golds, config(tmp_path, from_cache=True), RaisingClient())
        assert RaisingClient.calls == 0
        a, b = first.to_dict(), second.to_dict()
        assert a["programs"] == b["programs"]
        assert a["summary"] == b["summary"]
        assert a["missing"] == b["missing"] == []
        assert b["meta"]["from_cache"] is True

    def test_from_cache_missing_key_errors(self, tmp_path):
        with pytest.raises(CacheMiss) as info:
            run_harness(
                _gold_set(1), config(tmp_path, from_cache=True), RaisingClient()
            )
        assert "p00" in str(info.value) and "--from-cache" in str(info.value)

    def test_cache_key_covers_model_pattern_and_prompt(self):
        base = cache_key("m", "baseline", "sys", [{"role": "user", "content": "x"}])
        assert base == cache_key(
            "m", "baseline", "sys", [{"role": "user", "content": "x"}]
        )
        assert base != cache_key(
            "m2", "baseline", "sys", [{"role": "user", "content": "x"}]
        )
        assert base != cache_key(
            "m", "four-turn", "sys", [{"role": "user", "content": "x"}]
        )
        assert base != cache_key(
            "m", "baseline", "sys", [{"role": "user", "content": "y"}]
        )
        assert base != cache_key(
            "m", "baseline", None, [{"role": "user", "content": "x"}]
        )

    def test_cache_hit_within_one_run_is_reused(self, tmp_path):
        # Two gold programs with identical source and context render the
        # same prompt, so the second is a cache hit and the model is called once.
        golds = [mk_gold(slug="a"), mk_gold(slug="b")]
        client = FakeClient([fenced(mk_program())])
        result = run_harness(golds, config(tmp_path), client)
        assert len(client.calls) == 1
        assert [s.slug for s in result.programs] == ["a", "b"]
        assert (
            result.programs[0].extras["cache_keys"]
            == result.programs[1].extras["cache_keys"]
        )

    def test_limit(self, tmp_path):
        client = FakeClient({"*": fenced(mk_program())})
        result = run_harness(_gold_set(6), config(tmp_path, limit=2), client)
        assert [s.slug for s in result.programs] == ["p00", "p01"]
        assert len(client.calls) == 2
        assert result.meta["limit"] == 2

    def test_only_selects_slugs(self, tmp_path):
        client = FakeClient({"*": fenced(mk_program())})
        result = run_harness(
            _gold_set(6), config(tmp_path, only=["p04", "p01"]), client
        )
        # Gold order is kept, not --only order.
        assert [s.slug for s in result.programs] == ["p01", "p04"]
        assert len(client.calls) == 2
        assert result.meta["only"] == ["p04", "p01"]

    def test_only_then_limit(self, tmp_path):
        client = FakeClient({"*": fenced(mk_program())})
        result = run_harness(
            _gold_set(6), config(tmp_path, only=["p02", "p03", "p05"], limit=2), client
        )
        assert [s.slug for s in result.programs] == ["p02", "p03"]

    def test_only_unknown_slug_errors(self, tmp_path):
        with pytest.raises(KeyError) as info:
            run_harness(_gold_set(2), config(tmp_path, only=["nope"]), FakeClient([]))
        assert "nope" in str(info.value) and "p00" in str(info.value)

    def test_from_cache_results_json_is_byte_identical(self, tmp_path):
        from rhylthyme.eval.report import stamp, write_results

        golds = _gold_set(2)
        first = run_harness(
            golds, config(tmp_path), FakeClient({"*": fenced(mk_program())})
        )
        second = run_harness(golds, config(tmp_path, from_cache=True), RaisingClient())
        # The run timestamp is the only field that cannot repeat; pin it and
        # drop the from_cache/limit bookkeeping so the scores can be compared
        # byte for byte.
        for result in (first, second):
            result.meta["generated_at"] = "2026-09-14T00:00:00+00:00"
            result.meta["from_cache"] = False
            stamp(result, gold_dir="gold")
        a = write_results(first, tmp_path / "a")[0].read_bytes()
        b = write_results(second, tmp_path / "b")[0].read_bytes()
        assert a == b

    def test_concurrency_runs_all_and_keeps_order(self, tmp_path):
        class SlowClient(FakeClient):
            def complete(self, messages, **kwargs):
                # Later programs finish first; order must still follow the gold set.
                slug_hint = FakeClient.last_user_text(messages)
                time.sleep(0.05 if "p00" in slug_hint else 0.0)
                return super().complete(messages, **kwargs)

        golds = _gold_set(4)
        for gold in golds:
            gold.source_text = SOURCE + f" ({gold.slug})"
        client = SlowClient({"*": fenced(mk_program())})
        result = run_harness(golds, config(tmp_path, concurrency=4), client)
        assert [s.slug for s in result.programs] == ["p00", "p01", "p02", "p03"]
        assert len(client.calls) == 4
        assert result.meta["concurrency"] == 4
        assert result.meta["totals"]["calls"] == 4

    def test_missing_lists_zeroed_programs(self, tmp_path):
        golds = _gold_set(2)
        golds[1].source_text = SOURCE + " (broken)"
        client = FakeClient({"(broken)": "nothing to see", "*": fenced(mk_program())})
        result = run_harness(golds, config(tmp_path, max_fix_iterations=0), client)
        assert result.missing == ["p01"]
        assert result.summary["steps_f1"] == 0.5

    def test_baseline_payload_shape(self, tmp_path):
        result = run_harness(
            _gold_set(1),
            config(tmp_path, model="claude-haiku-4-5"),
            FakeClient([fenced(mk_program())]),
        )
        result.meta["generated_at"] = "2026-09-14T12:00:00+00:00"
        payload = baseline_payload(result, note="test note")
        assert payload["model"] == "claude-haiku-4-5"
        assert payload["git_note"] == "test note"
        assert payload["pattern"] == "baseline"
        assert payload["date"] == "2026-09-14"
        assert payload["results"]["summary"]["steps_f1"] == 1.0
        path = write_baseline(result, tmp_path / "baseline.json")
        written = json.loads(path.read_text())
        assert written["model"] == "claude-haiku-4-5"
        assert isinstance(written["git_note"], str) and written["git_note"]

    def test_git_note_mentions_the_repo(self):
        note = git_note()
        assert isinstance(note, str) and note


# --------------------------------------------------------------------------
# four-turn pattern: turn order, 2/1/0 scoring, retry, extras
# --------------------------------------------------------------------------

T1_OK = (
    "Here is the read-back.\n\n```json\n"
    '{"summary": "Pasta night for two people by 19:00.", '
    '"servesOrScale": "2 people", "deadline": "19:00", '
    '"constraints": ["two burners", "one cook"]}\n```'
)
T1_BAD = "Sure: pasta for two, ready at seven, on two burners."
T2_OK = (
    "```json\n"
    '{"acknowledgement": "Tracks are sequential lines of work; one duration '
    'kind per step; stepIds are global.", '
    '"resourceConstraints": [{"task": "burner", "maxConcurrent": 2}], '
    '"actors": 1}\n```'
)
T2_BAD = "Understood: tracks, steps, triggers, constraints."
T3_OK = (
    "```json\n"
    '{"steps": ['
    '{"stepId": "boil", "name": "Boil the pasta", "task": "burner",'
    ' "duration": {"type": "fixed", "seconds": 600},'
    ' "sourceSpan": {"quote": "Boil the pasta for 10 minutes", "occurrence": 1},'
    ' "inferred": false},'
    '{"stepId": "warm", "name": "Warm the sauce", "task": "burner",'
    ' "duration": {"type": "fixed", "seconds": 300},'
    ' "sourceSpan": {"quote": "warm the sauce for 5 minutes", "occurrence": 1},'
    ' "inferred": false},'
    '{"stepId": "preheat", "name": "Heat the pan", "task": "burner",'
    ' "duration": {"type": "fixed", "seconds": 120},'
    ' "sourceSpan": null, "inferred": true}'
    "]}\n```"
)


def four_turn_replies(t1=T1_OK, t2=T2_OK, t3=T3_OK, t4=None):
    return [t1, t2, t3, t4 if t4 is not None else fenced(mk_program())]


class TestFourTurnRendering:
    def test_renders_the_four_turns_in_order(self):
        turns = FourTurnPattern().render(mk_gold())
        assert [turn.name for turn in turns] == [
            "T1 read-back",
            "T2 model check",
            "T3 extraction",
            "T4 relationships",
        ]
        texts = [turn.text() for turn in turns]
        for index, text in enumerate(texts):
            assert text.startswith(f"Turn {index + 1} of 4 — ")
            assert not re.search(r"\{[A-Za-z][A-Za-z0-9_]*\}", text), text[:200]
        # The source text is embedded in T1 and T3 only.
        assert SOURCE in texts[0] and SOURCE in texts[2]
        assert SOURCE not in texts[1] and SOURCE not in texts[3]
        # Slots come from context.json, as the baseline's do.
        assert "Pasta night for 2 people" in texts[0]
        assert "Resource limits: two burners, one cook." in texts[0]
        assert "Everything must be finished by 19:00." in texts[0]
        assert "in a kitchen where you have: two burners, one cook" in texts[2]
        assert "ready at 19:00" in texts[2]
        assert 'analyze_schedule with finishAt="19:00"' in texts[3]
        assert "search_public_recipes or cook_recipe" in texts[3]
        # One system prompt for every turn: the same guide the baseline sends.
        assert len({turn.system for turn in turns}) == 1
        assert FOUR_TURN_PREAMBLE in turns[0].system
        assert authoring_guide() in turns[0].system
        assert render_server_instructions("kitchen") in turns[0].system
        assert turns[0].slots["vertical"] == "kitchen"
        assert turns[2].slots["turn"] == "T3"

    def test_without_deadline_or_constraints_the_slots_still_fill(self):
        gold = mk_gold(deadline=None)
        gold.context.pop("constraints")
        texts = [turn.text() for turn in FourTurnPattern().render(gold)]
        assert "No finishing time was given" in texts[0]
        assert "Resource limits: none were given" in texts[0]
        assert "ready at the earliest time the work allows" in texts[2]
        assert "5. Run analyze_schedule to check" in texts[3]

    def test_render_rejects_unknown_and_missing_slots(self):
        assert render("a {x}", {"x": "1"}) == "a 1"
        with pytest.raises(KeyError, match="unknown slot"):
            render("a {x}", {"x": "1", "y": "2"})
        with pytest.raises(KeyError, match="missing slot"):
            render("a {x} {y}", {"x": "1"})
        assert slots_in('{"type":"afterStep"}') == []

    def test_parse_takes_t4s_program(self):
        other = copy.deepcopy(mk_program())
        other["programId"] = "t4"
        pattern = FourTurnPattern()
        assert pattern.parse([fenced(mk_program()), fenced(other)])["programId"] == "t4"
        # An earlier turn's program is never used: only the last reply counts.
        assert pattern.parse([fenced(mk_program()), "no json here"]) is None
        assert pattern.parse([]) is None

    def test_shape_checks(self):
        assert check_t1(T1_OK)["servesOrScale"] == "2 people"
        assert check_t1(T1_BAD) is None
        assert check_t1('{"summary": "x", "servesOrScale": "y"}') is None  # no list
        assert check_t2(T2_OK)["actors"] == 1
        assert check_t2(T2_BAD) is None
        assert check_t2('{"acknowledgement": "x", "resourceConstraints": []}') is None
        steps = t3_steps(T3_OK)
        assert [step["stepId"] for step in steps] == ["boil", "warm", "preheat"]
        assert unsupported_step_ids(steps) == ["preheat"]
        assert t3_steps("nothing here") == []


class TestFourTurnRun:
    def test_four_calls_one_growing_conversation(self, tmp_path):
        client = FakeClient(four_turn_replies())
        scores = run_program(
            mk_gold(), FourTurnPattern(), client, config(tmp_path, pattern="four-turn")
        )
        assert scores.error is None
        assert scores.extras["calls"] == 4
        assert scores.extras["t1_score"] == 2
        assert scores.extras["t2_score"] == 2
        assert scores.extras["retries"] == 0
        assert scores.extras["t3_step_count"] == 3
        assert scores.extras["unsupported_step_ids"] == ["preheat"]
        assert scores.extras["turns"] == 4
        assert scores.headline()["relationships_f1"] == 1.0
        # Each call carries every earlier turn: user, (assistant, user)*.
        lengths = [len(call.messages) for call in client.calls]
        assert lengths == [1, 3, 5, 7]
        roles = [m["role"] for m in client.calls[-1].messages]
        assert roles == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        starts = [
            m["content"][:14] for m in client.calls[-1].messages if m["role"] == "user"
        ]
        assert starts == [
            "Turn 1 of 4 — ",
            "Turn 2 of 4 — ",
            "Turn 3 of 4 — ",
            "Turn 4 of 4 — ",
        ]
        assert len({call.system for call in client.calls}) == 1

    def test_retry_scores_one(self, tmp_path):
        client = FakeClient([T1_BAD, T1_OK] + four_turn_replies()[1:])
        scores = run_program(
            mk_gold(), FourTurnPattern(), client, config(tmp_path, pattern="four-turn")
        )
        assert scores.error is None
        assert scores.extras["t1_score"] == 1
        assert scores.extras["t2_score"] == 2
        assert scores.extras["retries"] == 1
        assert scores.extras["calls"] == 5
        retry = client.calls[1].messages[-1]["content"]
        assert retry.startswith("Your reply did not carry the JSON object")
        assert "filled in for this recipe" in retry
        assert '"servesOrScale"' in retry
        # The retry stays in the transcript: rejected answer, the retry
        # request, then the accepted answer, and only then T2.
        after = client.calls[2].messages
        assert [m["role"] for m in after] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert after[1]["content"] == T1_BAD
        assert after[3]["content"] == T1_OK
        assert after[4]["content"].startswith("Turn 2 of 4 — ")

    def test_zero_when_the_retry_also_misses(self, tmp_path):
        client = FakeClient(
            [T1_BAD, T1_BAD, T2_BAD, T2_BAD, T3_OK, fenced(mk_program())]
        )
        scores = run_program(
            mk_gold(), FourTurnPattern(), client, config(tmp_path, pattern="four-turn")
        )
        assert scores.extras["t1_score"] == 0
        assert scores.extras["t2_score"] == 0
        assert scores.extras["retries"] == 2
        assert scores.extras["calls"] == 6
        # A missed read-back is not fatal: the program still lands and scores.
        assert scores.error is None
        assert scores.headline()["steps_f1"] == 1.0

    def test_fix_loop_runs_after_t4(self, tmp_path):
        client = FakeClient(
            four_turn_replies(t4=fenced(mk_program(valid=False)))
            + [fenced(mk_program())]
        )
        scores = run_program(
            mk_gold(),
            FourTurnPattern(),
            client,
            config(tmp_path, pattern="four-turn", max_fix_iterations=1),
        )
        assert scores.error is None
        assert scores.extras["fix_iterations"] == 1
        assert scores.extras["calls"] == 5
        fix = client.calls[-1].messages[-1]["content"]
        assert fix.startswith("validate_program reported")

    def test_registered_and_runs_through_the_harness(self, tmp_path):
        assert "four-turn" in PATTERNS
        assert isinstance(get_pattern("four-turn"), FourTurnPattern)
        result = run_harness(
            _gold_set(1),
            config(tmp_path, pattern="four-turn"),
            FakeClient(four_turn_replies()),
        )
        assert result.programs[0].error is None
        assert result.programs[0].extras["pattern"] == "four-turn"
        assert result.programs[0].extras["t1_score"] == 2


class TestJsParity:
    """The Python copies of the templates are the JS templates."""

    def test_python_templates_match_prompts_js(self):
        import shutil as _shutil

        if _shutil.which("node") is None:
            pytest.skip("node not available")
        if find_prompts_js() is None:
            pytest.skip("rhylthyme-server/mcp-api/prompts.js not found")
        assert parity_differences() == [], (
            "eval/patterns/four_turn.py has drifted from "
            "rhylthyme-server/mcp-api/prompts.js; copy the JS strings over"
        )

    def test_render_four_turns_helper(self):
        turns = render_four_turns(
            goal="g",
            finish_at="18:00",
            constraints="one oven",
            source_text="Bake it.",
            vertical="kitchen",
            one_shot="cook_recipe",
        )
        assert [turn["key"] for turn in turns] == ["T1", "T2", "T3", "T4"]
        assert [turn["key"] for turn in FOUR_TURNS[:1]] == ["T1"]
        assert "Bake it." in turns[0]["text"]
        assert "Bake it." not in turns[1]["text"]


# --------------------------------------------------------------------------
# Registry, client and prices
# --------------------------------------------------------------------------


class TestRegistryAndClients:
    def test_registry_has_baseline_and_rejects_unknown(self):
        assert "baseline" in PATTERNS
        assert isinstance(get_pattern("baseline"), Pattern)
        with pytest.raises(KeyError):
            get_pattern("four-turn-not-yet")

    def test_custom_pattern_plugs_in(self, tmp_path):
        class Echo(Pattern):
            name = "echo"

            def run(self, gold, session):
                completion = session.complete([{"role": "user", "content": "hi"}])
                return PatternResult(program=extract_program(completion.text))

        try:
            PATTERNS["echo"] = Echo
            result = run_harness(
                _gold_set(1),
                config(tmp_path, pattern="echo"),
                FakeClient([json.dumps(mk_program())]),
            )
        finally:
            PATTERNS.pop("echo", None)
        assert result.programs[0].error is None
        assert result.programs[0].extras["pattern"] == "echo"

    def test_fake_client_substring_and_sequence(self):
        by_key = FakeClient({"brunch": "B", "*": "default"})
        assert (
            by_key.complete(
                [{"role": "user", "content": "plan brunch"}], model="m", max_tokens=1
            ).text
            == "B"
        )
        assert (
            by_key.complete(
                [{"role": "user", "content": "other"}], model="m", max_tokens=1
            ).text
            == "default"
        )
        seq = FakeClient(
            ["one", Completion(text="two", input_tokens=7, output_tokens=3)]
        )
        assert seq.complete([], model="m", max_tokens=1).text == "one"
        two = seq.complete([], model="m", max_tokens=1)
        assert (two.text, two.input_tokens) == ("two", 7)
        assert seq.complete([], model="m", max_tokens=1).text == "two"
        assert len(seq.calls) == 3

    def test_prices(self):
        assert price_for("claude-haiku-4-5") == (1.0, 5.0)
        assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
        assert price_for("claude-opus-5") == (5.0, 25.0)
        assert price_for("gpt-99") is None
        assert estimate_cost("gpt-99", 1, 1) is None
        assert estimate_cost("claude-sonnet-5", 1_000_000, 100_000) == pytest.approx(
            3.0
        )


# --------------------------------------------------------------------------
# Real gold set through the harness (fake client)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not GOLD_DIR.exists(), reason="gold set not available")
def test_gold_set_echo_scores_one(tmp_path):
    golds = load_gold_set(GOLD_DIR)
    responses = {
        gold.context.get("title", gold.slug): fenced(gold.program) for gold in golds
    }
    result = run_harness(golds, config(tmp_path, concurrency=3), FakeClient(responses))
    assert result.missing == []
    for key, value in result.summary.items():
        assert value == (0.0 if key == "unsupported_rate" else 1.0), (key, value)


# --------------------------------------------------------------------------
# Real API (opt in)
# --------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.skipif(
    os.environ.get("RHYLTHYME_EVAL_LIVE") != "1",
    reason="set RHYLTHYME_EVAL_LIVE=1 to call the real API",
)
@pytest.mark.skipif(not GOLD_DIR.exists(), reason="gold set not available")
def test_live_one_program(tmp_path):
    from rhylthyme.cli.eval.llm import AnthropicClient

    model = os.environ.get("RHYLTHYME_EVAL_MODEL", "claude-haiku-4-5")
    golds = load_gold_set(GOLD_DIR)[:1]
    result = run_harness(
        golds,
        config(tmp_path, model=model, max_fix_iterations=0),
        AnthropicClient(),
    )
    scores = result.programs[0]
    assert scores.extras["calls"] == 1
    assert scores.extras["output_tokens"] > 0
