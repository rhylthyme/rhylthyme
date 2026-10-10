"""
The ``rhylthyme`` command.

A thin layer of click commands over the :mod:`rhylthyme` API. Everything that
touches the terminal (the curses runner) or the network (sign-in, the bridge,
remote generation, model-backed evaluation) lives under this package, so
``import rhylthyme`` stays free of both.
"""

from .main import cli, main

__all__ = ["cli", "main"]
