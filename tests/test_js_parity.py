"""
Parity with the JavaScript validator in @rhylthyme/timeline/schedule, the
one mcp.rhylthyme.com runs: every program in rhylthyme-examples must get
the same verdict, valid or not, from both, and every invalid/<CODE>.json
its declared code from both. The JS side runs under Node from a
rhylthyme-timeline checkout beside this repository (CI checks one out
there), or from $RHYLTHYME_TIMELINE; the test skips without Node or the
checkout. Its twin is rhylthyme-timeline's test/parity.test.js.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from rhylthyme_spec import get_program_schema_path

from rhylthyme import validate_program_structured

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = Path(os.environ.get("RHYLTHYME_EXAMPLES", ROOT / "rhylthyme-examples"))
TIMELINE = Path(os.environ.get("RHYLTHYME_TIMELINE", ROOT / "rhylthyme-timeline"))
SCHEDULE = TIMELINE / "src" / "schedule" / "index.js"

# Known, deliberate differences, keyed by path under rhylthyme-examples.
# Keep in step with KNOWN_DIFFERENCES in rhylthyme-timeline's test/parity.test.js.
KNOWN_DIFFERENCES = {
    # JS: a task with no resourceConstraint is an error unless the program
    # names an environment or actors. Python: only with --strict.
    "programs/comprehensive_manual_demo.json": "task_not_constrained policy",
}

# Codes only the JS validator emits.
JS_ONLY_CODES = {"I_IMPLICIT_BARRIER"}

JS = """
const fs = require("fs");
const { validateProgram } = require(process.argv[1]);
const [examples, ...files] = process.argv.slice(2);
const out = {};
for (const rel of files) {
  const v = validateProgram(JSON.parse(fs.readFileSync(examples + "/" + rel, "utf8")));
  out[rel] = { valid: v.valid, codes: [...v.errors, ...v.warnings, ...v.info].map((f) => f.code) };
}
process.stdout.write(JSON.stringify(out));
"""


def _program_files():
    files = sorted(f"programs/{p.name}" for p in (EXAMPLES / "programs").glob("*.json"))
    files += sorted(f"invalid/{p.name}" for p in (EXAMPLES / "invalid").glob("*.json"))
    files += sorted(
        f"gold/{p.parent.name}/program.json"
        for p in (EXAMPLES / "gold").glob("*/program.json")
    )
    return files


def _schema_for(program, cache={}):
    version = (
        "0.3.0-alpha"
        if str(program.get("schemaVersion", "")).startswith("0.3")
        else "0.2.0-alpha"
    )
    if version not in cache:
        with open(get_program_schema_path(version)) as fh:
            cache[version] = json.load(fh)
    return cache[version]


def _load(rel):
    with open(EXAMPLES / rel) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def verdicts():
    node = shutil.which("node")
    if not node or not SCHEDULE.exists() or not (EXAMPLES / "programs").exists():
        reason = f"needs node, {SCHEDULE} and {EXAMPLES}/programs"
        # CI sets this so a broken checkout fails instead of skipping.
        if os.environ.get("RHYLTHYME_PARITY_REQUIRED"):
            pytest.fail(reason)
        pytest.skip(reason)
    files = _program_files()
    out = subprocess.run(
        [node, "-e", JS, str(SCHEDULE), str(EXAMPLES), *files],
        capture_output=True,
        text=True,
        check=True,
    )
    js = json.loads(out.stdout)
    py = {}
    for rel in files:
        program = _load(rel)
        r = validate_program_structured(program, _schema_for(program))
        py[rel] = {"valid": r["is_valid"], "codes": [f["code"] for f in r["findings"]]}
    return files, js, py


def test_js_and_python_agree_on_every_example(verdicts):
    files, js, py = verdicts
    assert len(files) > 50
    disagreements = [
        f"{rel}: JS {js[rel]['valid']} {js[rel]['codes']}, Python {py[rel]['valid']} {py[rel]['codes']}"
        for rel in files
        if js[rel]["valid"] != py[rel]["valid"] and rel not in KNOWN_DIFFERENCES
    ]
    assert disagreements == []


def test_known_differences_are_still_differences(verdicts):
    _, js, py = verdicts
    for rel in KNOWN_DIFFERENCES:
        if rel in js:
            assert (
                js[rel]["valid"] != py[rel]["valid"]
            ), f"{rel} now agrees: remove it from KNOWN_DIFFERENCES"


def test_both_emit_each_invalid_files_declared_code(verdicts):
    files, js, py = verdicts
    for rel in (f for f in files if f.startswith("invalid/")):
        code = _load(rel)["metadata"]["expectedFinding"]["code"]
        assert code in js[rel]["codes"], f"{rel}: JS emitted {js[rel]['codes']}"
        if code not in JS_ONLY_CODES:
            assert code in py[rel]["codes"], f"{rel}: Python emitted {py[rel]['codes']}"
