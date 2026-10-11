"""
Structured validator findings (schema 0.3.0-alpha `instances` codes).

Covers the ``Finding`` type shared by copy in ``instance_checks.py``, the
``validate_instances`` checks themselves, the cli-runner's
``validate_program_file_structured`` result shape, and that the legacy
string outputs of the pre-existing checks are unchanged.
"""

import json
from pathlib import Path

import pytest

from rhylthyme.instance_checks import Finding, validate_instances
from rhylthyme.validate_program import (
    perform_additional_validations,
    perform_additional_validations_structured,
    validate_program_file_structured,
)

pytestmark = pytest.mark.unit

MONOREPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_030 = (
    MONOREPO_ROOT
    / "rhylthyme-spec"
    / "src"
    / "rhylthyme_spec"
    / "schemas"
    / "program_schema_0.3.0-alpha.json"
)
COOKIES = MONOREPO_ROOT / "rhylthyme-examples" / "programs" / "cookies_three_trays.json"

requires_examples = pytest.mark.skipif(
    not COOKIES.exists(), reason="rhylthyme-examples checkout not found"
)


def fixed(step_id, seconds, trigger, **extra):
    step = {
        "stepId": step_id,
        "name": step_id.upper(),
        "task": "prep",
        "duration": {"type": "fixed", "seconds": seconds},
        "startTrigger": trigger,
    }
    step.update(extra)
    return step


def track(track_id, *steps):
    return {"trackId": track_id, "name": track_id.upper(), "steps": list(steps)}


def program(*tracks):
    return {
        "schemaVersion": "0.3.0-alpha",
        "programId": "p",
        "name": "P",
        "tracks": list(tracks),
        "resourceConstraints": [{"task": "prep", "maxConcurrent": 9}],
    }


def codes(findings):
    return [f.code for f in findings]


# --------------------------------------------------------------------------
# Finding type
# --------------------------------------------------------------------------


def test_finding_renders_code_message_and_fix():
    f = Finding(code="E_X", message="Bad thing.", where="step:a", fix="do this")
    assert str(f) == "[E_X] Bad thing. (fix: do this)"
    assert f.severity == "error"
    assert f.to_dict() == {
        "code": "E_X",
        "message": "Bad thing.",
        "where": "step:a",
        "fix": "do this",
        "severity": "error",
    }
    assert str(Finding(code="W_Y", message="Hmm.", severity="warning")) == "[W_Y] Hmm."


def test_legacy_finding_renders_bare_message():
    f = Finding.legacy_error(
        "duplicate_step_id", "Duplicate step ID 'a' found 2 times", "step:a"
    )
    assert str(f) == "Duplicate step ID 'a' found 2 times"
    assert f.code == "duplicate_step_id" and f.where == "step:a" and f.fix is None
    assert f.to_dict()["severity"] == "error"


# --------------------------------------------------------------------------
# validate_instances
# --------------------------------------------------------------------------


@requires_examples
def test_clean_program_and_expanded_program_yield_nothing():
    assert (
        validate_instances(program(track("t", fixed("a", 1, {"type": "programStart"}))))
        == []
    )
    with open(COOKIES) as fh:
        cookies = json.load(fh)
    assert validate_instances(cookies) == []
    from rhylthyme.expand_replicates import expand_replicates

    assert validate_instances(expand_replicates(cookies)) == []


@pytest.mark.parametrize("value", ["each", "all", "any"])
def test_instances_on_single(value):
    p = program(
        track(
            "t",
            fixed("m", 10, {"type": "programStart"}),
            fixed("z", 20, {"type": "afterStep", "stepId": "m", "instances": value}),
        )
    )
    (f,) = validate_instances(p)
    assert f.code == "E_INSTANCES_ON_SINGLE" and f.severity == "error"
    assert f.where == "step:z"
    assert f.fix == "remove `instances`, or add `replicates` to `m`"
    assert "z" in f.message and "m" in f.message


def test_instances_on_dangling_reference_is_left_to_dangling_check():
    p = program(
        track(
            "t",
            fixed(
                "z", 20, {"type": "afterStep", "stepId": "ghost", "instances": "each"}
            ),
        )
    )
    assert validate_instances(p) == []
    assert any("ghost" in e for e in perform_additional_validations(p))


def test_each_with_replicates():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 3, "mode": "serial"},
            ),
            fixed(
                "s",
                50,
                {"type": "afterStep", "stepId": "x", "instances": "each"},
                replicates={"count": 2},
            ),
        )
    )
    (f,) = validate_instances(p)
    assert f.code == "E_EACH_WITH_REPLICATES" and f.where == "step:s"
    assert f.fix == "drop `replicates` on `s`; it inherits `3` instances from `x`"


def test_each_count_mismatch_and_equal_counts():
    def build(ny):
        return program(
            track(
                "a",
                fixed(
                    "x",
                    100,
                    {"type": "programStart"},
                    replicates={"count": 2, "mode": "parallel"},
                ),
            ),
            track(
                "b",
                fixed(
                    "y",
                    80,
                    {"type": "programStart"},
                    replicates={"count": ny, "mode": "parallel"},
                ),
            ),
            track(
                "c",
                fixed(
                    "s",
                    10,
                    {
                        "logic": "all",
                        "triggers": [
                            {"type": "afterStep", "stepId": "x", "instances": "each"},
                            {"type": "afterStep", "stepId": "y", "instances": "each"},
                        ],
                    },
                ),
            ),
        )

    (f,) = validate_instances(build(3))  # the other root is not a "later step"
    assert f.code == "E_EACH_COUNT_MISMATCH" and f.where == "step:s"
    assert f.fix == "both upstream steps must have `count: 2`"
    assert validate_instances(build(2)) == []


def test_each_is_transitive_for_instances_on_single():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
            fixed("u", 20, {"type": "afterStep", "stepId": "s", "instances": "each"}),
            fixed("z", 20, {"type": "afterStep", "stepId": "u", "instances": "all"}),
        )
    )
    assert validate_instances(p) == []


def test_unbarriered_chain_warning():
    chain = [
        fixed(
            "x",
            100,
            {"type": "programStart"},
            replicates={"count": 2, "mode": "parallel"},
        ),
        fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
    ]
    later = track(
        "u", fixed("w", 5, {"type": "programStartOffset", "offsetSeconds": 500})
    )
    (f,) = validate_instances(program(track("t", *chain), later))
    assert (
        f.code == "W_UNBARRIERED_CHAIN"
        and f.severity == "warning"
        and f.where == "step:x"
    )
    assert (
        f.fix
        == 'add a step with `instances:"all"` if later work should wait for every instance'
    )
    assert "s" in f.message and "w" in f.message
    # Nothing later: no warning. A barrier (explicit or the implicit join): no warning.
    assert validate_instances(program(track("t", *chain))) == []
    barrier = fixed("z", 5, {"type": "afterStep", "stepId": "s", "instances": "all"})
    assert validate_instances(program(track("t", *chain, barrier), later)) == []
    implicit = fixed("z", 5, {"type": "afterStep", "stepId": "s"})
    assert validate_instances(program(track("t", *chain, implicit), later)) == []
    # "any" is not a barrier.
    any_ = fixed("z", 5, {"type": "afterStep", "stepId": "s", "instances": "any"})
    assert codes(validate_instances(program(track("t", *chain, any_), later))) == [
        "W_UNBARRIERED_CHAIN"
    ]


# --------------------------------------------------------------------------
# perform_additional_validations: legacy strings unchanged
# --------------------------------------------------------------------------


def test_legacy_strings_unchanged_and_coded():
    p = program(
        track(
            "t",
            fixed("a", 10, {"type": "programStart"}),
            fixed("a", 10, {"type": "afterStep", "stepId": "nope"}),
        )
    )
    legacy = perform_additional_validations(p)
    assert "Duplicate step ID 'a' found 2 times" in legacy
    assert "Referenced step ID 'nope' does not exist in any track" in legacy
    structured = perform_additional_validations_structured(p)
    assert [str(f) for f in structured] == legacy
    by_code = {f.code: f for f in structured}
    assert by_code["duplicate_step_id"].where == "step:a"
    assert by_code["dangling_step_ref"].where == "step:nope"
    assert all(f.severity == "error" for f in structured)


def test_warnings_are_excluded_from_legacy_error_list():
    p = program(
        track(
            "t",
            fixed(
                "x",
                100,
                {"type": "programStart"},
                replicates={"count": 2, "mode": "parallel"},
            ),
            fixed("s", 50, {"type": "afterStep", "stepId": "x", "instances": "each"}),
        ),
        track("u", fixed("w", 5, {"type": "programStartOffset", "offsetSeconds": 500})),
    )
    assert perform_additional_validations(p) == []
    assert codes(perform_additional_validations_structured(p)) == [
        "W_UNBARRIERED_CHAIN"
    ]


def test_strict_mode_still_reports_unconstrained_tasks():
    p = program(track("t", fixed("a", 10, {"type": "programStart"}, task="mystery")))
    assert perform_additional_validations(p) == []
    assert perform_additional_validations(p, strict=True) == [
        "Task 'mystery' is used in steps but not defined in resourceConstraints"
    ]


# --------------------------------------------------------------------------
# validate_program_file_structured
# --------------------------------------------------------------------------


@requires_examples
def test_structured_file_result_shape(tmp_path):
    with open(COOKIES) as fh:
        cookies = json.load(fh)
    result = validate_program_file_structured(str(COOKIES), str(SCHEMA_030))
    assert result["is_valid"] is True
    assert result["findings"] == [] and result["warnings"] == []
    assert set(result) >= {
        "is_valid",
        "schema_errors",
        "logic_errors",
        "warnings",
        "findings",
        "summary",
    }

    cookies["tracks"][0]["steps"][2]["replicates"] = {"count": 2}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(cookies))
    result = validate_program_file_structured(str(bad), str(SCHEMA_030))
    assert result["is_valid"] is False
    assert result["schema_errors"] == []
    (f,) = result["findings"]
    assert f == {
        "code": "E_EACH_WITH_REPLICATES",
        "message": 'Step "cool" has both an instances: "each" trigger (on "bake") and its own `replicates`.',
        "where": "step:cool",
        "fix": "drop `replicates` on `cool`; it inherits `3` instances from `bake`",
        "severity": "error",
    }
    assert result["logic_errors"] == [
        "[E_EACH_WITH_REPLICATES] " + f["message"] + " (fix: " + f["fix"] + ")"
    ]
    json.dumps(result)  # --json output must stay serializable


def test_fractional_seconds_are_not_zero():
    from rhylthyme.validate_program import parse_duration_to_seconds

    assert parse_duration_to_seconds({"type": "fixed", "seconds": 2.5}) == 2.5
    assert parse_duration_to_seconds({"type": "fixed", "seconds": 5.0}) == 5
    assert parse_duration_to_seconds(True) == 0
