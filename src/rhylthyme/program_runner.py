#!/usr/bin/env python3
"""
Real-Time Program Runner

This script executes real-time program files according to the program schema.
It provides a command-line interface to visualize program execution and
allows manual triggering of steps with variable durations.
"""

import datetime
import json
import logging
import os
import queue
import re  # Add import for regular expressions
import signal
import subprocess
import sys
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import yaml  # Add import for YAML support

from .drivers import blocking_action, step_tools

# Instance naming produced by the replicate expander: "Bake tray (2 of 3)".
# The runner splits it back apart so that grouped rows can show the base name
# once ("Bake tray ×3") and per-instance rows a canonical "[2 of 3]" label.
INSTANCE_NAME_RE = re.compile(r"^(?P<base>.*?)\s*\((?P<index>\d+) of (?P<count>\d+)\)$")


def split_instance_name(name: Optional[str]):
    """Split ``"Bake tray (2 of 3)"`` into ``("Bake tray", 2, 3)``.

    Names without the expander's instance suffix come back unchanged with
    ``None`` for the index and count.
    """
    match = INSTANCE_NAME_RE.match(name or "")
    if not match:
        return name, None, None
    return match.group("base"), int(match.group("index")), int(match.group("count"))


# Define sort modes for the display
class SortMode(Enum):
    """Enum representing the sort mode for the display."""

    DEFAULT = "default"  # Sort by step ID/definition order
    REMAINING = "remaining"  # Sort by remaining time
    STATUS = "status"  # Sort by status (running first, then pending, then completed)


# Define duration types
class DurationType(str, Enum):
    """
    Enum representing the duration type of a step.

    A ``str`` enum so that ``DurationType.FIXED == "fixed"`` holds: the step
    logic compares against both the members and their string values.
    """

    FIXED = "fixed"  # Fixed duration
    VARIABLE = "variable"  # Variable duration with min/max
    INDEFINITE = "indefinite"  # Indefinite duration (manual end)


# Try to import the validator to reuse its functions
try:
    from .environment_loader import EnvironmentLoader, load_resource_constraints
    from .validate_program import (
        load_program_file,
        perform_additional_validations,
        validate_program,
    )
except ImportError:
    # Define our own load_program_file function if the validator is not available
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
            sys.exit(1)
        except FileNotFoundError:
            print(f"File not found: {file_path}")
            sys.exit(1)

    # Stub functions for validation if the validator is not available
    def validate_program(
        program: Dict[str, Any], schema: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        return True, []

    def perform_additional_validations(
        program: Dict[str, Any], strict: bool = False, workcell: Any = None
    ) -> List[str]:
        return []


try:
    # Offsets and buffers may be signed unit strings ("-45m"); parse_time_string
    # below drops the sign, so trigger arithmetic uses the validator's parser.
    from .validate_program import _signed_seconds
except ImportError:  # pragma: no cover - validator not importable

    def _signed_seconds(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        sign = -1.0 if text.startswith("-") else 1.0
        return sign * parse_time_string(text.lstrip("+-"))


INSTRUMENT_DURATION_PARAMS = (
    "duration",
    "duration_seconds",
    "seconds",
    "run_time",
    "time",
    "timeout",
)


def _instrument_estimate_seconds(instrument: Dict[str, Any]) -> float:
    """rhylthyme_galago.fill_durations' offline estimate: params, else 60 s."""
    params = instrument.get("params") or {}
    for key in INSTRUMENT_DURATION_PARAMS:
        value = params.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return float(value)
    return 60.0


class StepStatus(Enum):
    """Enum representing the status of a step."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    WAITING_FOR_MANUAL = "WAITING_FOR_MANUAL"
    ABORTED = "ABORTED"
    # An instrument command failed; the operator retries, skips or aborts
    FAILED = "FAILED"


class StepVariables:
    """Class to hold step variables for code execution."""

    def __init__(self, step: "Step"):
        """Initialize step variables from a Step object."""
        self.stepId = step.step_id
        self.name = step.name
        self.trackId = step.track_id
        self.description = step.description
        self.status = step.status.value

        # Add task types and fractions
        self.taskTypes = step.task_types
        self.taskFractions = step.task_fractions

        # Add duration information if available
        if step.duration_type:
            self.durationType = step.duration_type.value
            if step.duration_seconds is not None:
                self.durationSeconds = step.duration_seconds
            if step.min_seconds is not None:
                self.minSeconds = step.min_seconds
            if step.max_seconds is not None:
                self.maxSeconds = step.max_seconds
            if step.default_seconds is not None:
                self.defaultSeconds = step.default_seconds


class Step:
    """Class representing a step in a program."""

    def __init__(self, step_data, track_id, batch_index=0):
        self.step_id = step_data["stepId"]
        self.name = step_data["name"]
        self.description = step_data.get("description", "")
        self.track_id = track_id
        # Replicate-instance identity, stamped by expand_replicates(). The
        # name suffix is a fallback for programs expanded by older tooling.
        base_name, name_index, name_count = split_instance_name(self.name)
        self.instance_of = step_data.get("instanceOf")
        self.instance_index = step_data.get("instanceIndex", name_index)
        self.base_name = base_name if self.instance_of else self.name
        self.instance_count_hint = name_count
        self.batch_index = batch_index
        self.priority = step_data.get("priority", 100)
        self.expected_end_time = None
        self.manual_trigger_name = None
        self.manual_start_trigger_name = None

        # Extract duration information
        self.duration_type = None
        self.duration_seconds = None
        self.min_seconds = None
        self.max_seconds = None
        self.default_seconds = None

        if "duration" in step_data:
            duration = step_data["duration"]
            if isinstance(duration, (int, float)):
                # Handle simple numeric duration (fixed duration in seconds)
                self.duration_type = DurationType.FIXED
                self.duration_seconds = float(duration)
            elif isinstance(duration, dict):
                duration_type = duration.get("type", "fixed")
                if duration_type == "fixed":
                    self.duration_type = DurationType.FIXED
                    self.duration_seconds = float(duration["seconds"])
                elif duration_type == "variable":
                    self.duration_type = DurationType.VARIABLE
                    self.min_seconds = float(duration.get("minSeconds", 0))
                    self.max_seconds = float(duration.get("maxSeconds", float("inf")))
                    self.default_seconds = float(
                        duration.get(
                            "defaultSeconds",
                            (
                                (self.min_seconds + self.max_seconds) / 2
                                if self.max_seconds < float("inf")
                                else self.min_seconds + 60
                            ),
                        )
                    )
                    self.manual_trigger_name = duration.get("triggerName")
                elif duration_type == "indefinite" or duration_type == "manual":
                    self.duration_type = DurationType.INDEFINITE
                    self.min_seconds = float(duration.get("minSeconds", 0))
                    self.default_seconds = float(
                        duration.get("defaultSeconds", self.min_seconds + 60)
                    )
                    self.manual_trigger_name = duration.get("triggerName")

        # Instrument calls (each through its workcell tool's driver): start
        # actions when the step starts, then its command or until; end
        # actions when it ends. A step with a command or an until ends when
        # that call replies, never on a timer, and with no authored duration
        # plans as indefinite. A step with only start/end actions runs on its
        # own duration (or until the operator ends it).
        self.instrument = step_data.get("instrument")
        blocking = blocking_action(self.instrument) if self.instrument else None
        self.instrument_blocking = blocking is not None
        self.instrument_reply: Optional[Dict[str, Any]] = None
        self.instrument_attempts = 0
        # Set while FAILED: {tool, command, code, errorMessage}
        self.failure: Optional[Dict[str, Any]] = None
        if blocking is not None and self.duration_type is None:
            self.duration_type = DurationType.INDEFINITE
            self.min_seconds = 0.0
            # For display and upcoming events only: the same estimate as
            # planning and the timeline (a duration-like param, else 60 s)
            self.default_seconds = _instrument_estimate_seconds(blocking)

        # Extract start trigger information
        start_trigger_data = step_data["startTrigger"]

        # Check if it's multiple triggers with logic
        if "logic" in start_trigger_data and "triggers" in start_trigger_data:
            self.start_trigger_logic = start_trigger_data["logic"]  # "all" or "any"
            self.start_triggers = start_trigger_data[
                "triggers"
            ]  # List of trigger objects
            self.start_trigger = None  # Legacy field - set to None for multi-trigger
        else:
            # Single trigger (backward compatibility)
            self.start_trigger_logic = None
            self.start_triggers = None
            self.start_trigger = start_trigger_data

        # Extract task types
        self.task_types = []
        self.task_fractions = {}  # Dictionary mapping task name to fraction

        # Handle backward compatibility with single task
        if "task" in step_data and step_data["task"]:
            self.task_types.append(step_data["task"])
            self.task_fractions[step_data["task"]] = 1.0

        # Handle multiple tasks
        if "tasks" in step_data and step_data["tasks"]:
            for task in step_data["tasks"]:
                if task not in self.task_types:
                    self.task_types.append(task)
                    self.task_fractions[task] = 1.0

        # Handle fractional task resources
        if "taskResources" in step_data and step_data["taskResources"]:
            for task_resource in step_data["taskResources"]:
                task_name = task_resource["name"]
                fraction = task_resource["fraction"]
                if task_name not in self.task_types:
                    self.task_types.append(task_name)
                self.task_fractions[task_name] = fraction

        # Check for code execution
        self.has_code = "codeBlock" in step_data
        self.code_type = None
        self.code_block = None
        if self.has_code:
            self.code_type = step_data["codeBlock"]["type"]
            self.code_block = step_data["codeBlock"]["code"]

        # Extract buffer information
        pre_buffer = step_data.get("preBuffer", {})
        post_buffer = step_data.get("postBuffer", {})
        self.pre_buffer_seconds = 0.0
        self.post_buffer_seconds = 0.0
        if pre_buffer:
            dur = pre_buffer.get("duration", 0)
            self.pre_buffer_seconds = float(parse_time_string(dur)) if dur else 0.0
        if post_buffer:
            dur = post_buffer.get("duration", 0)
            self.post_buffer_seconds = float(parse_time_string(dur)) if dur else 0.0

        # Initialize status
        self.status = StepStatus.PENDING
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.progress = 0.0
        self.abort_reason: Optional[str] = None
        # Program-clock time at which the start trigger was first satisfied
        # (may precede start_time when resources or actors were busy)
        self.trigger_fired_time: Optional[float] = None

        # Initialize code execution attributes
        self.code_result: Optional[Any] = None
        self.code_executed = False
        self.code_error: Optional[str] = None

    def to_dict(self):
        return {
            "stepId": self.step_id,
            "name": self.name,
            "description": self.description,
            "trackId": self.track_id,
            "batchIndex": self.batch_index,
            "priority": self.priority,
            "status": self.status.value,
            "startTime": self.start_time.isoformat() if self.start_time else None,
            "endTime": self.end_time.isoformat() if self.end_time else None,
            "expectedEndTime": (
                self.expected_end_time.isoformat() if self.expected_end_time else None
            ),
            "progress": self.progress,
            "abortReason": self.abort_reason,
            "taskTypes": self.task_types,
            "taskFractions": self.task_fractions,
            "manualTriggerName": self.manual_trigger_name,
        }

    def start(self, current_time: float) -> None:
        """Start the step."""
        self.status = StepStatus.RUNNING
        self.start_time = current_time

        if self.duration_type == "fixed":
            self.expected_end_time = current_time + self.duration_seconds
        elif self.duration_type == "variable":
            self.expected_end_time = current_time + self.default_seconds
        else:
            self.expected_end_time = None

        # Execute code block if present
        if self.code_type and self.code_block:
            self.execute_code_block()

    def execute_code_block(self) -> None:
        """Execute the code block associated with this step."""
        if not self.code_type or not self.code_block:
            return

        try:
            # Create step variables for substitution
            step_vars = StepVariables(self)

            # Replace variables in the code
            code_with_vars = self._substitute_variables(self.code_block, step_vars)

            if self.code_type == "python":
                # Execute Python code
                local_vars = {}
                # Add step variables to local_vars
                local_vars["rhyl"] = step_vars
                exec(code_with_vars, globals(), local_vars)
                self.code_result = local_vars
            elif self.code_type == "shell":
                # Execute shell command with variable substitution
                result = subprocess.run(
                    code_with_vars, shell=True, capture_output=True, text=True
                )
                self.code_result = {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }
            self.code_executed = True
        except Exception as e:
            self.code_error = str(e)
            self.code_executed = True

    def _substitute_variables(self, code: str, step_vars: StepVariables) -> str:
        """
        Substitute variables in the code with their values.

        Args:
            code: The code to substitute variables in
            step_vars: The step variables object

        Returns:
            The code with variables substituted
        """
        # Find all {rhyl.variable} patterns
        pattern = r"\{rhyl\.([a-zA-Z0-9_]+)\}"

        def replace_var(match):
            var_name = match.group(1)
            if hasattr(step_vars, var_name):
                return str(getattr(step_vars, var_name))
            return match.group(0)  # Return the original if not found

        # Replace all matches
        return re.sub(pattern, replace_var, code)

    def complete(self, current_time: float) -> None:
        """Complete the step."""
        self.status = StepStatus.COMPLETED
        self.end_time = current_time

    def abort(self, current_time: float) -> None:
        """Abort the step."""
        if self.status != StepStatus.RUNNING:
            return

        self.status = StepStatus.ABORTED
        self.end_time = current_time

    def can_be_aborted(self) -> bool:
        """Check if the step can be aborted."""
        return self.status == StepStatus.RUNNING

    def is_ready_to_start(
        self,
        completed_steps: Set[str],
        program_start_time: float,
        current_time: float,
        aborted_steps: Optional[Set[str]] = None,
    ) -> bool:
        """Check if the step is ready to start based on its start trigger(s)."""
        if aborted_steps is None:
            aborted_steps = set()

        if (
            self.status != StepStatus.PENDING
            and self.status != StepStatus.WAITING_FOR_MANUAL
        ):
            return False

        if self.status == StepStatus.WAITING_FOR_MANUAL:
            return True

        # Handle multiple triggers
        if self.start_triggers is not None and self.start_trigger_logic is not None:
            trigger_results = []

            for trigger in self.start_triggers:
                trigger_ready = self._evaluate_single_trigger(
                    trigger,
                    completed_steps,
                    program_start_time,
                    current_time,
                    aborted_steps,
                )
                trigger_results.append(trigger_ready)

            if self.start_trigger_logic == "all":
                return all(trigger_results)  # ALL triggers must be satisfied
            elif self.start_trigger_logic == "any":
                return any(trigger_results)  # ANY trigger can start the step
            else:
                return False  # Unknown logic

        # Handle single trigger (backward compatibility)
        elif self.start_trigger is not None:
            return self._evaluate_single_trigger(
                self.start_trigger,
                completed_steps,
                program_start_time,
                current_time,
                aborted_steps,
            )

        return False

    def _evaluate_single_trigger(
        self,
        trigger: Dict[str, Any],
        completed_steps: Set[str],
        program_start_time: float,
        current_time: float,
        aborted_steps: Set[str],
    ) -> bool:
        """Evaluate a single trigger condition."""
        start_trigger_type = trigger.get("type")

        if start_trigger_type == "programStart":
            return True
        elif start_trigger_type == "programStartOffset":
            # Parse offset with flexible time format
            offset_value = trigger.get("offsetSeconds", 0)
            offset_seconds = parse_time_string(offset_value)
            return (current_time - program_start_time) >= offset_seconds
        elif start_trigger_type == "afterStep":
            ref_step_id = trigger.get("stepId")
            return ref_step_id in completed_steps
        elif start_trigger_type == "afterStepWithBuffer":
            ref_step_id = trigger.get("stepId")
            # Parse buffer with flexible time format
            buffer_value = trigger.get("bufferSeconds", 0)
            buffer_seconds = parse_time_string(buffer_value)

            # Check if the referenced step is completed and the buffer time has passed
            if ref_step_id in completed_steps:
                return True
            return False
        elif start_trigger_type == "manual":
            return False
        elif start_trigger_type == "onAbort":
            ref_step_id = trigger.get("stepId")
            return ref_step_id in aborted_steps

        return False

    def is_ready_to_complete(self, current_time: float) -> bool:
        """Check if the step is ready to complete based on its duration."""
        if self.status != StepStatus.RUNNING:
            return False

        if self.duration_type == "fixed":
            if self.expected_end_time is None:
                return False
            # Add small epsilon for floating point precision
            # If we're within 0.05 seconds of completion, complete it
            return current_time >= (self.expected_end_time - 0.05)
        elif self.duration_type == "variable":
            # For variable duration, we check if we've reached the minimum duration
            if self.start_time is None or self.min_seconds is None:
                return False
            return current_time >= (self.start_time + self.min_seconds)
        elif self.duration_type == "indefinite":
            # Indefinite steps are never automatically completed
            return False

        return False

    def must_complete(self, current_time: float) -> bool:
        """Check if the step must be completed (reached max duration)."""
        if self.status != StepStatus.RUNNING:
            return False

        if self.duration_type == "variable":
            return current_time >= (self.start_time + self.max_seconds)

        return False

    def get_progress(self, current_time: float) -> float:
        """Get the progress of the step as a percentage (0-100)."""
        if self.status == StepStatus.PENDING:
            return 0.0
        elif self.status == StepStatus.COMPLETED:
            return 100.0
        elif self.status == StepStatus.RUNNING:
            if self.duration_type == "indefinite":
                # For indefinite steps, we don't show progress
                return -1.0

            # Defensive checks
            if self.start_time is None:
                logging.warning(
                    f"Step {self.step_id} is RUNNING but start_time is None!"
                )
                return 0.0

            elapsed = current_time - self.start_time
            if self.duration_type == "fixed":
                if self.duration_seconds is None or self.duration_seconds == 0:
                    logging.warning(
                        f"Step {self.step_id} has invalid duration_seconds: {self.duration_seconds}"
                    )
                    return 0.0
                progress = (elapsed / self.duration_seconds) * 100.0
                # Debug: Store values for display
                self._debug_elapsed = elapsed
                self._debug_duration = self.duration_seconds
                self._debug_progress = progress
                return min(100.0, progress)
            elif self.duration_type == "variable":
                if self.default_seconds is None or self.default_seconds == 0:
                    logging.warning(
                        f"Step {self.step_id} has invalid default_seconds: {self.default_seconds}"
                    )
                    return 0.0
                return min(100.0, (elapsed / self.default_seconds) * 100.0)

        return 0.0

    def get_remaining_time(self, current_time: float) -> Optional[float]:
        """Get the remaining time in seconds."""
        if self.status != StepStatus.RUNNING:
            return None

        if self.expected_end_time is None:
            logging.warning(
                f"Step {self.step_id} is RUNNING but expected_end_time is None!"
            )
            return None

        return max(0, self.expected_end_time - current_time)

    def set_waiting_for_manual(self) -> None:
        """Set the step as waiting for manual trigger."""
        self.status = StepStatus.WAITING_FOR_MANUAL

    def has_manual_trigger(self) -> bool:
        """Check if this step has any manual triggers."""
        # Handle multiple triggers
        if self.start_triggers is not None:
            return any(
                trigger.get("type") == "manual" for trigger in self.start_triggers
            )
        # Handle single trigger
        elif self.start_trigger is not None:
            return self.start_trigger.get("type") == "manual"
        return False


def parse_time_string(time_str: str) -> float:
    """
    Parse a time string with optional units (s, m, h) into seconds.

    Examples:
        "60" -> 60 (seconds)
        "60s" -> 60 (seconds)
        "5m" -> 300 (seconds)
        "1h" -> 3600 (seconds)
        "1h30m" -> 5400 (seconds)
        "1h30m10s" -> 5410 (seconds)

    Args:
        time_str: The time string to parse

    Returns:
        The time in seconds
    """
    if isinstance(time_str, (int, float)):
        return float(time_str)

    # If it's just a number, assume seconds
    if str(time_str).isdigit():
        return float(time_str)

    # Parse complex time strings like "1h20m30s"
    total_seconds = 0

    # Find all hour, minute, and second components
    hour_match = re.search(r"(\d+)h", str(time_str))
    if hour_match:
        total_seconds += int(hour_match.group(1)) * 3600

    minute_match = re.search(r"(\d+)m", str(time_str))
    if minute_match:
        total_seconds += int(minute_match.group(1)) * 60

    second_match = re.search(r"(\d+)s", str(time_str))
    if second_match:
        total_seconds += int(second_match.group(1))

    # If no units were found but it's not a pure digit, try to convert directly
    if total_seconds == 0 and not str(time_str).isdigit():
        try:
            return float(time_str)
        except ValueError:
            # If we can't parse it, return 0
            return 0

    return total_seconds


class ProgramRunner:
    """Class for running a program."""

    def __init__(
        self,
        program: Dict[str, Any],
        time_scale: float = 1.0,
        auto_start: bool = False,
        environment: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the program runner.

        Args:
            program: The program to run
            time_scale: The time scale factor (1.0 = real-time, 2.0 = 2x speed, etc.)
            auto_start: Whether to start the program automatically
            environment: Optional environment data to use instead of loading from file
        """
        # Expand replicates (and legacy batch_size) before processing
        from rhylthyme.expand_replicates import expand_replicates

        program = expand_replicates(program)

        self.program = program
        self.time_scale = time_scale
        self.auto_start = auto_start

        # Initialize resource constraints using environment loader
        self.resource_constraints = {}
        self.resource_usage = {}
        self.actor_requirements = {}  # Track how many actors each task requires
        self.qualified_actor_types = {}  # Track which actor types can perform each task
        self.actor_types = {}  # Available actor types and their counts
        self.actor_usage_by_type = {}  # Track usage by actor type

        # Load resource constraints (handles both embedded and environment-based)
        # If environment is provided directly, use it; otherwise load from program
        if environment is not None:
            # Merge provided environment resource constraints into the program temporarily
            temp_program = program.copy()
            if "resourceConstraints" in environment:
                temp_program["resourceConstraints"] = (
                    temp_program.get("resourceConstraints", [])
                    + environment["resourceConstraints"]
                )
            resource_constraints = load_resource_constraints(temp_program)
        else:
            resource_constraints = load_resource_constraints(program)
        for constraint in resource_constraints:
            task = constraint.get("task")
            max_concurrent = constraint.get("maxConcurrent", 1)
            actors_required = constraint.get(
                "actorsRequired", 1.0
            )  # Default to 1 actor if not specified
            qualified_types = constraint.get(
                "qualifiedActorTypes", []
            )  # Actor types that can perform this task
            if task:
                self.resource_constraints[task] = max_concurrent
                self.resource_usage[task] = 0.0
                self.actor_requirements[task] = actors_required
                self.qualified_actor_types[task] = qualified_types

        # Load actor types from environment if available
        environment_data = environment  # Use provided environment first
        if environment_data is None:
            environment_id = program.get("environment")
            if environment_id:
                from .environment_loader import EnvironmentLoader

                loader = EnvironmentLoader()
                environment_data = loader.get_environment(environment_id)

        if environment_data:
            # Handle new actorTypes format
            if "actorTypes" in environment_data:
                for actor_type_id, actor_info in environment_data["actorTypes"].items():
                    self.actor_types[actor_type_id] = {
                        "name": actor_info.get("name", actor_type_id),
                        "count": actor_info.get("count", 1),
                        "description": actor_info.get("description", ""),
                    }
                    self.actor_usage_by_type[actor_type_id] = 0.0
            # Handle legacy actors field for backward compatibility
            elif "actors" in environment_data:
                self.actor_types["generic"] = {
                    "name": "Generic Actor",
                    "count": environment_data["actors"],
                    "description": "Generic actor type for backward compatibility",
                }
                self.actor_usage_by_type["generic"] = 0.0
                # Update qualified actor types to use generic if empty
                for task in self.qualified_actor_types:
                    if not self.qualified_actor_types[task]:
                        self.qualified_actor_types[task] = ["generic"]
        else:
            # Fall back to program-level actors if no environment
            actor_count = program.get("actors", 1)
            self.actor_types["generic"] = {
                "name": "Generic Actor",
                "count": actor_count,
                "description": "Generic actor type from program definition",
            }
            self.actor_usage_by_type["generic"] = 0.0
            # Update qualified actor types to use generic if empty
            for task in self.qualified_actor_types:
                if not self.qualified_actor_types[task]:
                    self.qualified_actor_types[task] = ["generic"]

        # Calculate total actors available (for display purposes)
        self.actors_available = sum(info["count"] for info in self.actor_types.values())
        self.actor_usage = 0.0  # Track total actor usage for display

        self.is_running = False
        self.program_start_time: Optional[float] = None
        self.current_time: float = 0.0
        self.status_message = "Program waiting for manual start. Press 's' to start."

        # Add program_started property for backward compatibility with tests
        self.program_started = False

        self.steps: Dict[str, Step] = {}
        self.tracks: Dict[str, List[Step]] = {}
        self.manual_triggers: Dict[str, List[Step]] = {}
        self.completed_steps: Set[str] = set()
        self.aborted_steps: Set[str] = set()
        # self.resource_usage will be initialized below during resource constraints setup
        self.sort_mode = SortMode.DEFAULT
        self.selected_step_index = 0
        self.running_steps: List[str] = []  # Initialize running_steps as an empty list
        self.manually_triggered_steps: Set[str] = (
            set()
        )  # Initialize manually_triggered_steps as an empty set
        self.event_listeners: List[Any] = []  # Add event listeners list

        # --- predicted offsets (metadata.offsetsUse, program schema 0.3.0-alpha)
        #
        # A negative offset ("peel the potatoes 45 min before the roast is
        # done") is resolved against the anchor's PROJECTED end, because the
        # runner cannot see a future end. The projection is normally the
        # author's own number (defaultSeconds for a variable or indefinite
        # step, the duration of a fixed one). With
        # ``metadata.offsetsUse: "predicted"`` the projection of an indefinite
        # anchor may instead come from run history — see
        # ``set_predictions`` and ``anchor_projected_seconds``.
        metadata = program.get("metadata")
        self.offsets_use = "planned"
        if isinstance(metadata, dict) and metadata.get("offsetsUse") == "predicted":
            self.offsets_use = "predicted"
        # {anchor stepId: seconds} actually used to project an anchor's end,
        # and {gated stepId: seconds} for the record.
        self.predictions: Dict[str, Dict[str, Any]] = {}
        self.predicted_anchor_seconds: Dict[str, float] = {}

        # Pause state: while paused the program clock does not advance
        self.is_paused = False
        self._pause_started_wall: Optional[float] = None
        self.paused_wall_seconds = 0.0

        self.command_queue: queue.Queue[str] = queue.Queue()
        self.failed_steps: List[str] = []
        self.program_abort_reason: Optional[str] = None
        # Commands from the web bridge, by id: {"accepted": bool, "reason": str}.
        # Written on the runner thread, read by the bridge's thread.
        self.remote_outcomes: Dict[str, Dict[str, Any]] = {}
        self._abort_armed_at: Optional[float] = None
        # Instrument replies, posted from executor threads by
        # post_instrument_reply() and applied in process_commands().
        self.instrument_replies: queue.Queue = queue.Queue()
        # Safe stops sent for a failed or aborted step and not yet answered,
        # by step id; a step is not retried while its tools are being stopped.
        self.safe_stops_pending: Dict[str, int] = {}

        # Initialize tracks - will be populated later during step processing
        tracks = program.get("tracks", [])
        for track_data in tracks:
            track_id = track_data.get("trackId")
            if not track_id:
                continue
            # Initialize track as empty list of steps
            self.tracks[track_id] = []

        # Get the actors count (default to 1 if not specified)
        self.actors = max(1, program.get("actors", 1))

        # Collect all task types used in the program
        used_task_types = set()
        for track in program.get("tracks", []):
            for step_data in track.get("steps", []):
                task_type = step_data.get("task")
                if task_type:
                    used_task_types.add(task_type)

        # Initialize resource constraints
        for constraint in program.get("resourceConstraints", []):
            task_type = constraint.get("task")
            max_concurrent = constraint.get("maxConcurrent", 1)
            self.resource_constraints[task_type] = max_concurrent
            self.resource_usage[task_type] = 0

        # Add default constraints for tasks that don't have explicit constraints
        # These will be limited by the actors count
        for task_type in used_task_types:
            if task_type not in self.resource_constraints:
                self.resource_constraints[task_type] = self.actors
                self.resource_usage[task_type] = 0
            # Ensure all task types have qualified actor types
            if task_type not in self.qualified_actor_types:
                self.qualified_actor_types[task_type] = []

        # Assign generic actor type to any task types with empty qualified lists
        if self.actor_types:
            for task in self.qualified_actor_types:
                if not self.qualified_actor_types[task]:
                    self.qualified_actor_types[task] = list(self.actor_types.keys())

        # Initialize steps (replicates/batch_size already expanded)
        for track in program.get("tracks", []):
            track_id = track.get("trackId")
            track_steps = []

            for step_data in track.get("steps", []):
                step_id = step_data.get("stepId")

                step = Step(step_data, track_id)
                self.steps[step_id] = step
                track_steps.append(step)

                # Register manual triggers from duration triggerName
                if step.manual_trigger_name:
                    if step.manual_trigger_name not in self.manual_triggers:
                        self.manual_triggers[step.manual_trigger_name] = []
                    self.manual_triggers[step.manual_trigger_name].append(step)

                # Register manual start triggers from startTrigger.triggerName
                if step.has_manual_trigger():
                    start_trig = step.start_trigger or {}
                    start_trigger_name = start_trig.get("triggerName")
                    if start_trigger_name:
                        step.manual_start_trigger_name = start_trigger_name
                        if start_trigger_name not in self.manual_triggers:
                            self.manual_triggers[start_trigger_name] = []
                        if step not in self.manual_triggers[start_trigger_name]:
                            self.manual_triggers[start_trigger_name].append(step)
                    elif not step.manual_trigger_name:
                        fallback_name = f"start-{step.step_id}"
                        step.manual_start_trigger_name = fallback_name
                        if fallback_name not in self.manual_triggers:
                            self.manual_triggers[fallback_name] = []
                        self.manual_triggers[fallback_name].append(step)

            self.tracks[track_id] = track_steps

        self._add_instrument_tool_resources(resource_constraints)

        # ---- replicate instance grouping -------------------------------
        # Instances of one replicated step (``instanceOf``) collapse into a
        # single row in the step list; sub-tracks created for instances
        # (``parentTrackId``) display under their parent track's name.
        self.track_parents: Dict[str, Optional[str]] = {}
        for track_data in program.get("tracks", []):
            tid = track_data.get("trackId")
            if tid:
                self.track_parents[tid] = track_data.get("parentTrackId")
        self.instance_groups: Dict[str, List[Step]] = {}
        for step in self.steps.values():
            if step.instance_of:
                self.instance_groups.setdefault(step.instance_of, []).append(step)
        for members in self.instance_groups.values():
            members.sort(key=lambda s: (s.instance_index or 0, s.step_id))
        self.instance_counts: Dict[str, int] = {
            key: len(members) for key, members in self.instance_groups.items()
        }
        # Grouped rows are on by default and start collapsed; 'g' toggles the
        # selected group (and turns grouping off entirely on a plain row).
        self.group_instances = True
        self.expanded_groups: Set[str] = set()

    def _add_instrument_tool_resources(self, declared: List[Dict[str, Any]]) -> None:
        """
        Every instrument tool is a resource, so two steps never command the
        same tool at once (a step holds every tool its actions name):
        capacity 1 unless the program declares a constraint named after the
        tool. The instrument does the work, so the tool needs no actor unless
        that constraint says actorsRequired.
        """
        declared_by_task = {str(c["task"]): c for c in declared if c.get("task")}
        self.implicit_tool_resources: Set[str] = set()
        for step in self.steps.values():
            for tool in step_tools(step.instrument or {}):
                self._add_tool_resource(step, tool, declared_by_task)

    def _add_tool_resource(
        self, step: "Step", tool: str, declared_by_task: Dict[str, Any]
    ) -> None:
        if tool not in self.resource_constraints:
            self.resource_constraints[tool] = 1
            self.resource_usage[tool] = 0.0
            self.actor_requirements[tool] = 0.0
            self.qualified_actor_types[tool] = []
            self.implicit_tool_resources.add(tool)
        elif "actorsRequired" not in declared_by_task.get(tool, {}):
            self.actor_requirements[tool] = 0.0
        if tool not in step.task_types:
            step.task_types.append(tool)
            step.task_fractions[tool] = 1.0

    def start(self) -> None:
        """Start the program execution."""
        self.program_start_time = time.time()
        self.current_time = self.program_start_time
        self.is_running = True
        self.program_started = True

        # If auto_start is False, set the program to wait for manual start
        if not self.auto_start:
            # The clock is running from here (is_running stays True), so say
            # so: instrument steps may already be sending commands.
            self.status_message = "Program running. Press 'p' to pause, 'q' to quit."
            self.emit_event(
                "program_started",
                {"time": self.program_start_time, "wall_time": time.time()},
            )
            return

        # Process the program start trigger
        start_trigger = self.program.get("startTrigger", {})
        trigger_type = start_trigger.get("type", "manual")

        if trigger_type == "absolute":
            # For absolute time, we would wait until the specified time
            # For simplicity, we'll just start immediately in this example
            self.status_message = "Program started with absolute time trigger."
        elif trigger_type == "offset":
            # For offset, we would wait for the specified offset
            # For simplicity, we'll just start immediately in this example
            self.status_message = "Program started with offset trigger."
        elif trigger_type == "manual":
            # For manual, we wait for user input
            self.status_message = (
                "Program waiting for manual start. Press 's' to start."
            )
            self.is_running = False

        if self.is_running:
            self.emit_event(
                "program_started",
                {"time": self.program_start_time, "wall_time": time.time()},
            )

    def update(self) -> None:
        """Update the program state."""
        # Always process commands first, even if not running
        # (this allows starting the program via 's' key)
        self.process_commands()

        if not self.is_running:
            return

        # Update current time (wall time minus any time spent paused)
        if self.program_start_time is not None:
            now = time.time()
            paused = self.paused_wall_seconds
            if self.is_paused and self._pause_started_wall is not None:
                paused += now - self._pause_started_wall
            real_elapsed = now - self.program_start_time - paused
            self.current_time = self.program_start_time + (
                real_elapsed * self.time_scale
            )

        # While paused the clock is frozen: no automatic transitions
        if self.is_paused:
            return

        # Start steps that are ready
        self.start_ready_steps(self.current_time)

        # Complete steps that are finished
        self.complete_finished_steps()

        # Check if all steps are completed
        if all(step.status == StepStatus.COMPLETED for step in self.steps.values()):
            self.is_running = False
            self.status_message = "Program execution completed."

    def post_instrument_reply(self, step_id: str, reply: Dict[str, Any]) -> None:
        """
        Hand an instrument's reply to the runner. Thread-safe; applied on the
        next update().

        Args:
            step_id: The instrument step the reply is for
            reply: {"ok": bool, "code": str, "errorMessage": str, "metadata": dict}
        """
        self.instrument_replies.put((step_id, reply))

    def apply_instrument_replies(self) -> None:
        """Apply every instrument reply posted so far (on the runner thread)."""
        while not self.instrument_replies.empty():
            try:
                step_id, reply = self.instrument_replies.get_nowait()
            except queue.Empty:
                break
            self._apply_instrument_reply(step_id, reply)

    FAILED_PROMPT = (
        "Paused new steps: an instrument command failed. "
        "r retries it, x marks it done, A (twice) aborts the program."
    )

    def note_safe_stops(self, step_id: str, count: int) -> None:
        """``count`` safe stops are being sent for ``step_id`` (runner thread)."""
        self.safe_stops_pending[step_id] = (
            self.safe_stops_pending.get(step_id, 0) + count
        )
        step = self.steps.get(step_id)
        if step is not None and step.status == StepStatus.FAILED:
            self.status_message = (
                f"{step_id} failed: stopping its instruments safely "
                "before you retry, skip or abort..."
            )

    def _apply_instrument_reply(self, step_id: str, reply: Dict[str, Any]) -> None:
        """
        A reply's ``phase`` says what it answers: ``start`` actions fail the
        step when they fail; ``call`` and ``until`` (the default) end it;
        ``end``, ``onAbort``, ``pause`` and ``resume`` replies are only
        recorded (and a failed one reported).
        """
        step = self.steps.get(step_id)
        phase = reply.get("phase") or "call"
        if step is not None and phase in ("end", "onAbort", "pause", "resume"):
            self.emit_event(
                "instrument_reply",
                {"step_id": step_id, "time": self.current_time, **reply},
            )
            if phase == "onAbort":
                left = self.safe_stops_pending.get(step_id, 1) - 1
                if left > 0:
                    self.safe_stops_pending[step_id] = left
                else:
                    self.safe_stops_pending.pop(step_id, None)
                    if step.status == StepStatus.FAILED and reply.get("ok"):
                        self.status_message = (
                            f"Instruments stopped. {self.FAILED_PROMPT}"
                        )
            if not reply.get("ok"):
                self.status_message = (
                    f"{step_id}: {phase} action {reply.get('tool')}."
                    f"{reply.get('command')} failed: {reply.get('code')} "
                    f"{reply.get('errorMessage') or ''}".rstrip()
                )
                logging.error(self.status_message)
            return
        if step is None or step.status != StepStatus.RUNNING:
            # Ended some other way first (aborted, completed by hand)
            logging.info(f"Ignoring instrument reply for {step_id}: not running")
            return
        if phase != "start":
            step.instrument_reply = reply
        self.emit_event(
            "instrument_reply",
            {"step_id": step_id, "time": self.current_time, **reply},
        )
        if phase == "start":
            if not reply.get("ok"):
                self.fail_step(step, reply)
            return
        if reply.get("ok"):
            self.complete_step(step, self.current_time, ended_by="instrument")
            return
        self.fail_step(step, reply)

    def fail_step(self, step: Step, reply: Dict[str, Any]) -> None:
        """
        Mark an instrument step FAILED. It keeps its resources, nothing new
        starts, and running steps carry on until the operator decides:
        retry_failed_step, skip_failed_step or abort_program.
        """
        instrument = step.instrument or {}
        main = blocking_action(instrument) or {}
        step.status = StepStatus.FAILED
        step.failure = {
            "tool": reply.get("tool") or instrument.get("tool"),
            "command": reply.get("command") or main.get("command"),
            "code": reply.get("code"),
            "errorMessage": reply.get("errorMessage") or "",
        }
        if step.step_id in self.running_steps:
            self.running_steps.remove(step.step_id)
        if step.step_id not in self.failed_steps:
            self.failed_steps.append(step.step_id)
        self.status_message = self.FAILED_PROMPT
        logging.error(f"Step {step.step_id} failed: {self.failure_text(step)}")
        self.emit_event(
            "step_failed",
            {"step_id": step.step_id, "time": self.current_time, **step.failure},
        )

    def failure_text(self, step: Step) -> str:
        f = step.failure or {}
        text = f"{step.step_id}: {f.get('tool')}.{f.get('command')} {f.get('code')}"
        return text + (f" ({f['errorMessage']})" if f.get("errorMessage") else "")

    def _failed_step(self, step_id: Optional[str]) -> Optional[Step]:
        if step_id is None and self.failed_steps:
            step_id = self.failed_steps[0]
        step = self.steps.get(step_id) if step_id else None
        return step if step is not None and step.status == StepStatus.FAILED else None

    def retry_failed_step(self, step_id: Optional[str] = None) -> bool:
        """Resend a failed step's instrument command (default: first failure)."""
        step = self._failed_step(step_id)
        if step is None or not step.instrument:
            self.status_message = "No failed instrument step to retry."
            return False
        if self.safe_stops_pending.get(step.step_id):
            self.status_message = (
                f"{step.step_id}: its instruments are still being stopped; "
                "retry once the stops have replied."
            )
            return False
        step.status = StepStatus.RUNNING
        step.failure = None
        self.failed_steps.remove(step.step_id)
        self.running_steps.append(step.step_id)
        self.status_message = f"Retrying {step.step_id}..."
        self.emit_event(
            "step_retry",
            {
                "step_id": step.step_id,
                "time": self.current_time,
                "attempt": step.instrument_attempts + 1,
            },
        )
        return True

    def skip_failed_step(self, step_id: Optional[str] = None) -> bool:
        """Mark a failed step done by hand; its dependents may start."""
        step = self._failed_step(step_id)
        if step is None:
            self.status_message = "No failed step to skip."
            return False
        self.failed_steps.remove(step.step_id)
        step.failure = None
        self.complete_step(step, self.current_time, ended_by="skipped")
        self.status_message = f"Skipped {step.step_id} (marked done)."
        return True

    REMOTE_KINDS = ("pause", "resume", "retry", "skip", "abort")

    def apply_remote_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply a command from the web bridge (rhylthyme bridge), on the runner
        thread, if it makes sense right now. Returns {"accepted", "reason"}.
        The bridge has already checked who sent it and how old it is.
        """
        kind = command.get("kind")
        args = command.get("args") or {}
        started = self.program_started and self.program_start_time is not None
        live = started and self.is_running

        def no(reason: str) -> Dict[str, Any]:
            return {"accepted": False, "reason": reason}

        if kind not in self.REMOTE_KINDS:
            return no(f"unknown command {kind!r}")
        if kind == "pause":
            if not live:
                return no("the program is not running")
            if self.is_paused:
                return no("already paused")
            self.toggle_pause()
        elif kind == "resume":
            if not self.is_paused:
                return no("not paused")
            self.toggle_pause()
        elif kind in ("retry", "skip"):
            step_id = args.get("step_id")
            if step_id not in self.failed_steps:
                return no(f"step {step_id!r} has not failed")
            ok = (self.retry_failed_step if kind == "retry" else self.skip_failed_step)(
                step_id
            )
            if not ok:
                return no(self.status_message)
        elif kind == "abort":
            if not live:
                return no("the program is not running")
            reason = str(args.get("reason") or "Aborted from the web")[:200]
            self.abort_program(reason)
        self.status_message = (
            f"Remote: {kind}"
            + (f" {args.get('step_id')}" if args.get("step_id") else "")
            + " (from the web)"
        )
        return {"accepted": True, "reason": ""}

    def abort_program(self, reason: str = "Aborted by operator") -> None:
        """End the program: running and failed steps are aborted with reason."""
        for step in self.steps.values():
            if step.status in (StepStatus.RUNNING, StepStatus.FAILED):
                self.abort_step(step, self.current_time, reason=reason)
        self.failed_steps = []
        self.program_abort_reason = reason
        self.is_running = False
        self.status_message = f"Program aborted: {reason}"
        self.emit_event(
            "program_aborted", {"time": self.current_time, "reason": reason}
        )

    def process_commands(self) -> None:
        """Process commands from the command queue."""
        self.apply_instrument_replies()
        while not self.command_queue.empty():
            try:
                command = self.command_queue.get_nowait()
                if command == "start_program":
                    was_running = self.is_running
                    self.is_running = True
                    self.program_started = True
                    # Initialize program start time if not already set; if the
                    # program was waiting for 's' and nothing has run yet,
                    # re-anchor the clock so waiting time is not counted
                    nothing_ran = not self.running_steps and not self.completed_steps
                    if self.program_start_time is None or (
                        not was_running and nothing_ran
                    ):
                        self.program_start_time = time.time()
                        self.current_time = self.program_start_time
                    self.status_message = "Program started manually."
                    if not was_running:
                        self.emit_event(
                            "program_started",
                            {"time": self.program_start_time, "wall_time": time.time()},
                        )
                elif command.startswith("trigger:"):
                    parts = command.split(":", 2)
                    if len(parts) == 3:
                        trigger_name, step_id = parts[1], parts[2]
                        self.trigger_manual_step(trigger_name, step_id)
                    else:
                        trigger_name = parts[1]
                        self.trigger_manual_step(trigger_name)
                elif command.startswith("remote:"):
                    remote = json.loads(command[len("remote:") :])
                    outcome = self.apply_remote_command(remote)
                    self.remote_outcomes[str(remote.get("id"))] = outcome
                    self.emit_event(
                        "remote_command",
                        {
                            "time": self.current_time,
                            "id": remote.get("id"),
                            "kind": remote.get("kind"),
                            "args": remote.get("args") or {},
                            **outcome,
                        },
                    )
                elif command.startswith("retry:"):
                    self.retry_failed_step(command.split(":", 1)[1] or None)
                elif command.startswith("skip:"):
                    self.skip_failed_step(command.split(":", 1)[1] or None)
                elif command.startswith("abort_program"):
                    reason = command.split(":", 1)[1] if ":" in command else ""
                    self.abort_program(reason or "Aborted by operator")
                elif command.startswith("abort:"):
                    step_id = command.split(":", 1)[1]
                    if step_id in self.steps and self.steps[step_id].can_be_aborted():
                        self.abort_step(self.steps[step_id], self.current_time)
            except queue.Empty:
                break

    def toggle_pause(self) -> bool:
        """
        Pause or resume the program clock. Returns the new paused state.

        While paused, current_time stops advancing, no steps start or
        complete automatically, and the paused wall-clock time is excluded
        from the program clock once resumed.
        """
        if self.program_start_time is None or not self.program_started:
            self.status_message = "Nothing to pause: the program has not started."
            return self.is_paused
        now = time.time()
        if self.is_paused:
            paused_for = 0.0
            if self._pause_started_wall is not None:
                paused_for = now - self._pause_started_wall
                self.paused_wall_seconds += paused_for
            self._pause_started_wall = None
            self.is_paused = False
            self.status_message = f"Resumed after {paused_for:.1f}s paused."
            self.emit_event(
                "program_resumed",
                {
                    "time": self.current_time,
                    "wall_time": now,
                    "paused_seconds": paused_for,
                },
            )
        else:
            self._pause_started_wall = now
            self.is_paused = True
            self.status_message = "PAUSED - press 'p' to resume."
            self.emit_event(
                "program_paused", {"time": self.current_time, "wall_time": now}
            )
        return self.is_paused

    def start_ready_steps(self, current_time: float) -> None:
        """Start steps that are ready to start."""
        # A failed instrument step holds the program: nothing new starts
        # until the operator retries, skips or aborts (running steps go on).
        if self.failed_steps:
            return
        # Sort steps by priority (lower number = higher priority)
        # Include WAITING_FOR_MANUAL steps so they can be started after user triggers them
        pending_steps = [
            step
            for step in self.steps.values()
            if step.status in (StepStatus.PENDING, StepStatus.WAITING_FOR_MANUAL)
        ]
        pending_steps.sort(key=lambda s: s.priority)

        for step in pending_steps:
            if self.is_step_ready_to_start(step, current_time):
                if step.trigger_fired_time is None:
                    step.trigger_fired_time = current_time
                # Check resource constraints and actor availability for each task type
                can_start = True
                required_actors_by_type: Dict[str, float] = (
                    {}
                )  # Track how many actors of each type are needed

                for task_type in step.task_types:
                    if task_type in self.resource_constraints:
                        max_concurrent = self.resource_constraints[task_type]
                        fraction = step.task_fractions.get(task_type, 1.0)
                        current_usage = self.resource_usage.get(task_type, 0.0)

                        # Check if adding this step's fractional usage would exceed the constraint
                        if current_usage + fraction > max_concurrent:
                            can_start = False
                            break

                        # Check actor constraints
                        actors_required = (
                            self.actor_requirements.get(task_type, 1.0) * fraction
                        )
                        if actors_required <= 0:
                            continue  # e.g. an instrument tool: no person needed
                        qualified_types = self.qualified_actor_types.get(task_type, [])

                        if not qualified_types:
                            # If no qualified types specified, can't run the task
                            can_start = False
                            break

                        # Find the best actor type to assign (one with lowest current usage)
                        best_actor_type = None
                        best_available_capacity = 0

                        for actor_type in qualified_types:
                            if actor_type not in self.actor_types:
                                continue

                            total_capacity = self.actor_types[actor_type]["count"]
                            current_usage = self.actor_usage_by_type.get(
                                actor_type, 0.0
                            )
                            pending_usage = required_actors_by_type.get(actor_type, 0.0)
                            available_capacity = (
                                total_capacity - current_usage - pending_usage
                            )

                            if (
                                available_capacity >= actors_required
                                and available_capacity > best_available_capacity
                            ):
                                best_actor_type = actor_type
                                best_available_capacity = available_capacity

                        if best_actor_type is None:
                            # No qualified actor type has enough capacity
                            can_start = False
                            break

                        # Reserve the actors
                        if best_actor_type not in required_actors_by_type:
                            required_actors_by_type[best_actor_type] = 0.0
                        required_actors_by_type[best_actor_type] += actors_required

                if can_start:
                    # Update resource usage for each task type
                    for task_type in step.task_types:
                        if task_type in self.resource_constraints:
                            fraction = step.task_fractions.get(task_type, 1.0)
                            self.resource_usage[task_type] = (
                                self.resource_usage.get(task_type, 0.0) + fraction
                            )

                    # Update actor usage by type
                    total_actor_usage = 0.0
                    for actor_type, usage in required_actors_by_type.items():
                        self.actor_usage_by_type[actor_type] = (
                            self.actor_usage_by_type.get(actor_type, 0.0) + usage
                        )
                        total_actor_usage += usage

                    # Update total actor usage for display
                    self.actor_usage += total_actor_usage

                    step.start(current_time)

                    # Set expected end time for fixed duration steps
                    if (
                        step.duration_type == DurationType.FIXED
                        and step.duration_seconds is not None
                    ):
                        step.expected_end_time = current_time + step.duration_seconds
                    elif (
                        step.duration_type == DurationType.VARIABLE
                        and step.default_seconds is not None
                    ):
                        step.expected_end_time = current_time + step.default_seconds
                    elif (
                        step.duration_type == DurationType.INDEFINITE
                        and step.default_seconds is not None
                    ):
                        step.expected_end_time = current_time + step.default_seconds

                    # Execute code block if present
                    if step.has_code:
                        self.execute_code_block(step)

                    # Add to running steps
                    self.running_steps.append(step.step_id)

                    # Log the step start
                    logging.info(f"Started step {step.step_id}: {step.name}")

                    # Emit event
                    self.emit_event(
                        "step_started",
                        {
                            "step_id": step.step_id,
                            "time": current_time,
                            "trigger_fired_at": step.trigger_fired_time,
                        },
                    )

    def complete_finished_steps(self) -> None:
        """
        Complete steps whose timer has expired.

        Fixed steps end when their duration elapses; variable steps are forced
        to end at maxSeconds (between minSeconds and maxSeconds only the
        executor ends them); indefinite steps never end on their own.
        """
        for step in self.steps.values():
            if step.status != StepStatus.RUNNING or step.instrument_blocking:
                continue
            if step.duration_type == DurationType.FIXED:
                if step.is_ready_to_complete(self.current_time):
                    self.complete_step(step, self.current_time, ended_by="timer")
                    continue
                # Aggressive completion: if remaining time displays as "< 0.1s", force complete
                remaining = step.get_remaining_time(self.current_time)
                if remaining is not None and remaining < 0.1:
                    logging.info(
                        f"Force completing step {step.step_id} with {remaining:.3f}s remaining (< 0.1s threshold)"
                    )
                    self.complete_step(step, self.current_time, ended_by="timer")
            elif step.duration_type == DurationType.VARIABLE:
                if step.must_complete(self.current_time):
                    self.complete_step(step, self.current_time, ended_by="timer")

    def complete_step(
        self, step: Step, current_time: float, ended_by: str = "executor"
    ) -> None:
        """
        Complete a step.

        Args:
            step: The step to complete
            current_time: The current time
            ended_by: "timer" when the planned duration expired, "executor"
                when a person ended it (manual trigger, 'c' key, direct call),
                "instrument" when its instrument replied, "skipped" when the
                operator marked a failed instrument step done
        """
        step.status = StepStatus.COMPLETED
        step.end_time = current_time
        step.progress = 1.0

        # Calculate actor usage to free by type
        freed_actors_by_type = {}

        # Decrement resource usage for each task type
        for task_type in step.task_types:
            if task_type in self.resource_constraints:
                fraction = step.task_fractions.get(task_type, 1.0)
                current_usage = self.resource_usage.get(task_type, 0.0)
                # Ensure we don't go below zero due to rounding errors
                self.resource_usage[task_type] = max(0.0, current_usage - fraction)

                # Calculate actor usage for this task
                actors_required = self.actor_requirements.get(task_type, 1.0) * fraction
                qualified_types = self.qualified_actor_types.get(task_type, [])

                # Find which actor type was actually used (choose the one with highest usage)
                best_actor_type = None
                best_usage = 0.0

                for actor_type in qualified_types:
                    if actor_type in self.actor_usage_by_type:
                        current_usage = self.actor_usage_by_type[actor_type]
                        if current_usage > best_usage:
                            best_actor_type = actor_type
                            best_usage = current_usage

                if best_actor_type:
                    if best_actor_type not in freed_actors_by_type:
                        freed_actors_by_type[best_actor_type] = 0.0
                    freed_actors_by_type[best_actor_type] += actors_required

        # Free actor usage by type
        total_freed = 0.0
        for actor_type, usage in freed_actors_by_type.items():
            current_usage = self.actor_usage_by_type.get(actor_type, 0.0)
            self.actor_usage_by_type[actor_type] = max(0.0, current_usage - usage)
            total_freed += usage

        # Update total actor usage for display
        self.actor_usage = max(0.0, self.actor_usage - total_freed)

        # Remove from running steps
        if step.step_id in self.running_steps:
            self.running_steps.remove(step.step_id)

        # Log the step completion
        logging.info(f"Completed step {step.step_id}: {step.name}")

        # Emit event
        self.emit_event(
            "step_completed",
            {"step_id": step.step_id, "time": current_time, "ended_by": ended_by},
        )

    def trigger_manual_step(
        self, trigger_name: str, step_id: Optional[str] = None
    ) -> None:
        """
        Trigger a manual step.

        Args:
            trigger_name: The name of the trigger
            step_id: Optional specific step ID to trigger (if multiple steps have the same trigger name)
        """
        if trigger_name == "start_program":
            self.is_running = True
            self.program_started = True
            self.program_start_time = self.current_time
            self.status_message = "Program started."
            self.emit_event(
                "program_started",
                {"time": self.program_start_time, "wall_time": time.time()},
            )
            return

        if trigger_name not in self.manual_triggers:
            self.status_message = f"Unknown trigger: {trigger_name}"
            return

        # If a specific step_id is provided, only trigger that step
        if step_id:
            for step in self.manual_triggers[trigger_name]:
                if step.step_id == step_id:
                    self._trigger_step(step)
                    return
            self.status_message = (
                f"Step with ID {step_id} not found for trigger {trigger_name}"
            )
            return

        # Otherwise, trigger all steps with this trigger name
        for step in self.manual_triggers[trigger_name]:
            self._trigger_step(step)

    def _trigger_step(self, step: Step) -> None:
        """
        Trigger a specific step.

        Args:
            step: The step to trigger
        """
        if step.status == StepStatus.PENDING and step.has_manual_trigger():
            step.set_waiting_for_manual()
            self.status_message = f"Step {step.name} is now waiting to start."
        elif step.status == StepStatus.RUNNING:
            # Check if step has a completable duration trigger first
            if (
                step.duration_type == DurationType.VARIABLE
                or step.duration_type == DurationType.INDEFINITE
            ):
                self.complete_step(step, self.current_time)
                self.status_message = f"Manually completed step: {step.name}"
            elif step.can_be_aborted():
                self.abort_step(step, self.current_time)
                self.status_message = f"Aborted step: {step.name}"

    def abort_step(
        self, step: Step, current_time: float, reason: str = "Aborted"
    ) -> None:
        """
        Abort a step.

        Args:
            step: The step to abort
            current_time: The current time
            reason: The reason for aborting the step
        """
        step.status = StepStatus.ABORTED
        step.end_time = current_time
        step.abort_reason = reason
        if step.step_id in self.failed_steps:
            self.failed_steps.remove(step.step_id)

        # Calculate actor usage to free by type
        freed_actors_by_type = {}

        # Decrement resource usage for each task type
        for task_type in step.task_types:
            if task_type in self.resource_constraints:
                fraction = step.task_fractions.get(task_type, 1.0)
                current_usage = self.resource_usage.get(task_type, 0.0)
                # Ensure we don't go below zero due to rounding errors
                self.resource_usage[task_type] = max(0.0, current_usage - fraction)

                # Calculate actor usage for this task
                actors_required = self.actor_requirements.get(task_type, 1.0) * fraction
                qualified_types = self.qualified_actor_types.get(task_type, [])

                # Find which actor type was actually used (choose the one with highest usage)
                best_actor_type = None
                best_usage = 0.0

                for actor_type in qualified_types:
                    if actor_type in self.actor_usage_by_type:
                        current_usage = self.actor_usage_by_type[actor_type]
                        if current_usage > best_usage:
                            best_actor_type = actor_type
                            best_usage = current_usage

                if best_actor_type:
                    if best_actor_type not in freed_actors_by_type:
                        freed_actors_by_type[best_actor_type] = 0.0
                    freed_actors_by_type[best_actor_type] += actors_required

        # Free actor usage by type
        total_freed = 0.0
        for actor_type, usage in freed_actors_by_type.items():
            current_usage = self.actor_usage_by_type.get(actor_type, 0.0)
            self.actor_usage_by_type[actor_type] = max(0.0, current_usage - usage)
            total_freed += usage

        # Update total actor usage for display
        self.actor_usage = max(0.0, self.actor_usage - total_freed)

        # Remove from running steps
        if step.step_id in self.running_steps:
            self.running_steps.remove(step.step_id)

        # Add to aborted steps
        self.aborted_steps.add(step.step_id)

        # Log the step abortion
        logging.info(f"Aborted step {step.step_id}: {step.name} - Reason: {reason}")

        # Emit event
        self.emit_event(
            "step_aborted",
            {"step_id": step.step_id, "time": current_time, "reason": reason},
        )

    def get_available_triggers(self) -> List[Dict[str, Any]]:
        """
        Get a list of available manual triggers.

        Returns:
            List of dictionaries containing trigger information:
            {
                "id": Trigger ID (for selection),
                "name": Display name,
                "type": Trigger type (program, start, end),
                "step_id": Step ID (if applicable),
                "step_name": Step name (if applicable),
                "track_id": Track ID (if applicable)
            }
        """
        available_triggers: List[Dict[str, Any]] = []

        # Check for program start trigger
        if (
            not self.is_running
            and self.program.get("startTrigger", {}).get("type") == "manual"
        ):
            available_triggers.append(
                {"id": "start_program", "name": "Start Program", "type": "program"}
            )

        # Check for step triggers
        for trigger_name, steps in self.manual_triggers.items():
            for step in steps:
                if step.status == StepStatus.PENDING and step.has_manual_trigger():
                    available_triggers.append(
                        {
                            "id": f"start:{trigger_name}:{step.step_id}",
                            "name": f"Start: {self.instance_step_name(step)}",
                            "type": "start",
                            "step_id": step.step_id,
                            "step_name": self.instance_step_name(step),
                            "track_id": step.track_id,
                            "instance_of": step.instance_of,
                            "instance_index": step.instance_index,
                            "instance_count": self.instance_count(step),
                            "instance_label": self.instance_label(step),
                        }
                    )
                elif step.status == StepStatus.RUNNING and (
                    step.duration_type == "variable"
                    or step.duration_type == "indefinite"
                ):
                    if (
                        step.duration_type == "variable"
                        and step.get_progress(self.current_time)
                        < (step.min_seconds / step.default_seconds) * 100
                    ):
                        continue  # Can't end yet if we haven't reached minimum duration
                    available_triggers.append(
                        {
                            "id": f"end:{trigger_name}:{step.step_id}",
                            "name": f"End: {self.instance_step_name(step)}",
                            "type": "end",
                            "step_id": step.step_id,
                            "step_name": self.instance_step_name(step),
                            "track_id": step.track_id,
                            "instance_of": step.instance_of,
                            "instance_index": step.instance_index,
                            "instance_count": self.instance_count(step),
                            "instance_label": self.instance_label(step),
                        }
                    )

        # Check for steps that can be aborted
        for step_id, step in self.steps.items():
            if step.can_be_aborted():
                track_id = step.track_id
                track_name = self.program.get("tracks", [{}])[0].get("name", "Unknown")
                for track in self.program.get("tracks", []):
                    if track.get("trackId") == track_id:
                        track_name = track.get("name", "Unknown")
                        break

                available_triggers.append(
                    {
                        "id": f"abort:{step_id}",
                        "name": f"Abort: {self.instance_step_name(step)}",
                        "type": "abort",
                        "step_id": step_id,
                        "step_name": self.instance_step_name(step),
                        "track_id": track_id,
                        "track_name": track_name,
                        "instance_of": step.instance_of,
                        "instance_index": step.instance_index,
                        "instance_count": self.instance_count(step),
                        "instance_label": self.instance_label(step),
                    }
                )

        return available_triggers

    def format_time(self, seconds: float) -> str:
        """Format time in seconds to a human-readable string."""
        if seconds == float("inf"):
            return "∞"

        # For times less than 10 seconds, show decimal precision
        if seconds < 10.0:
            if seconds < 0.05:
                return "< 0.1s"
            return f"{seconds:.1f}s"

        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)

        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{int(seconds)}s"

    # ------------------------------------------------------------------
    # Replicate instances
    # ------------------------------------------------------------------
    def instance_count(self, step: Step) -> Optional[int]:
        """How many instances the replicated step ``step`` belongs to has."""
        if not step.instance_of:
            return None
        return self.instance_counts.get(step.instance_of) or step.instance_count_hint

    def instance_label(self, step: Step) -> str:
        """``"[2 of 3]"`` for a replicate instance, ``""`` for a plain step."""
        count = self.instance_count(step)
        if not step.instance_of or not step.instance_index or not count:
            return ""
        return f"[{step.instance_index} of {count}]"

    def instance_step_name(self, step: Step) -> str:
        """Step name with a canonical instance label, e.g. ``Bake tray [2 of 3]``."""
        label = self.instance_label(step)
        return f"{step.base_name} {label}" if label else step.name

    def display_track_id(self, step: Step) -> str:
        """Track to show for a step: instance sub-tracks show their parent."""
        return self.track_parents.get(step.track_id) or step.track_id

    def get_step_display_info(self, step: Step) -> Dict[str, Any]:
        """Get display information for a step."""
        progress = step.get_progress(self.current_time)
        remaining = step.get_remaining_time(self.current_time)

        # Debug: Add timing info to status for selected step
        debug_info = ""
        if hasattr(step, "_debug_elapsed") and step.status == StepStatus.RUNNING:
            debug_info = f" [DBG: e={step._debug_elapsed:.1f}s d={step._debug_duration:.1f}s p={step._debug_progress:.1f}%]"

        instance_count = self.instance_count(step)
        return {
            "id": step.step_id,
            "step_id": step.step_id,  # Add for backward compatibility
            "id_display": step.step_id,
            "row_type": "step",
            "name": self.instance_step_name(step) + debug_info,
            "base_name": step.base_name,
            "track": step.track_id,
            "track_display": self.display_track_id(step),
            "instance_of": step.instance_of,
            "instance_index": step.instance_index,
            "instance_count": instance_count,
            "instance_label": self.instance_label(step),
            "group_id": f"group:{step.instance_of}" if step.instance_of else None,
            "depth": 0,
            "status": step.status.value,
            "progress": progress,
            "remaining": (
                self.format_time(remaining) if remaining is not None else "N/A"
            ),
            "task_type": (
                step.task_types[0] if step.task_types else "N/A"
            ),  # Keep for backward compatibility
            "task_types": step.task_types,  # Add all task types
            "trigger": step.manual_trigger_name
            or getattr(step, "manual_start_trigger_name", None)
            or "N/A",
            # Instrument steps: the tool badge, and which tool a running
            # step is waiting on (its command is in flight)
            "instrument_tool": (step.instrument or {}).get("tool"),
            "waiting_on": (
                (blocking_action(step.instrument) or {}).get("tool")
                if step.instrument_blocking and step.status == StepStatus.RUNNING
                else None
            ),
            "failure_code": (step.failure or {}).get("code"),
        }

    def _group_row(
        self, instance_of: str, members: List[Dict[str, Any]], expanded: bool
    ) -> Dict[str, Any]:
        """Build the single collapsed row that stands for a set of instances."""
        count = len(members)
        statuses = [member["status"] for member in members]
        done = statuses.count("COMPLETED")
        running = statuses.count("RUNNING")
        aborted = statuses.count("ABORTED")
        waiting = statuses.count("WAITING_FOR_MANUAL")
        pending = count - done - running - aborted - waiting

        if running:
            status = "RUNNING"
        elif waiting:
            status = "WAITING_FOR_MANUAL"
        elif done + aborted == count:
            status = "ABORTED" if done == 0 and aborted else "COMPLETED"
        else:
            status = "PENDING"

        # Aggregate progress: finished instances count as 100 %, indefinite
        # instances (progress -1) as 0 so the bar never runs backwards.
        progress = sum(max(0.0, member["progress"]) for member in members) / count
        running_members = [m for m in members if m["status"] == "RUNNING"]
        remaining = running_members[0]["remaining"] if running_members else "N/A"
        base_name = members[0].get("base_name") or instance_of

        return {
            "id": f"group:{instance_of}",
            "step_id": None,
            "id_display": f"{instance_of} ×{count}",
            "row_type": "group",
            "name": f"{base_name} ×{count}",
            "base_name": base_name,
            "track": members[0]["track"],
            "track_display": members[0].get("track_display", members[0]["track"]),
            "instance_of": instance_of,
            "instance_index": None,
            "instance_count": count,
            "instance_label": "",
            "group_id": f"group:{instance_of}",
            "depth": 0,
            "status": status,
            "progress": progress,
            "remaining": remaining,
            "task_type": members[0]["task_type"],
            "task_types": members[0]["task_types"],
            "trigger": "N/A",
            "done": done,
            "running": running,
            "pending": pending,
            "aborted": aborted,
            "waiting": waiting,
            "summary": f"{done} done / {running} running / {pending} pending",
            "expanded": expanded,
            "members": [member["id"] for member in members],
        }

    def group_display_rows(
        self,
        steps_info: List[Dict[str, Any]],
        expanded: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Collapse instances of one replicated step into a single group row.

        Pure: it only rewrites the list it is handed. A program with no
        replicate instances (or grouping switched off) comes back unchanged,
        so the step list of an ordinary program is exactly what it was.
        """
        if expanded is None:
            expanded = self.expanded_groups
        if not self.group_instances:
            return list(steps_info)

        members_by_group: Dict[str, List[Dict[str, Any]]] = {}
        for info in steps_info:
            group = info.get("instance_of")
            if group:
                members_by_group.setdefault(group, []).append(info)
        # A "group" of one is just a step; leave it alone.
        members_by_group = {
            group: members
            for group, members in members_by_group.items()
            if len(members) > 1
        }
        if not members_by_group:
            return list(steps_info)

        rows: List[Dict[str, Any]] = []
        emitted: Set[str] = set()
        for info in steps_info:
            group = info.get("instance_of")
            if group not in members_by_group:
                rows.append(info)
                continue
            is_expanded = group in expanded
            if group not in emitted:
                emitted.add(group)
                rows.append(
                    self._group_row(group, members_by_group[group], is_expanded)
                )
            if is_expanded:
                member = dict(info)
                member["depth"] = 1
                rows.append(member)
        return rows

    def get_display_rows(self) -> List[Dict[str, Any]]:
        """The visible step-list rows: sorted, then grouped by instance."""
        steps_info = [self.get_step_display_info(step) for step in self.steps.values()]
        return self.group_display_rows(self.sort_steps(steps_info))

    def select_next_step(self) -> None:
        """Select the next row in the list."""
        rows = self.get_display_rows()
        if rows:
            self.selected_step_index = (self.selected_step_index + 1) % len(rows)
            self.status_message = (
                f"Selected step: {rows[self.selected_step_index]['name']}"
            )

    def select_previous_step(self) -> None:
        """Select the previous row in the list."""
        rows = self.get_display_rows()
        if rows:
            self.selected_step_index = (self.selected_step_index - 1) % len(rows)
            self.status_message = (
                f"Selected step: {rows[self.selected_step_index]['name']}"
            )

    def get_selected_row(self) -> Optional[Dict[str, Any]]:
        """The currently selected row (a step row or a collapsed group row)."""
        rows = self.get_display_rows()
        if not rows:
            return None
        if self.selected_step_index >= len(rows):
            self.selected_step_index = 0
        return rows[self.selected_step_index]

    def get_selected_step_id(self) -> Optional[str]:
        """Get the ID of the currently selected step (None on a group row)."""
        row = self.get_selected_row()
        if not row:
            return None
        return row.get("step_id")

    def toggle_group(self, instance_of: Optional[str] = None) -> Optional[str]:
        """Expand or collapse an instance group. Returns the group toggled."""
        if instance_of is None:
            row = self.get_selected_row()
            instance_of = row.get("instance_of") if row else None
        if not instance_of or instance_of not in self.instance_counts:
            self.status_message = "No instance group on this row."
            return None
        count = self.instance_counts[instance_of]
        if instance_of in self.expanded_groups:
            self.expanded_groups.discard(instance_of)
            self.status_message = f"Collapsed {instance_of} ×{count}."
        else:
            self.expanded_groups.add(instance_of)
            self.status_message = f"Expanded {instance_of} ×{count}."
        # Keep the selection on the group row after the row count changes.
        rows = self.get_display_rows()
        for index, row in enumerate(rows):
            if row.get("row_type") == "group" and row.get("instance_of") == instance_of:
                self.selected_step_index = index
                break
        return instance_of

    def get_all_steps_display_info(self) -> List[Dict[str, Any]]:
        """Get display information for all rows of the step list."""
        rows = self.get_display_rows()
        if rows and self.selected_step_index >= len(rows):
            self.selected_step_index = 0

        # Add selection indicator
        for index, row in enumerate(rows):
            row["selected"] = index == self.selected_step_index

        return rows

    def get_resource_usage_display(self) -> List[Dict[str, Any]]:
        """Get display information for resource usage."""
        result = []

        # Make sure we have resource constraints defined
        if not hasattr(self, "resource_constraints") or not self.resource_constraints:
            # Check if resourceConstraints is defined in the program
            resource_constraints = self.program.get("resourceConstraints", [])
            if resource_constraints:
                # Convert to the format we need
                for constraint in resource_constraints:
                    name = constraint.get("name")
                    max_concurrent = constraint.get("maxConcurrent", 1)
                    if name:
                        current_usage = self.resource_usage.get(name, 0)
                        result.append(
                            {
                                "task_type": name,
                                "usage": f"{current_usage}/{max_concurrent}",
                                "percentage": (
                                    (current_usage / max_concurrent) * 100
                                    if max_concurrent > 0
                                    else 0
                                ),
                            }
                        )
            else:
                # No resource constraints defined
                result.append({"task_type": "None", "usage": "0/1", "percentage": 0})
        else:
            # Use the resource constraints we have
            for task_type, max_concurrent in self.resource_constraints.items():
                # Skip null task types
                if task_type is None:
                    continue

                current_usage = self.resource_usage.get(task_type, 0)
                result.append(
                    {
                        "task_type": task_type,
                        "usage": f"{current_usage}/{max_concurrent}",
                        "percentage": (
                            (current_usage / max_concurrent) * 100
                            if max_concurrent > 0
                            else 0
                        ),
                    }
                )

        return result

    def get_actor_types_display(self) -> List[Dict[str, Any]]:
        """Get display information for actor types usage."""
        result = []

        for actor_type_id, actor_info in self.actor_types.items():
            total_capacity = actor_info["count"]
            current_usage = self.actor_usage_by_type.get(actor_type_id, 0.0)
            percentage = (
                (current_usage / total_capacity * 100) if total_capacity > 0 else 0
            )

            result.append(
                {
                    "actor_type": actor_info["name"],
                    "usage": f"{current_usage:.1f}/{total_capacity}",
                    "percentage": percentage,
                }
            )

        return result

    def sort_steps(self, steps_info: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort steps based on the current sort mode."""
        if self.sort_mode == SortMode.DEFAULT:
            # Default order (as defined in the program)
            return steps_info
        elif self.sort_mode == SortMode.REMAINING:
            # Sort by remaining time (running steps with shortest time first, then pending, then completed)
            def remaining_time_key(step_info):
                if step_info["status"] == "COMPLETED":
                    return (2, 0)  # Completed steps last
                elif step_info["status"] == "PENDING":
                    return (1, 0)  # Pending steps in the middle
                else:
                    # For running steps, convert remaining time to seconds for sorting
                    remaining = step_info["remaining"]
                    if remaining == "N/A":
                        return (0, float("inf"))  # Indefinite duration at the end

                    # Parse the time string (e.g., "1h 30m 15s", "45m 10s", "20s")
                    seconds = 0
                    if "h" in remaining:
                        hours, remaining = remaining.split("h", 1)
                        seconds += int(hours.strip()) * 3600
                    if "m" in remaining:
                        minutes, remaining = remaining.split("m", 1)
                        seconds += int(minutes.strip()) * 60
                    if "s" in remaining:
                        s, _ = remaining.split("s", 1)
                        seconds += int(s.strip())

                    return (0, seconds)

            return sorted(steps_info, key=remaining_time_key)
        elif self.sort_mode == SortMode.STATUS:
            # Sort by status (running first, then pending, then completed)
            def status_key(step_info):
                if step_info["status"] == "RUNNING":
                    return 0
                elif step_info["status"] == "WAITING_FOR_MANUAL":
                    return 1
                elif step_info["status"] == "PENDING":
                    return 2
                else:  # COMPLETED
                    return 3

            return sorted(steps_info, key=status_key)

        return steps_info

    def display_step(self, step: Step, is_selected: bool = False) -> None:
        """Display a step with its status and progress."""
        from colorama import Style

        info = self.get_step_display_info(step)

        # Determine color based on status
        color = self.get_status_color(step.status)

        # Add selection indicator
        prefix = "→ " if is_selected else "  "

        # Format the step display
        step_display = f"{prefix}{color}{info['name']} ({info['id']}){Style.RESET_ALL}"

        # Add status and progress
        status_display = (
            f"{color}[{self.get_status_display(step.status)}]{Style.RESET_ALL}"
        )
        progress_display = f"{self.get_progress_bar(info['progress'])}"
        time_display = f"{info['remaining']}"

        # Add code execution status if applicable
        code_display = ""
        if info.get("has_code", False):
            code_type = info.get("code_type", "")
            from colorama import Fore

            if info.get("code_executed", False):
                if info.get("code_error"):
                    code_display = f" {Fore.RED}[{code_type} error]{Style.RESET_ALL}"
                else:
                    code_display = (
                        f" {Fore.GREEN}[{code_type} executed]{Style.RESET_ALL}"
                    )
            else:
                code_display = f" {Fore.BLUE}[{code_type} pending]{Style.RESET_ALL}"

        # Print the formatted step information
        print(
            f"{step_display} {status_display} {progress_display} {time_display}{code_display}"
        )

    def display_help(self) -> None:
        """Display help information."""
        print("\nRhylthyme Program Runner Help:")
        print("------------------------------")
        print("Controls:")
        print("  q, Ctrl+C: Quit the program")
        print("  p: Pause/resume the program")
        print("  h: Display this help message")
        print("  t: Trigger the selected step (if it's waiting for manual trigger)")
        print("  e: End the selected step (if it has variable duration)")
        print("  s: Sort steps (cycles through different sort modes)")
        print("  ↑/↓: Navigate between steps")
        print("  Enter: Trigger or end the selected step")
        print("\nStep Status Colors:")
        print("  Green: Running")
        print("  Yellow: Waiting for manual trigger or end")
        print("  Red: Blocked by resource constraints")
        print("  Blue: Pending")
        print("  Gray: Completed")
        print("\nCode Execution:")
        print(
            "  Steps can include Python or shell code blocks that execute when the step starts"
        )
        print("  Code execution status is shown next to the step:")
        print("    Blue [python/shell pending]: Code has not been executed yet")
        print("    Green [python/shell executed]: Code executed successfully")
        print("    Red [python/shell error]: Code execution failed with an error")
        print("\nPress any key to continue...")

    def get_step_by_id(self, step_id: str) -> Optional[Step]:
        """Get a step by its ID."""
        return self.steps.get(step_id)

    def execute_code_block(self, step: Step) -> None:
        """Execute a code block in a step."""
        if not step.code_type or not step.code_block:
            return

        try:
            # Create step variables for substitution
            step_vars = StepVariables(step)

            # Replace variables in the code
            code_with_vars = self._substitute_variables(step.code_block, step_vars)

            if step.code_type == "python":
                # Execute Python code
                local_vars = {}
                # Add step variables to local_vars
                local_vars["rhyl"] = step_vars
                exec(code_with_vars, globals(), local_vars)
                step.code_result = local_vars
            elif step.code_type == "shell":
                # Execute shell command with variable substitution
                result = subprocess.run(
                    code_with_vars, shell=True, capture_output=True, text=True
                )
                step.code_result = {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }
            step.code_executed = True
        except Exception as e:
            step.code_error = str(e)
            step.code_executed = True

    def _substitute_variables(self, code: str, step_vars: StepVariables) -> str:
        """
        Substitute variables in the code with their values.

        Args:
            code: The code to substitute variables in
            step_vars: The step variables object

        Returns:
            The code with variables substituted
        """
        # Find all {rhyl.variable} patterns
        pattern = r"\{rhyl\.([a-zA-Z0-9_]+)\}"

        def replace_var(match):
            var_name = match.group(1)
            if hasattr(step_vars, var_name):
                return str(getattr(step_vars, var_name))
            return match.group(0)  # Return the original if not found

        # Replace all matches
        return re.sub(pattern, replace_var, code)

    def get_progress_bar(self, progress: float) -> str:
        """Generate a progress bar string."""
        if progress == 0.0:
            return "[----------]"
        elif progress >= 100.0:
            return "[##########]"
        else:
            filled = int(progress / 10)
            empty = 10 - filled
            return f"[{'#' * filled}{'-' * empty}]"

    def get_status_color(self, status: StepStatus) -> int:
        """Get the color for a step status."""
        import curses

        if status == StepStatus.PENDING:
            return curses.COLOR_WHITE
        elif status == StepStatus.RUNNING:
            return curses.COLOR_GREEN
        elif status == StepStatus.COMPLETED:
            return curses.COLOR_BLUE
        elif status == StepStatus.WAITING_FOR_MANUAL:
            return curses.COLOR_YELLOW
        elif status in (StepStatus.ABORTED, StepStatus.FAILED):
            return curses.COLOR_RED
        return curses.COLOR_WHITE

    def get_status_display(self, status: Union[StepStatus, str]) -> str:
        """
        Get the display string for a step status.

        Args:
            status: The step status (either StepStatus enum or string)

        Returns:
            The display string for the status
        """
        # Convert string status to enum if needed
        if isinstance(status, str):
            try:
                # Try to match the string to an enum value
                for enum_status in StepStatus:
                    if enum_status.value == status:
                        status = enum_status
                        break
                else:
                    # If no match found, return the string as is
                    return status
            except:
                # If conversion fails, return the string as is
                return str(status)

        if isinstance(status, StepStatus):
            if status == StepStatus.PENDING:
                return "PENDING"
            elif status == StepStatus.RUNNING:
                return "RUNNING"
            elif status == StepStatus.COMPLETED:
                return "COMPLETED"
            elif status == StepStatus.WAITING_FOR_MANUAL:
                return "WAITING"
            elif status == StepStatus.ABORTED:
                return "ABORTED"
            elif status == StepStatus.FAILED:
                return "FAILED"
        return "UNKNOWN"  # Default for unhandled status values

    def is_step_ready_to_start(self, step: Step, current_time: float) -> bool:
        """
        Check if a step is ready to start.

        Args:
            step: The step to check
            current_time: The current time

        Returns:
            True if the step is ready to start, False otherwise
        """
        # Only pending or waiting-for-manual steps can be started
        if step.status == StepStatus.WAITING_FOR_MANUAL:
            return True  # Already triggered by user, ready to start
        if step.status != StepStatus.PENDING:
            return False

        # Compound trigger ({"logic": "all"|"any", "triggers": [...]})
        if step.start_trigger is None:
            if step.start_triggers:
                results = [
                    self._is_trigger_satisfied(step, trigger, current_time)
                    for trigger in step.start_triggers
                ]
                if step.start_trigger_logic == "any":
                    return any(results)
                return all(results)
            return False

        return self._is_trigger_satisfied(step, step.start_trigger, current_time)

    # ------------------------------------------------- predicted offsets
    def set_predictions(self, predictions: Optional[Dict[str, Any]]) -> None:
        """
        Supply history-based duration predictions for this run.

        ``predictions`` is what
        ``rhylthyme.history.predict.predict_durations`` returns:
        ``{authored stepId: {seconds, low, high, basis, n, ...}}``. They are
        used for one thing only — projecting the end of an *indefinite* anchor
        of a negative offset — and only when the program asks for it with
        ``metadata.offsetsUse: "predicted"``.
        """
        self.predictions = predictions or {}

    def _eligible_prediction(self, authored_id: str, step: Step) -> Optional[float]:
        """
        The predicted seconds usable as ``step``'s projected duration, or None.

        The conditions are PRD §7's: the program opted in, the step is
        indefinite, a prediction exists (``basis`` other than ``none`` with a
        number on it) and the prediction is *sharper than the guess* — its
        80 % interval is narrower than the author's ``defaultSeconds``. A wide
        interval is history saying it does not know, and the author's number
        is then no worse.
        """
        if self.offsets_use != "predicted":
            return None
        if step.duration_type != DurationType.INDEFINITE:
            return None
        prediction = self.predictions.get(authored_id)
        if not isinstance(prediction, dict):
            return None
        if prediction.get("basis") in (None, "none"):
            return None
        seconds = prediction.get("seconds")
        low, high = prediction.get("low"), prediction.get("high")
        planned = step.default_seconds
        if seconds is None or planned is None or not planned:
            return None
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        if low is None or high is None:
            return None
        try:
            width = float(high) - float(low)
        except (TypeError, ValueError):
            return None
        if not width < float(planned):
            return None
        return seconds

    def anchor_projected_seconds(self, ref_step: Step, gated_step: Step) -> float:
        """
        How long ``ref_step`` is expected to take, for projecting its end.

        The author's number by default (a fixed step's duration, otherwise
        ``defaultSeconds``); the predicted duration when the program set
        ``metadata.offsetsUse: "predicted"`` and the prediction qualifies. The
        prediction actually used is remembered against ``gated_step`` so the
        run record can say which number the trigger fired from.
        """
        authored_id = ref_step.instance_of or ref_step.step_id
        predicted = self._eligible_prediction(str(authored_id), ref_step)
        if predicted is not None:
            self.predicted_anchor_seconds[gated_step.step_id] = predicted
            return predicted
        if ref_step.duration_type == DurationType.FIXED:
            return float(ref_step.duration_seconds or 0.0)
        if ref_step.default_seconds is not None:
            return float(ref_step.default_seconds)
        if ref_step.duration_seconds is not None:
            return float(ref_step.duration_seconds)
        return 0.0

    def _is_trigger_satisfied(
        self, step: Step, start_trigger: Dict[str, Any], current_time: float
    ) -> bool:
        """
        Evaluate one (non-compound) start trigger of ``step``.

        This, not ``Step._evaluate_single_trigger``, is the trigger resolver
        the run loop uses; the ``Step`` method sees only the set of completed
        step ids and cannot resolve an anchor's start or projected end.
        """
        trigger_type = start_trigger.get("type")

        # Program start trigger
        if trigger_type == "programStart":
            return self.is_running

        # Absolute time trigger
        elif trigger_type == "absolute":
            trigger_time = datetime.datetime.fromisoformat(
                start_trigger["time"]
            ).timestamp()
            return current_time >= trigger_time

        # Offset time trigger
        elif trigger_type == "offset":
            offset_seconds = float(start_trigger["offsetSeconds"])
            reference_time = self.program_start_time
            if reference_time is None:
                return False
            return current_time >= reference_time + offset_seconds

        # Program start with offset trigger
        elif trigger_type == "programStartOffset":
            offset_seconds = float(start_trigger["offsetSeconds"])
            if self.program_start_time is None:
                return False
            return current_time >= self.program_start_time + offset_seconds

        # After step trigger
        elif trigger_type == "afterStep" or trigger_type == "afterStepWithBuffer":
            ref_step_id = start_trigger["stepId"]
            if ref_step_id not in self.steps:
                return False
            ref_step = self.steps[ref_step_id]

            # Buffers are always required, whichever anchor the trigger uses.
            required_delay = ref_step.post_buffer_seconds + step.pre_buffer_seconds
            if trigger_type == "afterStepWithBuffer":
                required_delay += _signed_seconds(start_trigger.get("bufferSeconds", 0))
            offset_seconds = _signed_seconds(start_trigger.get("offsetSeconds", 0))

            # event: "start" anchors on the referenced step's START, as the
            # timing engine does (computeStepTimings: `trig.event === 'start'
            # ? ref.start : ref.end`). The step need only have begun.
            if start_trigger.get("event") == "start":
                if ref_step.start_time is None or ref_step.status == StepStatus.PENDING:
                    return False
                return current_time >= ref_step.start_time + max(
                    0.0, offset_seconds + required_delay
                )

            # A negative offset fires BEFORE the anchor ends, so it cannot
            # wait for the anchor to complete. It is resolved against the
            # anchor's projected end — actual start plus the duration the
            # anchor is expected to take — which is what the timing engine
            # computes for an unfinished step (`start + defaultSeconds`).
            # Should the anchor end before that instant the projection was
            # wrong and the trigger fires immediately, never later than the
            # engine would have placed it.
            if offset_seconds < 0:
                if ref_step.start_time is None:
                    return False
                projected = self.anchor_projected_seconds(ref_step, step)
                target = ref_step.start_time + projected + offset_seconds
                target += required_delay
                target = max(target, ref_step.start_time)
                if ref_step.end_time is not None:
                    target = min(target, ref_step.end_time)
                return current_time >= target

            # The ordinary case: the anchor has to finish first.
            if ref_step.status != StepStatus.COMPLETED:
                return False
            if ref_step.end_time is not None:
                required_delay += offset_seconds
                if required_delay > 0:
                    return current_time >= ref_step.end_time + required_delay
            return True

        # Manual trigger — step must be set to WAITING_FOR_MANUAL via trigger_manual_step()
        elif trigger_type == "manual":
            return False

        # Unknown trigger type
        else:
            return False

    def emit_event(self, event_type: str, event_data: Dict[str, Any]) -> None:
        """
        Emit an event to all registered listeners.

        Args:
            event_type: The type of event
            event_data: The event data
        """
        # Log the event
        logging.debug(f"Event: {event_type} - {event_data}")

        # Call any registered event listeners
        for listener in self.event_listeners:
            try:
                listener(event_type, event_data)
            except Exception as e:
                logging.error(f"Error in event listener: {e}")

    def add_event_listener(self, listener):
        """
        Add an event listener.

        Args:
            listener: A function that takes event_type and event_data as arguments
        """
        self.event_listeners.append(listener)

    def get_upcoming_events(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get the next upcoming events (step starts or ends).

        Args:
            limit: Maximum number of events to return

        Returns:
            List of upcoming events, each with event_type, step_id, name, time, and time_str
        """
        events = []

        # Add potential start events for pending steps
        for step in self.steps.values():
            if step.status == StepStatus.PENDING:
                # Try to estimate when this step will start
                estimated_start_time = self._estimate_step_start_time(step)
                if (
                    estimated_start_time is not None
                    and estimated_start_time >= self.current_time
                ):
                    events.append(
                        {
                            "event_type": "start",
                            "step_id": step.step_id,
                            "name": step.name,
                            "time": estimated_start_time,
                            "time_str": self.format_time(
                                estimated_start_time - self.current_time
                            ),
                        }
                    )

        # Add end events for running steps
        for step in self.steps.values():
            if step.status == StepStatus.RUNNING and step.expected_end_time is not None:
                events.append(
                    {
                        "event_type": "end",
                        "step_id": step.step_id,
                        "name": step.name,
                        "time": step.expected_end_time,
                        "time_str": self.format_time(
                            step.expected_end_time - self.current_time
                        ),
                    }
                )

        # Sort by time and limit
        events.sort(key=lambda e: e["time"])
        return events[:limit]

    def _estimate_step_start_time(self, step: Step) -> Optional[float]:
        """
        Estimate when a pending step will start.

        Args:
            step: The step to estimate

        Returns:
            Estimated start time or None if it can't be determined
        """
        if not self.is_running or self.program_start_time is None:
            return None

        trigger = step.start_trigger
        trigger_type = trigger.get("type")

        if trigger_type == "programStart":
            return self.program_start_time

        elif trigger_type == "programStartOffset":
            offset_seconds = trigger.get("offsetSeconds", 0)
            if isinstance(offset_seconds, str):
                offset_seconds = parse_time_string(offset_seconds)
            return self.program_start_time + offset_seconds

        elif trigger_type == "afterStep" or trigger_type == "afterStepWithBuffer":
            ref_step_id = trigger.get("stepId")
            if ref_step_id not in self.steps:
                return None

            ref_step = self.steps[ref_step_id]
            event = trigger.get("event", "end")

            # If reference step is completed, use its actual end time
            if (
                ref_step.status == StepStatus.COMPLETED
                and ref_step.end_time is not None
            ):
                base_time = ref_step.end_time if event == "end" else ref_step.start_time
            # If reference step is running and has expected end time
            elif (
                ref_step.status == StepStatus.RUNNING
                and ref_step.expected_end_time is not None
            ):
                if event == "end":
                    base_time = ref_step.expected_end_time
                else:
                    base_time = ref_step.start_time
            # If reference step is still pending, recursively estimate its start/end time
            elif ref_step.status == StepStatus.PENDING:
                ref_start_time = self._estimate_step_start_time(ref_step)
                if ref_start_time is None:
                    return None

                if event == "start":
                    base_time = ref_start_time
                else:
                    # Estimate end time based on duration
                    if (
                        ref_step.duration_type == DurationType.FIXED
                        and ref_step.duration_seconds is not None
                    ):
                        base_time = ref_start_time + ref_step.duration_seconds
                    elif (
                        ref_step.duration_type == DurationType.VARIABLE
                        and ref_step.default_seconds is not None
                    ):
                        base_time = ref_start_time + ref_step.default_seconds
                    elif (
                        ref_step.duration_type == DurationType.INDEFINITE
                        and ref_step.default_seconds is not None
                    ):
                        base_time = ref_start_time + ref_step.default_seconds
                    else:
                        return None
            else:
                return None

            # Add predecessor's post-buffer
            base_time += ref_step.post_buffer_seconds

            # Add explicit buffer if afterStepWithBuffer
            if trigger_type == "afterStepWithBuffer":
                buffer_seconds = trigger.get("bufferSeconds", 0)
                if isinstance(buffer_seconds, str):
                    buffer_seconds = parse_time_string(buffer_seconds)
                base_time += buffer_seconds

            # Handle offsetSeconds on afterStep (same as web visualizer)
            offset_seconds = trigger.get("offsetSeconds", 0)
            if offset_seconds:
                if isinstance(offset_seconds, str):
                    offset_seconds = parse_time_string(offset_seconds)
                base_time += offset_seconds

            # Add this step's pre-buffer
            base_time += step.pre_buffer_seconds

            return base_time

        elif trigger_type == "manual" or trigger_type == "onAbort":
            # Can't estimate manual triggers or abort triggers
            return None

        return None
