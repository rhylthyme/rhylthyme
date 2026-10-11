"""
Render a run record as a planned-vs-actual SVG.

The timing engine and the Gantt renderer live in JavaScript
(``rhylthyme-timeline/src/index.js``, mirrored byte-for-byte into
``rhylthyme-server/static/js/timeline-render.js`` and
``rhylthyme-mcp/static/js/timeline-render.js``), and the overlay is one
option on ``renderTimelineSvg``:

    renderTimelineSvg(program, { run: record })

so ``rhylthyme runs show --svg`` shells Node rather than reimplementing the
drawing. Both inputs are handed over as temporary files, which keeps
programs with quotes, newlines or non-ASCII out of the command line.

Locating the renderer, in order:

1. ``$RHYLTHYME_TIMELINE_JS`` (a path to index.js or a mirror of it),
2. a checkout beside this package: ``rhylthyme-timeline/src/index.js``, then
   the server's or the MCP server's static copy, searching upwards from this
   file for the monorepo root,
3. whatever ``require.resolve('@rhylthyme/timeline')`` finds, so an npm
   install of the package works too.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

RENDERER_ENV = "RHYLTHYME_TIMELINE_JS"
NODE_ENV = "RHYLTHYME_NODE"

# Relative to the monorepo root, in preference order.
_RENDERER_PATHS = (
    os.path.join("rhylthyme-timeline", "src", "index.js"),
    os.path.join("rhylthyme-server", "static", "js", "timeline-render.js"),
    os.path.join("rhylthyme-mcp", "static", "js", "timeline-render.js"),
)

# argv[1..]: renderer, program.json, record.json, out.svg, width
_SCRIPT = """
'use strict';
const fs = require('fs');
const R = require(process.argv[1]);
const program = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const record = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const opts = { run: record };
const width = Number(process.argv[5]);
if (isFinite(width) && width > 0) opts.width = width;
const svg = R.renderTimelineSvg(program, opts);
if (!svg) { process.stderr.write('renderTimelineSvg returned an empty document\\n'); process.exit(3); }
fs.writeFileSync(process.argv[4], svg);
"""


class RendererNotFound(RuntimeError):
    """Neither Node nor the JavaScript renderer could be located."""


def find_node() -> Optional[str]:
    """Path to the Node executable, or ``None``."""
    explicit = os.environ.get(NODE_ENV)
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which("node")


def find_renderer() -> Optional[Path]:
    """Path to the JavaScript timeline engine/renderer, or ``None``."""
    explicit = os.environ.get(RENDERER_ENV)
    if explicit:
        candidate = Path(explicit).expanduser()
        # Absolute: `node -e` has no script directory to resolve against.
        return candidate.resolve() if candidate.is_file() else None
    # The timeline extra (pip install 'rhylthyme[timeline]') carries the renderer.
    try:
        from rhylthyme_timeline import renderer_path

        packaged = renderer_path()
        if packaged.is_file():
            return packaged
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for parent in here.parents:
        for relative in _RENDERER_PATHS:
            candidate = parent / relative
            if candidate.is_file():
                return candidate
    node = find_node()
    if node:
        try:
            found = subprocess.run(
                [node, "-p", "require.resolve('@rhylthyme/timeline')"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        path = found.stdout.strip()
        if found.returncode == 0 and path and os.path.isfile(path):
            return Path(path).resolve()
    return None


def missing_renderer_message() -> str:
    """Why an SVG cannot be produced, and what to do about it."""
    problems: List[str] = []
    if find_node() is None:
        problems.append(
            "Node.js was not found on PATH (set $%s to the node executable)" % NODE_ENV
        )
    if find_renderer() is None:
        problems.append(
            "the JavaScript timeline renderer was not found (set $%s to "
            "rhylthyme-timeline/src/index.js or a byte-identical mirror such as "
            "rhylthyme-server/static/js/timeline-render.js)" % RENDERER_ENV
        )
    return "Cannot render SVG: " + "; ".join(problems or ["unknown reason"])


def render_run_svg(
    program: Dict[str, Any],
    record: Dict[str, Any],
    out_path: str,
    width: Optional[int] = None,
) -> Path:
    """
    Write the planned-vs-actual SVG for ``record`` against ``program``.

    Raises ``RendererNotFound`` when Node or the renderer is missing and
    ``RuntimeError`` when Node fails.
    """
    node = find_node()
    renderer = find_renderer()
    if node is None or renderer is None:
        raise RendererNotFound(missing_renderer_message())

    target = Path(out_path).expanduser()
    if target.parent and str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rhylthyme-run-svg-") as tmp:
        program_file = os.path.join(tmp, "program.json")
        record_file = os.path.join(tmp, "run.json")
        for path, payload in ((program_file, program), (record_file, record)):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        result = subprocess.run(
            [
                node,
                "-e",
                _SCRIPT,
                str(renderer),
                program_file,
                record_file,
                str(target),
                str(width or ""),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"node exited {result.returncode} while rendering the run SVG: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return target


__all__ = [
    "NODE_ENV",
    "RENDERER_ENV",
    "RendererNotFound",
    "find_node",
    "find_renderer",
    "missing_renderer_message",
    "render_run_svg",
]
