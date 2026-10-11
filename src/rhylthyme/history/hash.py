"""
Canonical program hashing.

``programVersion`` in a run record identifies the exact program JSON that
was executed. It is ``"sha256:<hex>"`` of the canonical JSON serialisation
of the program:

* object keys sorted by Unicode code point,
* no whitespace (``separators=(",", ":")``),
* UTF-8 with non-ASCII characters left unescaped (``ensure_ascii=False``),
* integral numbers written without a fractional part (``1.0`` -> ``1``),
  so that Python and JavaScript agree.

The JavaScript twin lives in ``rhylthyme-timeline/tools/hash-program.js``;
the parity corpus is ``rhylthyme-timeline/test/fixtures/hash-parity.json``.
"""

import hashlib
import json
import os
from typing import Any, Dict


def _canonicalize(value: Any) -> Any:
    """Return ``value`` with integral floats replaced by ints, recursively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


def canonical_json(program: Dict[str, Any]) -> str:
    """Serialise ``program`` in the canonical form described in the module docstring."""
    return json.dumps(
        _canonicalize(program),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def program_version(program: Dict[str, Any]) -> str:
    """Return ``"sha256:<hex>"`` of the canonical JSON of ``program``."""
    digest = hashlib.sha256(canonical_json(program).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_program_for_hash(path: str) -> Dict[str, Any]:
    """
    Load a program file exactly as authored (JSON or YAML) without any of the
    normalisation the validator applies, so the hash matches other tools that
    parse the same file.
    """
    _, ext = os.path.splitext(path)
    with open(path, "r", encoding="utf-8") as fh:
        if ext.lower() in (".yaml", ".yml"):
            import yaml

            return yaml.safe_load(fh)
        return json.load(fh)


__all__ = ["canonical_json", "program_version", "load_program_for_hash"]
