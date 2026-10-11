"""
The rhylthyme-cli-runner compatibility package (``compat/``): old
``rhylthyme_cli_runner`` imports keep working, with a DeprecationWarning.
"""

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

COMPAT_SRC = os.path.join(os.path.dirname(__file__), "..", "compat", "src")


def _run(code):
    env = {**os.environ, "PYTHONPATH": os.path.abspath(COMPAT_SRC)}
    out = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    return out


def test_importing_the_old_name_warns():
    out = _run("import rhylthyme_cli_runner")
    assert out.returncode != 0
    assert "renamed to rhylthyme" in out.stderr


@pytest.mark.parametrize(
    "old, new",
    [
        ("validate_program", "rhylthyme.validate_program"),
        ("history.store", "rhylthyme.history.store"),
        ("cli", "rhylthyme.cli.main"),
        ("remote.auth", "rhylthyme.cli.remote.auth"),
        ("bridge", "rhylthyme.cli.bridge"),
        ("history.report_cli", "rhylthyme.cli.history.report_cli"),
        ("eval.metrics", "rhylthyme.eval.metrics"),
        ("eval.llm", "rhylthyme.cli.eval.llm"),
    ],
)
def test_old_modules_are_the_new_modules(old, new):
    code = (
        "import warnings, importlib\n"
        "warnings.simplefilter('ignore')\n"
        f"a = importlib.import_module('rhylthyme_cli_runner.{old}')\n"
        f"b = importlib.import_module('{new}')\n"
        "assert a is b, (a, b)\n"
    )
    out = _run(code)
    assert out.returncode == 0, out.stderr


def test_split_modules_keep_their_old_names():
    code = (
        "import warnings\n"
        "warnings.simplefilter('ignore')\n"
        "from rhylthyme_cli_runner.program_runner import ProgramRunner, run_program\n"
        "from rhylthyme_cli_runner.instruments import open_instruments, start_bridge\n"
        "from rhylthyme_cli_runner import cli, main, validate_program_file\n"
        "import rhylthyme.cli.tui as tui, rhylthyme.cli.bridge as bridge\n"
        "assert run_program is tui.run_program and start_bridge is bridge.start_bridge\n"
    )
    out = _run(code)
    assert out.returncode == 0, out.stderr
