#!/usr/bin/env python3
"""
Auto-planner for Rhylthyme programs.

This module implements automatic timeline planning with various strategies:
- Synchronized finish: All tracks finish around the same time
- Cover: Minimize dead time/pauses between activities
- Minimize overall length: Shortest total duration

Features:
- Respects track priorities
- Avoids overutilization of resources
- Handles flexible (flex) steps that can expand to fill gaps
- Considers step min/max duration limits

Scope: this module has no callers outside its own ``__main__`` CLI (the
browser planners in rhylthyme-server/static/js/auto-plan.js are what the
product uses) and it does not run replicate expansion, so it cannot honour
program schema 0.3.0-alpha's per-instance constructs. ``auto_plan_program``
refuses a program that declares ``instances`` or ``replicates.maxInFlight``
rather than silently planning a schedule that breaks them.
"""

import copy
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class PlanningStrategy(Enum):
    SYNCHRONIZED_FINISH = "synchronized_finish"
    COVER = "cover"
    MINIMIZE_LENGTH = "minimize_length"


@dataclass
class ResourceConstraint:
    name: str
    capacity: float = 1.0


@dataclass
class ResourceUsage:
    resource: str
    start_time: float
    end_time: float
    fraction: float = 1.0
    step_id: str = ""
    track_id: str = ""


@dataclass
class StepTiming:
    step_id: str
    track_id: str
    start_time: float
    duration: float
    end_time: float
    is_flex: bool = False
    min_duration: float = 0
    max_duration: float = float("inf")
    priority: int = 100
    resources: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.resources is None:
            self.resources = []
        self.end_time = self.start_time + self.duration


@dataclass
class TrackInfo:
    track_id: str
    name: str
    priority: int = 100
    steps: List[Dict] = field(default_factory=list)

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


class AutoPlanner:
    """
    Auto-planner that implements various planning strategies for Rhylthyme programs.
    """

    def __init__(
        self,
        program: Dict[str, Any],
        resource_constraints: Optional[List[Dict[str, Any]]] = None,
    ):
        self.program = program
        self.resource_constraints = self._parse_resource_constraints(
            resource_constraints or []
        )
        self.tracks = self._parse_tracks()
        self.steps = self._parse_steps()

    def _parse_resource_constraints(
        self, constraints: List[Dict[str, Any]]
    ) -> Dict[str, ResourceConstraint]:
        """Parse resource constraints from the program.

        Accepts the schema shape ``{"task": ..., "maxConcurrent": ...}`` as
        well as the legacy ``{"name": ..., "capacity": ...}`` shape this
        module was originally written against.
        """
        result = {}
        for constraint in constraints:
            name = constraint.get("name") or constraint.get("task") or ""
            if "capacity" in constraint:
                capacity = constraint.get("capacity", 1.0)
            else:
                capacity = constraint.get("maxConcurrent", 1.0)
            if not name:
                continue
            result[name] = ResourceConstraint(name=name, capacity=capacity)
        return result

    def _parse_tracks(self) -> Dict[str, TrackInfo]:
        """Parse track information including priorities."""
        tracks = {}
        for track_data in self.program.get("tracks", []):
            track_id = track_data.get("trackId", "")
            name = track_data.get("name", "")
            priority = track_data.get("priority", 100)
            steps = track_data.get("steps", [])

            tracks[track_id] = TrackInfo(
                track_id=track_id, name=name, priority=priority, steps=steps
            )
        return tracks

    def _parse_steps(self) -> Dict[str, Dict]:
        """Parse all steps from all tracks."""
        steps = {}
        for track in self.program.get("tracks", []):
            track_id = track.get("trackId", "")
            for step in track.get("steps", []):
                step_id = step.get("stepId", "")
                step_copy = copy.deepcopy(step)
                step_copy["trackId"] = track_id
                steps[step_id] = step_copy
        return steps

    def _parse_duration(self, duration_spec: Any) -> Tuple[float, float, float]:
        """
        Parse duration specification into (min, optimal, max) seconds.

        Args:
            duration_spec: Duration specification from step

        Returns:
            Tuple of (min_duration, optimal_duration, max_duration) in seconds
        """
        if isinstance(duration_spec, (int, float)):
            return (duration_spec, duration_spec, duration_spec)

        if isinstance(duration_spec, dict):
            duration_type = duration_spec.get("type", "fixed")

            if duration_type == "fixed":
                seconds = self._parse_time_string(duration_spec.get("seconds", 0))
                return (seconds, seconds, seconds)

            elif duration_type == "variable":
                min_sec = self._parse_time_string(duration_spec.get("minSeconds", 0))
                max_sec = self._parse_time_string(
                    duration_spec.get("maxSeconds", min_sec)
                )
                default_sec = self._parse_time_string(
                    duration_spec.get("defaultSeconds", (min_sec + max_sec) / 2)
                )
                optimal_sec = self._parse_time_string(
                    duration_spec.get("optimalSeconds", default_sec)
                )
                return (min_sec, optimal_sec, max_sec)

            elif duration_type == "indefinite":
                return (0, 3600, float("inf"))  # Default 1 hour optimal for indefinite

        return (0, 0, 0)

    def _parse_time_string(self, time_str: Any) -> float:
        """Parse time string like '30s', '2m', '1h' into seconds."""
        if isinstance(time_str, (int, float)):
            return float(time_str)

        if not isinstance(time_str, str):
            return 0.0

        time_str = str(time_str).strip().lower()

        if time_str.endswith("s"):
            return float(time_str[:-1])
        elif time_str.endswith("m"):
            return float(time_str[:-1]) * 60
        elif time_str.endswith("h"):
            return float(time_str[:-1]) * 3600
        else:
            try:
                return float(time_str)
            except ValueError:
                return 0.0

    def _get_step_resources(self, step: Dict[str, Any]) -> List[str]:
        """Extract resource requirements from a step."""
        resources = []

        # Single task
        if "task" in step:
            resources.append(step["task"])

        # Multiple tasks
        if "tasks" in step:
            resources.extend(step["tasks"])

        # Task resources with fractions
        if "taskResources" in step:
            for res in step["taskResources"]:
                resources.append(res.get("name", ""))

        return [r for r in resources if r]  # Remove empty strings

    def _build_dependency_graph(self) -> Dict[str, List[str]]:
        """Build dependency graph: step_id -> [prerequisite_step_ids]."""
        dependencies: Dict[str, Any] = {}

        for step_id, step in self.steps.items():
            dependencies[step_id] = []

            start_trigger = step.get("trigger", {})
            deps = self._extract_dependencies_from_trigger(start_trigger)
            dependencies[step_id].extend(deps)

        return dependencies

    def _extract_dependencies_from_trigger(self, trigger: Dict[str, Any]) -> List[str]:
        """Extract step dependencies from a start trigger."""
        deps = []

        # If no type is specified but stepId or on is present, default to afterStep
        trigger_type = trigger.get("type")
        if not trigger_type and (trigger.get("stepId") or trigger.get("on")):
            trigger_type = "afterStep"

        if trigger_type in ["afterStep", "afterStepWithBuffer"]:
            step_id = trigger.get("stepId") or trigger.get("on")
            if step_id:
                deps.append(step_id)

        elif trigger.get("logic"):  # Multiple triggers
            for sub_trigger in trigger.get("triggers", []):
                deps.extend(self._extract_dependencies_from_trigger(sub_trigger))

        return deps

    def _calculate_earliest_start_times(
        self, dependencies: Dict[str, List[str]]
    ) -> Dict[str, float]:
        """Calculate earliest possible start time for each step."""
        earliest_times: Dict[str, float] = {}

        def calculate_earliest(
            step_id: str, visited: Optional[Set[str]] = None
        ) -> float:
            if visited is None:
                visited = set()

            if step_id in visited:
                return 0  # Cycle detected

            if step_id in earliest_times:
                return earliest_times[step_id]

            visited.add(step_id)

            max_predecessor_end = 0.0

            for dep_step_id in dependencies.get(step_id, []):
                if dep_step_id in self.steps:
                    dep_start = calculate_earliest(dep_step_id, visited.copy())
                    _, dep_duration, _ = self._parse_duration(
                        self.steps[dep_step_id].get("duration", 0)
                    )
                    dep_end = dep_start + dep_duration
                    max_predecessor_end = max(max_predecessor_end, dep_end)

            earliest_times[step_id] = max_predecessor_end
            return max_predecessor_end

        for step_id in self.steps:
            calculate_earliest(step_id)

        return earliest_times

    def _check_resource_overutilization(
        self, step_timings: List[StepTiming]
    ) -> List[Tuple[str, float, float]]:
        """
        Check for resource overutilization in the current plan.

        Returns:
            List of (resource_name, start_time, end_time) tuples for overutilized periods
        """
        overutilizations = []

        # Group resource usage by resource
        resource_usage: Dict[str, Any] = {}
        for timing in step_timings:
            for resource in timing.resources:
                if resource not in resource_usage:
                    resource_usage[resource] = []

                resource_usage[resource].append(
                    ResourceUsage(
                        resource=resource,
                        start_time=timing.start_time,
                        end_time=timing.end_time,
                        step_id=timing.step_id,
                        track_id=timing.track_id,
                    )
                )

        # Check each resource for overutilization
        for resource_name, usages in resource_usage.items():
            constraint = self.resource_constraints.get(resource_name)
            if not constraint:
                continue  # No constraint defined

            # Sort usages by start time
            usages.sort(key=lambda x: x.start_time)

            # Find overlapping periods
            for i, usage1 in enumerate(usages):
                concurrent_count = 1
                for j in range(i + 1, len(usages)):
                    usage2 = usages[j]

                    # Check if they overlap
                    if usage2.start_time < usage1.end_time:
                        concurrent_count += 1
                    else:
                        break

                if concurrent_count > constraint.capacity:
                    overutilizations.append(
                        (resource_name, usage1.start_time, usage1.end_time)
                    )

        return overutilizations

    def _apply_flex_adjustments(
        self, step_timings: List[StepTiming]
    ) -> List[StepTiming]:
        """Apply flex adjustments to fill gaps between steps."""
        # Group timings by track
        track_timings: Dict[str, Any] = {}
        for timing in step_timings:
            if timing.track_id not in track_timings:
                track_timings[timing.track_id] = []
            track_timings[timing.track_id].append(timing)

        # Process each track
        adjusted_timings = []
        for track_id, timings in track_timings.items():
            # Sort by start time
            timings.sort(key=lambda x: x.start_time)

            for i, timing in enumerate(timings):
                step = self.steps.get(timing.step_id, {})
                flex_config = step.get("flex", {})

                if not flex_config.get("enabled", False):
                    adjusted_timings.append(timing)
                    continue

                # Find next step in the same track
                next_step_start = None
                if i + 1 < len(timings):
                    next_step_start = timings[i + 1].start_time

                # Calculate flex limits
                min_dur = timing.min_duration
                max_dur = timing.max_duration

                if flex_config.get("minDuration"):
                    min_dur = max(
                        min_dur, self._parse_time_string(flex_config["minDuration"])
                    )

                if flex_config.get("maxDuration"):
                    max_dur = min(
                        max_dur, self._parse_time_string(flex_config["maxDuration"])
                    )

                # Apply flex based on mode
                flex_mode = flex_config.get("mode", "fill")
                new_duration = timing.duration

                if flex_mode == "fill" and next_step_start is not None:
                    # Fill gap to next step
                    available_time = next_step_start - timing.start_time
                    new_duration = min(max_dur, max(min_dur, available_time))

                elif flex_mode == "max":
                    # Extend to maximum duration
                    new_duration = max_dur

                # Update timing
                adjusted_timing = copy.deepcopy(timing)
                adjusted_timing.duration = new_duration
                adjusted_timing.end_time = adjusted_timing.start_time + new_duration
                adjusted_timings.append(adjusted_timing)

        return adjusted_timings

    def plan_synchronized_finish(self) -> List[StepTiming]:
        """
        Plan timeline with synchronized finish strategy.
        All tracks should finish around the same time.
        """
        dependencies = self._build_dependency_graph()
        earliest_times = self._calculate_earliest_start_times(dependencies)

        # Calculate track durations at earliest start times
        track_durations = {}
        step_timings = []

        for track_id, track_info in self.tracks.items():
            track_duration = 0.0
            track_steps = []

            for step in track_info.steps:
                step_id = step.get("stepId", "")
                start_time = earliest_times.get(step_id, 0)
                min_dur, opt_dur, max_dur = self._parse_duration(
                    step.get("duration", 0)
                )

                step_timing = StepTiming(
                    step_id=step_id,
                    track_id=track_id,
                    start_time=start_time,
                    duration=opt_dur,
                    end_time=start_time + opt_dur,
                    min_duration=min_dur,
                    max_duration=max_dur,
                    priority=step.get("priority", 100),
                    resources=self._get_step_resources(step),
                )

                track_steps.append(step_timing)
                track_duration = max(track_duration, step_timing.end_time)

            track_durations[track_id] = track_duration
            step_timings.extend(track_steps)

        # Find the longest track
        max_duration = max(track_durations.values()) if track_durations else 0

        # Delay shorter tracks to finish at the same time
        for track_id, duration in track_durations.items():
            if duration < max_duration:
                delay = max_duration - duration

                # Apply delay to all steps in this track
                for timing in step_timings:
                    if timing.track_id == track_id:
                        timing.start_time += delay
                        timing.end_time += delay

        # Apply flex adjustments
        step_timings = self._apply_flex_adjustments(step_timings)

        return step_timings

    def plan_cover(self) -> List[StepTiming]:
        """
        Plan timeline with cover strategy.
        Minimize dead time/pauses between activities.
        """
        dependencies = self._build_dependency_graph()
        earliest_times = self._calculate_earliest_start_times(dependencies)

        step_timings = []

        # Sort tracks by priority (higher priority = lower number)
        sorted_tracks = sorted(self.tracks.items(), key=lambda x: x[1].priority)

        for track_id, track_info in sorted_tracks:
            for step in track_info.steps:
                step_id = step.get("stepId", "")
                start_time = earliest_times.get(step_id, 0)
                min_dur, opt_dur, max_dur = self._parse_duration(
                    step.get("duration", 0)
                )

                step_timing = StepTiming(
                    step_id=step_id,
                    track_id=track_id,
                    start_time=start_time,
                    duration=opt_dur,
                    end_time=start_time + opt_dur,
                    min_duration=min_dur,
                    max_duration=max_dur,
                    priority=step.get("priority", 100),
                    resources=self._get_step_resources(step),
                )

                step_timings.append(step_timing)

        # Apply flex adjustments to minimize gaps
        step_timings = self._apply_flex_adjustments(step_timings)

        return step_timings

    def plan_minimize_length(self) -> List[StepTiming]:
        """
        Plan timeline with minimize overall length strategy.
        Shortest total duration while respecting dependencies.
        """
        dependencies = self._build_dependency_graph()
        earliest_times = self._calculate_earliest_start_times(dependencies)

        step_timings = []

        for step_id, step in self.steps.items():
            start_time = earliest_times.get(step_id, 0)
            min_dur, opt_dur, max_dur = self._parse_duration(step.get("duration", 0))

            # Use minimum duration for shortest timeline
            duration = min_dur if min_dur > 0 else opt_dur

            step_timing = StepTiming(
                step_id=step_id,
                track_id=step.get("trackId", ""),
                start_time=start_time,
                duration=duration,
                end_time=start_time + duration,
                min_duration=min_dur,
                max_duration=max_dur,
                priority=step.get("priority", 100),
                resources=self._get_step_resources(step),
            )

            step_timings.append(step_timing)

        return step_timings

    def auto_plan(self, strategy: PlanningStrategy) -> Dict[str, Any]:
        """
        Generate an auto-planned timeline using the specified strategy.

        Args:
            strategy: The planning strategy to use

        Returns:
            Dictionary with the planned timeline and metadata
        """
        if strategy == PlanningStrategy.SYNCHRONIZED_FINISH:
            step_timings = self.plan_synchronized_finish()
        elif strategy == PlanningStrategy.COVER:
            step_timings = self.plan_cover()
        elif strategy == PlanningStrategy.MINIMIZE_LENGTH:
            step_timings = self.plan_minimize_length()
        else:
            raise ValueError(f"Unknown planning strategy: {strategy}")

        # Check for resource overutilization
        overutilizations = self._check_resource_overutilization(step_timings)

        # Calculate total duration
        total_duration = (
            max(timing.end_time for timing in step_timings) if step_timings else 0
        )

        # Format results
        result = {
            "strategy": strategy.value,
            "totalDuration": total_duration,
            "overutilizations": [
                {"resource": res_name, "startTime": start_time, "endTime": end_time}
                for res_name, start_time, end_time in overutilizations
            ],
            "stepTimings": [
                {
                    "stepId": timing.step_id,
                    "trackId": timing.track_id,
                    "startTime": timing.start_time,
                    "duration": timing.duration,
                    "endTime": timing.end_time,
                    "isFlex": timing.is_flex,
                    "minDuration": timing.min_duration,
                    "maxDuration": timing.max_duration,
                    "priority": timing.priority,
                    "resources": timing.resources,
                }
                for timing in step_timings
            ],
        }

        return result


UNSUPPORTED_0_3_0 = (
    "auto_planner does not support program schema 0.3.0-alpha per-instance "
    "constructs ({found}). It has no callers outside this CLI and does not "
    'expand replicates, so it cannot honour instances:"each"/"all" '
    "barriers or replicates.maxInFlight. Plan this program in the web "
    "visualizer (rhylthyme-server/static/js/auto-plan.js), which respects "
    "both maxConcurrent and maxInFlight."
)


def _unsupported_constructs(program: Dict[str, Any]) -> List[str]:
    """Names of the 0.3.0-alpha constructs this planner cannot honour."""
    found = []

    def walk_trigger(trigger):
        if not isinstance(trigger, dict):
            return
        if trigger.get("instances") and "instances" not in found:
            found.append("instances")
        for sub in trigger.get("triggers", []) or []:
            walk_trigger(sub)

    for track in program.get("tracks", []) or []:
        for step in track.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            walk_trigger(step.get("startTrigger"))
            walk_trigger(step.get("trigger"))
            replicates = step.get("replicates")
            if (
                isinstance(replicates, dict)
                and replicates.get("maxInFlight") is not None
            ):
                if "maxInFlight" not in found:
                    found.append("maxInFlight")
    return found


def auto_plan_program(
    program_file: str,
    strategy: str = "cover",
    resource_constraints_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Auto-plan a program with the specified strategy.

    Args:
        program_file: Path to the program JSON file
        strategy: Planning strategy ("synchronized_finish", "cover", "minimize_length")
        resource_constraints_file: Optional path to resource constraints JSON file

    Returns:
        Dictionary with the planned timeline

    Raises:
        ValueError: if the program declares `instances` or `replicates.maxInFlight`.
    """
    # Load program
    with open(program_file, "r") as f:
        program = json.load(f)

    unsupported = _unsupported_constructs(program)
    if unsupported:
        raise ValueError(UNSUPPORTED_0_3_0.format(found=", ".join(unsupported)))

    # Load resource constraints if provided
    resource_constraints = []
    if resource_constraints_file:
        with open(resource_constraints_file, "r") as f:
            constraints_data = json.load(f)
            resource_constraints = constraints_data.get("resources", [])

    # Map strategy string to enum
    strategy_map = {
        "synchronized_finish": PlanningStrategy.SYNCHRONIZED_FINISH,
        "cover": PlanningStrategy.COVER,
        "minimize_length": PlanningStrategy.MINIMIZE_LENGTH,
    }

    strategy_enum = strategy_map.get(strategy)
    if not strategy_enum:
        raise ValueError(f"Unknown strategy: {strategy}")

    # Create planner and generate plan
    planner = AutoPlanner(program, resource_constraints)
    return planner.auto_plan(strategy_enum)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Auto-plan a Rhylthyme program")
    parser.add_argument("program_file", help="Path to program JSON file")
    parser.add_argument(
        "--strategy",
        choices=["synchronized_finish", "cover", "minimize_length"],
        default="cover",
        help="Planning strategy",
    )
    parser.add_argument("--resources", help="Path to resource constraints JSON file")
    parser.add_argument("--output", help="Output file for the plan")

    args = parser.parse_args()

    try:
        result = auto_plan_program(args.program_file, args.strategy, args.resources)

        if args.output:
            with open(args.output, "w") as f:
                json.dump(result, f, indent=2)
            print(f"Plan saved to {args.output}")
        else:
            print(json.dumps(result, indent=2))

    except Exception as e:
        print(f"Error: {e}")
        exit(1)
