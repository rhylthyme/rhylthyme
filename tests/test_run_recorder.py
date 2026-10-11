"""
Tests for the run recorder (execution history, Phase 1).

The runner is driven headlessly with a fake clock: ``time.time`` is patched
so ``ProgramRunner.update()`` sees exactly the seconds we advance, which
makes every actual timing in the record deterministic.
"""

import json
import os

import pytest

import rhylthyme.cli.tui as tui_module
import rhylthyme.program_runner as program_runner_module
from rhylthyme.history import (
    RunRecorder,
    freeze_planned,
    list_runs,
    program_version,
    runtime_step_id,
    step_identity,
    validate_run,
)
from rhylthyme.program_runner import ProgramRunner, StepStatus

T0 = 1_700_000_000.0


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(program_runner_module.time, "time", fake)
    return fake


def _step(step_id, trigger, duration, **extra):
    step = {
        "stepId": step_id,
        "name": step_id.title(),
        "startTrigger": trigger,
        "duration": duration,
        "task": "prep",
    }
    step.update(extra)
    return step


@pytest.fixture
def history_program():
    """Fixed, indefinite and variable steps plus a serial replicate."""
    return {
        "programId": "history-test",
        "name": "History Test",
        "actors": 4,
        "metadata": {"serves": "4", "sourceUrl": "https://example.test/recipe"},
        "tracks": [
            {
                "trackId": "a",
                "name": "A",
                "steps": [
                    _step(
                        "fixed1",
                        {"type": "programStart"},
                        {"type": "fixed", "seconds": 5},
                    ),
                    _step(
                        "indef",
                        {"type": "afterStep", "stepId": "fixed1"},
                        {
                            "type": "indefinite",
                            "defaultSeconds": 100,
                            "triggerName": "done",
                        },
                    ),
                ],
            },
            {
                "trackId": "b",
                "name": "B",
                "steps": [
                    _step(
                        "var",
                        {"type": "programStartOffset", "offsetSeconds": 2},
                        {
                            "type": "variable",
                            "minSeconds": 2,
                            "maxSeconds": 10,
                            "defaultSeconds": 4,
                        },
                    ),
                    _step(
                        "rep",
                        {"type": "afterStep", "stepId": "var"},
                        {"type": "fixed", "seconds": 1},
                        replicates={"count": 2, "mode": "serial"},
                    ),
                ],
            },
        ],
        "resourceConstraints": [{"task": "prep", "maxConcurrent": 4}],
    }


def _start(runner):
    runner.start()
    runner.command_queue.put("start_program")
    runner.update()


def _tick(runner, clock, seconds=0.5, times=1):
    for _ in range(times):
        clock.advance(seconds)
        runner.update()


def _all_done(runner):
    return all(s.status == StepStatus.COMPLETED for s in runner.steps.values())


def _by_id(record):
    return {(s["stepId"], s.get("instance", 1)): s for s in record["steps"]}


def _run_to_completion(runner, recorder, clock, max_ticks=200):
    """Advance until every step is complete, ending the indefinite step by hand."""
    for _ in range(max_ticks):
        _tick(runner, clock)
        indef = runner.steps["indef"]
        if indef.status == StepStatus.RUNNING and clock.now - indef.start_time >= 3:
            runner.trigger_manual_step("done")
        if _all_done(runner):
            break
    assert _all_done(runner), {k: v.status for k, v in runner.steps.items()}


# ------------------------------------------------------------------ recording


@pytest.mark.unit
class TestRunRecorder:
    def test_record_validates_against_runs_schema(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _run_to_completion(runner, recorder, clock)

        path = recorder.finalize()
        assert path is not None and os.path.isfile(path)
        with open(path) as fh:
            record = json.load(fh)
        assert validate_run(record) == []
        assert record["outcome"] == "completed"
        assert record["programId"] == "history-test"
        assert record["programVersion"] == program_version(history_program)
        assert record["runtime"] == {
            "kind": "cli",
            "version": record["runtime"]["version"],
            "clockMode": "wall",
            "speed": 1.0,
        }
        assert record["context"]["serves"] == "4"
        assert record["context"]["actors"] == 4
        assert record["context"]["userTags"] == {}
        # Which durations negative offsets were resolved against (Phase 7).
        # A program that does not set metadata.offsetsUse used the plan.
        assert record["context"]["offsetsUse"] == "planned"
        assert all("predictedAnchorSeconds" not in s for s in record["steps"])
        assert record["startedAt"].startswith("2023-11-14T22:13:20")
        assert record["runId"].startswith("2023-11-14T22:13:20Z-")
        # stored under <runs-dir>/<programId>/
        assert os.path.basename(os.path.dirname(path)) == "history-test"

    def test_ended_by_per_duration_kind(self, history_program, clock, temp_dir):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _run_to_completion(runner, recorder, clock)
        steps = _by_id(recorder.build_record())

        assert steps[("fixed1", 1)]["endedBy"] == "timer"
        assert steps[("var", 1)]["endedBy"] == "timer"  # forced at maxSeconds
        assert steps[("indef", 1)]["endedBy"] == "executor"
        assert steps[("rep", 1)]["endedBy"] == "timer"
        assert steps[("fixed1", 1)]["actual"] == {"start": 0.0, "end": 5.0}
        assert (
            steps[("var", 1)]["actual"]["end"] - steps[("var", 1)]["actual"]["start"]
            == 10.0
        )

    def test_force_complete_key_counts_as_executor(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _tick(runner, clock, 0.5, 2)
        fixed1 = runner.steps["fixed1"]
        assert fixed1.status == StepStatus.RUNNING
        runner.complete_step(fixed1, runner.current_time)  # what the 'c' key does
        steps = _by_id(recorder.build_record())
        assert steps[("fixed1", 1)]["endedBy"] == "executor"
        assert steps[("fixed1", 1)]["actual"]["end"] == 1.0

    def test_abort_is_recorded(self, history_program, clock, temp_dir):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _tick(runner, clock)
        runner.abort_step(runner.steps["fixed1"], runner.current_time)
        record = recorder.build_record()
        assert _by_id(record)[("fixed1", 1)]["endedBy"] == "abort"
        assert record["outcome"] == "aborted"

    def test_planned_is_frozen_before_first_step_starts(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        )
        # Frozen at construction, before attach/start
        assert recorder.planned["fixed1"] == {
            "start": 0,
            "end": 5,
            "durationType": "fixed",
            "seconds": 5,
        }
        assert recorder.planned["indef"] == {
            "start": 5,
            "end": 105,
            "durationType": "indefinite",
            "defaultSeconds": 100,
        }
        assert recorder.planned["var"]["minSeconds"] == 2
        assert recorder.planned["var"]["maxSeconds"] == 10
        assert recorder.planned["rep-r2"] == {
            "start": 7,
            "end": 8,
            "durationType": "fixed",
            "seconds": 1,
        }
        recorder.attach()
        _start(runner)
        # Mutating the program afterwards must not change the record
        runner.program["tracks"][0]["steps"][0]["duration"]["seconds"] = 999
        _tick(runner, clock)
        steps = _by_id(recorder.build_record())
        assert steps[("fixed1", 1)]["planned"]["seconds"] == 5
        assert all("planned" in s for s in recorder.build_record()["steps"])

    def test_waited_on_and_trigger_fired_at(self, history_program, clock, temp_dir):
        # One actor: 'var' becomes ready at t=2 but must wait for the actor
        history_program["actors"] = 1
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _run_to_completion(runner, recorder, clock)
        steps = _by_id(recorder.build_record())

        assert steps[("indef", 1)]["waitedOn"] == ["fixed1"]
        assert steps[("rep", 2)]["waitedOn"] == ["rep-r1"]
        assert "waitedOn" not in steps[("fixed1", 1)]
        # afterStep is observed on the update tick after the predecessor ends
        lag = (
            steps[("indef", 1)]["triggerFiredAt"]
            - steps[("fixed1", 1)]["actual"]["end"]
        )
        assert 0 <= lag <= 0.5
        assert steps[("var", 1)]["triggerFiredAt"] == 2.0
        assert (
            steps[("var", 1)]["actual"]["start"] > steps[("var", 1)]["triggerFiredAt"]
        )

    def test_instance_from_replicate_suffix(self, history_program, clock, temp_dir):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        record = recorder.build_record()
        reps = [s for s in record["steps"] if s["stepId"] == "rep"]
        assert [s["instance"] for s in reps] == [1, 2]
        assert all(s["instance"] == 1 for s in record["steps"] if s["stepId"] != "rep")
        assert "rep-r1" not in {s["stepId"] for s in record["steps"]}
        assert runtime_step_id(reps[1], replicated=True) == "rep-r2"
        assert (
            runtime_step_id({"stepId": "fixed1", "instance": 1}, replicated=False)
            == "fixed1"
        )

    def test_instance_stamps_take_precedence_over_suffix(self):
        assert step_identity(
            {"stepId": "x-r2", "instanceOf": "x", "instanceIndex": 2}, None
        ) == ("x", 2)
        assert step_identity({"stepId": "x-r2"}, None) == ("x", 2)
        # a genuine id ending in -r2 is not a replicate when the author wrote it
        assert step_identity({"stepId": "x-r2"}, {"x-r2", "y"}) == ("x-r2", 1)
        assert step_identity({"stepId": "x-r2"}, {"x", "y"}) == ("x", 2)

    def test_paused_seconds_attributed_to_running_steps(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _tick(runner, clock, 0.5, 5)  # t = 2.5: fixed1 and var running
        assert runner.steps["fixed1"].status == StepStatus.RUNNING
        assert runner.steps["var"].status == StepStatus.RUNNING
        before = runner.current_time

        assert runner.toggle_pause() is True
        assert runner.is_paused
        clock.advance(7)
        runner.update()
        assert runner.current_time == before  # clock frozen
        assert runner.steps["fixed1"].status == StepStatus.RUNNING
        assert runner.toggle_pause() is False

        _run_to_completion(runner, recorder, clock)
        steps = _by_id(recorder.build_record())
        assert steps[("fixed1", 1)]["pausedSeconds"] == 7.0
        assert steps[("var", 1)]["pausedSeconds"] == 7.0
        assert steps[("indef", 1)]["pausedSeconds"] == 0.0
        # program-clock durations are unaffected by the pause
        assert steps[("fixed1", 1)]["actual"] == {"start": 0.0, "end": 5.0}

    def test_pause_before_start_is_a_no_op(self, history_program, clock):
        runner = ProgramRunner(history_program)
        assert runner.toggle_pause() is False
        assert "not started" in runner.status_message

    def test_finalize_writes_nothing_if_program_never_started(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        assert recorder.finalize() is None
        assert list_runs(temp_dir) == []

    def test_speed_records_time_scale(self, history_program, clock, temp_dir):
        runner = ProgramRunner(history_program, time_scale=10.0)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _tick(runner, clock, 0.1, 3)
        assert recorder.build_record()["runtime"]["speed"] == 10.0

    def test_runs_dir_env_var(self, history_program, clock, temp_dir, monkeypatch):
        monkeypatch.setenv("RHYLTHYME_RUNS_DIR", os.path.join(temp_dir, "from-env"))
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(runner, source_program=history_program).attach()
        _start(runner)
        _tick(runner, clock)
        path = recorder.finalize()
        assert str(path).startswith(os.path.join(temp_dir, "from-env"))


# ------------------------------------------------------------ run_program path


@pytest.mark.unit
class TestRunProgramWritesRecord:
    def _program_file(self, programs_dir, program):
        path = os.path.join(programs_dir, "history_test.json")
        with open(path, "w") as fh:
            json.dump(program, fh)
        return path

    def test_keyboard_interrupt_yields_abandoned(
        self, history_program, clock, temp_dir, programs_dir, monkeypatch
    ):
        program_file = self._program_file(programs_dir, history_program)
        captured = {}

        def fake_wrapper(fn):
            # Run headlessly for a few ticks, then simulate Ctrl-C mid-run
            runner = fn.__closure__[0].cell_contents if fn.__closure__ else None
            captured["runner"] = runner
            runner.start()
            for _ in range(6):
                clock.advance(0.5)
                runner.update()
            raise KeyboardInterrupt

        monkeypatch.setattr(tui_module.curses, "wrapper", fake_wrapper)
        written = tui_module.run_program(
            program_file, validate=False, runs_dir=temp_dir
        )
        assert written and os.path.isfile(written)
        with open(written) as fh:
            record = json.load(fh)
        assert validate_run(record) == []
        assert record["outcome"] == "abandoned"
        steps = _by_id(record)
        assert "actual" in steps[("fixed1", 1)]
        assert "end" not in steps[("fixed1", 1)]["actual"]  # still running at Ctrl-C
        assert "actual" not in steps[("indef", 1)]  # never started
        assert record["programVersion"] == program_version(history_program)

    def test_no_record_flag_writes_nothing(
        self, history_program, clock, temp_dir, programs_dir, monkeypatch
    ):
        program_file = self._program_file(programs_dir, history_program)
        monkeypatch.setattr(tui_module.curses, "wrapper", lambda fn: None)
        written = tui_module.run_program(
            program_file, validate=False, runs_dir=temp_dir, record=False
        )
        assert written is None
        assert list_runs(temp_dir) == []

    def test_quit_writes_record_with_outcome_from_state(
        self, history_program, clock, temp_dir, programs_dir, monkeypatch
    ):
        program_file = self._program_file(programs_dir, history_program)

        def fake_wrapper(fn):
            runner = fn.__closure__[0].cell_contents
            runner.start()
            for _ in range(4):
                clock.advance(0.5)
                runner.update()
            # user presses 'q': main loop returns normally

        monkeypatch.setattr(tui_module.curses, "wrapper", fake_wrapper)
        written = tui_module.run_program(
            program_file, validate=False, runs_dir=temp_dir
        )
        with open(written) as fh:
            record = json.load(fh)
        assert record["outcome"] == "abandoned"
        assert validate_run(record) == []


# ------------------------------------------------------------- freeze_planned


@pytest.mark.unit
def test_freeze_planned_matches_reference_resolver(history_program):
    from rhylthyme.expand_replicates import expand_replicates
    from rhylthyme.validate_program import calculate_step_start_time

    expanded = expand_replicates(history_program)
    planned = freeze_planned(expanded)
    for track in expanded["tracks"]:
        for step in track["steps"]:
            expected = calculate_step_start_time(step, track["steps"], expanded)
            assert planned[step["stepId"]]["start"] == expected


# ------------------------------------------------ duration-kind semantics


@pytest.mark.unit
class TestDurationKindEndings:
    """Fixed ends on time; variable is executor-ended after min or forced at
    max; indefinite never ends on its own."""

    def test_indefinite_runs_past_default_until_executor_ends_it(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        indef = runner.steps["indef"]
        _tick(runner, clock, 1.0, 6)  # fixed1 ends at 5, indef starts
        assert indef.status == StepStatus.RUNNING
        _tick(runner, clock, 10.0, 30)  # 300 s later, well past defaultSeconds=100
        assert indef.status == StepStatus.RUNNING
        assert indef.expected_end_time == indef.start_time + 100  # projection kept
        runner._trigger_step(indef)  # the 't' key
        assert indef.status == StepStatus.COMPLETED
        entry = _by_id(recorder.build_record())[("indef", 1)]
        assert entry["endedBy"] == "executor"
        assert entry["actual"]["end"] - entry["actual"]["start"] > 100

    def test_variable_forced_at_max_seconds_is_timer(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        var = runner.steps["var"]
        _tick(runner, clock, 0.5, 4)  # t=2: var starts (min 2, max 10, default 4)
        assert var.status == StepStatus.RUNNING
        _tick(runner, clock, 0.5, 14)  # t=9: past default, still running
        assert var.status == StepStatus.RUNNING
        _tick(runner, clock, 0.5, 6)  # t=12: past max
        assert var.status == StepStatus.COMPLETED
        entry = _by_id(recorder.build_record())[("var", 1)]
        assert entry["endedBy"] == "timer"
        assert entry["actual"]["end"] - entry["actual"]["start"] == 10.0

    def test_variable_ended_by_executor_after_min(
        self, history_program, clock, temp_dir
    ):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        var = runner.steps["var"]
        _tick(runner, clock, 0.5, 4)
        _tick(runner, clock, 0.5, 6)  # 3 s in: past minSeconds=2
        assert var.is_ready_to_complete(runner.current_time)
        assert not var.must_complete(runner.current_time)
        runner._trigger_step(var)
        entry = _by_id(recorder.build_record())[("var", 1)]
        assert entry["endedBy"] == "executor"
        assert entry["actual"]["end"] - entry["actual"]["start"] == 3.0

    def test_fixed_ends_on_time(self, history_program, clock, temp_dir):
        runner = ProgramRunner(history_program)
        recorder = RunRecorder(
            runner, source_program=history_program, runs_dir=temp_dir
        ).attach()
        _start(runner)
        _tick(runner, clock, 0.5, 9)  # t=4.5
        assert runner.steps["fixed1"].status == StepStatus.RUNNING
        _tick(runner, clock, 0.5, 1)  # t=5
        assert runner.steps["fixed1"].status == StepStatus.COMPLETED
        entry = _by_id(recorder.build_record())[("fixed1", 1)]
        assert entry["endedBy"] == "timer"
        assert entry["actual"] == {"start": 0.0, "end": 5.0}

    def test_progress_uses_duration_kind(self, history_program, clock):
        runner = ProgramRunner(history_program)
        _start(runner)
        _tick(runner, clock, 0.5, 5)  # t=2.5
        assert runner.steps["fixed1"].get_progress(runner.current_time) == 50.0
        _tick(runner, clock, 1.0, 4)  # indef running
        assert runner.steps["indef"].get_progress(runner.current_time) == -1.0


# ------------------------------------------------------- Thanksgiving example


THANKSGIVING = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "rhylthyme-timeline",
        "test",
        "fixtures",
        "programs",
        "thanksgiving_one_oven.json",
    )
)


@pytest.mark.unit
def test_thanksgiving_headless_run_records_executor_and_timer(clock, temp_dir):
    """PRD acceptance: force-completing the indefinite roast gives endedBy executor,
    fixed steps give timer; the record validates and shows planned vs actual."""
    if not os.path.isfile(THANKSGIVING):
        pytest.skip("rhylthyme-timeline fixtures not available")
    with open(THANKSGIVING) as fh:
        program = json.load(fh)

    runner = ProgramRunner(program, time_scale=60.0)
    recorder = RunRecorder(runner, source_program=program, runs_dir=temp_dir).attach()
    _start(runner)
    roast = runner.steps["turkey-roast"]
    for _ in range(2000):
        _tick(runner, clock, 1.0)  # 1 wall second = 60 program seconds
        if (
            roast.status == StepStatus.RUNNING
            and runner.current_time - roast.start_time >= 9000
        ):
            runner._trigger_step(roast)  # the 't' key: executor ends the roast early
        guests = runner.steps["guests-seated"]
        if (
            guests.status == StepStatus.PENDING
            and runner.steps["turkey-rest"].status == StepStatus.COMPLETED
        ):
            runner.trigger_manual_step("guests-seated")
        if _all_done(runner):
            break
    assert _all_done(runner), {
        k: v.status.value
        for k, v in runner.steps.items()
        if v.status != StepStatus.COMPLETED
    }

    path = recorder.finalize()
    with open(path) as fh:
        record = json.load(fh)
    assert validate_run(record) == []
    steps = _by_id(record)
    assert steps[("turkey-roast", 1)]["endedBy"] == "executor"
    assert steps[("turkey-roast", 1)]["planned"]["durationType"] == "indefinite"
    assert steps[("turkey-prep", 1)]["endedBy"] == "timer"
    assert steps[("stuffing-bake", 1)]["endedBy"] == "timer"
    assert record["runtime"]["speed"] == 60.0
    assert record["outcome"] == "completed"
    # every step has frozen planned timing and an observed interval
    assert all("planned" in s and "actual" in s for s in record["steps"])
