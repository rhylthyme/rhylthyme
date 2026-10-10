"""`rhylthyme render` hands its arguments to the rhylthyme-timeline package."""

import sys
import types

from click.testing import CliRunner

from rhylthyme.cli.main import cli


def test_arguments_pass_through_unchanged(monkeypatch):
    seen = {}
    module = types.ModuleType("rhylthyme_timeline")
    module.main = lambda argv: seen.setdefault("argv", argv) and 0
    monkeypatch.setitem(sys.modules, "rhylthyme_timeline", module)
    r = CliRunner().invoke(
        cli, ["render", "p.json", "-o", "p.png", "--style", "web", "--help"]
    )
    assert r.exit_code == 0, r.output
    assert seen["argv"] == ["p.json", "-o", "p.png", "--style", "web", "--help"]


def test_exit_code_is_the_renderers(monkeypatch):
    module = types.ModuleType("rhylthyme_timeline")
    module.main = lambda argv: 2
    monkeypatch.setitem(sys.modules, "rhylthyme_timeline", module)
    assert CliRunner().invoke(cli, ["render", "p.json"]).exit_code == 2


def test_without_the_package_the_message_says_how_to_get_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "rhylthyme_timeline", None)
    r = CliRunner().invoke(cli, ["render", "p.json"])
    assert r.exit_code != 0 and "rhylthyme[timeline]" in r.output
