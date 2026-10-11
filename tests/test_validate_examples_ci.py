#!/usr/bin/env python3
"""
Validate all examples for CI - converted to pytest format.

This module can be run as a pytest test or as a standalone script.
"""

import glob
import json
import os
import sys
from pathlib import Path

import pytest

# Add src to path
src_path = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, src_path)

from rhylthyme.validate_program import validate_program_file_structured


@pytest.mark.integration
@pytest.mark.slow
def test_validate_all_examples_for_ci():
    """Validate all examples for CI - fails only on schema errors."""
    # Find all example files
    current_dir = Path(__file__).parent
    examples_dir = current_dir / ".." / ".." / "rhylthyme-examples" / "programs"
    schema_file = (
        current_dir
        / ".."
        / ".."
        / "rhylthyme-spec"
        / "src"
        / "rhylthyme_spec"
        / "schemas"
        / "program_schema_0.2.0-alpha.json"
    )

    if not examples_dir.exists():
        pytest.skip("Examples directory not found")

    if not schema_file.exists():
        pytest.skip("Schema file not found")

    files = (
        list(examples_dir.glob("*.json"))
        + list(examples_dir.glob("*.yaml"))
        + list(examples_dir.glob("*.yml"))
    )

    if not files:
        pytest.skip("No example files found")

    print(f"\nFound {len(files)} example files to validate")
    print("=" * 60)

    schema_errors = []
    logic_errors = []
    valid_count = 0

    for filepath in sorted(files):
        filename = filepath.name
        print(f"Validating {filename}...")

        try:
            result = validate_program_file_structured(str(filepath), str(schema_file))

            if result["schema_errors"]:
                schema_errors.append((filename, result["schema_errors"]))
                print(f'  ❌ Schema errors: {len(result["schema_errors"])}')
            else:
                print(f"  ✅ Schema: OK")

            if result["logic_errors"]:
                logic_errors.append((filename, result["logic_errors"]))
                print(f'  ⚠️  Logic errors: {len(result["logic_errors"])}')
            else:
                print(f"  ✅ Logic: OK")

            if not result["schema_errors"] and not result["logic_errors"]:
                valid_count += 1

        except Exception as e:
            schema_errors.append((filename, [f"Validation failed: {str(e)}"]))
            print(f"  ❌ Exception: {e}")

    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    print(f"Total files: {len(files)}")
    print(f"Fully valid: {valid_count}")
    print(f"Files with schema errors: {len(schema_errors)}")
    print(f"Files with logic errors: {len(logic_errors)}")

    if logic_errors:
        print("\n⚠️  LOGIC ERRORS (WARNINGS):")
        print("-" * 40)
        for filename, errors in logic_errors:
            print(f"\n{filename}:")
            for error in errors:
                print(f"  - {error}")

    # Schema errors cause test failure
    if schema_errors:
        error_msg = "\n❌ SCHEMA ERRORS (FATAL):\n"
        error_msg += "-" * 40 + "\n"
        for filename, errors in schema_errors:
            error_msg += f"\n{filename}:\n"
            for error in errors:
                error_msg += f"  - {error}\n"
        print(error_msg)
        pytest.fail(error_msg)
    else:
        print("\n✅ All examples passed schema validation")
        if logic_errors:
            print("⚠️  Some examples have logic errors (warnings only)")


def main():
    """Run validation as standalone script."""
    # Run the test function directly
    try:
        test_validate_all_examples_for_ci()
        print("\n✅ All validations passed!")
    except Exception as e:
        print(f"\n❌ Validation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
