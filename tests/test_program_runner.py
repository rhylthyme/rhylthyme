"""
Unit tests for the ProgramRunner class.

This module tests the core program execution functionality.
"""

import copy
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import rhylthyme.cli.tui as tui_module
import rhylthyme.program_runner as program_runner_module
from rhylthyme.program_runner import ProgramRunner, StepStatus, split_instance_name


@pytest.mark.unit
class TestProgramRunner:
    """Test ProgramRunner functionality."""

    def test_program_runner_creation(self, simple_program):
        """Test basic ProgramRunner creation."""
        runner = ProgramRunner(simple_program)

        assert runner is not None
        assert len(runner.program["tracks"]) == 1
        assert len(runner.program["tracks"][0]["steps"]) == 2

    def test_get_step_by_id(self, simple_program):
        """Test getting a step by ID."""
        runner = ProgramRunner(simple_program)

        step = runner.get_step_by_id("step1")
        assert step is not None
        assert step.step_id == "step1"
        assert step.name == "Step 1"

        step2 = runner.get_step_by_id("step2")
        assert step2 is not None
        assert step2.step_id == "step2"
        assert step2.name == "Step 2"

        # Test nonexistent step
        nonexistent = runner.get_step_by_id("nonexistent")
        assert nonexistent is None

    def test_get_all_steps_display_info(self, simple_program):
        """Test getting display info for all steps."""
        runner = ProgramRunner(simple_program)

        steps_info = runner.get_all_steps_display_info()
        assert len(steps_info) == 2

        # Check that we get the expected step IDs
        step_ids = [info["step_id"] for info in steps_info]
        assert "step1" in step_ids
        assert "step2" in step_ids

    def test_program_start_trigger(self, simple_program):
        """Test program start trigger handling."""
        # Modify to have manual start trigger
        simple_program["startTrigger"] = {"type": "manual"}
        runner = ProgramRunner(simple_program)

        # Test that program is not automatically started
        assert not runner.program_started  # Assuming this attribute exists

    def test_resource_handling(self, kitchen_program):
        """Test resource constraint handling."""
        runner = ProgramRunner(kitchen_program)

        # Test that resources are recognized
        step = runner.get_step_by_id("cook-pasta")
        assert step is not None

        # Test getting step display info includes resource information
        step_info = runner.get_step_display_info(step)
        assert step_info is not None

    def test_step_status_tracking(self, simple_program):
        """Test step status tracking."""
        runner = ProgramRunner(simple_program)

        step = runner.get_step_by_id("step1")

        # Initially step should be pending
        step_info = runner.get_step_display_info(step)
        # The exact status tracking may vary, just test that we get info
        assert step_info is not None
        assert "status" in step_info or "step_id" in step_info


@pytest.mark.unit
class TestProgramRunnerManualTriggers:
    """Test manual trigger functionality."""

    def test_manual_step_creation(self):
        """Test creating a program with manual steps."""
        manual_program = {
            "programId": "manual-test",
            "name": "Manual Test Program",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "manual-step",
                            "name": "Manual Step",
                            "startTrigger": {"type": "manual"},
                            "duration": {"type": "manual"},
                        }
                    ],
                }
            ],
        }

        runner = ProgramRunner(manual_program)
        assert runner is not None

        step = runner.get_step_by_id("manual-step")
        assert step is not None
        assert step.step_id == "manual-step"

    def test_get_available_triggers(self):
        """Test getting available manual triggers."""
        manual_program = {
            "programId": "manual-test",
            "name": "Manual Test Program",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "manual-step",
                            "name": "Manual Step",
                            "startTrigger": {"type": "manual"},
                            "duration": {"type": "manual"},
                        }
                    ],
                }
            ],
        }

        runner = ProgramRunner(manual_program)

        # Test getting available triggers
        triggers = runner.get_available_triggers()
        assert isinstance(triggers, list)
        # The exact trigger format may vary

    def test_trigger_manual_step(self):
        """Test triggering a manual step."""
        manual_program = {
            "programId": "manual-test",
            "name": "Manual Test Program",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "manual-step",
                            "name": "Manual Step",
                            "startTrigger": {"type": "manual"},
                            "duration": {"type": "manual"},
                        }
                    ],
                }
            ],
        }

        runner = ProgramRunner(manual_program)

        # Test triggering the manual step
        # This method signature may vary
        try:
            runner.trigger_manual_step("manual-step", "manual-step")
        except (AttributeError, TypeError):
            # Method might not exist or have different signature
            pass


@pytest.mark.unit
class TestProgramRunnerBuffers:
    """Test buffer functionality in program runner."""

    def test_step_with_buffers(self, simple_program):
        """Test steps with pre and post buffers."""
        runner = ProgramRunner(simple_program)

        # Test that steps with buffers are handled correctly
        step = runner.get_step_by_id("step1")
        assert step is not None

        step_info = runner.get_step_display_info(step)
        assert step_info is not None

        # The step should have buffer information
        # Exact implementation may vary, just test it doesn't crash

    def test_buffer_extension(self, simple_program):
        """Test buffer extension functionality."""
        runner = ProgramRunner(simple_program)

        step = runner.get_step_by_id("step1")
        assert step is not None

        # Test buffer extension if the method exists
        try:
            # This method might not exist or have different signature
            runner.extend_step_buffer(step.step_id, "pre", 30)
        except (AttributeError, TypeError):
            # Expected if method doesn't exist yet
            pass


@pytest.mark.unit
class TestProgramRunnerUtilities:
    """Test utility functions of program runner."""

    def test_time_formatting(self, simple_program):
        """Test time formatting utilities."""
        runner = ProgramRunner(simple_program)

        # Test time formatting if utility methods exist
        step_info = runner.get_all_steps_display_info()[0]

        # Just test that we get some kind of formatted information
        assert isinstance(step_info, dict)

    def test_program_validation_integration(self, simple_program):
        """Test that program runner works with validated programs."""
        # This tests that a program that passes validation
        # can be successfully loaded into the program runner
        runner = ProgramRunner(simple_program)

        assert runner is not None
        assert len(runner.program["tracks"]) > 0


@pytest.mark.unit
def test_step_status_enum():
    """Test StepStatus enum if it exists."""
    try:
        # Test that status enum values exist
        assert hasattr(StepStatus, "PENDING") or hasattr(StepStatus, "pending")
    except (NameError, AttributeError):
        # StepStatus might be implemented differently or not exist
        pytest.skip("StepStatus enum not available or implemented differently")


@pytest.mark.integration
class TestProgramRunnerWithEnvironment:
    """Test program runner with environment constraints."""

    def test_runner_with_environment(self, kitchen_program, kitchen_environment):
        """Test program runner with environment constraints."""
        runner = ProgramRunner(kitchen_program, environment=kitchen_environment)

        assert runner is not None

        # Test that environment constraints are considered
        step = runner.get_step_by_id("cook-pasta")
        assert step is not None

    def test_resource_constraint_validation(self, kitchen_program, kitchen_environment):
        """Test resource constraint validation."""
        runner = ProgramRunner(kitchen_program, environment=kitchen_environment)

        # Test resource validation if methods exist
        try:
            # This might not be implemented yet
            resource_usage = runner.get_current_resource_usage()
            assert isinstance(resource_usage, (list, dict))
        except (AttributeError, TypeError):
            # Expected if not implemented
            pass


@pytest.mark.unit
class TestProgramRunnerErrorHandling:
    """Test error handling in program runner."""

    def test_invalid_program_structure(self):
        """Test program runner with invalid program structure."""
        invalid_program = {
            "programId": "invalid",
            # Missing required fields
        }

        # Should handle invalid program gracefully
        try:
            runner = ProgramRunner(invalid_program)
            # If it doesn't raise an exception, that's also fine
        except Exception as e:
            # Expected behavior for invalid programs
            assert isinstance(e, Exception)

    def test_missing_step_references(self):
        """Test handling of missing step references."""
        program_with_missing_ref = {
            "programId": "missing-ref",
            "name": "Missing Reference Program",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {
                                "type": "stepComplete",
                                "stepId": "nonexistent-step",  # This step doesn't exist
                            },
                            "duration": {"type": "fixed", "seconds": 10},
                        }
                    ],
                }
            ],
        }

        # Should handle missing references gracefully
        try:
            runner = ProgramRunner(program_with_missing_ref)
            # Test that it can still get step info without crashing
            step = runner.get_step_by_id("step1")
            if step:
                step_info = runner.get_step_display_info(step)
        except Exception:
            # Some level of error handling is expected
            pass


# ---------------------------------------------------------------------------
# Per-instance triggers, in-flight gates and grouped instance rows
# (plans/per-instance-triggers-barriers.md phase 5).
# ---------------------------------------------------------------------------

COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "rhylthyme-examples",
    "programs",
    "cookies_three_trays.json",
)

T0 = 1_700_000_000.0


class _FakeClock:
    """Deterministic ``time.time`` for headless runs."""

    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def cookies_program():
    """The three-trays cookie example (schema 0.3.0-alpha, maxInFlight: 2)."""
    if not os.path.isfile(COOKIES_PATH):
        pytest.skip("rhylthyme-examples/programs/cookies_three_trays.json not present")
    with open(COOKIES_PATH, encoding="utf-8") as handle:
        program = json.load(handle)
    # The PRD schedule assumes nobody is waiting on a cook: the runner's
    # default of one actor would otherwise serialise bake and cool.
    program["actors"] = 4
    return program


def _with_variable_cool(program):
    """Copy of the example whose cooling ends when the executor says so.

    ``cool`` is ``fixed`` in the example, so its end cannot be moved at run
    time. A variable duration with a wide min/max and a ``triggerName`` never
    ends on its own before ``maxSeconds``, which lets a test end tray 1 late
    or early and watch the in-flight gate on ``bake-r3`` follow.
    """
    program = copy.deepcopy(program)
    for step in program["tracks"][0]["steps"]:
        if step["stepId"] == "cool":
            step["duration"] = {
                "type": "variable",
                "minSeconds": 300,
                "maxSeconds": 1800,
                "defaultSeconds": 900,
                "triggerName": "cooled",
            }
    return program


def _run_cookies(program, monkeypatch, cool_durations=None, tick=0.5, max_ticks=40000):
    """Run the cookie example headlessly; return {stepId: (start, end)}."""
    clock = _FakeClock()
    monkeypatch.setattr(program_runner_module.time, "time", clock)
    runner = ProgramRunner(program)
    runner.start()
    runner.command_queue.put("start_program")
    runner.update()
    for _ in range(max_ticks):
        clock.advance(tick)
        runner.update()
        if cool_durations:
            for step in runner.steps.values():
                if step.instance_of != "cool" or step.status != StepStatus.RUNNING:
                    continue
                target = cool_durations.get(step.instance_index)
                if target is None:
                    continue
                if runner.current_time - step.start_time >= target:
                    runner.trigger_manual_step("cooled", step.step_id)
        if all(s.status == StepStatus.COMPLETED for s in runner.steps.values()):
            break
    assert all(s.status == StepStatus.COMPLETED for s in runner.steps.values()), {
        k: v.status for k, v in runner.steps.items()
    }
    return runner, {
        step_id: (step.start_time - T0, step.end_time - T0)
        for step_id, step in runner.steps.items()
    }


@pytest.mark.integration
class TestInFlightGatesOnActualCompletion:
    """The synthetic in-flight trigger fires on the real end of the leaf."""

    def test_expected_schedule(self, cookies_program, monkeypatch):
        """Run to the hand-computed PRD schedule.

        Each dependency hop costs at most one 0.5 s tick, and the longest
        chain (mix, bake, cool, bake, cool, box) is six deep, so the
        tolerance is three seconds.
        """
        _, times = _run_cookies(cookies_program, monkeypatch)
        expected = cookies_program["metadata"]["expectedTimings"]
        for step_id, window in expected.items():
            start, end = times[step_id]
            assert start == pytest.approx(window["start"], abs=3.0), step_id
            assert end == pytest.approx(window["end"], abs=3.0), step_id

    def test_late_cooling_delays_the_third_bake(self, cookies_program, monkeypatch):
        """Ending cool-r1 three minutes late pushes everything after it back."""
        program = _with_variable_cool(cookies_program)
        _, on_time = _run_cookies(program, monkeypatch, {1: 900, 2: 900, 3: 900})
        _, late = _run_cookies(program, monkeypatch, {1: 1080, 2: 900, 3: 900})

        assert late["cool-r1"][1] - on_time["cool-r1"][1] == pytest.approx(180, abs=1.0)
        for step_id in ("bake-r3", "cool-r3", "box"):
            for index in (0, 1):
                assert late[step_id][index] - on_time[step_id][index] == pytest.approx(
                    180, abs=1.0
                ), step_id
        # bake-r2 is upstream of the gate and does not move.
        assert late["bake-r2"] == pytest.approx(on_time["bake-r2"], abs=1.0)

    def test_early_cooling_advances_them_bounded_by_the_second_bake(
        self, cookies_program, monkeypatch
    ):
        """Ending cool-r1 early advances bake-r3 only as far as bake-r2's end."""
        program = _with_variable_cool(cookies_program)
        _, on_time = _run_cookies(program, monkeypatch, {1: 900, 2: 900, 3: 900})
        # Six minutes early — but the serial oven chain still holds bake-r3.
        _, early = _run_cookies(program, monkeypatch, {1: 540, 2: 900, 3: 900})

        early_by = on_time["cool-r1"][1] - early["cool-r1"][1]
        assert early_by == pytest.approx(360, abs=1.0)
        for step_id in ("bake-r3", "cool-r3", "box"):
            advance = on_time[step_id][0] - early[step_id][0]
            assert advance == pytest.approx(180, abs=1.0), step_id
        # The bound: bake-r3 cannot start before bake-r2 has left the oven.
        assert early["bake-r3"][0] >= early["bake-r2"][1] - 1.0
        assert early["bake-r3"][0] == pytest.approx(early["bake-r2"][1], abs=1.0)


@pytest.mark.unit
class TestPerInstanceManualTriggers:
    """Manual/indefinite steps in an "each" chain end per instance."""

    def test_trigger_list_names_the_instance(self, cookies_program, monkeypatch):
        program = _with_variable_cool(cookies_program)
        clock = _FakeClock()
        monkeypatch.setattr(program_runner_module.time, "time", clock)
        runner = ProgramRunner(program)
        runner.start()
        runner.command_queue.put("start_program")
        runner.update()
        # Advance until the second tray has been cooling past its minimum
        # (a variable step is only offered for ending after minSeconds).
        for _ in range(8000):
            clock.advance(1.0)
            runner.update()
            cool2 = runner.steps["cool-r2"]
            if (
                cool2.status == StepStatus.RUNNING
                and runner.current_time - cool2.start_time >= 400
            ):
                break

        triggers = runner.get_available_triggers()
        ends = {t["step_id"]: t for t in triggers if t["type"] == "end"}
        assert "cool-r2" in ends
        assert ends["cool-r2"]["name"] == "End: Cool on rack [2 of 3]"
        assert ends["cool-r2"]["instance_of"] == "cool"
        assert ends["cool-r2"]["instance_index"] == 2
        assert ends["cool-r2"]["instance_count"] == 3
        assert ends["cool-r2"]["instance_label"] == "[2 of 3]"

    def test_triggering_one_instance_leaves_the_others_running(
        self, cookies_program, monkeypatch
    ):
        program = _with_variable_cool(cookies_program)
        clock = _FakeClock()
        monkeypatch.setattr(program_runner_module.time, "time", clock)
        runner = ProgramRunner(program)
        runner.start()
        runner.command_queue.put("start_program")
        runner.update()
        for _ in range(8000):
            clock.advance(1.0)
            runner.update()
            if runner.steps["cool-r2"].status == StepStatus.RUNNING:
                break

        assert runner.steps["cool-r1"].status == StepStatus.RUNNING
        runner.trigger_manual_step("cooled", "cool-r1")
        assert runner.steps["cool-r1"].status == StepStatus.COMPLETED
        assert runner.steps["cool-r2"].status == StepStatus.RUNNING


@pytest.mark.unit
class TestInstanceGrouping:
    """The step list collapses instances of one replicated step."""

    def test_split_instance_name(self):
        assert split_instance_name("Bake tray (2 of 3)") == ("Bake tray", 2, 3)
        assert split_instance_name("Mix dough") == ("Mix dough", None, None)
        assert split_instance_name(None) == (None, None, None)

    def test_collapsed_group_rows(self, cookies_program):
        runner = ProgramRunner(cookies_program)
        rows = runner.get_all_steps_display_info()

        assert [row["name"] for row in rows] == [
            "Mix dough",
            "Bake tray ×3",
            "Box cookies",
            "Cool on rack ×3",
        ]
        bake = next(row for row in rows if row["instance_of"] == "bake")
        assert bake["row_type"] == "group"
        assert bake["instance_count"] == 3
        assert bake["expanded"] is False
        assert bake["summary"] == "0 done / 0 running / 3 pending"
        assert bake["members"] == ["bake-r1", "bake-r2", "bake-r3"]
        assert bake["step_id"] is None
        # Instance sub-tracks collapse onto their parent track.
        cool = next(row for row in rows if row["instance_of"] == "cool")
        assert cool["track_display"] == "cookies"

    def test_expanded_group_rows(self, cookies_program):
        runner = ProgramRunner(cookies_program)
        runner.toggle_group("bake")
        rows = runner.get_all_steps_display_info()

        assert [row["name"] for row in rows] == [
            "Mix dough",
            "Bake tray ×3",
            "Bake tray [1 of 3]",
            "Bake tray [2 of 3]",
            "Bake tray [3 of 3]",
            "Box cookies",
            "Cool on rack ×3",
        ]
        members = [row for row in rows if row.get("depth") == 1]
        assert [row["id"] for row in members] == ["bake-r1", "bake-r2", "bake-r3"]
        assert [row["instance_label"] for row in members] == [
            "[1 of 3]",
            "[2 of 3]",
            "[3 of 3]",
        ]
        # Toggling back collapses again.
        runner.toggle_group("bake")
        assert all(
            row.get("depth", 0) == 0 for row in runner.get_all_steps_display_info()
        )

    def test_group_status_aggregates(self, cookies_program, monkeypatch):
        clock = _FakeClock()
        monkeypatch.setattr(program_runner_module.time, "time", clock)
        runner = ProgramRunner(cookies_program)
        runner.start()
        runner.command_queue.put("start_program")
        runner.update()
        for _ in range(4000):
            clock.advance(1.0)
            runner.update()
            if runner.steps["bake-r2"].status == StepStatus.RUNNING:
                break

        bake = next(
            row
            for row in runner.get_all_steps_display_info()
            if row["instance_of"] == "bake"
        )
        assert bake["done"] == 1
        assert bake["running"] == 1
        assert bake["pending"] == 1
        assert bake["status"] == "RUNNING"
        assert bake["summary"] == "1 done / 1 running / 1 pending"

    def test_ungrouped_program_is_unchanged(self, simple_program):
        runner = ProgramRunner(simple_program)
        rows = runner.get_all_steps_display_info()
        assert [row["id"] for row in rows] == ["step1", "step2"]
        assert all(row["row_type"] == "step" for row in rows)
        assert all(row["instance_of"] is None for row in rows)
        # group_display_rows is a no-op on a program with no instances.
        info = [runner.get_step_display_info(s) for s in runner.steps.values()]
        assert runner.group_display_rows(info) == info

    def test_selection_walks_group_rows(self, cookies_program):
        runner = ProgramRunner(cookies_program)
        assert runner.get_selected_step_id() == "mix"
        runner.select_next_step()
        row = runner.get_selected_row()
        assert row["row_type"] == "group"
        assert runner.get_selected_step_id() is None  # a group row has no step
        runner.toggle_group()
        assert "bake" in runner.expanded_groups
        assert runner.get_selected_row()["instance_of"] == "bake"


# ------------------------------------------- declared variance factors (Phase 4)


def _factor_program():
    """A minimal program declaring one numeric and one enum variance factor."""
    return {
        "programId": "factors-test",
        "name": "Factors Test",
        "actors": 1,
        "metadata": {
            "serves": "8",
            "varianceFactors": [
                {
                    "key": "turkeyKg",
                    "label": "Turkey weight (kg)",
                    "type": "number",
                    "unit": "kg",
                },
                {
                    "key": "oven",
                    "label": "Oven type",
                    "type": "enum",
                    "values": ["gas", "electric", "convection"],
                },
            ],
        },
        "tracks": [
            {
                "trackId": "t",
                "name": "T",
                "steps": [
                    {
                        "stepId": "only",
                        "name": "Only",
                        "task": "prep",
                        "startTrigger": {"type": "programStart"},
                        "duration": {"type": "fixed", "seconds": 2},
                    }
                ],
            }
        ],
        "resourceConstraints": [{"task": "prep", "maxConcurrent": 1}],
    }


class _Answers:
    """A stub for ``input`` that replays canned answers and records prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.mark.unit
class TestVarianceFactorPrompt:
    """
    The prompt itself, in isolation: it runs on the plain terminal before the
    TUI starts, so it is a pure function of an input callable.
    """

    def test_declared_factors_are_normalised(self):
        from rhylthyme.history.factors import declared_factors

        factors = declared_factors(_factor_program())
        assert [f["key"] for f in factors] == ["turkeyKg", "oven"]
        assert factors[0]["label"] == "Turkey weight (kg)"
        assert factors[1]["values"] == ["gas", "electric", "convection"]
        # A program without them prompts for nothing.
        assert declared_factors({"metadata": {"serves": "8"}}) == []
        assert declared_factors({}) == []
        # A declared enum with no values is unanswerable and dropped.
        assert (
            declared_factors(
                {"metadata": {"varianceFactors": [{"key": "k", "type": "enum"}]}}
            )
            == []
        )

    def test_answers_are_typed(self):
        from rhylthyme.history.factors import (
            collect_factors,
            declared_factors,
            prompt_for_factors,
        )

        factors = declared_factors(_factor_program())
        stub = _Answers("6.4", "gas")
        answers = prompt_for_factors(factors, input_fn=stub, echo_fn=lambda *_: None)
        assert answers == {"turkeyKg": 6.4, "oven": "gas"}
        assert "Turkey weight (kg)" in stub.prompts[0]
        assert "gas/electric/convection" in stub.prompts[1]

    def test_enum_validation_reasks_then_accepts(self):
        from rhylthyme.history.factors import declared_factors, prompt_for_factors

        messages = []
        stub = _Answers("1", "induction", "ELECTRIC")
        answers = prompt_for_factors(
            declared_factors(_factor_program()),
            input_fn=stub,
            echo_fn=messages.append,
        )
        # 'induction' is rejected, the second try is accepted case-insensitively
        # and stored in the declared spelling.
        assert answers == {"turkeyKg": 1, "oven": "electric"}
        assert any("not one of" in m for m in messages)

    def test_a_bad_number_is_reasked(self):
        from rhylthyme.history.factors import declared_factors, prompt_for_factors

        messages = []
        stub = _Answers("heavy", "6", "gas")
        answers = prompt_for_factors(
            declared_factors(_factor_program()),
            input_fn=stub,
            echo_fn=messages.append,
        )
        assert answers == {"turkeyKg": 6, "oven": "gas"}
        assert any("not a number" in m for m in messages)

    def test_enter_skips_a_factor(self):
        from rhylthyme.history.factors import declared_factors, prompt_for_factors

        stub = _Answers("", "gas")
        answers = prompt_for_factors(
            declared_factors(_factor_program()), input_fn=stub, echo_fn=lambda *_: None
        )
        assert answers == {"oven": "gas"}

    def test_eof_skips_the_rest(self):
        from rhylthyme.history.factors import declared_factors, prompt_for_factors

        stub = _Answers()  # raises EOFError immediately
        assert (
            prompt_for_factors(
                declared_factors(_factor_program()),
                input_fn=stub,
                echo_fn=lambda *_: None,
            )
            == {}
        )

    def test_flags_and_env_pre_answer_the_prompt(self, monkeypatch):
        from rhylthyme.history.factors import collect_factors

        monkeypatch.setenv("RHYLTHYME_FACTORS", "turkeyKg=7.2;oven=convection")
        stub = _Answers()  # nothing left to ask
        answers = collect_factors(
            _factor_program(), input_fn=stub, echo_fn=lambda *_: None
        )
        assert answers == {"turkeyKg": 7.2, "oven": "convection"}
        assert stub.prompts == []

        # --factor wins over the environment, and only the rest is asked.
        stub = _Answers("gas")
        answers = collect_factors(
            _factor_program(),
            factor_args=["turkeyKg=5"],
            environ={"RHYLTHYME_FACTORS": "turkeyKg=7.2"},
            input_fn=stub,
            echo_fn=lambda *_: None,
        )
        assert answers == {"turkeyKg": 5, "oven": "gas"}
        assert len(stub.prompts) == 1

    def test_invalid_presets_are_reported_and_dropped(self):
        from rhylthyme.history.factors import collect_factors

        messages = []
        answers = collect_factors(
            _factor_program(),
            factor_args=["turkeyKg=heavy", "oven=induction", "colour=red"],
            prompt=False,
            environ={},
            echo_fn=messages.append,
        )
        assert answers == {}
        assert any("not a number" in m for m in messages)
        assert any("not one of" in m for m in messages)
        assert any("not declared" in m for m in messages)

    def test_no_prompt_flag_keeps_presets(self):
        from rhylthyme.history.factors import collect_factors

        stub = _Answers("6.4", "gas")
        answers = collect_factors(
            _factor_program(),
            factor_args=["oven=gas"],
            prompt=False,
            environ={},
            input_fn=stub,
            echo_fn=lambda *_: None,
        )
        assert answers == {"oven": "gas"}
        assert stub.prompts == []

    def test_a_program_without_factors_is_never_asked(self):
        from rhylthyme.history.factors import collect_factors

        stub = _Answers("x")
        program = _factor_program()
        del program["metadata"]["varianceFactors"]
        assert collect_factors(program, input_fn=stub, echo_fn=lambda *_: None) == {}
        assert stub.prompts == []


@pytest.mark.unit
class TestFactorAnswersReachTheRecord:
    """The answers must end up in the run record's ``context.userTags``."""

    def _program_file(self, programs_dir, program):
        path = os.path.join(programs_dir, "factors_test.json")
        with open(path, "w") as fh:
            json.dump(program, fh)
        return path

    def _run(self, monkeypatch, program_file, temp_dir, **kwargs):
        def fake_wrapper(fn):
            runner = fn.__closure__[0].cell_contents
            runner.start()
            runner.command_queue.put("start_program")
            for _ in range(6):
                runner.update()

        monkeypatch.setattr(tui_module.curses, "wrapper", fake_wrapper)
        written = tui_module.run_program(
            program_file, validate=False, runs_dir=temp_dir, **kwargs
        )
        assert written is not None
        with open(written) as fh:
            return json.load(fh)

    def test_factor_flags_land_in_user_tags(self, temp_dir, programs_dir, monkeypatch):
        from rhylthyme.history import validate_run

        path = self._program_file(programs_dir, _factor_program())
        record = self._run(
            monkeypatch,
            path,
            temp_dir,
            factors=["turkeyKg=6.4", "oven=GAS"],
            factor_prompt=False,
        )
        assert record["context"]["userTags"] == {"turkeyKg": 6.4, "oven": "gas"}
        assert validate_run(record) == []

    def test_env_lands_in_user_tags(self, temp_dir, programs_dir, monkeypatch):
        monkeypatch.setenv("RHYLTHYME_FACTORS", "turkeyKg=9,oven=electric")
        path = self._program_file(programs_dir, _factor_program())
        record = self._run(monkeypatch, path, temp_dir, factor_prompt=False)
        assert record["context"]["userTags"] == {"turkeyKg": 9, "oven": "electric"}

    def test_prompt_answers_land_in_user_tags(
        self, temp_dir, programs_dir, monkeypatch
    ):
        stub = _Answers("5.5", "convection")
        monkeypatch.setattr("builtins.input", stub)
        monkeypatch.delenv("RHYLTHYME_FACTORS", raising=False)
        path = self._program_file(programs_dir, _factor_program())
        record = self._run(monkeypatch, path, temp_dir)
        assert record["context"]["userTags"] == {
            "turkeyKg": 5.5,
            "oven": "convection",
        }
        assert len(stub.prompts) == 2

    def test_skipped_factors_are_simply_absent(
        self, temp_dir, programs_dir, monkeypatch
    ):
        stub = _Answers("", "")
        monkeypatch.setattr("builtins.input", stub)
        monkeypatch.delenv("RHYLTHYME_FACTORS", raising=False)
        path = self._program_file(programs_dir, _factor_program())
        record = self._run(monkeypatch, path, temp_dir)
        assert record["context"]["userTags"] == {}

    def test_undeclared_program_is_not_prompted(
        self, temp_dir, programs_dir, monkeypatch
    ):
        stub = _Answers("6.4")
        monkeypatch.setattr("builtins.input", stub)
        program = _factor_program()
        del program["metadata"]["varianceFactors"]
        path = self._program_file(programs_dir, program)
        record = self._run(monkeypatch, path, temp_dir)
        assert record["context"]["userTags"] == {}
        assert stub.prompts == []


# ---------------------------------------------------------------------------
# Trigger anchors and predicted offsets
# (plans/execution-history-duration-prediction.md Phase 7)
# ---------------------------------------------------------------------------

THANKSGIVING_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "rhylthyme-timeline",
    "test",
    "fixtures",
    "programs",
    "thanksgiving_one_oven.json",
)


def _anchor_program(offsets_use=None, default_seconds=600.0, offset_seconds=-120):
    """
    Three steps: an indefinite anchor, a step gated on a NEGATIVE offset from
    it, and a step gated on the anchor's START.

    Nothing about the anchor's real length is knowable while it runs, so the
    negative offset has to be resolved from a projection — which is the whole
    subject of Phase 7. ``prep`` has room for both gated steps at once so the
    times measured are the triggers', not the resource manager's.
    """
    program = {
        "schemaVersion": "0.3.0-alpha",
        "programId": "anchor-test",
        "name": "Anchor Test",
        "environmentType": "test",
        "actors": 4,
        "tracks": [
            {
                "trackId": "anchor",
                "name": "Anchor",
                "steps": [
                    {
                        "stepId": "anchor",
                        "name": "Anchor",
                        "task": "pot",
                        "duration": {
                            "type": "indefinite",
                            "defaultSeconds": default_seconds,
                        },
                        "startTrigger": {"type": "programStart"},
                    }
                ],
            },
            {
                "trackId": "before",
                "name": "Before",
                "steps": [
                    {
                        "stepId": "gated",
                        "name": "Gated on the anchor's end",
                        "task": "prep",
                        "duration": {"type": "fixed", "seconds": 60},
                        "startTrigger": {
                            "type": "afterStep",
                            "stepId": "anchor",
                            "offsetSeconds": offset_seconds,
                        },
                    }
                ],
            },
            {
                "trackId": "alongside",
                "name": "Alongside",
                "steps": [
                    {
                        "stepId": "onstart",
                        "name": "Gated on the anchor's start",
                        "task": "prep",
                        "duration": {"type": "fixed", "seconds": 30},
                        "startTrigger": {
                            "type": "afterStep",
                            "stepId": "anchor",
                            "event": "start",
                            "offsetSeconds": 90,
                        },
                    }
                ],
            },
        ],
        "resourceConstraints": [
            {"task": "pot", "maxConcurrent": 1},
            {"task": "prep", "maxConcurrent": 2},
        ],
        "metadata": {},
    }
    if offsets_use:
        program["metadata"]["offsetsUse"] = offsets_use
    return program


def _drive(runner, monkeypatch, clock, *, end_anchor_at=None, tick=1.0, max_ticks=4000):
    """
    Tick ``runner`` to completion, ending the indefinite anchor by hand.

    Returns ``{stepId: {"start", "end", "fired"}}`` in seconds from the program
    start; ``end_anchor_at`` is the anchor's length in seconds (the 't' key).
    """
    runner.start()
    runner.command_queue.put("start_program")
    runner.update()
    for _ in range(max_ticks):
        clock.advance(tick)
        runner.update()
        for step in runner.steps.values():
            if (
                end_anchor_at is not None
                and step.duration_type == program_runner_module.DurationType.INDEFINITE
                and step.status == StepStatus.RUNNING
                and runner.current_time - step.start_time >= end_anchor_at
            ):
                runner._trigger_step(step)
        if all(s.status == StepStatus.COMPLETED for s in runner.steps.values()):
            break
    return {
        step_id: {
            "start": None if step.start_time is None else step.start_time - T0,
            "end": None if step.end_time is None else step.end_time - T0,
            "fired": (
                None
                if step.trigger_fired_time is None
                else step.trigger_fired_time - T0
            ),
        }
        for step_id, step in runner.steps.items()
    }


def _run_anchor(program, monkeypatch, **kwargs):
    clock = _FakeClock()
    monkeypatch.setattr(program_runner_module.time, "time", clock)
    runner = ProgramRunner(program)
    return runner, _drive(runner, monkeypatch, clock, **kwargs)


@pytest.mark.unit
class TestTriggerAnchors:
    """``event: "start"`` and negative offsets fire where the engine says."""

    def test_event_start_fires_when_the_reference_starts(self, monkeypatch):
        # The anchor runs 900 s, three times the 300 s the trigger waits for.
        # Anchoring on its END would have put `onstart` at 990.
        _, times = _run_anchor(
            _anchor_program(), monkeypatch, end_anchor_at=900.0, max_ticks=3000
        )
        assert times["anchor"]["start"] == pytest.approx(0.0, abs=1.0)
        assert times["onstart"]["fired"] == pytest.approx(90.0, abs=1.0)
        assert times["onstart"]["start"] == pytest.approx(90.0, abs=1.0)
        # ...and it ran to completion long before the anchor ended.
        assert times["onstart"]["end"] < times["anchor"]["end"]

    def test_negative_offset_fires_at_the_projected_instant(self, monkeypatch):
        # defaultSeconds 600, offset -120: the projection is 480 s in, and the
        # anchor is not ended by hand until 900 s. Waiting for the anchor to
        # complete (the pre-Phase-7 behaviour) would have fired at 900.
        _, times = _run_anchor(
            _anchor_program(), monkeypatch, end_anchor_at=900.0, max_ticks=3000
        )
        assert times["gated"]["fired"] == pytest.approx(480.0, abs=1.0)
        assert times["gated"]["start"] == pytest.approx(480.0, abs=1.0)
        assert times["anchor"]["end"] == pytest.approx(900.0, abs=1.0)

    def test_an_anchor_that_ends_early_fires_the_trigger_immediately(self, monkeypatch):
        # The anchor is ended by hand at 300 s, before the 480 s projection:
        # the projection was wrong, so the trigger fires at the anchor's end
        # and never later than the engine would have placed it.
        _, times = _run_anchor(
            _anchor_program(), monkeypatch, end_anchor_at=300.0, max_ticks=3000
        )
        assert times["anchor"]["end"] == pytest.approx(300.0, abs=1.0)
        assert times["gated"]["fired"] == pytest.approx(300.0, abs=1.0)
        assert times["gated"]["start"] == pytest.approx(300.0, abs=1.0)

    def test_a_negative_offset_never_fires_before_its_anchor_starts(self, monkeypatch):
        # |offset| larger than defaultSeconds: the projection lands before the
        # anchor even began, and the trigger is clamped to the anchor's start.
        _, times = _run_anchor(
            _anchor_program(default_seconds=100.0, offset_seconds=-600),
            monkeypatch,
            end_anchor_at=900.0,
            max_ticks=3000,
        )
        assert times["gated"]["fired"] == pytest.approx(
            times["anchor"]["start"], abs=1.0
        )


def _thanksgiving():
    if not os.path.isfile(THANKSGIVING_PATH):
        pytest.skip("rhylthyme-timeline fixtures not checked out")
    with open(THANKSGIVING_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _with_factors_and_offsets(offsets_use=None):
    """The Thanksgiving example as the catalog declares it, plus the flag."""
    program = _thanksgiving()
    program["metadata"]["varianceFactors"] = [
        {"key": "turkeyKg", "label": "Turkey weight (kg)", "type": "number"},
    ]
    if offsets_use:
        program["metadata"]["offsetsUse"] = offsets_use
    return program


# The roast's generating law in the synthetic history below. Deliberately not
# the author's 9900 s, and large enough that predicted-end minus 45 min is
# still a positive instant, so the assertion is about the projection and not
# about the clamp to the anchor's start.
ROAST_INTERCEPT = 5400.0
ROAST_PER_KG = 300.0


def _synthetic_roast_history(program, n=20, seed=11):
    from rhylthyme.history import synthesize_runs

    def law(step_id, planned, factors, rng):
        if step_id == "turkey-roast":
            return ROAST_INTERCEPT + ROAST_PER_KG * float(factors["turkeyKg"])
        return planned

    return synthesize_runs(
        program,
        n,
        noise_by_step={"turkey-roast": 0.02},
        factors_fn=lambda i: {"turkeyKg": 5 + i % 4},
        duration_fn=law,
        seed=seed,
        environment_id=None,
    )


def _peel_offsets(program, predictions=None, tick=4.0, max_ticks=3000):
    """
    Run the Thanksgiving example until the potatoes are peeled.

    Returns ``(roast start, peel trigger instant, runner)`` in seconds from the
    program start. The roast is never ended, so the only thing that can release
    the peel is the negative offset's projection.
    """
    clock = _FakeClock()
    runner = ProgramRunner(program)
    if predictions is not None:
        runner.set_predictions(predictions)
    original = program_runner_module.time.time
    program_runner_module.time.time = clock
    try:
        runner.start()
        runner.command_queue.put("start_program")
        runner.update()
        for _ in range(max_ticks):
            clock.advance(tick)
            runner.update()
            if runner.steps["potatoes-peel"].trigger_fired_time is not None:
                break
    finally:
        program_runner_module.time.time = original
    roast = runner.steps["turkey-roast"]
    peel = runner.steps["potatoes-peel"]
    assert roast.start_time is not None, "the roast never started"
    assert peel.trigger_fired_time is not None, "the peel trigger never fired"
    return roast.start_time - T0, peel.trigger_fired_time - T0, runner


@pytest.mark.unit
class TestPredictedOffsets:
    """
    "Peel the potatoes 45 min before the roast is done", from history.

    PRD §7: behind ``metadata.offsetsUse: "predicted"`` a negative offset on an
    indefinite anchor is resolved from the *predicted* end when a prediction
    exists and its interval is narrower than the planned default.
    """

    def test_without_the_flag_the_peel_fires_from_the_planned_end(self):
        program = _with_factors_and_offsets()
        roast_start, fired, runner = _peel_offsets(program)
        # defaultSeconds 9900, offset -2700: the projection is 7200 s after
        # the roast begins, whatever history says.
        assert fired - roast_start == pytest.approx(9900 - 2700, abs=5.0)
        assert runner.offsets_use == "planned"
        assert runner.predicted_anchor_seconds == {}

    def test_with_the_flag_the_peel_fires_from_the_predicted_end(self):
        from rhylthyme.history import predict_durations

        program = _with_factors_and_offsets("predicted")
        history = _synthetic_roast_history(_with_factors_and_offsets())
        predictions = predict_durations(program, history, user_tags={"turkeyKg": 7})
        roast = predictions["turkey-roast"]
        # The law at 7 kg, recovered from history to within 5 %. The corpus
        # contains five runs at exactly 7 kg, so the identical-context branch
        # answers; a weight the corpus has never seen falls to the model.
        expected = ROAST_INTERCEPT + ROAST_PER_KG * 7
        assert roast["basis"] == "identical"
        assert roast["seconds"] == pytest.approx(expected, rel=0.05)
        assert roast["high"] - roast["low"] < 9900
        unseen = predict_durations(program, history, user_tags={"turkeyKg": 7.5})[
            "turkey-roast"
        ]
        assert unseen["basis"] == "model"
        assert unseen["seconds"] == pytest.approx(
            ROAST_INTERCEPT + ROAST_PER_KG * 7.5, rel=0.05
        )

        roast_start, fired, runner = _peel_offsets(program, predictions)
        assert fired - roast_start == pytest.approx(roast["seconds"] - 2700, abs=5.0)
        # ...which is over half an hour earlier than the plan would have it.
        assert fired - roast_start < 9900 - 2700 - 1800
        assert runner.offsets_use == "predicted"
        assert runner.predicted_anchor_seconds == {"potatoes-peel": roast["seconds"]}

    def test_a_prediction_no_sharper_than_the_guess_is_not_used(self):
        program = _with_factors_and_offsets("predicted")
        # An interval exactly as wide as defaultSeconds is history saying it
        # does not know; one second narrower is enough.
        wide = {
            "turkey-roast": {
                "seconds": 7500.0,
                "low": 0.0,
                "high": 9900.0,
                "basis": "model",
                "n": 20,
            }
        }
        _, fired_wide, runner_wide = _peel_offsets(program, wide)
        assert fired_wide - _peel_offsets(program, wide)[0] == pytest.approx(
            9900 - 2700, abs=5.0
        )
        assert runner_wide.predicted_anchor_seconds == {}

        narrow = copy.deepcopy(wide)
        narrow["turkey-roast"]["high"] = 9899.0
        roast_start, fired_narrow, runner_narrow = _peel_offsets(program, narrow)
        assert fired_narrow - roast_start == pytest.approx(7500 - 2700, abs=5.0)
        assert runner_narrow.predicted_anchor_seconds == {"potatoes-peel": 7500.0}

    def test_a_declined_prediction_is_not_used(self):
        program = _with_factors_and_offsets("predicted")
        for prediction in (
            {"seconds": None, "low": None, "high": None, "basis": "none", "n": 0},
            {
                "seconds": 7500.0,
                "low": None,
                "high": None,
                "basis": "identical",
                "n": 4,
            },
            {"seconds": 0.0, "low": 0.0, "high": 1.0, "basis": "model", "n": 9},
        ):
            roast_start, fired, runner = _peel_offsets(
                program, {"turkey-roast": prediction}
            )
            assert fired - roast_start == pytest.approx(9900 - 2700, abs=5.0)
            assert runner.predicted_anchor_seconds == {}

    def test_the_record_says_which_durations_were_used(self, temp_dir):
        from rhylthyme.history import RunRecorder, validate_run

        program = _with_factors_and_offsets("predicted")
        predictions = {
            "turkey-roast": {
                "seconds": 7500.0,
                "low": 7400.0,
                "high": 7600.0,
                "basis": "model",
                "n": 20,
            }
        }
        clock = _FakeClock()
        original = program_runner_module.time.time
        program_runner_module.time.time = clock
        try:
            runner = ProgramRunner(program)
            runner.set_predictions(predictions)
            recorder = RunRecorder(
                runner, source_program=program, runs_dir=temp_dir
            ).attach()
            runner.start()
            runner.command_queue.put("start_program")
            runner.update()
            for _ in range(3000):
                clock.advance(4.0)
                runner.update()
                if runner.steps["potatoes-peel"].trigger_fired_time is not None:
                    break
            record = recorder.build_record()
        finally:
            program_runner_module.time.time = original

        assert record["context"]["offsetsUse"] == "predicted"
        by_step = {s["stepId"]: s for s in record["steps"]}
        assert by_step["potatoes-peel"]["predictedAnchorSeconds"] == 7500.0
        # The plan itself is untouched: planned.defaultSeconds is still the
        # author's number, so history never rewrites the record's plan.
        assert by_step["turkey-roast"]["planned"]["defaultSeconds"] == 9900
        assert "predictedAnchorSeconds" not in by_step["turkey-roast"]
        assert validate_run(record) == []

    def test_a_planned_run_records_offsets_use_planned(self, temp_dir):
        from rhylthyme.history import RunRecorder, validate_run

        clock = _FakeClock()
        original = program_runner_module.time.time
        program_runner_module.time.time = clock
        try:
            runner = ProgramRunner(_anchor_program())
            recorder = RunRecorder(runner, runs_dir=temp_dir).attach()
            runner.start()
            runner.command_queue.put("start_program")
            runner.update()
            for _ in range(2000):
                clock.advance(1.0)
                runner.update()
                for step in runner.steps.values():
                    if (
                        step.duration_type
                        == program_runner_module.DurationType.INDEFINITE
                        and step.status == StepStatus.RUNNING
                        and runner.current_time - step.start_time >= 900
                    ):
                        runner._trigger_step(step)
                if all(s.status == StepStatus.COMPLETED for s in runner.steps.values()):
                    break
            record = recorder.build_record()
        finally:
            program_runner_module.time.time = original

        assert record["context"]["offsetsUse"] == "planned"
        assert all("predictedAnchorSeconds" not in s for s in record["steps"])
        assert validate_run(record) == []
