"""
Tests for the validate_program module.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import mock_open, patch

import jsonschema

from rhylthyme.validate_program import (
    load_program_file,
    perform_additional_validations,
    validate_program,
    validate_program_file,
)


class TestLoadProgramFile(unittest.TestCase):
    """Tests for the load_program_file function."""

    def test_load_valid_json(self):
        """Test loading a valid JSON file."""
        test_json = '{"name": "Test Program", "tracks": []}'

        with patch("builtins.open", mock_open(read_data=test_json)):
            result = load_program_file("dummy_path.json")

        self.assertEqual(result, {"name": "Test Program", "tracks": []})

    def test_invalid_json(self):
        """Test loading an invalid JSON file."""
        test_json = '{"name": "Test Program", "tracks": []'  # Missing closing brace

        with patch("builtins.open", mock_open(read_data=test_json)):
            with self.assertRaises(ValueError):
                load_program_file("dummy_path.json")

    def test_file_not_found(self):
        """Test loading a non-existent file."""
        with patch("builtins.open", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                load_program_file("nonexistent_file.json")


class TestValidateProgram(unittest.TestCase):
    """Tests for the validate_program function."""

    def test_valid_program(self):
        """Test validating a valid program against the schema."""
        # Minimal valid program
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        }
                    ],
                }
            ],
        }

        # Minimal schema that accepts the program
        schema = {
            "type": "object",
            "required": ["programId", "name", "tracks"],
            "properties": {
                "programId": {"type": "string"},
                "name": {"type": "string"},
                "tracks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["trackId", "name", "steps"],
                        "properties": {
                            "trackId": {"type": "string"},
                            "name": {"type": "string"},
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": [
                                        "stepId",
                                        "name",
                                        "startTrigger",
                                        "duration",
                                    ],
                                    "properties": {
                                        "stepId": {"type": "string"},
                                        "name": {"type": "string"},
                                        "startTrigger": {
                                            "type": "object",
                                            "required": ["type"],
                                            "properties": {
                                                "type": {
                                                    "type": "string",
                                                    "enum": ["programStart"],
                                                }
                                            },
                                        },
                                        "duration": {
                                            "type": "object",
                                            "required": ["type", "seconds"],
                                            "properties": {
                                                "type": {
                                                    "type": "string",
                                                    "enum": ["fixed"],
                                                },
                                                "seconds": {"type": "number"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

        is_valid, errors = validate_program(program, schema)
        self.assertTrue(is_valid)
        self.assertEqual(errors, [])

    def test_invalid_program(self):
        """Test validating an invalid program against the schema."""
        # Invalid program (missing required name field)
        program = {"programId": "test-program", "tracks": []}

        # Schema that requires name
        schema = {
            "type": "object",
            "required": ["programId", "name", "tracks"],
            "properties": {
                "programId": {"type": "string"},
                "name": {"type": "string"},
                "tracks": {"type": "array"},
            },
        }

        is_valid, errors = validate_program(program, schema)
        self.assertFalse(is_valid)
        self.assertTrue(len(errors) > 0)


class TestAdditionalValidations(unittest.TestCase):
    """Tests for the perform_additional_validations function."""

    def test_valid_program_no_errors(self):
        """Test a valid program with no additional validation errors."""
        # Simple valid program
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        }
                    ],
                }
            ],
        }

        errors = perform_additional_validations(program)
        self.assertEqual(errors, [])

    def test_duplicate_step_ids(self):
        """Test a program with duplicate step IDs."""
        # Program with duplicate step IDs
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        },
                        {
                            "stepId": "step1",  # Duplicate ID
                            "name": "Step 2",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        },
                    ],
                }
            ],
        }

        errors = perform_additional_validations(program)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("duplicate" in error.lower() for error in errors))

    def test_invalid_step_reference(self):
        """Test a program with an invalid step reference."""
        # Program with invalid step reference
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {
                                "type": "afterStep",
                                "stepId": "non_existent_step",
                            },
                            "duration": {"type": "fixed", "seconds": 60},
                        }
                    ],
                }
            ],
        }

        errors = perform_additional_validations(program)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("reference" in error.lower() for error in errors))

    def test_resource_constraint_format_validation(self):
        """Test that resource constraints with incorrect format are caught."""
        # Program with resource constraints using "name" instead of "task"
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                            "task": "stove-burner",
                        }
                    ],
                }
            ],
            "resourceConstraints": [
                {"name": "stove-burner", "maxConcurrent": 2}  # Wrong: should be "task"
            ],
        }

        errors = perform_additional_validations(program, strict=True)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(
            any(
                "stove-burner" in error and "used in steps but not defined" in error
                for error in errors
            )
        )

    def test_buffer_task_validation(self):
        """Test that buffer tasks are validated against resource constraints."""
        # Program with buffer tasks not defined in resourceConstraints
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                            "preBuffer": {
                                "duration": {"type": "fixed", "seconds": 30},
                                "tasks": ["prep-station"],
                            },
                            "postBuffer": {
                                "duration": {"type": "fixed", "seconds": 20},
                                "taskResources": [
                                    {"name": "cleanup-tools", "amount": 1.0}
                                ],
                            },
                        }
                    ],
                }
            ],
            "resourceConstraints": [{"task": "different-task", "maxConcurrent": 1}],
        }

        errors = perform_additional_validations(program, strict=True)
        self.assertTrue(len(errors) > 0)
        # Should find errors for both prep-station and cleanup-tools
        prep_station_error = any(
            "prep-station" in error and "used in steps but not defined" in error
            for error in errors
        )
        cleanup_tools_error = any(
            "cleanup-tools" in error and "used in steps but not defined" in error
            for error in errors
        )
        self.assertTrue(prep_station_error)
        self.assertTrue(cleanup_tools_error)

    def test_buffer_task_validation_with_actors(self):
        """Test that buffer task validation is skipped when actors are defined."""
        # Program with buffer tasks and actors (should not generate errors)
        program = {
            "programId": "test-program",
            "name": "Test Program",
            "actors": ["actor1", "actor2"],  # Having actors means default constraints
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                            "preBuffer": {
                                "duration": {"type": "fixed", "seconds": 30},
                                "tasks": ["prep-station"],
                            },
                        }
                    ],
                }
            ],
        }

        errors = perform_additional_validations(program)
        # Should not have task-related errors because actors provides default constraints
        task_errors = [
            error for error in errors if "used in steps but not defined" in error
        ]
        self.assertEqual(len(task_errors), 0)


class TestValidateProgramFile(unittest.TestCase):
    """Tests for the validate_program_file function."""

    def setUp(self):
        """Set up test fixtures."""
        self.valid_program = {
            "programId": "test-program",
            "name": "Test Program",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        }
                    ],
                }
            ],
        }

        # Create a temporary schema file
        self.schema = {
            "type": "object",
            "required": ["programId", "name", "tracks"],
            "properties": {
                "programId": {"type": "string"},
                "name": {"type": "string"},
                "tracks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["trackId", "name", "steps"],
                        "properties": {
                            "trackId": {"type": "string"},
                            "name": {"type": "string"},
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": [
                                        "stepId",
                                        "name",
                                        "startTrigger",
                                        "duration",
                                    ],
                                    "properties": {
                                        "stepId": {"type": "string"},
                                        "name": {"type": "string"},
                                        "startTrigger": {
                                            "type": "object",
                                            "required": ["type"],
                                            "properties": {"type": {"type": "string"}},
                                        },
                                        "duration": {
                                            "type": "object",
                                            "required": ["type"],
                                            "properties": {"type": {"type": "string"}},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

    @patch("rhylthyme.validate_program.load_program_file")
    @patch("rhylthyme.validate_program.validate_program")
    @patch("rhylthyme.validate_program.perform_additional_validations_structured")
    def test_validate_valid_program_file(
        self, mock_additional, mock_validate, mock_load
    ):
        """Test validating a valid program file."""
        # Set up mocks
        mock_load.side_effect = [
            self.schema,
            self.valid_program,
        ]  # Schema loaded first, then program
        mock_validate.return_value = (True, [])
        mock_additional.return_value = []

        # Call the function
        result = validate_program_file(
            "valid_program.json", "schema.json", verbose=False
        )

        # Check results
        self.assertTrue(result)
        mock_load.assert_any_call("schema.json")  # Schema is loaded first
        mock_load.assert_any_call("valid_program.json")  # Then program
        mock_validate.assert_called_once_with(self.valid_program, self.schema)
        mock_additional.assert_called_once_with(
            self.valid_program, strict=False, workcell=None
        )

    @patch("rhylthyme.validate_program.load_program_file")
    @patch("rhylthyme.validate_program.validate_program")
    @patch("rhylthyme.validate_program.perform_additional_validations_structured")
    def test_validate_invalid_program_file(
        self, mock_additional, mock_validate, mock_load
    ):
        """Test validating an invalid program file."""
        # Set up mocks
        mock_load.side_effect = [
            self.schema,
            self.valid_program,
        ]  # Schema loaded first, then program
        mock_validate.return_value = (False, ["Error: Invalid program"])
        mock_additional.return_value = []

        # Call the function
        result = validate_program_file(
            "invalid_program.json", "schema.json", verbose=False
        )

        # Check results
        self.assertFalse(result)
        mock_load.assert_any_call("schema.json")  # Schema is loaded first
        mock_load.assert_any_call("invalid_program.json")  # Then program
        mock_validate.assert_called_once_with(self.valid_program, self.schema)
        mock_additional.assert_called_once_with(
            self.valid_program, strict=False, workcell=None
        )


if __name__ == "__main__":
    unittest.main()
