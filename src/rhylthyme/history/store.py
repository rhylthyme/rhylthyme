"""
On-disk store for run records.

Layout: ``<runs-dir>/<programId>/<runId>.json``. The runs directory is, in
order of precedence, the explicit argument (``--runs-dir``), the
``RHYLTHYME_RUNS_DIR`` environment variable, or ``~/.rhylthyme/runs``.
"""

import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_RUNS_DIR = os.path.join("~", ".rhylthyme", "runs")
RUNS_DIR_ENV = "RHYLTHYME_RUNS_DIR"
PROGRAMS_DIR_ENV = "RHYLTHYME_PROGRAMS_DIR"

# Directories, relative to a monorepo/source checkout, that hold programs a
# recorded run may have executed. Searched only when $RHYLTHYME_PROGRAMS_DIR
# is unset and the run directory has no sidecar copy.
_PROGRAM_SEARCH_PATHS = (
    os.path.join("rhylthyme-examples", "programs"),
    os.path.join("rhylthyme-timeline", "test", "fixtures", "programs"),
    os.path.join("rhylthyme-server", "static", "examples"),
)


def resolve_runs_dir(explicit: Optional[str] = None) -> Path:
    """Return the runs directory to use (not created)."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(RUNS_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_RUNS_DIR).expanduser()


def _safe_segment(value: str) -> str:
    """Make ``value`` safe to use as a single path segment."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value) or "_"


def run_path(runs_dir: Path, program_id: str, run_id: str) -> Path:
    """Path a record with ``program_id``/``run_id`` is written to."""
    return runs_dir / _safe_segment(program_id) / f"{_safe_segment(run_id)}.json"


def write_run(record: Dict[str, Any], runs_dir: Optional[str] = None) -> Path:
    """Write ``record`` under the runs directory and return its path."""
    target = run_path(resolve_runs_dir(runs_dir), record["programId"], record["runId"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, target)
    return target


def load_run(path: str) -> Dict[str, Any]:
    """Load one run record from ``path``."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_run_paths(
    runs_dir: Optional[str] = None, program_id: Optional[str] = None
) -> Iterator[Path]:
    """Yield record paths, optionally restricted to one program."""
    base = resolve_runs_dir(runs_dir)
    if not base.is_dir():
        return
    if program_id is not None:
        pattern = str(base / _safe_segment(program_id) / "*.json")
    else:
        pattern = str(base / "*" / "*.json")
    for p in sorted(glob.glob(pattern)):
        yield Path(p)


def list_runs(
    runs_dir: Optional[str] = None, program_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Load every record (optionally for one program), newest first.

    Records that fail to parse are skipped. Each returned dict is the record
    itself with an extra ``_path`` key.
    """
    records = []
    for path in iter_run_paths(runs_dir, program_id):
        try:
            record = load_run(str(path))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or "runId" not in record:
            continue
        record["_path"] = str(path)
        records.append(record)
    records.sort(
        key=lambda r: (r.get("startedAt", ""), r.get("runId", "")), reverse=True
    )
    return records


def find_run(ref: str, runs_dir: Optional[str] = None) -> Optional[Path]:
    """
    Resolve ``ref`` — a path to a record file or a runId — to a file path.

    A runId is searched for under every program directory; a unique prefix of a
    runId is accepted too.
    """
    candidate = Path(ref).expanduser()
    if candidate.is_file():
        return candidate
    matches = [
        p
        for p in iter_run_paths(runs_dir)
        if p.stem == _safe_segment(ref) or p.stem.startswith(_safe_segment(ref))
    ]
    exact = [p for p in matches if p.stem == _safe_segment(ref)]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    return None


def program_search_dirs(
    runs_dir: Optional[str] = None, program_id: str = ""
) -> List[Path]:
    """
    Directories to look in for the program a run executed, in order.

    A sidecar beside the run records first, then ``$RHYLTHYME_PROGRAMS_DIR``,
    then the example and fixture directories of a source checkout found by
    walking up from this file.
    """
    dirs: List[Path] = []
    base = resolve_runs_dir(runs_dir)
    if program_id:
        dirs.append(base / _safe_segment(program_id))
    env = os.environ.get(PROGRAMS_DIR_ENV)
    if env:
        dirs.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    for parent in here.parents:
        for relative in _PROGRAM_SEARCH_PATHS:
            candidate = parent / relative
            if candidate.is_dir():
                dirs.append(candidate)
    seen, unique = set(), []
    for directory in dirs:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def find_program_for_record(
    record: Dict[str, Any], runs_dir: Optional[str] = None
) -> Optional[Path]:
    """
    Locate the program JSON a run record refers to, or ``None``.

    Candidates are files whose ``programId`` matches the record's; a file
    whose canonical hash equals the record's ``programVersion`` is preferred,
    so an edited program is never silently substituted for the one that ran.
    """
    from .hash import load_program_for_hash, program_version

    program_id = record.get("programId")
    if not program_id:
        return None
    wanted = record.get("programVersion")
    fallback: Optional[Path] = None
    for directory in program_search_dirs(runs_dir, str(program_id)):
        for path in sorted(directory.glob("*.json")) + sorted(directory.glob("*.y*ml")):
            try:
                program = load_program_for_hash(str(path))
            except (OSError, ValueError, ImportError):
                continue
            # A program has tracks; a run record in the same directory has a
            # matching programId but no tracks, and must not be mistaken for one.
            if (
                not isinstance(program, dict)
                or program.get("programId") != program_id
                or not isinstance(program.get("tracks"), list)
            ):
                continue
            if wanted and program_version(program) == wanted:
                return path
            if fallback is None:
                fallback = path
    return fallback


def validate_run(record: Dict[str, Any]) -> List[str]:
    """Validate ``record`` against the runs schema; return error messages."""
    import jsonschema
    from rhylthyme_spec import get_runs_schema_path

    with open(get_runs_schema_path(), "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(record), key=lambda e: list(e.path))
    ]


__all__ = [
    "DEFAULT_RUNS_DIR",
    "RUNS_DIR_ENV",
    "PROGRAMS_DIR_ENV",
    "find_program_for_record",
    "program_search_dirs",
    "resolve_runs_dir",
    "run_path",
    "write_run",
    "load_run",
    "iter_run_paths",
    "list_runs",
    "find_run",
    "validate_run",
]
