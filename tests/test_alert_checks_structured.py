"""
Tests for the step `alerts` checks (rhylthyme/alert_checks.py) and their
integration into the structured validator.
"""

import copy
import json
import os

import pytest
import rhylthyme_spec

from rhylthyme.alert_checks import alert_findings, parse_alert_offset
from rhylthyme.validate_program import (
    perform_additional_validations,
    validate_program_structured,
)

SCHEMA = os.path.join(
    os.path.dirname(rhylthyme_spec.__file__),
    "schemas",
    "program_schema_0.3.0-alpha.json",
)


def program(*steps):
    return {
        "programId": "alerts",
        "name": "Alerts",
        "schemaVersion": "0.3.0-alpha",
        "resourceConstraints": [],
        "tracks": [{"trackId": "t", "name": "T", "steps": list(steps)}],
    }


def step(step_id, trigger, duration, *alerts):
    return {
        "stepId": step_id,
        "name": step_id.title(),
        "startTrigger": trigger,
        "duration": duration,
        "alerts": list(alerts),
    }


FIXED = {"type": "fixed", "seconds": 600}
INDEFINITE = {"type": "indefinite"}


def codes(findings):
    return [(f.code, f.severity, f.where) for f in findings]


@pytest.mark.parametrize(
    "value, seconds",
    [
        (None, 0),
        (0, 0),
        (-90, -90),
        (12.5, 12.5),
        ("-2m", -120),
        ("+30s", 30),
        ("1h30m", 5400),
        ("-1h 5min", -3900),
        ("45", 45),
        ("-7.5", -7.5),
        ("2 minutes", 120),
    ],
)
def test_parse_alert_offset_keeps_the_sign(value, seconds):
    assert parse_alert_offset(value) == seconds


@pytest.mark.parametrize(
    "value", ["", "soon", "5x", "1h30", "--2m", True, [], float("inf")]
)
def test_parse_alert_offset_rejects_garbage(value):
    assert parse_alert_offset(value) is None


def test_projectable_alerts_have_no_findings():
    p = program(
        step(
            "a",
            {"type": "programStart"},
            FIXED,
            {"event": "end", "offsetSeconds": "-2m"},
        ),
        step(
            "b",
            {"type": "manual"},
            INDEFINITE,
            {"event": "start", "offsetSeconds": 30},
            {"event": "end"},
            {"event": "end", "offsetSeconds": "5m"},
        ),
        step(
            "c",
            {"type": "afterStep", "stepId": "a"},
            FIXED,
            {"event": "start", "offsetSeconds": -60},
        ),
        step(
            "d",
            {"type": "programStartOffset", "offsetSeconds": "10m"},
            FIXED,
            {"event": "start", "offsetSeconds": "-5m"},
        ),
    )
    assert alert_findings(p) == []


def test_unprojectable_alerts_warn():
    p = program(
        step(
            "manual",
            {"type": "manual"},
            FIXED,
            {"event": "start", "offsetSeconds": "-1m"},
        ),
        step(
            "open",
            {"type": "afterStep", "stepId": "manual"},
            INDEFINITE,
            {"event": "end", "offsetSeconds": -30},
        ),
        step(
            "first",
            {"type": "programStart"},
            FIXED,
            {"event": "start", "offsetSeconds": "-10s"},
        ),
        step(
            "late",
            {"type": "programStartOffset", "offsetSeconds": 60},
            FIXED,
            {"event": "start", "offsetSeconds": "-2m"},
        ),
    )
    assert codes(alert_findings(p)) == [
        ("W_ALERT_BEFORE_MANUAL_START", "warning", "step:manual"),
        ("W_ALERT_BEFORE_INDEFINITE_END", "warning", "step:open"),
        ("W_ALERT_BEFORE_PROGRAM_START", "warning", "step:first"),
        ("W_ALERT_BEFORE_PROGRAM_START", "warning", "step:late"),
    ]


def test_bad_offset_is_an_error():
    p = program(
        step(
            "a",
            {"type": "programStart"},
            FIXED,
            {"event": "end", "offsetSeconds": "soon"},
        )
    )
    [f] = alert_findings(p)
    assert (f.code, f.severity) == ("E_ALERT_BAD_OFFSET", "error")
    assert any("E_ALERT_BAD_OFFSET" in e for e in perform_additional_validations(p))


def test_structured_validation_reports_alerts_once_per_authored_step():
    with open(SCHEMA) as fh:
        schema = json.load(fh)
    s = step(
        "bake", {"type": "manual"}, FIXED, {"event": "start", "offsetSeconds": "-1m"}
    )
    s["replicates"] = {"count": 3}
    result = validate_program_structured(program(s), schema)
    assert result["is_valid"], result
    alert = [f for f in result["findings"] if f["code"].startswith("W_ALERT_")]
    assert [(f["code"], f["where"]) for f in alert] == [
        ("W_ALERT_BEFORE_MANUAL_START", "step:bake")
    ]
    # Expansion copies alerts onto every instance.
    assert all(st["alerts"] for st in result["program"]["tracks"][0]["steps"])

    bad = copy.deepcopy(s)
    bad["alerts"] = [{"event": "end", "offsetSeconds": "whenever"}]
    result = validate_program_structured(program(bad), schema)
    assert not result["is_valid"]
    assert [f["code"] for f in result["findings"] if "ALERT" in f["code"]] == [
        "E_ALERT_BAD_OFFSET"
    ]
