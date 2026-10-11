#!/usr/bin/env python3
"""
Program Planner for Rhylthyme

This module provides functionality to analyze a program schedule,
identify resource bottlenecks, and optimize the schedule by
staggering track and step starts to reduce resource contention.
"""

import copy
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import yaml

from .instruments import fill_instrument_durations


def load_program_file(file_path: str) -> dict:
    """
    Load and parse a program file in JSON or YAML format.

    Args:
        file_path: Path to the program file

    Returns:
        The parsed program as a dictionary

    Raises:
        FileNotFoundError: If the file does not exist
        json.JSONDecodeError: If the JSON file is invalid
        yaml.YAMLError: If the YAML file is invalid
    """
    try:
        _, file_extension = os.path.splitext(file_path)
        with open(file_path, "r") as f:
            if file_extension.lower() in [".yaml", ".yml"]:
                return yaml.safe_load(f)
            else:
                return json.load(f)
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        raise
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON file '{file_path}': {e}")
        raise
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file '{file_path}': {e}")
        raise


class ResourceUsage:
    """
    Track resource usage over time.
    """

    def __init__(self):
        # Dictionary mapping time points to resource usage counts
        self.usage_profile = {}

    def add_usage(self, start_time: float, end_time: float, resource_id: str):
        """
        Add resource usage for a time period.

        Args:
            start_time: Start time of resource usage
            end_time: End time of resource usage
            resource_id: Identifier for the resource
        """
        # Ensure we have entries for start and end times
        if start_time not in self.usage_profile:
            self.usage_profile[start_time] = {}
        if end_time not in self.usage_profile:
            self.usage_profile[end_time] = {}

        # Add resource usage at start time
        if resource_id not in self.usage_profile[start_time]:
            self.usage_profile[start_time][resource_id] = 0
        self.usage_profile[start_time][resource_id] += 1

        # Remove resource usage at end time
        if resource_id not in self.usage_profile[end_time]:
            self.usage_profile[end_time][resource_id] = 0
        self.usage_profile[end_time][resource_id] -= 1

    def calculate_usage_profile(self) -> Dict[str, List[Tuple[float, float, int]]]:
        """
        Calculate the usage profile for each resource.

        Returns:
            Dictionary mapping resource IDs to lists of (start_time, end_time, usage_count) tuples
        """
        # Sort time points
        time_points = sorted(self.usage_profile.keys())

        # Initialize result
        result: Dict[str, List[Tuple[float, float, int]]] = {}

        # Initialize current usage counts
        current_usage = {}

        # Process time points
        for i in range(len(time_points) - 1):
            current_time = time_points[i]
            next_time = time_points[i + 1]

            # Update current usage counts
            for resource_id, count_change in self.usage_profile[current_time].items():
                if resource_id not in current_usage:
                    current_usage[resource_id] = 0
                current_usage[resource_id] += count_change

                # Add to result if usage is positive
                if current_usage[resource_id] > 0:
                    if resource_id not in result:
                        result[resource_id] = []
                    result[resource_id].append(
                        (current_time, next_time, current_usage[resource_id])
                    )

        return result

    def find_bottlenecks(
        self, threshold: int = 2
    ) -> List[Tuple[str, float, float, int]]:
        """
        Find resource bottlenecks where usage exceeds a threshold.

        Args:
            threshold: Usage count threshold for considering a bottleneck

        Returns:
            List of (resource_id, start_time, end_time, usage_count) tuples
        """
        usage_profile = self.calculate_usage_profile()
        bottlenecks = []

        for resource_id, usage_periods in usage_profile.items():
            for start_time, end_time, count in usage_periods:
                if count >= threshold:
                    bottlenecks.append((resource_id, start_time, end_time, count))

        return sorted(bottlenecks, key=lambda x: (x[3], x[1]), reverse=True)


class Step:
    """
    Represents a step in the program.
    """

    def __init__(self, step_data: dict, track_id: str):
        self.data = step_data
        self.track_id = track_id
        self.id = step_data.get("id", "")
        self.name = step_data.get("name", "")
        self.priority = step_data.get(
            "priority", 100
        )  # Default priority is 100 (lower is higher priority)
        self.resources = step_data.get("resources", [])
        self.dependencies = self._extract_dependencies()

        # Handle duration based on type
        self.duration_data = step_data.get("duration", {})
        if isinstance(self.duration_data, dict):
            self.duration_type = self.duration_data.get("type")

            if self.duration_type == "fixed":
                self.min_duration = self.duration_data.get("seconds", 0)
                self.max_duration = self.duration_data.get("seconds", 0)
                self.optimal_duration = self.duration_data.get("seconds", 0)
            elif self.duration_type == "variable":
                self.min_duration = self.duration_data.get("minSeconds", 0)
                self.max_duration = self.duration_data.get("maxSeconds", 0)
                # Use optimalSeconds if provided, otherwise use defaultSeconds, or average of min and max
                if "optimalSeconds" in self.duration_data:
                    self.optimal_duration = self.duration_data.get("optimalSeconds")
                elif "defaultSeconds" in self.duration_data:
                    self.optimal_duration = self.duration_data.get("defaultSeconds")
                else:
                    self.optimal_duration = (
                        self._seconds(self.min_duration)
                        + self._seconds(self.max_duration)
                    ) / 2
            else:  # indefinite or unknown
                self.min_duration = 0
                self.max_duration = float("inf")
                self.optimal_duration = step_data.get(
                    "duration", 0
                )  # Fallback to simple duration
        else:
            # Simple duration value
            self.duration_type = "fixed"
            self.min_duration = step_data.get("duration", 0)
            self.max_duration = step_data.get("duration", 0)
            self.optimal_duration = step_data.get("duration", 0)

    def _extract_dependencies(self) -> List[Tuple[str, str]]:
        """
        Extract dependencies from step data.
        Handles both old 'after' format and new 'startTrigger' format.

        Returns:
            List of (track_id, step_id) tuples representing dependencies
        """
        dependencies = []

        # Handle new startTrigger format
        start_trigger = self.data.get("startTrigger", {})
        if isinstance(start_trigger, dict):
            trigger_type = start_trigger.get("type")
            if trigger_type in ["afterStep", "afterStepWithBuffer"]:
                step_id = start_trigger.get("stepId")
                if step_id:
                    # For now, assume same track (we'll need to enhance this)
                    # TODO: Handle cross-track dependencies properly
                    dependencies.append((self.track_id, step_id))

        # Handle old 'after' format for backward compatibility
        after = self.data.get("after", [])
        for dep in after:
            if isinstance(dep, dict) and "trackId" in dep and "stepId" in dep:
                dependencies.append((dep["trackId"], dep["stepId"]))

        return dependencies

    @staticmethod
    def _seconds(value: Any) -> float:
        """Durations may be numbers or time strings ("90m", "1h30m")."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            from .program_runner import parse_time_string

            return float(parse_time_string(value))
        if isinstance(value, dict):  # indefinite: plan on its default
            return Step._seconds(value.get("defaultSeconds", 0))
        return 0.0

    def calculate_duration(self) -> float:
        """
        Calculate the duration of the step for planning purposes.
        Uses the optimal duration for planning.

        Returns:
            Duration in seconds
        """
        return self._seconds(self.optimal_duration)

    def get_min_duration(self) -> float:
        """
        Get the minimum possible duration of the step.

        Returns:
            Minimum duration in seconds
        """
        return self._seconds(self.min_duration)

    def get_max_duration(self) -> float:
        """
        Get the maximum possible duration of the step.

        Returns:
            Maximum duration in seconds
        """
        return self._seconds(self.max_duration)

    def get_trigger_info(self) -> dict:
        """
        Get trigger information for the step.

        Returns:
            Dictionary with trigger type and parameters
        """
        start_trigger = self.data.get("startTrigger", {})
        if isinstance(start_trigger, dict):
            return {
                "type": start_trigger.get("type", "programStart"),
                "stepId": start_trigger.get("stepId"),
                "offsetSeconds": start_trigger.get("offsetSeconds", 0),
                "event": start_trigger.get("event", "end"),
            }

        # Fallback for old format
        return {"type": "afterStep", "stepId": None, "offsetSeconds": 0, "event": "end"}


class ProgramPlanner:
    """
    Plan and optimize program schedules.
    """

    def __init__(
        self, program: dict, verbose: bool = False, environment: Optional[dict] = None
    ):
        self.program = program
        self.verbose = verbose
        self.environment = environment
        self.steps = self._extract_steps()
        self.resource_usage = ResourceUsage()
        self.min_resource_usage = ResourceUsage()
        self.max_resource_usage = ResourceUsage()
        self.resource_constraints = None
        self.equipment_constraints = None
        if environment:
            # Check environment type matches program
            prog_type = program.get("environmentType")
            env_type = environment.get("type")
            if prog_type and env_type and prog_type != env_type:
                raise ValueError(
                    f"Environment type '{env_type}' does not match program environmentType '{prog_type}'"
                )
            self.resource_constraints = environment.get("resourceConstraints", [])
            self.equipment_constraints = environment.get("equipment", [])
        else:
            self.resource_constraints = None  # Unlimited resources
            self.equipment_constraints = None

    def _extract_steps(self) -> Dict[str, Dict[str, Step]]:
        """
        Extract steps from the program.

        Returns:
            Dictionary mapping track IDs to dictionaries mapping step IDs to Step objects
        """
        steps: Dict[str, Dict[str, Step]] = {}

        for track in self.program.get("tracks", []):
            track_id = track.get("id", "")
            steps[track_id] = {}

            for step in track.get("steps", []):
                step_id = step.get("id", "")
                steps[track_id][step_id] = Step(step, track_id)

        return steps

    def simulate_execution(self) -> Dict[str, Dict[str, float]]:
        """
        Simulate program execution to calculate start times.

        Returns:
            Dictionary mapping track IDs to dictionaries mapping step IDs to start times
        """
        # Initialize start times
        start_times: Dict[str, Dict[str, float]] = {}
        for track_id in self.steps:
            start_times[track_id] = {}

        # Initialize completed steps
        completed: Set[Tuple[str, str]] = set()

        # Process steps until all are scheduled
        while True:
            # Find steps that can start
            steps_to_start = []

            for track_id, track_steps in self.steps.items():
                for step_id, step in track_steps.items():
                    # Skip if already scheduled
                    if (track_id, step_id) in completed:
                        continue

                    # Check if step can start
                    if self._can_start(step, completed, start_times):
                        steps_to_start.append((track_id, step_id, step))

            # If no steps can start, we're done
            if not steps_to_start:
                break

            # Sort steps by priority (lower priority number comes first)
            steps_to_start.sort(key=lambda x: x[2].priority)

            # Schedule steps
            for track_id, step_id, step in steps_to_start:
                start_time = self._calculate_start_time(step, start_times)
                start_times[track_id][step_id] = start_time
                completed.add((track_id, step_id))

                # Add resource usage for optimal duration (used for planning)
                for resource in step.resources:
                    # Extract resource ID from resource object if it's a dict
                    resource_id = resource
                    if isinstance(resource, dict):
                        resource_id = (
                            resource.get("resourceId")
                            or resource.get("type")
                            or resource.get("id")
                            or str(resource)
                        )
                    self.resource_usage.add_usage(
                        start_time, start_time + step.calculate_duration(), resource_id
                    )

                # Add task usage for optimal duration
                task = step.data.get("task")
                if task:
                    self.resource_usage.add_usage(
                        start_time, start_time + step.calculate_duration(), task
                    )

                # Also track min and max duration scenarios for bottleneck analysis
                for resource in step.resources:
                    # Extract resource ID from resource object if it's a dict
                    resource_id = resource
                    if isinstance(resource, dict):
                        resource_id = (
                            resource.get("resourceId")
                            or resource.get("type")
                            or resource.get("id")
                            or str(resource)
                        )
                    self.min_resource_usage.add_usage(
                        start_time, start_time + step.get_min_duration(), resource_id
                    )
                    self.max_resource_usage.add_usage(
                        start_time, start_time + step.get_max_duration(), resource_id
                    )

                # Track task usage for min and max scenarios
                if task:
                    self.min_resource_usage.add_usage(
                        start_time, start_time + step.get_min_duration(), task
                    )
                    self.max_resource_usage.add_usage(
                        start_time, start_time + step.get_max_duration(), task
                    )

        return start_times

    def optimize_schedule(self) -> dict:
        """
        Optimize the program schedule to reduce resource contention.

        Returns:
            Optimized program
        """
        # Create a copy of the program to modify
        optimized_program = copy.deepcopy(self.program)

        # First, fix overlapping steps by properly sequencing them
        self._fix_overlapping_steps(optimized_program)

        # Update the program and re-extract steps
        self.program = optimized_program
        self.steps = self._extract_steps()

        # Simulate execution to calculate resource usage
        self.simulate_execution()

        # Find bottlenecks based on environment constraints
        bottlenecks = []

        if self.resource_constraints or self.equipment_constraints:
            # Map task constraints
            task_limits = {}
            if self.resource_constraints:
                task_limits = {
                    rc["task"]: rc.get("maxConcurrent", float("inf"))
                    for rc in self.resource_constraints
                }

            # Map equipment constraints
            equipment_limits = {}
            if self.equipment_constraints:
                equipment_limits = {
                    eq["id"]: eq.get("maxConcurrent", float("inf"))
                    for eq in self.equipment_constraints
                }

            # Check task-based bottlenecks
            for (
                resource_id,
                usage_periods,
            ) in self.resource_usage.calculate_usage_profile().items():
                # Check if this is a task constraint
                if resource_id in task_limits:
                    limit = task_limits[resource_id]
                    for start_time, end_time, count in usage_periods:
                        if count > limit:
                            bottlenecks.append(
                                (resource_id, start_time, end_time, count)
                            )

                # Check if this is an equipment constraint
                if resource_id in equipment_limits:
                    limit = equipment_limits[resource_id]
                    for start_time, end_time, count in usage_periods:
                        if count > limit:
                            bottlenecks.append(
                                (resource_id, start_time, end_time, count)
                            )
        else:
            # Unlimited resources: no bottlenecks
            bottlenecks = []

        # Also consider bottlenecks in worst-case scenario (max durations)
        max_bottlenecks = self.max_resource_usage.find_bottlenecks()

        # Combine bottlenecks, prioritizing those that appear in both scenarios
        combined_bottlenecks = []
        seen_resources = set()

        # First add bottlenecks that appear in both optimal and max scenarios
        for bottleneck in bottlenecks:
            resource_id = bottleneck[0]
            for max_bottleneck in max_bottlenecks:
                if max_bottleneck[0] == resource_id:
                    combined_bottlenecks.append(bottleneck)
                    seen_resources.add(resource_id)
                    break

        # Then add remaining bottlenecks from max scenario
        for bottleneck in max_bottlenecks:
            resource_id = bottleneck[0]
            if resource_id not in seen_resources:
                combined_bottlenecks.append(bottleneck)
                seen_resources.add(resource_id)

        # Then add remaining bottlenecks from optimal scenario
        for bottleneck in bottlenecks:
            resource_id = bottleneck[0]
            if resource_id not in seen_resources:
                combined_bottlenecks.append(bottleneck)
                seen_resources.add(resource_id)

        if self.verbose and combined_bottlenecks:
            print("Resource bottlenecks found:")
            for resource_id, start_time, end_time, count in combined_bottlenecks:
                print(
                    f"  Resource '{resource_id}': {count} concurrent uses from {start_time} to {end_time}"
                )

        # Stagger track starts
        self._stagger_track_starts(optimized_program, combined_bottlenecks)

        # Stagger step starts within tracks
        self._stagger_step_starts(optimized_program, combined_bottlenecks)

        # Ensure all required fields are preserved for schema validation
        self._preserve_required_fields(optimized_program)

        return optimized_program

    def _fix_overlapping_steps(self, program: dict):
        """
        Fix overlapping steps by properly sequencing them based on their triggers.

        Args:
            program: Program to modify
        """
        for track in program.get("tracks", []):
            steps = track.get("steps", [])
            if len(steps) <= 1:
                continue

            # First pass: Convert old trigger format to new startTrigger format
            for step in steps:
                # If step uses old trigger format, convert it
                if "trigger" in step:
                    old_trigger = step["trigger"]
                    if isinstance(old_trigger, dict):
                        if old_trigger.get("type") == "programStart":
                            step["startTrigger"] = {"type": "programStart"}
                        elif old_trigger.get("type") == "manual":
                            step["startTrigger"] = {"type": "manual"}
                        elif old_trigger.get("type") in ["afterStep", "stepComplete"]:
                            step["startTrigger"] = {
                                "type": "afterStep",
                                "stepId": old_trigger.get("stepId"),
                            }
                        elif "on" in old_trigger:
                            step["startTrigger"] = {
                                "type": "afterStep",
                                "stepId": old_trigger["on"],
                            }
                    del step["trigger"]

                # If step has no startTrigger, default to programStart
                if "startTrigger" not in step:
                    step["startTrigger"] = {"type": "programStart"}

            # Second pass: Fix step sequencing to eliminate overlaps
            # Create a mapping of step IDs to step indices
            step_id_to_index = {step.get("stepId"): i for i, step in enumerate(steps)}

            # Process each step and fix its trigger if needed
            for i, step in enumerate(steps):
                start_trigger = step.get("startTrigger", {})

                # If step should start after another step, ensure proper sequencing
                if start_trigger.get("type") in ["afterStep", "afterStepWithBuffer"]:
                    referenced_step_id = start_trigger.get("stepId")

                    # If referenced step doesn't exist, fix the reference
                    if referenced_step_id not in step_id_to_index:
                        # Find the previous step in the track
                        if i > 0:
                            prev_step = steps[i - 1]
                            step["startTrigger"] = {
                                "type": "afterStep",
                                "stepId": prev_step.get("stepId"),
                            }
                            if self.verbose:
                                print(
                                    f"Fixed step '{step.get('name', step.get('stepId'))}' to start after '{prev_step.get('name', prev_step.get('stepId'))}'"
                                )
                        else:
                            # First step should start with program
                            step["startTrigger"] = {"type": "programStart"}
                            if self.verbose:
                                print(
                                    f"Fixed step '{step.get('name', step.get('stepId'))}' to start with program"
                                )
                    else:
                        # Ensure the referenced step comes before this step
                        ref_index = step_id_to_index[referenced_step_id]
                        if ref_index >= i:
                            # Move the referenced step before this step
                            ref_step = steps.pop(ref_index)
                            steps.insert(i - 1, ref_step)
                            # Update indices
                            step_id_to_index = {
                                step.get("stepId"): j for j, step in enumerate(steps)
                            }
                            if self.verbose:
                                print(
                                    f"Moved step '{ref_step.get('name', ref_step.get('stepId'))}' before '{step.get('name', step.get('stepId'))}'"
                                )

                # If step has no explicit trigger and isn't the first step, make it start after the previous step
                elif i > 0 and start_trigger.get("type") == "programStart":
                    prev_step = steps[i - 1]
                    step["startTrigger"] = {
                        "type": "afterStep",
                        "stepId": prev_step.get("stepId"),
                    }
                    if self.verbose:
                        print(
                            f"Fixed step '{step.get('name', step.get('stepId'))}' to start after '{prev_step.get('name', prev_step.get('stepId'))}'"
                        )

                # If step has manual trigger and isn't the first step, make it start after the previous step
                elif i > 0 and start_trigger.get("type") == "manual":
                    prev_step = steps[i - 1]
                    step["startTrigger"] = {
                        "type": "afterStep",
                        "stepId": prev_step.get("stepId"),
                    }
                    if self.verbose:
                        print(
                            f"Fixed step '{step.get('name', step.get('stepId'))}' to start after '{prev_step.get('name', prev_step.get('stepId'))}'"
                        )

    def _adjust_variable_durations(self, program: dict):
        """
        Adjust variable durations to use optimal values in the optimized program.

        Args:
            program: Program to modify
        """
        for track in program.get("tracks", []):
            for step in track.get("steps", []):
                duration = step.get("duration", {})
                if isinstance(duration, dict) and duration.get("type") == "variable":
                    # If optimalSeconds is specified, use it as defaultSeconds
                    if "optimalSeconds" in duration:
                        optimal = duration["optimalSeconds"]
                        # Ensure optimal is within min and max bounds
                        min_seconds = duration.get("minSeconds", 0)
                        max_seconds = duration.get("maxSeconds", float("inf"))
                        if min_seconds <= optimal <= max_seconds:
                            duration["defaultSeconds"] = optimal
                            if self.verbose:
                                print(
                                    f"Adjusted default duration for step '{step.get('id', '')}' to optimal value: {optimal}"
                                )

    def _can_start(
        self,
        step: Step,
        completed: Set[Tuple[str, str]],
        start_times: Dict[str, Dict[str, float]],
    ) -> bool:
        """
        Check if a step can start based on dependencies.

        Args:
            step: The step to check
            completed: Set of (track_id, step_id) tuples for completed steps
            start_times: Dictionary of start times for scheduled steps

        Returns:
            True if the step can start, False otherwise
        """
        # Check dependencies
        for track_id, step_id in step.dependencies:
            if (track_id, step_id) not in completed:
                return False

        # Check previous step in the same track
        track_steps = list(self.steps[step.track_id].keys())
        if track_steps:
            step_index = track_steps.index(step.id)
            if step_index > 0:
                prev_step_id = track_steps[step_index - 1]
                if (step.track_id, prev_step_id) not in completed:
                    return False

        return True

    def _calculate_start_time(
        self, step: Step, start_times: Dict[str, Dict[str, float]]
    ) -> float:
        """
        Calculate the start time for a step based on dependencies.

        Args:
            step: The step to calculate start time for
            start_times: Dictionary of start times for scheduled steps

        Returns:
            Start time in seconds
        """
        # Initialize with track start time or 0
        start_time = self.program.get("tracks", [{}])[0].get("startTime", 0)

        # Check dependencies
        for track_id, step_id in step.dependencies:
            dep_start = start_times[track_id][step_id]
            dep_step = self.steps[track_id][step_id]
            dep_end = dep_start + dep_step.calculate_duration()
            start_time = max(start_time, dep_end)

        # Check previous step in the same track
        track_steps = list(self.steps[step.track_id].keys())
        if track_steps:
            step_index = track_steps.index(step.id)
            if step_index > 0:
                prev_step_id = track_steps[step_index - 1]
                prev_start = start_times[step.track_id][prev_step_id]
                prev_step = self.steps[step.track_id][prev_step_id]
                prev_end = prev_start + prev_step.calculate_duration()
                start_time = max(start_time, prev_end)

        return start_time

    def _stagger_track_starts(
        self, program: dict, bottlenecks: List[Tuple[str, float, float, int]]
    ):
        """
        Stagger track start times to reduce resource contention.

        Args:
            program: Program to modify
            bottlenecks: List of resource bottlenecks
        """
        if not bottlenecks:
            return

        # Get tracks that use bottleneck resources
        tracks = program.get("tracks", [])

        # Extract bottleneck resources
        bottleneck_resources = set(b[0] for b in bottlenecks)

        # Group tracks by resource usage
        resource_to_tracks: Dict[str, List[int]] = {}
        for resource in bottleneck_resources:
            resource_to_tracks[resource] = []

        # Track priorities based on the average priority of steps in each track
        track_priorities = {}

        for i, track in enumerate(tracks):
            track_id = track.get("id", "")
            track_resources = set()
            track_steps = []

            for step in track.get("steps", []):
                # Extract resource IDs/types instead of the full resource objects
                step_resource_ids = []
                for resource in step.get("resources", []):
                    if isinstance(resource, dict) and resource is not None:
                        # Extract resource identifier (resourceId, type, or id)
                        resource_id = resource.get("resourceId")
                        if resource_id is None:
                            resource_id = resource.get("type")
                        if resource_id is None:
                            resource_id = resource.get("id")
                        if resource_id:
                            step_resource_ids.append(resource_id)
                    else:
                        # Handle string resources directly
                        step_resource_ids.append(resource)

                step_resources = set(step_resource_ids)
                track_resources.update(step_resources)
                track_steps.append(step)

            # Calculate average priority for the track based on its steps
            if track_steps:
                avg_priority = sum(
                    step.get("priority", 100)
                    for step in track_steps
                    if step is not None
                ) / len(track_steps)
                track_priorities[i] = avg_priority
            else:
                track_priorities[i] = 100  # Default priority

            for resource in track_resources.intersection(bottleneck_resources):
                resource_to_tracks[resource].append(i)

        # Stagger track start times
        stagger_interval = 5  # seconds
        for resource, track_indices in resource_to_tracks.items():
            # Sort track indices by priority (lower priority number comes first)
            track_indices.sort(key=lambda idx: track_priorities[idx])

            for i, track_index in enumerate(track_indices):
                # Skip the first track (highest priority)
                if i == 0:
                    continue

                # Add stagger to track start time
                track = tracks[track_index]
                current_start = track.get("startTime", 0)
                track["startTime"] = current_start + i * stagger_interval

                if self.verbose:
                    print(
                        f"Staggered track '{track.get('id', '')}' start time to {track['startTime']} (priority: {track_priorities[track_index]:.1f})"
                    )

    def _stagger_step_starts(
        self, program: dict, bottlenecks: List[Tuple[str, float, float, int]]
    ):
        """
        Stagger step start times within tracks to reduce resource contention.

        Args:
            program: Program to modify
            bottlenecks: List of resource bottlenecks
        """
        if not bottlenecks:
            return

        # Extract bottleneck resources
        bottleneck_resources = set(b[0] for b in bottlenecks)

        # Process tracks
        for track in program.get("tracks", []):
            # Find steps that use bottleneck resources and their priorities
            bottleneck_steps = []
            for i, step in enumerate(track.get("steps", [])):
                # Extract resource IDs/types instead of the full resource objects
                step_resource_ids = []
                for resource in step.get("resources", []):
                    if isinstance(resource, dict) and resource is not None:
                        # Extract resource identifier (resourceId, type, or id)
                        resource_id = resource.get("resourceId")
                        if resource_id is None:
                            resource_id = resource.get("type")
                        if resource_id is None:
                            resource_id = resource.get("id")
                        if resource_id:
                            step_resource_ids.append(resource_id)
                    else:
                        # Handle string resources directly
                        step_resource_ids.append(resource)

                step_resources = set(step_resource_ids)
                if step_resources.intersection(bottleneck_resources):
                    priority = step.get("priority", 100)
                    bottleneck_steps.append((i, priority))

            # Sort bottleneck steps by priority (higher priority/lower number first)
            bottleneck_steps.sort(key=lambda x: x[1])
            bottleneck_step_indices = [idx for idx, _ in bottleneck_steps]

            # Add padding before bottleneck steps
            padding = 2  # seconds
            for step_index in bottleneck_step_indices:
                # Skip the first step
                if step_index == 0:
                    continue

                # Add padding step before bottleneck step
                padding_step = {
                    "id": f"padding_{track.get('id', '')}_{step_index}",
                    "name": "Resource contention padding",
                    "description": "Added automatically to reduce resource contention",
                    "duration": padding,
                    "resources": [],
                }

                track["steps"].insert(step_index, padding_step)

                # Update indices for remaining bottleneck steps
                bottleneck_step_indices = [
                    i + 1 if i >= step_index else i for i in bottleneck_step_indices
                ]

                if self.verbose:
                    step_priority = track.get("steps", [])[step_index + 1].get(
                        "priority", 100
                    )  # +1 because we inserted a step
                    print(
                        f"Added padding step before step {step_index} in track '{track.get('id', '')}' (priority: {step_priority})"
                    )

    def _preserve_required_fields(self, program: dict):
        """
        Ensure all required fields for schema validation are preserved.

        Args:
            program: Program to modify
        """
        # Preserve all top-level fields that might be required
        required_fields = [
            "programId",
            "name",
            "description",
            "version",
            "environment",
            "environmentType",
            "actors",
            "duration",
            "startTrigger",
            "resourceConstraints",
            "trackTemplates",
            "metadata",
        ]

        # Copy any missing fields from the original program
        for field in required_fields:
            if field in self.program and field not in program:
                program[field] = self.program[field]

        # Don't add resourceConstraints if the program already has environment or environmentType
        # This would violate the schema's not clause
        if (
            "environment" in program or "environmentType" in program
        ) and "resourceConstraints" not in self.program:
            if "resourceConstraints" in program:
                del program["resourceConstraints"]


def save_optimized_program(program: dict, output_file: str):
    """
    Save the optimized program to a file.

    Args:
        program: Program to save
        output_file: Path to the output file
    """
    _, file_extension = os.path.splitext(output_file)

    with open(output_file, "w") as f:
        if file_extension.lower() in [".yaml", ".yml"]:
            yaml.dump(program, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(program, f, indent=2)


def planned_makespan(program: dict) -> float:
    """
    Seconds from program start to the end of the last step, following start
    triggers and durations (resource waits not included). Uses the
    validator's trigger arithmetic, which reads the current trackId/stepId
    schema.
    """
    from .validate_program import calculate_step_start_time, parse_duration_to_seconds

    end = 0.0
    for track in program.get("tracks", []):
        steps = track.get("steps", [])
        for step in steps:
            start = calculate_step_start_time(step, steps, program)
            end = max(end, start + parse_duration_to_seconds(step.get("duration")))
    return end


def plan_program(
    input_file: str,
    output_file: str,
    verbose: bool = False,
    environment_file: Optional[str] = None,
    workcell: Optional[str] = None,
) -> bool:
    """
    Plan and optimize a program schedule.

    Args:
        input_file: Path to the input program file
        output_file: Path to the output program file
        verbose: Whether to print verbose information
        environment_file: Path to the environment file
        workcell: Workcell file; instrument steps without a duration are
            estimated from its tools (else from their params, else a default)

    Returns:
        True if successful, False otherwise
    """
    try:
        program = load_program_file(input_file)
        program, estimate_lines = fill_instrument_durations(program, workcell)
        for line in estimate_lines:
            print(line)
        environment = None
        if environment_file:
            environment = load_program_file(environment_file)
        planner = ProgramPlanner(program, verbose, environment)
        optimized_program = planner.optimize_schedule()
        save_optimized_program(optimized_program, output_file)
        print(
            "Makespan (by start triggers and durations): "
            f"{planned_makespan(optimized_program):g} s"
        )
        if verbose:
            print(f"Saved optimized program to {output_file}")
        return True
    except Exception as e:
        print(f"Error planning program: {e}")
        if verbose:
            import traceback

            traceback.print_exc()
        return False


if __name__ == "__main__":
    # Example usage
    plan_program(
        "examples/breakfast_schedule.json",
        "examples/optimized_breakfast_schedule.json",
        verbose=True,
    )
