"""
Pytest configuration and shared fixtures for rhylthyme-cli-runner tests.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

# Add src directory to path so we can import modules
src_path = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, src_path)

from rhylthyme.cli.main import cli


@pytest.fixture
def cli_runner():
    """Provide a Click CLI runner for testing."""
    return CliRunner()


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that gets cleaned up after test."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def programs_dir(temp_dir):
    """Provide a temporary programs directory."""
    programs_dir = os.path.join(temp_dir, "programs")
    os.makedirs(programs_dir, exist_ok=True)
    return programs_dir


@pytest.fixture
def environments_dir(temp_dir):
    """Provide a temporary environments directory."""
    environments_dir = os.path.join(temp_dir, "environments")
    os.makedirs(environments_dir, exist_ok=True)
    return environments_dir


@pytest.fixture
def simple_program():
    """Provide a simple test program definition."""
    return {
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
        "resourceConstraints": [
            {
                "task": "test-task",
                "maxConcurrent": 1,
                "description": "Test task constraint",
            }
        ],
        "actors": 1,
    }


@pytest.fixture
def kitchen_program():
    """Provide a kitchen program with resources."""
    return {
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
                        "task": "cooking",
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
        "resourceConstraints": [
            {
                "task": "cooking",
                "maxConcurrent": 1,
                "description": "Cooking task constraint",
            }
        ],
        "actors": 1,
    }


@pytest.fixture
def kitchen_environment():
    """Provide a kitchen environment definition."""
    return {
        "environmentId": "test-kitchen",
        "name": "Test Kitchen",
        "type": "kitchen",
        "description": "A test kitchen environment",
        "resourceConstraints": [
            {
                "task": "cooking",
                "maxConcurrent": 1,
                "description": "Stove burner cooking constraint",
            },
            {
                "task": "prep",
                "maxConcurrent": 1,
                "description": "Prep station constraint",
            },
        ],
        "actors": 1,
    }


@pytest.fixture
def simple_program_file(programs_dir, simple_program):
    """Create a simple program file and return its path."""
    filepath = os.path.join(programs_dir, "simple_test.json")
    with open(filepath, "w") as f:
        json.dump(simple_program, f, indent=2)
    return filepath


@pytest.fixture
def kitchen_program_file(programs_dir, kitchen_program):
    """Create a kitchen program file and return its path."""
    filepath = os.path.join(programs_dir, "kitchen_test.json")
    with open(filepath, "w") as f:
        json.dump(kitchen_program, f, indent=2)
    return filepath


@pytest.fixture
def kitchen_environment_file(environments_dir, kitchen_environment):
    """Create a kitchen environment file and return its path."""
    filepath = os.path.join(environments_dir, "kitchen.json")
    with open(filepath, "w") as f:
        json.dump(kitchen_environment, f, indent=2)
    return filepath


@pytest.fixture
def examples_dir():
    """Provide path to the rhylthyme-examples directory if it exists."""
    current_dir = Path(__file__).parent
    examples_path = current_dir / ".." / ".." / "rhylthyme-examples"
    if examples_path.exists():
        return str(examples_path.resolve())
    return None


@pytest.fixture
def schema_file():
    """Provide path to the schema file if it exists."""
    current_dir = Path(__file__).parent
    schema_path = (
        current_dir
        / ".."
        / ".."
        / "rhylthyme-spec"
        / "src"
        / "rhylthyme_spec"
        / "schemas"
        / "program_schema_0.2.0-alpha.json"
    )
    if schema_path.exists():
        return str(schema_path.resolve())
    return None
