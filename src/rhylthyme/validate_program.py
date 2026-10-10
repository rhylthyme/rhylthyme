#!/usr/bin/env python3
"""
Program Schema Validator

This script validates JSON and YAML program files against the program schema.
It ensures that programs conform to the specification for real-time schedules.
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

import yaml
from jsonschema import SchemaError, ValidationError, validate

from .alert_checks import alert_findings
from .instance_checks import Finding, validate_instances
from .instruments import instrument_findings

# Import environment loader for environment-based validation
try:
    from .environment_loader import get_default_loader, load_resource_constraints
except ImportError:
    # Fallback if running as standalone script
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from .environment_loader import EnvironmentLoader

    def load_resource_constraints(program_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        return program_data.get("resourceConstraints", [])

    def get_default_loader() -> "EnvironmentLoader":  # type: ignore
        return None  # type: ignore


def load_program_file(file_path: str) -> Dict[str, Any]:
    """Load and parse a program file (JSON or YAML)."""
    try:
        with open(file_path, "r") as file:
            # Determine file type based on extension
            _, ext = os.path.splitext(file_path)
            if ext.lower() in [".yaml", ".yml"]:
                return yaml.safe_load(file)
            else:  # Default to JSON
                return json.load(file)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        print(f"Error parsing file {file_path}: {e}")
        raise ValueError(f"Error parsing file {file_path}: {e}")
    except FileNotFoundError:
        print(f"File not found: {file_path}")
        raise


def parse_time_string_to_seconds(time_value: Any) -> int:
    """
    Parse a time value (string or int) to seconds.

    Args:
        time_value: Time string like "5m", "30s", "1h30m" or integer seconds

    Returns:
        Time in seconds
    """
    if not time_value:
        return 0

    # Handle numeric format (the schema allows fractional seconds)
    if isinstance(time_value, bool):
        return 0
    if isinstance(time_value, int):
        return time_value
    if isinstance(time_value, float):
        return int(time_value) if time_value.is_integer() else time_value  # type: ignore[return-value]

    # Handle string format
    if not isinstance(time_value, str):
        return 0

    # Remove whitespace
    time_str = str(time_value).strip()

    # Handle pure numbers (assume seconds)
    if time_str.isdigit():
        return int(time_str)

    total_seconds = 0
    current_number = ""

    for char in time_str:
        if char.isdigit():
            current_number += char
        elif char in ["h", "m", "s"]:
            if current_number:
                value = int(current_number)
                if char == "h":
                    total_seconds += value * 3600
                elif char == "m":
                    total_seconds += value * 60
                elif char == "s":
                    total_seconds += value
                current_number = ""

    return total_seconds


def normalize_time_fields(data: Any) -> Any:
    """
    Recursively normalize time format strings to integers in the program data.
    This converts strings like '30s', '2m', '1h30m' to integer seconds.

    Args:
        data: Program data (dict, list, or primitive)

    Returns:
        Normalized data with time strings converted to integers
    """
    if isinstance(data, dict):
        normalized = {}
        for key, value in data.items():
            # Step alerts keep their signed offsets ("-2m"): this parser drops
            # the sign, and the schema accepts the strings as written.
            if key == "alerts":
                normalized[key] = value
                continue
            # Check if this is a time-related field
            if key in [
                "seconds",
                "minSeconds",
                "maxSeconds",
                "defaultSeconds",
                "optimalSeconds",
                "offsetSeconds",
                "bufferSeconds",
            ]:
                # Convert time string to integer
                if isinstance(value, str):
                    normalized[key] = parse_time_string_to_seconds(value)
                else:
                    normalized[key] = value
            else:
                # Recursively normalize nested structures
                normalized[key] = normalize_time_fields(value)
        return normalized
    elif isinstance(data, list):
        return [normalize_time_fields(item) for item in data]
    else:
        return data


def validate_program(
    program: Dict[str, Any], schema: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """
    Validate a program against the schema.

    Returns:
        Tuple containing (is_valid, error_messages)
    """
    try:
        # Normalize time fields before validation
        normalized_program = normalize_time_fields(program)
        validate(instance=normalized_program, schema=schema)
        return True, []
    except ValidationError as e:
        # Extract the validation error path and message
        path = ".".join(str(p) for p in e.path) if e.path else "root"
        message = e.message
        return False, [f"Validation error at {path}: {message}"]
    except SchemaError as e:
        return False, [f"Schema error: {e}"]


_LEGACY_CODES = (
    ("Duplicate step ID", "duplicate_step_id"),
    ("Referenced step ID", "dangling_step_ref"),
    ("references choiceId", "choice_ref"),
    ("not defined in resourceConstraints", "task_not_constrained"),
    ("environment", "environment_not_found"),
    ("negative offsetSeconds", "negative_offset"),
    ("Referenced template ID", "dangling_template_ref"),
    ("overlap by", "track_overlap"),
)


def _legacy_finding(message: str) -> Finding:
    """Wrap a pre-existing check's message as a coded Finding (string unchanged)."""
    code = next((c for needle, c in _LEGACY_CODES if needle in message), "logic_error")
    m = re.search(r"[Ss]tep(?: ID)? '([^']+)'", message)
    where = f"step:{m.group(1)}" if m else None
    return Finding.legacy_error(code, message, where)


def perform_additional_validations_structured(
    program: Dict[str, Any], strict: bool = False, workcell: Any = None
) -> List[Finding]:
    """
    All logic checks as structured findings: the pre-existing checks (each
    rendered by ``str()`` exactly as before) followed by the schema 0.3.0
    `instances` / `replicates` checks from :mod:`instance_checks`, which
    include warnings, the instrument-step checks (galago commands, against
    ``workcell`` — a path or dict — when given) and the step `alerts` checks
    from :mod:`alert_checks`. Pass the UNEXPANDED program for the `instances`
    checks to fire.
    """
    findings = [_legacy_finding(msg) for msg in _legacy_logic_errors(program, strict)]
    findings.extend(validate_instances(program))
    findings.extend(instrument_findings(program, workcell))
    findings.extend(alert_findings(program))
    return findings


def perform_additional_validations(
    program: Dict[str, Any], strict: bool = False, workcell: Any = None
) -> List[str]:
    """
    Perform additional validations that go beyond basic schema validation.
    If strict is True, always require all tasks used in steps/buffers to be defined in resourceConstraints.
    Returns:
        List of error messages (empty if all validations pass). Warnings are
        not included; see :func:`perform_additional_validations_structured`.
    """
    return [
        str(f)
        for f in perform_additional_validations_structured(program, strict, workcell)
        if f.severity == "error"
    ]


def _legacy_logic_errors(program: Dict[str, Any], strict: bool = False) -> List[str]:
    """The pre-0.3.0 logic checks, as their original message strings."""
    errors = []

    # Check for duplicate step IDs
    step_id_count: Dict[str, int] = {}
    step_ids = set()
    referenced_step_ids = set()

    # Collect all step IDs and check for duplicates
    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            step_id = step.get("stepId")
            step_ids.add(step_id)

            # Count occurrences for duplicate detection
            if step_id in step_id_count:
                step_id_count[step_id] += 1
            else:
                step_id_count[step_id] = 1

            # Collect referenced step IDs
            start_trigger = step.get("startTrigger", {})
            if (
                start_trigger.get("type") in ["afterStep", "afterStepWithBuffer"]
                and "stepId" in start_trigger
            ):
                referenced_step_ids.add(start_trigger.get("stepId"))

    # Report duplicate step IDs
    for step_id, count in step_id_count.items():
        if count > 1:
            errors.append(f"Duplicate step ID '{step_id}' found {count} times")

    # Check for references to non-existent steps
    for ref_id in referenced_step_ids:
        if ref_id not in step_ids:
            errors.append(f"Referenced step ID '{ref_id}' does not exist in any track")

    # Validate choice references: a trigger's choiceId must name an option of
    # a step that has a `choice` (also inside `logic` / `triggers`).
    choice_steps: Dict[str, set] = {}
    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            choice = step.get("choice")
            if choice and choice.get("options"):
                choice_steps[step.get("stepId")] = {
                    opt.get("choiceId")
                    for opt in choice["options"]
                    if opt.get("choiceId")
                }
    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            trigger = step.get("startTrigger", {})
            triggers_to_check = [trigger]
            if trigger.get("logic") and trigger.get("triggers"):
                triggers_to_check = trigger["triggers"]
            for t in triggers_to_check:
                choice_id = t.get("choiceId")
                ref_step = t.get("stepId")
                if not (choice_id and ref_step):
                    continue
                if ref_step not in choice_steps:
                    errors.append(
                        f"Step '{step.get('stepId')}' references choiceId "
                        f"'{choice_id}' on step '{ref_step}' which has no choice "
                        "property"
                    )
                elif choice_id not in choice_steps[ref_step]:
                    valid = ", ".join(sorted(choice_steps[ref_step]))
                    errors.append(
                        f"Step '{step.get('stepId')}' references choiceId "
                        f"'{choice_id}' but step '{ref_step}' only has options: "
                        f"{valid}"
                    )

    # Check for resource constraints
    task_types_in_steps = set()
    task_types_in_constraints = set()

    # Collect task types used in steps and buffers
    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            # Main step task
            if "task" in step:
                task_types_in_steps.add(step.get("task"))

            # Pre-buffer tasks
            pre_buffer = step.get("preBuffer", {})
            if "tasks" in pre_buffer:
                for task in pre_buffer.get("tasks", []):
                    task_types_in_steps.add(task)

            # Post-buffer tasks
            post_buffer = step.get("postBuffer", {})
            if "tasks" in post_buffer:
                for task in post_buffer.get("tasks", []):
                    task_types_in_steps.add(task)

            # Pre-buffer taskResources
            if "taskResources" in pre_buffer:
                for task_resource in pre_buffer.get("taskResources", []):
                    task_types_in_steps.add(task_resource.get("name"))

            # Post-buffer taskResources
            if "taskResources" in post_buffer:
                for task_resource in post_buffer.get("taskResources", []):
                    task_types_in_steps.add(task_resource.get("name"))

    # Try to load resource constraints (handles both embedded and environment-based)
    try:
        # Import here to avoid circular imports
        from .environment_loader import load_resource_constraints

        resource_constraints = load_resource_constraints(program)
    except:
        # Fallback to just embedded constraints
        resource_constraints = program.get("resourceConstraints", [])

    # Collect task types defined in constraints
    for constraint in resource_constraints:
        task_types_in_constraints.add(constraint.get("task"))

    # Check for task types used in steps but not defined in constraints
    # Only report errors in strict mode, or when there are actually some resource constraints
    # defined (which suggests the user is trying to use resource validation)
    if strict and task_types_in_steps:
        for task_type in task_types_in_steps:
            if task_type not in task_types_in_constraints:
                errors.append(
                    f"Task '{task_type}' is used in steps but not defined in resourceConstraints"
                )

    # If environment is referenced, validate it exists
    if "environment" in program:
        environment_id = program["environment"]
        try:
            from .environment_loader import get_default_loader

            loader = get_default_loader()
            loader.load_environment(environment_id)
        except FileNotFoundError:
            errors.append(f"Referenced environment '{environment_id}' not found")
        except Exception as e:
            errors.append(f"Error loading environment '{environment_id}': {str(e)}")

    # Check template references
    template_ids = set()
    referenced_template_ids = set()

    # Collect all template IDs
    for template in program.get("trackTemplates", []):
        template_ids.add(template.get("templateId"))

    # Collect referenced template IDs
    for track in program.get("tracks", []):
        if "templateId" in track:
            referenced_template_ids.add(track.get("templateId"))

    # Check for references to non-existent templates
    for ref_id in referenced_template_ids:
        if ref_id not in template_ids:
            errors.append(
                f"Referenced template ID '{ref_id}' does not exist in trackTemplates"
            )

    # Validate negative offsetSeconds: only allowed on afterStep triggers
    # referencing steps with indefinite duration
    step_lookup = {}
    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            step_lookup[step.get("stepId")] = step

    for track in program.get("tracks", []):
        for step in track.get("steps", []):
            start_trigger = step.get("startTrigger", {})
            trigger_type = start_trigger.get("type")
            offset = start_trigger.get("offsetSeconds", 0)
            if isinstance(offset, str):
                try:
                    offset = parse_duration_to_seconds(offset)
                except Exception:
                    offset = 0
            if offset is not None and offset < 0:
                if trigger_type not in ("afterStep", "afterStepWithBuffer"):
                    errors.append(
                        f"Step '{step.get('stepId')}': negative offsetSeconds is only allowed "
                        f"on afterStep triggers, not '{trigger_type}'"
                    )
                else:
                    ref_step_id = start_trigger.get("stepId")
                    ref_step = step_lookup.get(ref_step_id)
                    if ref_step:
                        ref_duration = ref_step.get("duration", {})
                        ref_duration_type = (
                            ref_duration.get("type")
                            if isinstance(ref_duration, dict)
                            else None
                        )
                        if ref_duration_type != "indefinite":
                            errors.append(
                                f"Step '{step.get('stepId')}': negative offsetSeconds "
                                f"(-{abs(offset)}s) requires referenced step "
                                f"'{ref_step_id}' to have duration.type='indefinite', "
                                f"but it has '{ref_duration_type}'"
                            )

    # Check for overlapping steps within the same track
    track_overlap_errors = validate_track_step_overlaps(program)
    errors.extend(track_overlap_errors)

    return errors


def validate_track_step_overlaps(program: Dict[str, Any]) -> List[str]:
    """
    Validate that steps within the same track do not overlap in time.

    Args:
        program: The program dictionary to validate

    Returns:
        List of error messages for any overlapping steps found
    """
    errors = []

    # Calculate step timing for each track
    for track_idx, track in enumerate(program.get("tracks", [])):
        track_name = track.get("name", f"Track {track_idx + 1}")
        steps = track.get("steps", [])

        if len(steps) <= 1:
            continue  # No overlaps possible with 0 or 1 steps

        # Calculate actual start times for each step
        step_timings = []

        for step in steps:
            step_id = step.get("stepId")

            duration = parse_duration_to_seconds(step.get("duration", "0s"))

            # Calculate start time based on trigger
            start_time = calculate_step_start_time(step, steps, program)
            end_time = start_time + duration

            # The choice branch a step is gated on, if any (choice-aware overlaps)
            trigger = step.get("startTrigger", {})
            step_choice_id = trigger.get("choiceId")
            if not step_choice_id and trigger.get("logic") and trigger.get("triggers"):
                for sub_t in trigger["triggers"]:
                    if sub_t.get("choiceId"):
                        step_choice_id = sub_t["choiceId"]
                        break

            step_timings.append(
                {
                    "stepId": step_id,
                    "name": step.get("name", step_id),
                    "startTime": start_time,
                    "endTime": end_time,
                    "duration": duration,
                    "choiceId": step_choice_id,
                }
            )

        # Sort by start time
        step_timings.sort(key=lambda x: x["startTime"])

        # Check for overlaps
        for i in range(len(step_timings) - 1):
            current_step = step_timings[i]
            next_step = step_timings[i + 1]

            # Only one choice branch runs, so steps on different branches never
            # overlap; nor does a choice-gated step with a convergence step
            # (e.g. a "logic: any" step after the branches).
            if current_step.get("choiceId") or next_step.get("choiceId"):
                if current_step.get("choiceId") != next_step.get("choiceId"):
                    continue

            # Check if current step ends after next step starts
            if current_step["endTime"] > next_step["startTime"]:
                overlap_duration = current_step["endTime"] - next_step["startTime"]
                errors.append(
                    f"Track '{track_name}': Steps '{current_step['name']}' and '{next_step['name']}' "
                    f"overlap by {overlap_duration} seconds. "
                    f"Step '{current_step['name']}' ends at {current_step['endTime']}s, "
                    f"but step '{next_step['name']}' starts at {next_step['startTime']}s."
                )

    return errors


def parse_duration_to_seconds(duration: Any) -> int:
    """
    Parse a duration (string or dict) to seconds.

    Args:
        duration: Duration string like "5m", "30s", "1h30m" or dict with type/seconds

    Returns:
        Duration in seconds
    """
    if not duration:
        return 0

    # Handle dict format (e.g., {"type": "fixed", "seconds": 180})
    if isinstance(duration, dict):
        duration_type = duration.get("type", "fixed")
        if duration_type == "indefinite":
            # Indefinite durations have no fixed time; use placeholder for validation
            return parse_time_string_to_seconds(duration.get("defaultSeconds", 60))
        if "seconds" in duration:
            seconds_value = duration["seconds"]
            return parse_time_string_to_seconds(seconds_value)
        elif "defaultSeconds" in duration:
            # Variable durations use defaultSeconds
            return parse_time_string_to_seconds(duration["defaultSeconds"])
        elif "maxSeconds" in duration:
            return parse_time_string_to_seconds(duration["maxSeconds"])
        elif "minSeconds" in duration:
            return parse_time_string_to_seconds(duration["minSeconds"])
        elif "minutes" in duration:
            return int(duration["minutes"]) * 60
        elif "hours" in duration:
            return int(duration["hours"]) * 3600
        else:
            return 0

    # Handle string format
    return parse_time_string_to_seconds(duration)


def _signed_seconds(value) -> float:
    """Parse an offset/buffer that may be a number or a signed unit string ('-20m')."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    sign = -1 if text.startswith("-") else 1
    return sign * parse_time_string_to_seconds(text.lstrip("+-"))


def calculate_step_start_time(
    step: Dict[str, Any], track_steps: List[Dict[str, Any]], program: Dict[str, Any]
) -> float:
    """
    Calculate the start time of a step based on its trigger.

    Args:
        step: The step to calculate start time for
        track_steps: All steps in the same track
        program: The full program (for cross-track dependencies)

    Returns:
        Start time in seconds from program start
    """
    start_trigger = step.get("startTrigger", {})
    trigger_type = start_trigger.get("type", "programStart")

    # Compound triggers ({"logic": "all"|"any", "triggers": [...]}): resolve
    # each sub-trigger for this step and combine, as the root package does.
    if "logic" in start_trigger and "triggers" in start_trigger:
        sub_times = [
            calculate_step_start_time(
                {"stepId": step.get("stepId"), "startTrigger": sub},
                track_steps,
                program,
            )
            for sub in start_trigger["triggers"]
        ]
        if not sub_times:
            return 0
        if start_trigger.get("logic") == "any":
            return min(sub_times)
        return max(sub_times)

    if trigger_type in ("programStart", "programStartOffset"):
        offset_seconds = _signed_seconds(start_trigger.get("offsetSeconds", 0))
        return max(0, offset_seconds)

    elif trigger_type in ["afterStep", "afterStepWithBuffer"]:
        referenced_step_id = start_trigger.get("stepId")
        offset_seconds = _signed_seconds(start_trigger.get("offsetSeconds", 0))
        if trigger_type == "afterStepWithBuffer":
            offset_seconds += _signed_seconds(start_trigger.get("bufferSeconds", 0))
        event = start_trigger.get("event", "end")  # Default to "end"

        if not referenced_step_id:
            return 0  # Invalid trigger, assume program start

        # Find the referenced step (could be in any track)
        referenced_step = None
        referenced_track_steps = None

        # First check current track
        for s in track_steps:
            if s.get("stepId") == referenced_step_id:
                referenced_step = s
                referenced_track_steps = track_steps
                break

        # If not found in current track, check all tracks
        if not referenced_step:
            for track in program.get("tracks", []):
                for s in track.get("steps", []):
                    if s.get("stepId") == referenced_step_id:
                        referenced_step = s
                        referenced_track_steps = track.get("steps", [])
                        break
                if referenced_step:
                    break

        if not referenced_step or referenced_track_steps is None:
            return 0  # Referenced step not found, assume program start

        # Calculate referenced step's timing
        referenced_start_time = calculate_step_start_time(
            referenced_step, referenced_track_steps, program
        )
        referenced_duration = parse_duration_to_seconds(
            referenced_step.get("duration", "0s")
        )

        # Negative offset: step starts before referenced step ends.
        # For timeline positioning, place at the referenced step's start time.
        if offset_seconds < 0:
            # Start before the referenced step's planned end, never before its start.
            return max(
                referenced_start_time,
                referenced_start_time + referenced_duration + offset_seconds,
            )

        if event == "start":
            base_time = referenced_start_time
        else:  # event == "end"
            base_time = referenced_start_time + referenced_duration

        return max(0, base_time + offset_seconds)

    elif trigger_type in ["manual", "previousStepComplete"]:
        # Manual/previousStepComplete: position after the previous step in the track
        step_id = step.get("stepId")
        offset_seconds = _signed_seconds(start_trigger.get("offsetSeconds", 0))
        prev_end_time: float = 0
        for s in track_steps:
            if s.get("stepId") == step_id:
                break
            s_start = calculate_step_start_time(s, track_steps, program)
            s_duration = parse_duration_to_seconds(s.get("duration", "0s"))
            prev_end_time = s_start + s_duration
        return max(0, prev_end_time + offset_seconds)

    else:
        # Unknown trigger type, assume program start
        return 0


def validate_program_structured(
    program: Dict[str, Any], schema: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Validate an (unexpanded) program dict and return structured findings.

    Order of operations:

    1. `instances` / `replicates` checks on the unexpanded program
       (:func:`instance_checks.validate_instances`), so a malformed program
       gets its proper code rather than an expansion failure, and the step
       `alerts` checks (:func:`alert_checks.alert_findings`);
    2. replicate expansion (skipped when step 1 found errors; an expander
       raise is reported as ``expansion_failed`` as a backstop);
    3. JSON Schema validation and the logic checks on the expanded program.

    Returns a dict with ``is_valid`` (errors only; warnings never invalidate),
    ``findings`` (list of ``{code, message, where, fix, severity}`` dicts),
    the legacy ``schema_errors`` / ``logic_errors`` string lists, ``warnings``
    (strings) and ``program`` (the expanded program that was checked).
    """
    from .expand_replicates import expand_replicates

    pre = validate_instances(program)
    # Step `alerts` checks also run once per authored step, not per instance.
    alerts = alert_findings(program)
    expanded = program
    expansion: List[Finding] = []
    if not any(f.severity == "error" for f in pre):
        try:
            expanded = expand_replicates(program)
        except ValueError as e:
            expansion.append(
                Finding(
                    code="expansion_failed",
                    message=f"Could not expand replicates: {e}",
                    where="program",
                    fix=(
                        "Check `replicates` and `instances` on the steps named "
                        "in the message."
                    ),
                )
            )

    is_valid, schema_errors = validate_program(expanded, schema)
    schema_findings = [
        Finding.legacy_error("schema_error", msg, "program") for msg in schema_errors
    ]

    # The legacy string API is the single entry point for the logic checks
    # (callers may patch it). Instance findings are re-attached structured
    # from the unexpanded pass: when expansion ran they are absent from the
    # legacy strings (the expanded program carries no `instances`); when it
    # was skipped they are present and are dropped here in favour of `pre`.
    # Alert errors are likewise taken from the unexpanded pass.
    pre_strings = {str(f) for f in pre}
    logic = list(expansion)
    logic.extend(
        _legacy_finding(msg)
        for msg in perform_additional_validations(expanded)
        if msg not in pre_strings and not msg.startswith("[E_ALERT_")
    )
    logic.extend(pre)
    logic.extend(alerts)

    findings = schema_findings + logic
    return {
        "is_valid": is_valid and not any(f.severity == "error" for f in logic),
        "findings": [f.to_dict() for f in findings],
        "schema_errors": schema_errors,
        "logic_errors": [str(f) for f in logic if f.severity == "error"],
        "warnings": [str(f) for f in findings if f.severity == "warning"],
        "program": expanded,
    }


def validate_program_file_structured(
    program_file: str,
    schema_file: str = "program_schema.json",
    verbose: bool = False,
    strict: bool = False,
    workcell: Any = None,
) -> dict:
    """
    Validate a program file and return a structured result for machine-readable output.
    Returns a dict with is_valid, schema_errors, logic_errors, warnings,
    findings (``{code, message, where, fix, severity}`` per finding; the
    schema 0.3.0 `instances` codes carry fix hints) and summary info.
    """
    schema = load_program_file(schema_file)
    program = load_program_file(program_file)
    is_valid, schema_errors = validate_program(program, schema)
    # Logic checks run on the unexpanded program, so the schema 0.3.0
    # `instances` / `replicates` checks see the authored declarations.
    logic_findings = perform_additional_validations_structured(
        program, strict=strict, workcell=workcell
    )
    logic_errors = [str(f) for f in logic_findings if f.severity == "error"]
    findings = [
        Finding.legacy_error("schema_error", msg, "program").to_dict()
        for msg in schema_errors
    ] + [f.to_dict() for f in logic_findings]
    warnings = [str(f) for f in logic_findings if f.severity == "warning"]
    summary = {
        "programId": program.get("programId"),
        "name": program.get("name"),
        "tracks": len(program.get("tracks", [])),
        "resourceConstraints": len(program.get("resourceConstraints", [])),
        "totalSteps": sum(
            len(track.get("steps", [])) for track in program.get("tracks", [])
        ),
    }
    return {
        "is_valid": is_valid and not logic_errors,
        "schema_errors": schema_errors,
        "logic_errors": logic_errors,
        "warnings": warnings,
        "findings": findings,
        "summary": summary,
    }


def validate_program_file(
    program_file: str,
    schema_file: str = "program_schema.json",
    verbose: bool = False,
    json_output: bool = False,
    strict: bool = False,
    workcell: Any = None,
) -> bool:
    """
    Validate a program file against the schema.
    If json_output is True, print machine-readable JSON result.
    If strict is True, enforce all tasks must be defined in resourceConstraints.
    """
    result = validate_program_file_structured(
        program_file, schema_file, verbose, strict, workcell
    )
    if json_output:
        import json as _json

        print(_json.dumps(result, indent=2))
        return result["is_valid"]
    # Default: print human-readable output
    print(f"Validating {program_file}...")
    valid = result["is_valid"]
    if valid:
        print(f"✅ {program_file} is valid")
    else:
        print(f"❌ {program_file} has validation errors:")
        if result["schema_errors"]:
            print("\nSchema validation errors:")
            for error in result["schema_errors"]:
                print(f"  - {error}")
        if result["logic_errors"]:
            print("\nAdditional validation errors:")
            for error in result["logic_errors"]:
                print(f"  - {error}")
    if result.get("warnings"):
        print("\nWarnings:")
        for warning in result["warnings"]:
            print(f"  - {warning}")
    if verbose:
        print("\nProgram details:")
        for k, v in result["summary"].items():
            print(f"  - {k}: {v}")
    return valid


def main():
    parser = argparse.ArgumentParser(
        description="Validate program files (JSON or YAML) against the schema"
    )
    parser.add_argument(
        "program_files", nargs="+", help="Program files (JSON or YAML) to validate"
    )
    parser.add_argument(
        "--schema",
        default="program_schema.json",
        help="Path to the schema file (default: program_schema.json)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed validation results"
    )
    parser.add_argument(
        "--json", "-j", action="store_true", help="Print machine-readable JSON result"
    )
    parser.add_argument(
        "--strict",
        "-s",
        action="store_true",
        help="Enforce all tasks must be defined in resourceConstraints",
    )

    args = parser.parse_args()

    all_valid = True

    for program_file in args.program_files:
        file_valid = validate_program_file(
            program_file, args.schema, args.verbose, args.json, args.strict
        )
        if not file_valid:
            all_valid = False

    if not all_valid:
        sys.exit(1)


if __name__ == "__main__":
    main()
