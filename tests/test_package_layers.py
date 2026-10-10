"""
The package has two layers: ``rhylthyme`` (the importable API, no network and
no terminal) and ``rhylthyme.cli`` (the commands). These tests hold the line,
and cover the behaviour folded in from the retired root ``rhylthyme`` package.
"""

import json
import subprocess
import sys

import pytest

from rhylthyme import (
    calculate_step_start_time,
    perform_additional_validations,
    validate_track_step_overlaps,
)
from rhylthyme.environment_loader import EnvironmentLoader

pytestmark = pytest.mark.unit

# Modules that mean the terminal or the network
FORBIDDEN = [
    "curses",
    "colorama",
    "click",
    "webbrowser",
    "urllib.request",
    "http.client",
    "anthropic",
    "rhylthyme.cli",
]

CORE_MODULES = [
    "rhylthyme",
    "rhylthyme.alert_checks",
    "rhylthyme.auto_planner",
    "rhylthyme.drivers",
    "rhylthyme.environment_icons",
    "rhylthyme.environment_loader",
    "rhylthyme.environment_schemas",
    "rhylthyme.eval",
    "rhylthyme.eval.compare",
    "rhylthyme.expand_replicates",
    "rhylthyme.fold_view",
    "rhylthyme.history",
    "rhylthyme.instance_checks",
    "rhylthyme.instrument_catalog",
    "rhylthyme.instruments",
    "rhylthyme.program_planner",
    "rhylthyme.program_runner",
    "rhylthyme.validate_program",
]


def test_core_imports_pull_in_no_terminal_or_network_module():
    code = (
        "import importlib, json, sys\n"
        f"for m in {CORE_MODULES!r}:\n"
        "    importlib.import_module(m)\n"
        f"print(json.dumps([m for m in {FORBIDDEN!r} if m in sys.modules]))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == []


def test_the_command_is_the_cli_layer():
    from rhylthyme.cli import cli, main
    from rhylthyme.cli.main import cli as cli_from_main

    assert cli is cli_from_main and callable(main)


def _step(step_id, trigger, seconds=60, task=None):
    step = {
        "stepId": step_id,
        "name": step_id,
        "startTrigger": trigger,
        "duration": {"type": "fixed", "seconds": seconds},
    }
    if task:
        step["task"] = task
    return step


def _choice_program(choice_id):
    return {
        "programId": "p",
        "name": "p",
        "tracks": [
            {
                "trackId": "t",
                "name": "t",
                "steps": [
                    {
                        **_step("pick", {"type": "programStart"}),
                        "choice": {
                            "options": [
                                {"choiceId": "rice", "label": "Rice"},
                                {"choiceId": "noodles", "label": "Noodles"},
                            ]
                        },
                    },
                    _step(
                        "cook",
                        {"type": "afterStep", "stepId": "pick", "choiceId": choice_id},
                    ),
                ],
            }
        ],
    }


def test_a_choice_id_must_name_an_option_of_the_choice_step():
    assert not [
        e
        for e in perform_additional_validations(_choice_program("rice"))
        if "choiceId" in e
    ]
    errors = perform_additional_validations(_choice_program("pasta"))
    assert any("only has options: noodles, rice" in e for e in errors)


def test_a_choice_id_on_a_step_without_a_choice_is_reported():
    program = _choice_program("rice")
    del program["tracks"][0]["steps"][0]["choice"]
    errors = perform_additional_validations(program)
    assert any("which has no choice property" in e for e in errors)


def test_steps_on_different_choice_branches_do_not_overlap():
    pick = {
        **_step("pick", {"type": "programStart"}, seconds=10),
        "choice": {"options": [{"choiceId": "a"}, {"choiceId": "b"}]},
    }
    a = _step("a", {"type": "afterStep", "stepId": "pick", "choiceId": "a"})
    b = _step("b", {"type": "afterStep", "stepId": "pick", "choiceId": "b"})
    program = {"tracks": [{"trackId": "t", "name": "T", "steps": [pick, a, b]}]}
    assert validate_track_step_overlaps(program) == []


def test_a_manual_step_offset_may_be_a_time_string():
    first = _step("first", {"type": "programStart"}, seconds=60)
    second = _step("second", {"type": "manual", "offsetSeconds": "5m"})
    program = {"tracks": [{"trackId": "t", "steps": [first, second]}]}
    assert calculate_step_start_time(second, [first, second], program) == 360


def _env(tmp_path, env_id, env_type, **extra):
    data = {
        "environmentId": env_id,
        "name": env_id,
        "type": env_type,
        "resourceConstraints": [{"task": "oven", "maxConcurrent": 1}],
        **extra,
    }
    (tmp_path / f"{env_id}.json").write_text(json.dumps(data))


def test_an_environment_marked_default_wins_for_its_type(tmp_path):
    _env(tmp_path, "a-standard-kitchen", "kitchen")
    _env(tmp_path, "b-home-kitchen", "kitchen", isDefault=True)
    loader = EnvironmentLoader(str(tmp_path))
    assert loader.get_default_environment_for_type("kitchen") == "b-home-kitchen"
    assert loader.load_environment_by_type("kitchen")["environmentId"] == (
        "b-home-kitchen"
    )
    assert loader.load_environment_by_type("laboratory") is None
    listed = {e["id"]: e for e in loader.list_environments()}
    assert listed["b-home-kitchen"]["isDefault"] is True
    assert listed["a-standard-kitchen"]["constraintCount"] == 1


def test_the_environments_path_can_come_from_the_environment(tmp_path, monkeypatch):
    _env(tmp_path, "lab", "laboratory")
    monkeypatch.setenv("RHYLTHYME_ENVIRONMENTS_PATH", str(tmp_path))
    assert EnvironmentLoader().environments_dir == tmp_path
