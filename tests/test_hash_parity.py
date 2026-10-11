"""
programVersion parity: Python must reproduce the hashes in the shared
fixture ``rhylthyme-timeline/test/fixtures/hash-parity.json``, which the
JavaScript twin (``rhylthyme-timeline/tools/hash-program.js``) also
reproduces.
"""

import json
import os

import pytest

from rhylthyme.history.hash import canonical_json, program_version

HERE = os.path.dirname(os.path.abspath(__file__))
TIMELINE_FIXTURES = os.path.normpath(
    os.path.join(HERE, "..", "..", "rhylthyme-timeline", "test", "fixtures")
)


@pytest.mark.unit
def test_canonical_json_form():
    program = {"b": 1, "a": {"z": [1.0, 2.5, "é"], "y": True}, "c": None}
    assert canonical_json(program) == '{"a":{"y":true,"z":[1,2.5,"é"]},"b":1,"c":null}'


@pytest.mark.unit
def test_program_version_is_sha256_prefixed_and_key_order_independent():
    a = {"programId": "p", "name": "n", "tracks": []}
    b = {"tracks": [], "name": "n", "programId": "p"}
    assert program_version(a) == program_version(b)
    assert program_version(a).startswith("sha256:")
    assert len(program_version(a)) == len("sha256:") + 64
    assert program_version(a) != program_version({**a, "name": "m"})


@pytest.mark.unit
def test_integral_floats_hash_like_integers():
    assert program_version({"x": 1.0}) == program_version({"x": 1})
    assert program_version({"x": 1.5}) != program_version({"x": 1})


@pytest.mark.unit
def test_python_reproduces_hash_parity_fixture():
    fixture_path = os.path.join(TIMELINE_FIXTURES, "hash-parity.json")
    if not os.path.isfile(fixture_path):
        pytest.skip(
            "rhylthyme-timeline fixtures not checked out beside rhylthyme-cli-runner"
        )
    with open(fixture_path, encoding="utf-8") as fh:
        fixture = json.load(fh)
    programs_dir = os.path.join(TIMELINE_FIXTURES, "programs")
    assert len(fixture["programs"]) >= 30
    mismatches = []
    for name, expected in fixture["programs"].items():
        with open(os.path.join(programs_dir, name), encoding="utf-8") as fh:
            program = json.load(fh)
        actual = program_version(program)
        if actual != expected:
            mismatches.append(f"{name}: {actual} != {expected}")
    assert mismatches == []
