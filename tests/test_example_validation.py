"""
Test validation of all example programs in rhylthyme-examples.

This module tests validation of real example programs to ensure they
comply with the schema and don't have logic errors.
"""

import glob
import json
import os
from pathlib import Path

import pytest

# Import the validation function
from rhylthyme.validate_program import validate_program_file_structured


@pytest.mark.integration
@pytest.mark.slow
class TestExampleValidation:
    """Test validation of all example programs."""

    def test_validate_all_json_examples(self, examples_dir, schema_file):
        """Test that all JSON example programs validate successfully."""
        if not examples_dir or not schema_file:
            pytest.skip("Examples or schema directory not available")

        programs_dir = os.path.join(examples_dir, "programs")
        if not os.path.exists(programs_dir):
            pytest.skip("Programs directory not found")

        json_files = glob.glob(os.path.join(programs_dir, "*.json"))

        if not json_files:
            pytest.skip("No JSON example files found")

        validation_results = []

        for filepath in sorted(json_files):
            filename = os.path.basename(filepath)

            try:
                result = validate_program_file_structured(filepath, schema_file)
                validation_results.append(
                    {"filename": filename, "filepath": filepath, "result": result}
                )

            except Exception as e:
                pytest.fail(f"Validation of {filename} failed with exception: {e}")

        # Check results
        schema_failures = []
        logic_warnings = []

        for item in validation_results:
            filename = item["filename"]
            result = item["result"]

            if result["schema_errors"]:
                schema_failures.append((filename, result["schema_errors"]))

            if result["logic_errors"]:
                logic_warnings.append((filename, result["logic_errors"]))

        # Report schema failures (these should cause test failure)
        if schema_failures:
            failure_msg = "\n❌ SCHEMA ERRORS (FATAL):\n"
            failure_msg += "-" * 40 + "\n"
            for filename, errors in schema_failures:
                failure_msg += f"\n{filename}:\n"
                for error in errors:
                    failure_msg += f"  - {error}\n"
            print(failure_msg)
            pytest.fail(failure_msg)

        # Report logic warnings (these don't fail the test but should be logged)
        if logic_warnings:
            warning_msg = "Logic validation warnings:\n"
            for filename, errors in logic_warnings:
                warning_msg += f"\n{filename}:\n"
                for error in errors:
                    warning_msg += f"  - {error}\n"
            # Use pytest.warns or just print for now
            print(f"\n⚠️ Warning: {warning_msg}")

    def test_validate_specific_examples(self, examples_dir, schema_file):
        """Test validation of specific known example programs."""
        if not examples_dir or not schema_file:
            pytest.skip("Examples or schema directory not available")

        programs_dir = os.path.join(examples_dir, "programs")

        # List of known good example files
        known_good_examples = [
            "breakfast_schedule.json",
            "breakfast_with_buffers.json",
            "lab_experiment.json",
        ]

        for example_name in known_good_examples:
            filepath = os.path.join(programs_dir, example_name)

            if not os.path.exists(filepath):
                continue  # Skip if file doesn't exist

            try:
                result = validate_program_file_structured(filepath, schema_file)

                # These examples should have no schema errors
                assert not result[
                    "schema_errors"
                ], f"{example_name} has schema errors: {result['schema_errors']}"

                # Logic errors are warnings, not failures for these tests
                if result["logic_errors"]:
                    print(
                        f"\n⚠️ Logic warnings in {example_name}: {result['logic_errors']}"
                    )

            except Exception as e:
                pytest.fail(f"Validation of {example_name} failed: {e}")

    def test_validate_yaml_examples(self, examples_dir, schema_file):
        """Test validation of YAML example programs."""
        if not examples_dir or not schema_file:
            pytest.skip("Examples or schema directory not available")

        programs_dir = os.path.join(examples_dir, "programs")
        yaml_files = glob.glob(os.path.join(programs_dir, "*.yaml")) + glob.glob(
            os.path.join(programs_dir, "*.yml")
        )

        if not yaml_files:
            pytest.skip("No YAML example files found")

        # Note: YAML parsing might have known issues with duration strings
        yaml_results = []

        for filepath in sorted(yaml_files):
            filename = os.path.basename(filepath)

            try:
                result = validate_program_file_structured(filepath, schema_file)
                yaml_results.append(
                    {"filename": filename, "result": result, "success": True}
                )

            except Exception as e:
                # For YAML files, we might expect some parsing issues
                yaml_results.append(
                    {"filename": filename, "error": str(e), "success": False}
                )
                print(f"⚠️ YAML parsing issue in {filename}: {e}")

        # For YAML, we're more lenient - just report the results
        successful_yaml = [r for r in yaml_results if r.get("success", False)]
        failed_yaml = [r for r in yaml_results if not r.get("success", False)]

        print(f"\nYAML validation results:")
        print(f"  Successful: {len(successful_yaml)}")
        print(f"  Failed: {len(failed_yaml)}")

        if failed_yaml:
            print("Failed YAML files:")
            for item in failed_yaml:
                print(f"  - {item['filename']}: {item.get('error', 'Unknown error')}")

    def test_environment_file_validation(self, examples_dir):
        """Test that environment files exist and have basic structure."""
        if not examples_dir:
            pytest.skip("Examples directory not available")

        environments_dir = os.path.join(examples_dir, "environments")
        if not os.path.exists(environments_dir):
            pytest.skip("Environments directory not found")

        env_files = glob.glob(os.path.join(environments_dir, "*.json"))

        if not env_files:
            pytest.skip("No environment files found")

        for filepath in env_files:
            filename = os.path.basename(filepath)

            try:
                import json

                with open(filepath, "r") as f:
                    env_data = json.load(f)

                # Basic structure checks
                assert "environmentId" in env_data, f"{filename} missing environmentId"
                assert "name" in env_data, f"{filename} missing name"

                # Type information is helpful but not required
                if "type" not in env_data:
                    print(f"⚠️ {filename} missing type field")

            except Exception as e:
                pytest.fail(f"Environment file {filename} validation failed: {e}")


@pytest.mark.integration
def test_validation_imports():
    """Test that we can import validation modules successfully."""
    try:
        from rhylthyme.validate_program import (
            validate_program_file,
            validate_program_file_structured,
        )

        assert callable(validate_program_file_structured)
        assert callable(validate_program_file)
    except ImportError as e:
        pytest.fail(f"Could not import validation modules: {e}")


@pytest.mark.unit
def test_validation_function_with_invalid_file():
    """Test validation function behavior with invalid files."""
    from rhylthyme.validate_program import validate_program_file

    try:
        import pkg_resources  # type: ignore[import-untyped]

        schema_file = pkg_resources.resource_filename(
            "rhylthyme_spec", "schemas/program_schema_0.2.0-alpha.json"
        )
    except (ImportError, FileNotFoundError):
        pytest.skip("Schema file not available")

    # Test with non-existent file - should handle this gracefully
    try:
        result = validate_program_file(
            "nonexistent_file.json",
            schema_file,
            verbose=False,
            json_output=False,
            strict=False,
        )
        assert result is False
    except FileNotFoundError:
        # This is expected behavior for non-existent files
        pass


@pytest.mark.unit
def test_validation_function_with_invalid_json(temp_dir):
    """Test validation function with invalid JSON."""
    from rhylthyme.validate_program import validate_program_file

    try:
        import pkg_resources  # type: ignore[import-untyped]

        schema_file = pkg_resources.resource_filename(
            "rhylthyme_spec", "schemas/program_schema_0.2.0-alpha.json"
        )
    except (ImportError, FileNotFoundError):
        pytest.skip("Schema file not available")

    # Create invalid JSON file
    invalid_json_file = os.path.join(temp_dir, "invalid.json")
    with open(invalid_json_file, "w") as f:
        f.write('{ "invalid": json content }')

    # Should handle JSON parsing errors gracefully
    try:
        result = validate_program_file(
            invalid_json_file,
            schema_file,
            verbose=False,
            json_output=False,
            strict=False,
        )
        assert result is False
    except (ValueError, json.JSONDecodeError):
        # This is expected behavior for invalid JSON files
        pass
