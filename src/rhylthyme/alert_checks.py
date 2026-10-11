"""
Validator checks for step `alerts` (package 0.2.3-alpha, the alerts
contract §2 rule 1: unpredictable anchors are not projected).

This module is maintained by byte-copy in two places (see
tools/check_mirrors.sh); the rhylthyme package is the source of truth:

    rhylthyme-cli-runner/src/rhylthyme/alert_checks.py
    rhylthyme-server/src/rhylthyme_server/rhylthyme/alert_checks.py

Its only import is the shared :class:`Finding` from ``instance_checks``
(mirrored beside it). It runs on the UNEXPANDED program, so each authored
step is reported once rather than once per replicate instance. The JS twin
is ``alertFindings`` in rhylthyme-server/mcp-api/schedule.js.

Alert offsets are signed ("-2m" is two minutes before the anchor); they are
parsed here, never through a sign-dropping normalizer.

Codes emitted here:

    E_ALERT_BAD_OFFSET             offsetSeconds is neither a finite number
                                   nor a signed time string ("-2m", "1h30m")
    W_ALERT_BEFORE_MANUAL_START    a before-start alert on a manual step: its
                                   start cannot be predicted, so it never fires
    W_ALERT_BEFORE_INDEFINITE_END  a before-end alert on an indefinite step: it
                                   has no projected end, so it never fires
    W_ALERT_BEFORE_PROGRAM_START   a before-start alert that would fall before
                                   the program starts (programStart, or a
                                   programStartOffset shorter than the offset)
"""

import math
import re
from typing import Any, Dict, List, Optional

from .instance_checks import Finding

# The unit vocabulary of the engine's parseSeconds (rhylthyme-timeline), but
# strict: the whole string must be consumed.
_NUMBER = r"\d+(?:\.\d+)?"
_UNIT = r"(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
_OFFSET_RE = re.compile(
    rf"^[+-]?\s*(?:{_NUMBER}|(?:{_NUMBER}\s*{_UNIT}\s*)+)$", re.IGNORECASE
)
_PART_RE = re.compile(rf"({_NUMBER})\s*({_UNIT})", re.IGNORECASE)


def parse_alert_offset(value: Any) -> Optional[float]:
    """
    Signed seconds of an alert offset, or None if it cannot be parsed.
    A missing offset is 0. Numbers pass through; strings are a signed plain
    number or a sum of unit parts ("-1h 30m", "+45s").
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not _OFFSET_RE.match(text):
        return None
    sign = -1.0 if text.startswith("-") else 1.0
    body = text.lstrip("+-").strip()
    if re.fullmatch(_NUMBER, body):
        return sign * float(body)
    total = 0.0
    for number, unit in _PART_RE.findall(body):
        u = unit[0].lower()
        total += float(number) * (3600 if u == "h" else 60 if u == "m" else 1)
    return sign * total


def _fmt(seconds: float) -> str:
    """Compact duration for messages: '45 s', '2 min', '1 h 5 min'."""
    s = int(round(abs(seconds)))
    if s < 60:
        return f"{s} s"
    h, m = divmod(s // 60, 60)
    if not h:
        return f"{m} min"
    return f"{h} h {m} min" if m else f"{h} h"


def alert_findings(program: Dict[str, Any]) -> List[Finding]:
    """Findings for every step's `alerts` (pass the unexpanded program)."""
    findings: List[Finding] = []
    if not isinstance(program, dict):
        return findings
    for track in program.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        for step in track.get("steps") or []:
            if not isinstance(step, dict) or not isinstance(step.get("alerts"), list):
                continue
            findings.extend(_step_findings(step))
    return findings


def _step_findings(step: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    step_id = step.get("stepId")
    where = f"step:{step_id}"
    trigger = step.get("startTrigger")
    if not isinstance(trigger, dict):
        trigger = {}
    trigger_type = trigger.get("type")
    duration = step.get("duration")
    indefinite = isinstance(duration, dict) and duration.get("type") == "indefinite"

    for i, alert in enumerate(step["alerts"]):
        if not isinstance(alert, dict):
            continue
        label = f"Alert {i} on step '{step_id}'"
        offset = parse_alert_offset(alert.get("offsetSeconds"))
        if offset is None:
            out.append(
                Finding(
                    code="E_ALERT_BAD_OFFSET",
                    message=f"{label} has offsetSeconds {alert.get('offsetSeconds')!r}, "
                    "which is not a number of seconds or a time string.",
                    where=where,
                    fix='Use seconds (-120) or a signed time string ("-2m", "30s", "1h30m").',
                )
            )
            continue
        if offset >= 0:
            continue
        event = alert.get("event")
        if event == "start" and trigger_type == "manual":
            out.append(
                Finding(
                    code="W_ALERT_BEFORE_MANUAL_START",
                    message=f"{label} fires {_fmt(offset)} before the step starts, but the "
                    "step starts manually, so its start cannot be predicted and the alert "
                    "never fires.",
                    where=where,
                    fix="Anchor it on the end of the step before, or use offsetSeconds >= 0.",
                    severity="warning",
                )
            )
        elif event == "start" and trigger_type in (
            "programStart",
            "programStartOffset",
        ):
            start = 0.0
            if trigger_type == "programStartOffset":
                start = parse_alert_offset(trigger.get("offsetSeconds")) or 0.0
            if start + offset < 0:
                out.append(
                    Finding(
                        code="W_ALERT_BEFORE_PROGRAM_START",
                        message=f"{label} fires {_fmt(offset)} before the step starts, which "
                        "is before the program starts; it never fires.",
                        where=where,
                        fix="Shorten the offset, or delay the step with programStartOffset.",
                        severity="warning",
                    )
                )
        elif event == "end" and indefinite:
            out.append(
                Finding(
                    code="W_ALERT_BEFORE_INDEFINITE_END",
                    message=f"{label} fires {_fmt(offset)} before the step ends, but the step "
                    "is indefinite and has no projected end, so the alert never fires.",
                    where=where,
                    fix='Use offsetSeconds >= 0 on event "end", or give the step a fixed or '
                    "variable duration.",
                    severity="warning",
                )
            )
    return out
