"""
Step `alerts` checks in the cli-runner validator (alert_checks.py is a byte
copy of the root package's module; see tools/check_mirrors.sh).
"""

import json
from pathlib import Path

import pytest

from rhylthyme.alert_checks import alert_findings, parse_alert_offset
from rhylthyme.validate_program import (
    normalize_time_fields,
    perform_additional_validations,
    perform_additional_validations_structured,
    validate_program_file_structured,
)

pytestmark = pytest.mark.unit

SCHEMA_030 = (
    Path(__file__).resolve().parents[2]
    / "rhylthyme-spec"
    / "src"
    / "rhylthyme_spec"
    / "schemas"
    / "program_schema_0.3.0-alpha.json"
)


def program(*steps):
    return {
        "programId": "alerts",
        "name": "Alerts",
        "schemaVersion": "0.3.0-alpha",
        "resourceConstraints": [],
        "tracks": [{"trackId": "t", "name": "T", "steps": list(steps)}],
    }


def step(step_id, trigger, duration_type, *alerts):
    duration = {"type": duration_type}
    if duration_type == "fixed":
        duration["seconds"] = 600
    return {
        "stepId": step_id,
        "name": step_id,
        "startTrigger": trigger,
        "duration": duration,
        "alerts": list(alerts),
    }


def test_offsets_keep_their_sign():
    assert parse_alert_offset("-2m") == -120
    assert parse_alert_offset("1h30m") == 5400
    assert parse_alert_offset("later") is None
    # The schema copy is normalized, but alerts are passed through untouched.
    p = program(
        step(
            "a",
            {"type": "programStart"},
            "fixed",
            {"event": "end", "offsetSeconds": "-2m"},
        )
    )
    normalized = normalize_time_fields(p)
    assert normalized["tracks"][0]["steps"][0]["alerts"][0]["offsetSeconds"] == "-2m"


def test_alert_findings_plug_in_next_to_instrument_findings():
    p = program(
        step(
            "go",
            {"type": "manual"},
            "fixed",
            {"event": "start", "offsetSeconds": "-1m"},
        ),
        step(
            "wait",
            {"type": "afterStep", "stepId": "go"},
            "indefinite",
            {"event": "end", "offsetSeconds": "-30s"},
            {"event": "end"},
        ),
        step(
            "first",
            {"type": "programStart"},
            "fixed",
            {"event": "start", "offsetSeconds": -5},
        ),
        step(
            "fine",
            {"type": "afterStep", "stepId": "first"},
            "fixed",
            {"event": "end", "offsetSeconds": "-2m", "message": "Nearly done"},
        ),
    )
    # One track per step: the overlap check is not what is under test.
    p["tracks"] = [
        {"trackId": s["stepId"], "name": s["stepId"], "steps": [s]}
        for s in p["tracks"][0]["steps"]
    ]
    found = [
        (f.code, f.severity, f.where)
        for f in perform_additional_validations_structured(p)
    ]
    assert found == [
        ("W_ALERT_BEFORE_MANUAL_START", "warning", "step:go"),
        ("W_ALERT_BEFORE_INDEFINITE_END", "warning", "step:wait"),
        ("W_ALERT_BEFORE_PROGRAM_START", "warning", "step:first"),
    ]
    assert perform_additional_validations(p) == []  # warnings only


def test_bad_offset_is_an_error(tmp_path):
    p = program(
        step(
            "a",
            {"type": "programStart"},
            "fixed",
            {"event": "end", "offsetSeconds": "2 fortnights"},
        )
    )
    assert [f.code for f in alert_findings(p)] == ["E_ALERT_BAD_OFFSET"]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(p))
    result = validate_program_file_structured(str(path), str(SCHEMA_030))
    assert result["is_valid"] is False
    assert result["schema_errors"] == []
    assert [f["code"] for f in result["findings"]] == ["E_ALERT_BAD_OFFSET"]


def test_valid_alerts_pass_the_schema(tmp_path):
    p = program(
        step(
            "a",
            {"type": "programStart"},
            "fixed",
            {"event": "end", "offsetSeconds": "-2m", "level": "alarm"},
        )
    )
    path = tmp_path / "p.json"
    path.write_text(json.dumps(p))
    result = validate_program_file_structured(str(path), str(SCHEMA_030))
    assert result["is_valid"] is True, result
    assert result["findings"] == []
