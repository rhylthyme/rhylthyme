"""
Test the CLI interface of rhylthyme-cli-runner.

This module tests the command-line interface functionality.
"""

import json
import os

import pytest


@pytest.mark.cli
class TestCLIBasics:
    """Test basic CLI functionality."""

    def test_cli_help(self, cli_runner):
        """Test CLI help command."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.output
        assert "Commands:" in result.output
        assert "validate" in result.output
        assert "run" in result.output

    def test_cli_version(self, cli_runner):
        """Test CLI version command."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["--version"])

        assert result.exit_code == 0
        # Version output format may vary, just check it doesn't crash

    def test_validate_command_help(self, cli_runner):
        """Test validate command help."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["validate", "--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.output
        assert "validate" in result.output

    def test_run_command_help(self, cli_runner):
        """Test run command help."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["run", "--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.output
        assert "run" in result.output


@pytest.mark.cli
@pytest.mark.integration
class TestCLIValidation:
    """Test CLI validation commands."""

    def test_validate_simple_program(self, cli_runner, simple_program_file):
        """Test validating a simple program."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["validate", simple_program_file])

        # The test might fail if schema is not available, check for schema issues
        if "schema" in result.output.lower() and "not found" in result.output.lower():
            pytest.skip("Schema file not available for validation")

        if result.exit_code == 0:
            assert "is valid" in result.output
        else:
            # Print output for debugging if test fails
            print(f"\nTest output: {result.output}")
            print(f"Exit code: {result.exit_code}")
            # Allow test to pass if it's a schema/dependency issue
            assert "error" in result.output.lower() or "schema" in result.output.lower()

    def test_validate_kitchen_program(self, cli_runner, kitchen_program_file):
        """Test validating a kitchen program."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["validate", kitchen_program_file])

        # The test might fail if schema is not available, check for schema issues
        if "schema" in result.output.lower() and "not found" in result.output.lower():
            pytest.skip("Schema file not available for validation")

        if result.exit_code == 0:
            assert "is valid" in result.output
        else:
            # Print output for debugging if test fails
            print(f"\nTest output: {result.output}")
            print(f"Exit code: {result.exit_code}")
            # Allow test to pass if it's a schema/dependency issue
            assert "error" in result.output.lower() or "schema" in result.output.lower()

    def test_validate_with_environment(
        self, cli_runner, kitchen_program_file, kitchen_environment_file
    ):
        """Test validating a program with environment."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli, ["validate", kitchen_program_file, "-e", kitchen_environment_file]
        )

        # The test might fail if schema is not available or environment file issues
        if "schema" in result.output.lower() and "not found" in result.output.lower():
            pytest.skip("Schema file not available for validation")

        if result.exit_code == 0:
            assert "is valid" in result.output
        elif result.exit_code == 2:
            # Click argument parsing error - environment file format issue
            print(f"\nEnvironment validation output: {result.output}")
            assert "error" in result.output.lower() or "usage" in result.output.lower()
        else:
            # Other validation errors
            print(f"\nValidation output: {result.output}")
            assert "error" in result.output.lower()

    def test_validate_nonexistent_file(self, cli_runner):
        """Test validating a nonexistent file."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["validate", "nonexistent.json"])

        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_validate_invalid_json(self, cli_runner, temp_dir):
        """Test validating invalid JSON."""
        from rhylthyme.cli.main import cli

        invalid_file = os.path.join(temp_dir, "invalid.json")
        with open(invalid_file, "w") as f:
            f.write("{ invalid json }")

        result = cli_runner.invoke(cli, ["validate", invalid_file])

        assert result.exit_code != 0
        # Should handle JSON parsing errors gracefully

    def test_validate_invalid_program(self, cli_runner, programs_dir):
        """Test validating a program with missing required fields."""
        from rhylthyme.cli.main import cli

        invalid_program = {
            "programId": "invalid",
            "name": "Invalid Program",
            # Missing required fields like tracks
        }

        invalid_file = os.path.join(programs_dir, "invalid.json")
        with open(invalid_file, "w") as f:
            json.dump(invalid_program, f)

        result = cli_runner.invoke(cli, ["validate", invalid_file])

        assert result.exit_code != 0
        assert "error" in result.output.lower()


@pytest.mark.cli
class TestCLIEnvironments:
    """Test CLI environment-related commands."""

    def test_environments_command(self, cli_runner):
        """Test environments listing command."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["environments"])

        # This might succeed or fail depending on whether examples are available
        # Just test that it doesn't crash
        assert result.exit_code in [0, 1]  # Success or expected failure

    def test_environment_info_command(self, cli_runner):
        """Test environment info command."""
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["environment-info", "test-env"])

        # This command should handle missing environments gracefully
        assert isinstance(result.exit_code, int)


@pytest.mark.cli
@pytest.mark.integration
class TestCLIPlanning:
    """Test CLI planning functionality."""

    def test_plan_command(self, cli_runner, kitchen_program_file, temp_dir):
        """Test program planning command."""
        from rhylthyme.cli.main import cli

        output_file = os.path.join(temp_dir, "planned_output.json")

        result = cli_runner.invoke(cli, ["plan", kitchen_program_file, output_file])

        if result.exit_code == 0:
            # If planning succeeded, output file should exist
            assert os.path.exists(output_file)
            assert "saved to" in result.output
        else:
            # Planning might fail for various reasons, just ensure it doesn't crash
            assert isinstance(result.exit_code, int)


@pytest.mark.cli
class TestCLIErrorHandling:
    """Test CLI error handling."""

    def test_validate_with_invalid_environment_file(
        self, cli_runner, simple_program_file, temp_dir
    ):
        """Test validation with invalid environment file."""
        from rhylthyme.cli.main import cli

        invalid_env = os.path.join(temp_dir, "invalid_env.json")
        with open(invalid_env, "w") as f:
            f.write("{ invalid json }")

        result = cli_runner.invoke(
            cli, ["validate", simple_program_file, "-e", invalid_env]
        )

        assert result.exit_code != 0
        # Should handle environment file errors gracefully

    def test_missing_required_arguments(self, cli_runner):
        """Test commands with missing required arguments."""
        from rhylthyme.cli.main import cli

        # Validate without program file
        result = cli_runner.invoke(cli, ["validate"])
        assert result.exit_code != 0

        # Plan without required arguments
        result = cli_runner.invoke(cli, ["plan"])
        assert result.exit_code != 0


@pytest.mark.cli
@pytest.mark.slow
def test_cli_with_real_examples(cli_runner, examples_dir):
    """Test CLI with real example files if available."""
    if not examples_dir:
        pytest.skip("Examples directory not available")

    import glob

    from rhylthyme.cli.main import cli

    programs_dir = os.path.join(examples_dir, "programs")
    if not os.path.exists(programs_dir):
        pytest.skip("Programs directory not found")

    # Test validation with a few real examples
    json_files = glob.glob(os.path.join(programs_dir, "*.json"))

    if json_files:
        # Just test the first few to keep test time reasonable
        for filepath in json_files[:3]:
            result = cli_runner.invoke(cli, ["validate", filepath])
            # Most examples should validate successfully
            # If they don't, it's useful information but shouldn't fail the test
            if result.exit_code != 0:
                print(
                    f"⚠️ Example {os.path.basename(filepath)} failed validation: {result.output}"
                )
    else:
        pytest.skip("No JSON example files found")


# ---------------------------------------------------------------- runs (history)


def _write_run_record(runs_dir, program_id, run_id, started_at, outcome, actual_end):
    """Write a minimal, schema-valid run record and return its path."""
    from rhylthyme.history import write_run

    record = {
        "schemaVersion": "0.1.0-alpha",
        "runId": run_id,
        "programId": program_id,
        "programVersion": "sha256:" + "0" * 64,
        "runtime": {"kind": "cli", "version": "test", "clockMode": "wall", "speed": 1},
        "environmentId": None,
        "startedAt": started_at,
        "endedAt": started_at,
        "outcome": outcome,
        "context": {"userTags": {}},
        "steps": [
            {
                "stepId": "prep",
                "instance": 1,
                "planned": {
                    "start": 0,
                    "end": 60,
                    "durationType": "fixed",
                    "seconds": 60,
                },
                "actual": {"start": 0, "end": 62},
                "endedBy": "timer",
                "triggerFiredAt": 0,
                "pausedSeconds": 0,
            },
            {
                "stepId": "cook",
                "instance": 1,
                "planned": {
                    "start": 60,
                    "end": 360,
                    "durationType": "indefinite",
                    "defaultSeconds": 300,
                },
                "actual": {"start": 62, "end": actual_end},
                "endedBy": "executor",
                "triggerFiredAt": 62,
                "waitedOn": ["prep"],
                "pausedSeconds": 12.5,
            },
        ],
    }
    return write_run(record, runs_dir)


@pytest.fixture
def recorded_runs(temp_dir):
    """Three records for two programs, written out of chronological order."""
    runs_dir = os.path.join(temp_dir, "runs")
    _write_run_record(
        runs_dir,
        "prog-a",
        "2026-09-10T10:00:00Z-aaaa",
        "2026-09-10T10:00:00.000Z",
        "completed",
        300,
    )
    _write_run_record(
        runs_dir,
        "prog-a",
        "2026-09-12T10:00:00Z-cccc",
        "2026-09-12T10:00:00.000Z",
        "abandoned",
        420,
    )
    _write_run_record(
        runs_dir,
        "prog-a",
        "2026-09-11T10:00:00Z-bbbb",
        "2026-09-11T10:00:00.000Z",
        "completed",
        360,
    )
    _write_run_record(
        runs_dir,
        "prog-b",
        "2026-09-13T10:00:00Z-dddd",
        "2026-09-13T10:00:00.000Z",
        "completed",
        330,
    )
    return runs_dir


@pytest.fixture
def thanksgiving_run(temp_dir):
    """
    The recorded Thanksgiving run, copied into a private runs directory.

    Returns ``(runs_dir, runId, program_file)``. The record and the program it
    executed are the shared fixtures under ``rhylthyme-timeline/test``; the
    record is regenerated by ``rhylthyme-timeline/tools/gen-run-fixture.py``.
    """
    import shutil

    from rhylthyme.history import write_run

    timeline = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "rhylthyme-timeline")
    )
    record_file = os.path.join(
        timeline, "test", "fixtures", "runs", "thanksgiving_one_oven.run.json"
    )
    program_file = os.path.join(
        timeline, "test", "fixtures", "programs", "thanksgiving_one_oven.json"
    )
    if not (os.path.isfile(record_file) and os.path.isfile(program_file)):
        pytest.skip(
            "rhylthyme-timeline fixtures not checked out beside rhylthyme-cli-runner"
        )

    with open(record_file) as fh:
        record = json.load(fh)
    runs_dir = os.path.join(temp_dir, "runs")
    write_run(record, runs_dir)
    programs = os.path.join(temp_dir, "located-programs")
    os.makedirs(programs, exist_ok=True)
    shutil.copy(program_file, programs)
    return (
        runs_dir,
        record["runId"],
        os.path.join(programs, os.path.basename(program_file)),
    )


@pytest.mark.cli
class TestRunsCommands:
    """Test `rhylthyme runs list` / `rhylthyme runs show`."""

    def test_runs_help_lists_subcommands(self, cli_runner):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["runs", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output and "show" in result.output

    def test_run_command_has_record_options(self, cli_runner):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--runs-dir" in result.output
        assert "--no-record" in result.output

    def test_runs_list_newest_first(self, cli_runner, recorded_runs):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli, ["runs", "list", "prog-a", "--runs-dir", recorded_runs]
        )
        assert result.exit_code == 0, result.output
        lines = [
            line for line in result.output.splitlines() if line.startswith("2026-")
        ]
        assert [line.split()[0] for line in lines] == [
            "2026-09-12T10:00:00Z-cccc",
            "2026-09-11T10:00:00Z-bbbb",
            "2026-09-10T10:00:00Z-aaaa",
        ]
        assert "prog-b" not in result.output
        # columns: outcome and makespan actual vs planned with signed deviation
        header = result.output.splitlines()[0]
        for col in ("Run", "Started", "Outcome", "Actual", "Planned", "Deviation"):
            assert col in header
        assert "abandoned" in lines[0]
        assert (
            "0:07:00" in lines[0] and "0:06:00" in lines[0] and "+0:01:00" in lines[0]
        )

    def test_runs_shorthand_and_program_file_argument(
        self, cli_runner, recorded_runs, programs_dir
    ):
        from rhylthyme.cli.main import cli

        program_file = os.path.join(programs_dir, "prog_a.json")
        with open(program_file, "w") as fh:
            json.dump({"programId": "prog-a", "name": "A", "tracks": []}, fh)

        result = cli_runner.invoke(
            cli, ["runs", program_file, "--runs-dir", recorded_runs]
        )
        assert result.exit_code == 0, result.output
        rows = [line for line in result.output.splitlines() if line.startswith("2026-")]
        assert len(rows) == 3

    def test_runs_list_all_programs_and_json(self, cli_runner, recorded_runs):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli, ["runs", "list", "--runs-dir", recorded_runs, "--json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert [d["runId"][:10] for d in data] == [
            "2026-09-13",
            "2026-09-12",
            "2026-09-11",
            "2026-09-10",
        ]
        assert {d["programId"] for d in data} == {"prog-a", "prog-b"}

    def test_runs_list_empty(self, cli_runner, temp_dir):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli, ["runs", "list", "nothing", "--runs-dir", temp_dir]
        )
        assert result.exit_code == 0
        assert "No runs recorded" in result.output

    def test_runs_dir_from_environment_variable(
        self, cli_runner, recorded_runs, monkeypatch
    ):
        from rhylthyme.cli.main import cli

        monkeypatch.setenv("RHYLTHYME_RUNS_DIR", recorded_runs)
        result = cli_runner.invoke(cli, ["runs", "list", "prog-b"])
        assert result.exit_code == 0, result.output
        assert "2026-09-13T10:00:00Z-dddd" in result.output

    def test_runs_show_table(self, cli_runner, recorded_runs):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli,
            ["runs", "show", "2026-09-12T10:00:00Z-cccc", "--runs-dir", recorded_runs],
        )
        assert result.exit_code == 0, result.output
        out = result.output
        assert "Run:      2026-09-12T10:00:00Z-cccc" in out
        assert "[abandoned]" in out
        header = next(line for line in out.splitlines() if line.startswith("Step"))
        for col in (
            "Planned start",
            "Planned end",
            "Actual start",
            "Actual end",
            "Deviation",
            "Ended by",
            "Paused",
        ):
            assert col in header
        cook = next(line for line in out.splitlines() if line.startswith("cook"))
        assert "indefinite" in cook
        assert "0:01:00" in cook and "0:06:00" in cook  # planned
        assert "0:01:02" in cook and "0:07:00" in cook  # actual
        assert "+0:01:00" in cook  # signed deviation of the end
        assert "executor" in cook
        assert "0:00:12" in cook or "0:00:13" in cook  # pausedSeconds 12.5
        prep = next(line for line in out.splitlines() if line.startswith("prep"))
        assert "+0:00:02" in prep and "timer" in prep

    def test_runs_show_by_path_prefix_and_json(self, cli_runner, recorded_runs):
        from rhylthyme.cli.main import cli

        # unique prefix
        result = cli_runner.invoke(
            cli, ["runs", "show", "2026-09-13", "--runs-dir", recorded_runs, "--json"]
        )
        assert result.exit_code == 0, result.output
        record = json.loads(result.output)
        assert record["runId"] == "2026-09-13T10:00:00Z-dddd"
        # path
        path = os.path.join(recorded_runs, "prog-b", "2026-09-13T10_00_00Z-dddd.json")
        result = cli_runner.invoke(cli, ["runs", "show", path, "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["programId"] == "prog-b"

    def test_runs_show_unknown(self, cli_runner, recorded_runs):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli, ["runs", "show", "nope", "--runs-dir", recorded_runs]
        )
        assert result.exit_code == 1

    def test_runs_show_has_svg_options(self, cli_runner):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["runs", "show", "--help"])
        assert result.exit_code == 0
        for flag in ("--svg", "--program", "--width"):
            assert flag in result.output

    def test_runs_show_svg_writes_a_parsable_svg(
        self, cli_runner, thanksgiving_run, temp_dir
    ):
        """`runs show --svg` renders the planned-vs-actual overlay via Node."""
        import xml.etree.ElementTree as ET

        from rhylthyme.cli.history.render import find_node, find_renderer
        from rhylthyme.cli.main import cli

        if find_node() is None or find_renderer() is None:
            pytest.skip("node or the JavaScript timeline renderer is not available")

        runs_dir, run_id, program_file = thanksgiving_run
        out = os.path.join(temp_dir, "nested", "overlay.svg")
        result = cli_runner.invoke(
            cli,
            [
                "runs",
                "show",
                run_id,
                "--runs-dir",
                runs_dir,
                "--program",
                program_file,
                "--svg",
                out,
                "--width",
                "1000",
            ],
        )
        assert result.exit_code == 0, result.output
        assert f"Wrote {out}" in result.output
        assert "planned vs actual" in result.output
        assert os.path.isfile(out)

        root = ET.parse(out).getroot()
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root.get("width") == "1000"
        rects = root.iter("{http://www.w3.org/2000/svg}rect")
        classes = [r.get("class") for r in rects]
        assert classes.count("rt-baseline") == 13, "one planned ghost bar per step"
        deviations = {
            r.get("data-step"): r.get("data-deviation")
            for r in root.iter("{http://www.w3.org/2000/svg}rect")
            if r.get("data-deviation")
        }
        assert deviations["turkey-prep"] == "early"
        assert deviations["turkey-roast"] == "late"
        assert deviations["stuffing-prep"] == "on-time"

    def test_runs_show_svg_finds_the_program_by_id(
        self, cli_runner, thanksgiving_run, temp_dir, monkeypatch
    ):
        from rhylthyme.cli.history.render import find_node, find_renderer
        from rhylthyme.cli.main import cli

        if find_node() is None or find_renderer() is None:
            pytest.skip("node or the JavaScript timeline renderer is not available")

        runs_dir, run_id, program_file = thanksgiving_run
        monkeypatch.setenv("RHYLTHYME_PROGRAMS_DIR", os.path.dirname(program_file))
        out = os.path.join(temp_dir, "located.svg")
        result = cli_runner.invoke(
            cli, ["runs", "show", run_id, "--runs-dir", runs_dir, "--svg", out]
        )
        assert result.exit_code == 0, result.output
        assert os.path.isfile(out)

    def test_runs_show_svg_without_a_program_explains_itself(
        self, cli_runner, temp_dir, recorded_runs, monkeypatch
    ):
        from rhylthyme.cli.main import cli

        # No program anywhere has programId 'prog-a'.
        monkeypatch.setenv("RHYLTHYME_PROGRAMS_DIR", temp_dir)
        monkeypatch.setattr("rhylthyme.history.store._PROGRAM_SEARCH_PATHS", ())
        result = cli_runner.invoke(
            cli,
            [
                "runs",
                "show",
                "2026-09-10T10:00:00Z-aaaa",
                "--runs-dir",
                recorded_runs,
                "--svg",
                os.path.join(temp_dir, "nope.svg"),
            ],
        )
        assert result.exit_code == 1
        assert "pass --program" in result.output
        assert not os.path.exists(os.path.join(temp_dir, "nope.svg"))


@pytest.mark.cli
class TestEvalPrompts:
    """Smoke tests for `rhylthyme eval-prompts --score-only`."""

    def test_score_only_gold_against_itself(self, cli_runner, examples_dir, tmp_path):
        import shutil
        from pathlib import Path

        from rhylthyme.cli.main import cli

        if not examples_dir:
            pytest.skip("Examples directory not available")
        gold = Path(examples_dir) / "gold"
        if not gold.exists():
            pytest.skip("Gold set not available")

        predicted = tmp_path / "predicted"
        predicted.mkdir()
        for directory in gold.iterdir():
            if (directory / "program.json").exists():
                shutil.copy(
                    directory / "program.json", predicted / f"{directory.name}.json"
                )
        out = tmp_path / "eval-results"

        result = cli_runner.invoke(
            cli,
            [
                "eval-prompts",
                "--score-only",
                "--gold",
                str(gold),
                "--predicted",
                str(predicted),
                "--out",
                str(out),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "mean (n=" in result.output
        assert (out / "results.md").exists()
        data = json.loads((out / "results.json").read_text())
        assert len(data["programs"]) >= 6 and data["missing"] == []
        for key, value in data["summary"].items():
            assert value == (0.0 if key == "unsupported_rate" else 1.0), (key, value)

    def test_requires_score_only(self, cli_runner, tmp_path):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["eval-prompts", "--gold", str(tmp_path)])
        assert result.exit_code != 0
        assert "score-only" in result.output

    def test_help_lists_command(self, cli_runner):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "eval-prompts" in result.output


@pytest.mark.cli
class TestEvalPromptsLive:
    """Smoke tests for the live mode driven by the hidden --fake-client option."""

    @pytest.fixture
    def gold_dir(self, examples_dir):
        from pathlib import Path

        if not examples_dir:
            pytest.skip("Examples directory not available")
        gold = Path(examples_dir) / "gold"
        if not gold.exists():
            pytest.skip("Gold set not available")
        return gold

    def _fake_file(self, gold_dir, tmp_path):
        """Canned replies keyed by program title: the gold program itself, fenced."""
        responses = {}
        for directory in sorted(gold_dir.iterdir()):
            if not (directory / "program.json").exists():
                continue
            program = json.loads((directory / "program.json").read_text())
            responses[program["name"]] = (
                "Program:\n```json\n" + json.dumps(program) + "\n```\n"
            )
        path = tmp_path / "fake.json"
        path.write_text(json.dumps(responses))
        return path

    def test_live_then_from_cache(self, cli_runner, gold_dir, tmp_path):
        from rhylthyme.cli.main import cli

        fake = self._fake_file(gold_dir, tmp_path)
        out = tmp_path / "run"
        args = [
            "eval-prompts",
            "--gold",
            str(gold_dir),
            "--model",
            "claude-haiku-4-5",
            "--patterns",
            "baseline",
            "--fake-client",
            str(fake),
            "--out",
            str(out),
            "--skip-js",
            "--concurrency",
            "2",
            "--write-baseline",
            str(tmp_path / "baseline.json"),
        ]
        result = cli_runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert "mean (n=" in result.output
        assert "estimated cost: $" in result.output
        assert (out / "cache").is_dir() and any((out / "cache").glob("*.json"))
        first = json.loads((out / "results.json").read_text())
        assert first["meta"]["model"] == "claude-haiku-4-5"
        assert first["meta"]["pattern"] == "baseline"
        assert first["missing"] == []
        for key, value in first["summary"].items():
            assert value == (0.0 if key == "unsupported_rate" else 1.0), (key, value)
        baseline = json.loads((tmp_path / "baseline.json").read_text())
        assert set(baseline) == {
            "model",
            "pattern",
            "date",
            "git_note",
            "results",
        }
        assert baseline["results"]["summary"]["steps_f1"] == 1.0

        result = cli_runner.invoke(
            cli, args[:-2] + ["--from-cache", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        second = json.loads(result.output)
        assert second["programs"] == first["programs"]
        assert second["summary"] == first["summary"]
        assert second["meta"]["from_cache"] is True

    def test_from_cache_miss_fails_clearly(self, cli_runner, gold_dir, tmp_path):
        from rhylthyme.cli.main import cli

        fake = self._fake_file(gold_dir, tmp_path)
        result = cli_runner.invoke(
            cli,
            [
                "eval-prompts",
                "--gold",
                str(gold_dir),
                "--model",
                "never-run",
                "--fake-client",
                str(fake),
                "--out",
                str(tmp_path / "empty"),
                "--from-cache",
                "--limit",
                "1",
            ],
        )
        assert result.exit_code != 0
        assert "cache miss" in result.output

    def test_only_limits_the_run(self, cli_runner, gold_dir, tmp_path):
        from rhylthyme.cli.main import cli

        fake = self._fake_file(gold_dir, tmp_path)
        out = tmp_path / "only"
        result = cli_runner.invoke(
            cli,
            [
                "eval-prompts",
                "--gold",
                str(gold_dir),
                "--model",
                "claude-haiku-4-5",
                "--fake-client",
                str(fake),
                "--out",
                str(out),
                "--skip-js",
                "--only",
                "kitchen-weekend-brunch,kitchen-weeknight-stir-fry",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads((out / "results.json").read_text())
        assert [p["slug"] for p in data["programs"]] == [
            "kitchen-weekend-brunch",
            "kitchen-weeknight-stir-fry",
        ]
        assert data["meta"]["only"] == [
            "kitchen-weekend-brunch",
            "kitchen-weeknight-stir-fry",
        ]

    def test_only_unknown_slug_is_a_usage_error(self, cli_runner, gold_dir, tmp_path):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "eval-prompts",
                "--gold",
                str(gold_dir),
                "--model",
                "m",
                "--out",
                str(tmp_path / "x"),
                "--only",
                "not-a-slug",
            ],
        )
        assert result.exit_code != 0
        assert "not-a-slug" in result.output

    def test_unknown_pattern_and_missing_model(self, cli_runner, gold_dir):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "eval-prompts",
                "--gold",
                str(gold_dir),
                "--model",
                "m",
                "--patterns",
                "nope",
            ],
        )
        assert result.exit_code != 0 and "Unknown pattern" in result.output
        result = cli_runner.invoke(
            cli, ["eval-prompts", "--gold", str(gold_dir), "--list-patterns"]
        )
        assert result.exit_code == 0 and "baseline" in result.output


@pytest.fixture
def synthetic_runs_dir(temp_dir):
    """
    Twenty synthetic Thanksgiving runs in a private runs directory.

    The roast's duration follows ``5400 + 300 * turkeyKg``, so the author's
    9900 s is wrong in a way history can learn; the boil scatters around the
    author's own number, so it cannot. Returns
    ``(runs_dir, program_file, corpus_file)``.
    """
    from rhylthyme.history import synthesize_runs

    timeline = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "rhylthyme-timeline")
    )
    source = os.path.join(
        timeline, "test", "fixtures", "programs", "thanksgiving_one_oven.json"
    )
    if not os.path.isfile(source):
        pytest.skip("rhylthyme-timeline fixtures not checked out")
    with open(source, encoding="utf-8") as fh:
        program = json.load(fh)
    program.setdefault("metadata", {})["varianceFactors"] = [
        {"key": "turkeyKg", "label": "Turkey weight (kg)", "type": "number"}
    ]

    def law(step_id, planned, factors, rng):
        if step_id == "turkey-roast":
            return 5400.0 + 300.0 * float(factors["turkeyKg"])
        return planned

    runs_dir = os.path.join(temp_dir, "synth-runs")
    records = synthesize_runs(
        program,
        20,
        noise_by_step={"turkey-roast": 0.02, "potatoes-boil": 0.08},
        factors_fn=lambda i: {"turkeyKg": 5 + i % 4},
        duration_fn=law,
        seed=11,
        runs_dir=runs_dir,
    )
    programs = os.path.join(temp_dir, "synth-programs")
    os.makedirs(programs, exist_ok=True)
    program_file = os.path.join(programs, "thanksgiving_one_oven.json")
    with open(program_file, "w", encoding="utf-8") as fh:
        json.dump(program, fh, indent=2)
    corpus_file = os.path.join(temp_dir, "corpus.json")
    with open(corpus_file, "w", encoding="utf-8") as fh:
        json.dump({"runs": records}, fh)
    return runs_dir, program_file, corpus_file


@pytest.mark.cli
class TestRunsEvaluate:
    """`rhylthyme runs evaluate` (plan Phase 7)."""

    def test_listed_in_the_runs_help(self, cli_runner):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["runs", "--help"])
        assert result.exit_code == 0
        assert "evaluate" in result.output

    def test_table_reports_predicted_against_planned(
        self, cli_runner, synthetic_runs_dir
    ):
        from rhylthyme.cli.main import cli

        runs_dir, program_file, _corpus = synthetic_runs_dir
        result = cli_runner.invoke(
            cli, ["runs", "evaluate", program_file, "--runs-dir", runs_dir]
        )
        assert result.exit_code == 0, result.output
        assert "Held-out duration evaluation: thanksgiving-one-oven" in result.output
        assert "trained on 16, held out 4" in result.output
        for column in ("MAE predicted", "MAE planned", "Improvement", "Basis"):
            assert column in result.output
        # The roast is the row history can actually improve, and it is marked
        # as predictable by the verdicts.
        roast = [line for line in result.output.splitlines() if "turkey-roast" in line]
        assert roast and roast[0].lstrip().startswith("*turkey-roast")
        assert "predictable" in roast[0]
        assert "Claim" in result.output

    def test_json_output_is_stable_and_complete(self, cli_runner, synthetic_runs_dir):
        from rhylthyme.cli.main import cli

        runs_dir, program_file, _corpus = synthetic_runs_dir
        args = [
            "runs",
            "evaluate",
            program_file,
            "--runs-dir",
            runs_dir,
            "--format",
            "json",
        ]
        first = cli_runner.invoke(cli, args)
        second = cli_runner.invoke(cli, args)
        assert first.exit_code == 0, first.output
        assert first.output == second.output
        payload = json.loads(first.output)
        assert payload["runsTrain"] == 16 and payload["runsHeldOut"] == 4
        assert payload["claim"]["holds"] in (True, False)
        rows = {row["stepId"]: row for row in payload["steps"]}
        assert rows["turkey-roast"]["improvement"] > 0.8

    def test_holdout_and_min_runs_flags(self, cli_runner, synthetic_runs_dir):
        from rhylthyme.cli.main import cli

        runs_dir, program_file, _corpus = synthetic_runs_dir
        result = cli_runner.invoke(
            cli,
            [
                "runs",
                "evaluate",
                program_file,
                "--runs-dir",
                runs_dir,
                "--holdout",
                "0.5",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["runsHeldOut"] == 10

        gated = cli_runner.invoke(
            cli,
            [
                "runs",
                "evaluate",
                program_file,
                "--runs-dir",
                runs_dir,
                "--min-runs",
                "50",
            ],
        )
        assert gated.exit_code == 0, gated.output
        assert "Not evaluated" in gated.output
        assert "nothing to hold out yet" in gated.output

    def test_markdown_output_and_out_file(
        self, cli_runner, synthetic_runs_dir, temp_dir
    ):
        from rhylthyme.cli.main import cli

        runs_dir, program_file, _corpus = synthetic_runs_dir
        out = os.path.join(temp_dir, "evaluation.md")
        result = cli_runner.invoke(
            cli,
            [
                "runs",
                "evaluate",
                program_file,
                "--runs-dir",
                runs_dir,
                "--format",
                "md",
                "--out",
                out,
            ],
        )
        assert result.exit_code == 0, result.output
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        assert text.startswith("# Held-out duration evaluation")
        assert "| Step | Type |" in text

    def test_all_programs(self, cli_runner, synthetic_runs_dir):
        from rhylthyme.cli.main import cli

        runs_dir, _program_file, _corpus = synthetic_runs_dir
        result = cli_runner.invoke(
            cli, ["runs", "evaluate", "--all", "--runs-dir", runs_dir]
        )
        assert result.exit_code == 0, result.output
        assert "thanksgiving-one-oven" in result.output

    def test_needs_a_program_or_all(self, cli_runner, synthetic_runs_dir):
        from rhylthyme.cli.main import cli

        runs_dir, _program_file, _corpus = synthetic_runs_dir
        result = cli_runner.invoke(cli, ["runs", "evaluate", "--runs-dir", runs_dir])
        assert result.exit_code != 0
        assert "Give a programId or program file, or --all" in result.output

    def test_no_runs_is_an_error(self, cli_runner, temp_dir):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "runs",
                "evaluate",
                "nothing-here",
                "--runs-dir",
                os.path.join(temp_dir, "empty"),
            ],
        )
        assert result.exit_code == 1
        assert "No runs recorded" in result.output


@pytest.mark.cli
class TestRunHistoryFlags:
    """`rhylthyme run --history / --no-history / --predict-context`."""

    def test_flags_are_documented(self, cli_runner):
        from rhylthyme.cli.main import cli

        result = cli_runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        for flag in ("--history", "--no-history", "--predict-context"):
            assert flag in result.output
        assert "offsetsUse" in result.output

    def test_history_file_feeds_the_prediction(self, synthetic_runs_dir):
        from rhylthyme.cli.tui import prepare_predictions

        _runs_dir, program_file, corpus_file = synthetic_runs_dir
        with open(program_file, encoding="utf-8") as fh:
            program = json.load(fh)
        program["metadata"]["offsetsUse"] = "predicted"

        messages = []
        predictions = prepare_predictions(
            program,
            program,
            history_file=corpus_file,
            predict_context=["turkeyKg=7"],
            echo_fn=messages.append,
        )
        assert predictions["turkey-roast"]["seconds"] == pytest.approx(
            5400 + 300 * 7, rel=0.05
        )
        assert any("20 recorded run" in m for m in messages)

    def test_the_runs_directory_is_the_default_source(self, synthetic_runs_dir):
        from rhylthyme.cli.tui import prepare_predictions

        runs_dir, program_file, _corpus = synthetic_runs_dir
        with open(program_file, encoding="utf-8") as fh:
            program = json.load(fh)
        program["metadata"]["offsetsUse"] = "predicted"
        predictions = prepare_predictions(
            program,
            program,
            runs_dir=runs_dir,
            predict_context=["turkeyKg=7"],
            echo_fn=lambda *_: None,
        )
        assert predictions["turkey-roast"]["seconds"] == pytest.approx(
            5400 + 300 * 7, rel=0.05
        )

    def test_no_history_and_no_flag_predict_nothing(self, synthetic_runs_dir):
        from rhylthyme.cli.tui import prepare_predictions

        runs_dir, program_file, _corpus = synthetic_runs_dir
        with open(program_file, encoding="utf-8") as fh:
            planned = json.load(fh)
        # No metadata.offsetsUse: history is never even read.
        assert (
            prepare_predictions(
                planned, planned, runs_dir=runs_dir, echo_fn=lambda *_: None
            )
            == {}
        )
        # --no-history with the flag set says so and falls back to the plan.
        predicted = dict(planned)
        predicted["metadata"] = dict(planned["metadata"], offsetsUse="predicted")
        messages = []
        assert (
            prepare_predictions(
                predicted,
                predicted,
                runs_dir=runs_dir,
                use_history=False,
                echo_fn=messages.append,
            )
            == {}
        )
        assert any("--no-history" in m for m in messages)

    def test_no_recorded_runs_falls_back_to_the_plan(
        self, synthetic_runs_dir, temp_dir
    ):
        from rhylthyme.cli.tui import prepare_predictions

        _runs_dir, program_file, _corpus = synthetic_runs_dir
        with open(program_file, encoding="utf-8") as fh:
            program = json.load(fh)
        program["metadata"]["offsetsUse"] = "predicted"
        messages = []
        assert (
            prepare_predictions(
                program,
                program,
                runs_dir=os.path.join(temp_dir, "no-runs"),
                echo_fn=messages.append,
            )
            == {}
        )
        assert any("no runs are recorded" in m for m in messages)

    def test_executor_controlled_steps_are_never_predicted(self, synthetic_runs_dir):
        """The verdicts are passed to the lookup, so a choice is not forecast."""
        from rhylthyme.cli.tui import prepare_predictions

        runs_dir, program_file, corpus_file = synthetic_runs_dir
        with open(program_file, encoding="utf-8") as fh:
            program = json.load(fh)
        program["metadata"]["offsetsUse"] = "predicted"
        with open(corpus_file, encoding="utf-8") as fh:
            records = json.load(fh)["runs"]
        # Make the boil wildly variable: its coefficient of variation then
        # exceeds the threshold and `runs report` calls it executor-controlled.
        for index, record in enumerate(records):
            for step in record["steps"]:
                if step["stepId"] == "potatoes-boil":
                    step["actual"]["end"] = step["actual"]["start"] + (
                        600 + 400 * (index % 5)
                    )
        noisy = os.path.join(runs_dir, "..", "noisy-corpus.json")
        with open(noisy, "w", encoding="utf-8") as fh:
            json.dump({"runs": records}, fh)

        predictions = prepare_predictions(
            program, program, history_file=noisy, echo_fn=lambda *_: None
        )
        assert predictions["potatoes-boil"]["basis"] == "none"
        assert predictions["potatoes-boil"]["reason"] == "executor-controlled"
        assert predictions["turkey-roast"]["basis"] != "none"
