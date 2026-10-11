"""Pull a program JSON object out of a model reply.

Replies arrive as prose with a fenced ```json block, as a bare JSON
object, or as JSON followed by commentary. Fenced blocks are tried first,
then every ``{`` in the text is offered to ``json.JSONDecoder.raw_decode``
and the first object that looks like a program (has a ``tracks`` list, or
wraps one under ``program``) wins.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, Optional

Program = Dict[str, Any]

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)
_DECODER = json.JSONDecoder()


def looks_like_program(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("tracks"), list)


def unwrap_program(value: Any) -> Optional[Program]:
    """Return the program inside ``value`` (direct or ``{"program": ...}``)."""
    if looks_like_program(value):
        return value
    if isinstance(value, dict) and looks_like_program(value.get("program")):
        return value["program"]
    return None


def _candidates(text: str) -> Iterator[Any]:
    for match in _FENCE.finditer(text):
        body = match.group(1).strip()
        try:
            yield json.loads(body)
        except ValueError:
            yield from _bare_objects(body)
    yield from _bare_objects(text)


def _bare_objects(text: str) -> Iterator[Any]:
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return
        try:
            value, end = _DECODER.raw_decode(text, start)
        except ValueError:
            position = start + 1
            continue
        yield value
        position = end


def extract_program(text: str) -> Optional[Program]:
    """First JSON object in ``text`` that parses as a program, else ``None``."""
    if not text:
        return None
    for candidate in _candidates(text):
        program = unwrap_program(candidate)
        if program is not None:
            return program
    return None
