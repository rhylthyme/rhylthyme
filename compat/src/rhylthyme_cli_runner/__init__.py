"""
Deprecated: ``rhylthyme_cli_runner`` is now ``rhylthyme``.

Every ``rhylthyme_cli_runner.X`` import resolves to the module that replaced
it (the very same module object, so patching one patches the other). Two old
modules were split and are thin re-exports instead: ``program_runner`` (the
curses front end moved to ``rhylthyme.cli.tui``) and ``instruments``
(``start_bridge`` moved to ``rhylthyme.cli.bridge``).
"""

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

warnings.warn(
    "rhylthyme_cli_runner has been renamed to rhylthyme; "
    "import rhylthyme (the API) or rhylthyme.cli (the commands) instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Old module -> new module; the longest matching prefix wins.
MOVED = {
    "cli": "rhylthyme.cli.main",
    "remote": "rhylthyme.cli.remote",
    "bridge": "rhylthyme.cli.bridge",
    "bridge_serve": "rhylthyme.cli.bridge_serve",
    "import_cli": "rhylthyme.cli.import_cli",
    "render_cli": "rhylthyme.cli.render_cli",
    "history.cli": "rhylthyme.cli.history.cli",
    "history.calibrate_cli": "rhylthyme.cli.history.calibrate_cli",
    "history.evaluate_cli": "rhylthyme.cli.history.evaluate_cli",
    "history.report_cli": "rhylthyme.cli.history.report_cli",
    "history.render": "rhylthyme.cli.history.render",
    "eval.cli": "rhylthyme.cli.eval.cli",
    "eval.harness": "rhylthyme.cli.eval.harness",
    "eval.llm": "rhylthyme.cli.eval.llm",
    "eval.patterns": "rhylthyme.cli.eval.patterns",
}

# Shipped as real files in this package (the old module was split in two).
_SPLIT = {"program_runner", "instruments"}


def new_name(old: str) -> str:
    """The module that replaced ``rhylthyme_cli_runner.<old>``."""
    parts = old.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in MOVED:
            return ".".join([MOVED[prefix]] + parts[i:])
    return "rhylthyme." + old


class _Alias(importlib.abc.Loader):
    def __init__(self, target: str):
        self.target = target

    def create_module(self, spec):
        return importlib.import_module(self.target)

    def exec_module(self, module):
        pass


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith(__name__ + "."):
            return None
        old = fullname[len(__name__) + 1 :]
        if old in _SPLIT:
            return None
        target = new_name(old)
        if importlib.util.find_spec(target) is None:
            return None
        return importlib.util.spec_from_loader(fullname, _Alias(target))


sys.meta_path.insert(0, _Finder())

__version__ = "0.5.0"


def __getattr__(name):
    # The old package exported the click group and entry point at the top.
    if name in ("cli", "main"):
        return getattr(importlib.import_module("rhylthyme.cli.main"), name)
    return getattr(importlib.import_module("rhylthyme"), name)
