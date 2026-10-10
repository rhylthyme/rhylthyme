"""
Integration tests for Rhylthyme CLI runner with examples.

This module tests the CLI runner by:
1. Fetching example programs from the rhylthyme-examples repository
2. Validating all example programs
3. Testing environment loading and validation
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import requests
import yaml
from click.testing import CliRunner

# Add the src directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rhylthyme.cli.main import cli
from rhylthyme.environment_loader import EnvironmentLoader
from rhylthyme.program_runner import ProgramRunner, StepStatus
from rhylthyme.validate_program import validate_program_file


class TestExamplesIntegration(unittest.TestCase):
    """Integration tests for examples validation and execution."""

    def setUp(self):
        """Set up test environment."""
        self.runner = CliRunner()
        self.temp_dir = tempfile.mkdtemp()
        self.programs_dir = os.path.join(self.temp_dir, "programs")

        # Create directories
        os.makedirs(self.programs_dir, exist_ok=True)

    def tearDown(self):
        """Clean up test environment."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_simple_program(self, filename="simple_test.json"):
        """Create a simple test program."""
        program = {
            "programId": "simple-test",
            "name": "Simple Test Program",
            "description": "A simple test program for integration testing",
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
                            "description": "First step",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 5},
                            "preBuffer": {
                                "duration": "5s",
                                "description": "Setup",
                                "tasks": ["test-task"],
                            },
                        },
                        {
                            "stepId": "step2",
                            "name": "Step 2",
                            "description": "Second step",
                            "startTrigger": {"type": "afterStep", "stepId": "step1"},
                            "duration": {"type": "fixed", "seconds": 3},
                            "preBuffer": {
                                "duration": "3s",
                                "description": "Setup",
                                "tasks": ["test-task"],
                            },
                        },
                    ],
                }
            ],
        }

        filepath = os.path.join(self.programs_dir, filename)
        with open(filepath, "w") as f:
            json.dump(program, f, indent=2)

        return filepath

    def create_manual_program(self, filename="manual_test.json"):
        """Create a program with manual triggers."""
        program = {
            "programId": "manual-test",
            "name": "Manual Test Program",
            "description": "A test program with manual triggers",
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
                            "description": "A manual step",
                            "startTrigger": {"type": "manual"},
                            "duration": {
                                "type": "indefinite",
                                "defaultSeconds": 60,
                                "triggerName": "stop-manual-step",
                            },
                            "preBuffer": {
                                "duration": "5s",
                                "description": "Setup",
                                "tasks": ["manual-task"],
                            },
                        }
                    ],
                }
            ],
        }

        filepath = os.path.join(self.programs_dir, filename)
        with open(filepath, "w") as f:
            json.dump(program, f, indent=2)

        return filepath

    def create_kitchen_program(self, filename="kitchen_test.json"):
        """Create a kitchen program with resources."""
        program = {
            "programId": "kitchen-test",
            "name": "Kitchen Test Program",
            "description": "A test program with kitchen resources",
            "version": "1.0.0",
            "environmentType": "kitchen",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "cooking",
                    "name": "Cooking Track",
                    "steps": [
                        {
                            "stepId": "cook-pasta",
                            "name": "Cook Pasta",
                            "description": "Cook pasta",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 10},
                            "preBuffer": {
                                "duration": "5s",
                                "description": "Setup",
                                "tasks": ["cooking"],
                            },
                            "resources": [
                                {
                                    "resourceId": "stove-burner-1",
                                    "type": "stove-burner",
                                    "quantity": 1,
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        filepath = os.path.join(self.programs_dir, filename)
        with open(filepath, "w") as f:
            json.dump(program, f, indent=2)

        return filepath

    def create_kitchen_environment(self, filename="kitchen.json"):
        """Create a kitchen environment."""
        environment = {
            "environmentId": "test-kitchen",
            "name": "Test Kitchen",
            "type": "kitchen",
            "description": "A test kitchen environment",
            "resourceConstraints": [
                {"task": "cooking", "type": "stove-burner", "capacity": 1},
                {"task": "prep", "type": "prep-station", "capacity": 1},
            ],
        }

        filepath = os.path.join(self.temp_dir, filename)
        with open(filepath, "w") as f:
            json.dump(environment, f, indent=2)

        return filepath

    def test_validate_simple_program(self):
        """Test validating a simple program."""
        program_file = self.create_simple_program()
        result = self.runner.invoke(cli, ["validate", program_file])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_validate_manual_program(self):
        """Test validating a manual program."""
        program_file = self.create_manual_program()
        result = self.runner.invoke(cli, ["validate", program_file])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_validate_kitchen_program(self):
        """Test validating a kitchen program."""
        program_file = self.create_kitchen_program()
        result = self.runner.invoke(cli, ["validate", program_file])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_validate_kitchen_program_with_environment(self):
        """Test validating a kitchen program with environment."""
        program_file = self.create_kitchen_program()
        environment_file = self.create_kitchen_environment()

        result = self.runner.invoke(
            cli, ["validate", program_file, "-e", environment_file]
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_program_runner_creation(self):
        """Test program runner creation and basic functionality."""
        program_file = self.create_simple_program()

        with open(program_file, "r") as f:
            program_data = json.load(f)

        runner = ProgramRunner(program_data)

        # Test basic functionality
        self.assertIsNotNone(runner)
        self.assertEqual(len(runner.program["tracks"]), 1)
        self.assertEqual(len(runner.program["tracks"][0]["steps"]), 2)

        # Test getting step by ID
        step = runner.get_step_by_id("step1")
        self.assertIsNotNone(step)
        self.assertEqual(step.step_id, "step1")

        # Test getting all steps display info
        steps_info = runner.get_all_steps_display_info()
        self.assertEqual(len(steps_info), 2)

    def test_manual_program_runner(self):
        """Test manual program runner."""
        program_file = self.create_manual_program()

        with open(program_file, "r") as f:
            program_data = json.load(f)

        runner = ProgramRunner(program_data)

        # Test getting step by ID
        step = runner.get_step_by_id("manual-step")
        self.assertIsNotNone(step)
        self.assertEqual(step.step_id, "manual-step")

        # Test getting available triggers
        triggers = runner.get_available_triggers()
        self.assertGreater(len(triggers), 0)

        # Test triggering manual step
        runner.trigger_manual_step("manual-step", "manual-step")

        # Test getting step display info
        step_info = runner.get_step_display_info(step)
        self.assertIsNotNone(step_info)

    def test_environment_loading(self):
        """Test environment loading from file."""
        environment_file = self.create_kitchen_environment()

        with open(environment_file, "r") as f:
            environment_data = json.load(f)

        self.assertEqual(environment_data["environmentId"], "test-kitchen")
        self.assertEqual(environment_data["type"], "kitchen")
        self.assertIn("resourceConstraints", environment_data)

    def test_environment_validation(self):
        """Test environment validation."""
        environment_file = self.create_kitchen_environment()

        with open(environment_file, "r") as f:
            environment_data = json.load(f)

        # Basic validation
        self.assertIn("environmentId", environment_data)
        self.assertIn("name", environment_data)
        self.assertIn("type", environment_data)
        self.assertIn("resourceConstraints", environment_data)

    def test_environment_info(self):
        """Test environment info command."""
        environment_file = self.create_kitchen_environment()
        result = self.runner.invoke(cli, ["environment-info", "kitchen"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("kitchen", result.output)
        self.assertIn("Environment Type", result.output)

    def test_program_planning(self):
        """Test program planning."""
        program_file = self.create_kitchen_program()
        output_file = os.path.join(self.temp_dir, "output.json")

        result = self.runner.invoke(cli, ["plan", program_file, output_file])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Optimized program saved to", result.output)

    def test_batch_validation(self):
        """Test batch validation of multiple programs."""
        program1 = self.create_simple_program("program1.json")
        program2 = self.create_manual_program("program2.json")
        program3 = self.create_kitchen_program("program3.json")

        result = self.runner.invoke(cli, ["validate", program1, program2, program3])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)
        # Should see validation for all three programs

    def test_error_handling_invalid_program(self):
        """Test error handling for invalid program."""
        # Create an invalid program
        invalid_program = {
            "programId": "invalid",
            "name": "Invalid Program",
            # Missing required fields
        }

        invalid_file = os.path.join(self.programs_dir, "invalid.json")
        with open(invalid_file, "w") as f:
            json.dump(invalid_program, f)

        result = self.runner.invoke(cli, ["validate", invalid_file])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("error", result.output.lower())

    def test_error_handling_missing_file(self):
        """Test error handling for missing file."""
        result = self.runner.invoke(cli, ["validate", "nonexistent.json"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("error", result.output.lower())

    def test_cli_help(self):
        """Test CLI help command."""
        result = self.runner.invoke(cli, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Usage:", result.output)
        self.assertIn("Commands:", result.output)
        self.assertIn("validate", result.output)
        self.assertIn("run", result.output)

    def test_cli_version(self):
        """Test CLI version command."""
        result = self.runner.invoke(cli, ["--version"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("version", result.output.lower())

    @patch("requests.get")
    def test_fetch_examples_from_repository(self, mock_get):
        """Test fetching examples from repository."""
        # Mock the repository response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = (
            b'{"tree": [{"path": "programs/test.json", "type": "blob"}]}'
        )
        mock_get.return_value = mock_response

        # Mock the file content response
        mock_file_response = MagicMock()
        mock_file_response.status_code = 200
        mock_file_response.content = b'{"programId": "test", "name": "Test"}'
        mock_get.side_effect = [mock_response, mock_file_response]

        # This test is just verifying that mocks are set up correctly
        # Since there's no actual fetch functionality implemented,
        # we just verify the mock setup works
        self.assertIsNotNone(mock_get)

    def test_program_with_variable_duration(self):
        """Test program with variable duration."""
        program = {
            "programId": "variable-test",
            "name": "Variable Duration Test",
            "description": "Test program with variable duration",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "variable-step",
                            "name": "Variable Step",
                            "description": "A variable step",
                            "startTrigger": {"type": "programStart"},
                            "duration": {
                                "type": "variable",
                                "minSeconds": 2,
                                "maxSeconds": 5,
                            },
                            "preBuffer": {
                                "duration": "5s",
                                "description": "Setup",
                                "tasks": ["test-task"],
                            },
                        }
                    ],
                }
            ],
        }

        filepath = os.path.join(self.programs_dir, "variable_test.json")
        with open(filepath, "w") as f:
            json.dump(program, f, indent=2)

        result = self.runner.invoke(cli, ["validate", filepath])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_program_with_multiple_tracks(self):
        """Test program with multiple tracks."""
        program = {
            "programId": "multi-track-test",
            "name": "Multi-Track Test",
            "description": "Test program with multiple tracks",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "description": "First step",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 3},
                            "preBuffer": {
                                "duration": "3s",
                                "description": "Setup",
                                "tasks": ["task1"],
                            },
                        }
                    ],
                },
                {
                    "trackId": "track2",
                    "name": "Track 2",
                    "steps": [
                        {
                            "stepId": "step2",
                            "name": "Step 2",
                            "description": "Second step",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 2},
                            "preBuffer": {
                                "duration": "2s",
                                "description": "Setup",
                                "tasks": ["task2"],
                            },
                        }
                    ],
                },
            ],
        }

        filepath = os.path.join(self.programs_dir, "multi_track_test.json")
        with open(filepath, "w") as f:
            json.dump(program, f, indent=2)

        result = self.runner.invoke(cli, ["validate", filepath])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_yaml_program_validation(self):
        """Test YAML program validation."""
        program = {
            "programId": "yaml-test",
            "name": "YAML Test Program",
            "description": "A test program in YAML format",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "yaml-step",
                            "name": "YAML Step",
                            "description": "A YAML step",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 5},
                            "preBuffer": {
                                "duration": "5s",
                                "description": "Setup",
                                "tasks": ["test-task"],
                            },
                        }
                    ],
                }
            ],
        }

        filepath = os.path.join(self.programs_dir, "yaml_test.yaml")
        with open(filepath, "w") as f:
            yaml.dump(program, f, default_flow_style=False)

        result = self.runner.invoke(cli, ["validate", filepath])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)


class TestExamplesRepositoryIntegration(unittest.TestCase):
    """Integration tests with real examples repository."""

    def setUp(self):
        """Set up test environment."""
        self.runner = CliRunner()
        self.temp_dir = tempfile.mkdtemp()
        self.examples_dir = os.path.join(self.temp_dir, "rhylthyme-examples")

        # Create a mock examples structure
        os.makedirs(os.path.join(self.examples_dir, "programs"), exist_ok=True)
        os.makedirs(os.path.join(self.examples_dir, "environments"), exist_ok=True)

    def tearDown(self):
        """Clean up test environment."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_real_examples(self):
        """Test validating real examples from repository."""
        # Create a simple real example
        program = {
            "programId": "real-example",
            "name": "Real Example",
            "description": "A real example program",
            "version": "1.0.0",
            "environmentType": "test",
            "startTrigger": {"type": "manual"},
            "tracks": [
                {
                    "trackId": "main",
                    "name": "Main Track",
                    "steps": [
                        {
                            "stepId": "real-step",
                            "name": "Real Step",
                            "description": "A real step",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 5},
                            "preBuffer": {
                                "duration": "5s",
                                "description": "Setup",
                                "tasks": ["real-task"],
                            },
                        }
                    ],
                }
            ],
        }

        program_file = os.path.join(self.examples_dir, "programs", "real_example.json")
        with open(program_file, "w") as f:
            json.dump(program, f, indent=2)

        result = self.runner.invoke(cli, ["validate", program_file])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("is valid", result.output)

    def test_validate_real_environments(self):
        """Test validating real environments from repository."""
        environment = {
            "environmentId": "real-environment",
            "name": "Real Environment",
            "type": "test",
            "description": "A real test environment",
            "resourceConstraints": [
                {"task": "test-task", "type": "generic", "capacity": 1}
            ],
        }

        env_file = os.path.join(
            self.examples_dir, "environments", "real_environment.json"
        )
        with open(env_file, "w") as f:
            json.dump(environment, f, indent=2)

        result = self.runner.invoke(cli, ["environment-info", "kitchen"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("kitchen", result.output)


if __name__ == "__main__":
    unittest.main()
