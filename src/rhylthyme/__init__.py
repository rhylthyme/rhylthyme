"""
rhylthyme: schedule work a person follows against a clock.

A program is JSON: parallel tracks of sequential steps with durations, start
triggers and shared-resource limits. This package validates, expands, plans
and runs programs. It has no network and no terminal dependencies; the
``rhylthyme`` command is a thin layer over it in :mod:`rhylthyme.cli`.

    >>> from rhylthyme import load_program_file, validate_program_structured
    >>> result = validate_program_structured(load_program_file("dinner.json"))
    >>> result["is_valid"], [f.code for f in result["findings"]]
"""

__version__ = "0.5.0"

from .alert_checks import alert_findings, parse_alert_offset
from .expand_replicates import expand_replicates
from .instance_checks import Finding, validate_instances
from .program_planner import plan_program
from .program_runner import ProgramRunner, StepStatus
from .validate_program import (
    calculate_step_start_time,
    load_program_file,
    parse_duration_to_seconds,
    perform_additional_validations,
    perform_additional_validations_structured,
    validate_program,
    validate_program_file,
    validate_program_file_structured,
    validate_program_structured,
    validate_track_step_overlaps,
)

__all__ = [
    "Finding",
    "ProgramRunner",
    "StepStatus",
    "__version__",
    "alert_findings",
    "calculate_step_start_time",
    "expand_replicates",
    "load_program_file",
    "parse_alert_offset",
    "parse_duration_to_seconds",
    "perform_additional_validations",
    "perform_additional_validations_structured",
    "plan_program",
    "validate_instances",
    "validate_program",
    "validate_program_file",
    "validate_program_file_structured",
    "validate_program_structured",
    "validate_track_step_overlaps",
]
