#!/usr/bin/env python3
"""
Environment Schema Validation System

This module provides schemas and validation for environment catalog files,
ensuring they conform to the expected structure for their environment type.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import jsonschema

# Base environment schema that all environments must conform to
BASE_ENVIRONMENT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Base Environment Schema",
    "description": "Base schema for environment catalog files",
    "type": "object",
    "required": ["environmentId", "name", "type", "resourceConstraints"],
    "properties": {
        "environmentId": {
            "type": "string",
            "description": "Unique identifier for the environment",
            "pattern": "^[a-z0-9-]+$",
        },
        "name": {
            "type": "string",
            "description": "Human-readable name for the environment",
        },
        "description": {
            "type": "string",
            "description": "Detailed description of the environment",
        },
        "type": {
            "type": "string",
            "description": "Type of environment (e.g., kitchen, laboratory, bakery, airport)",
            "enum": [
                "kitchen",
                "laboratory",
                "bakery",
                "airport",
                "restaurant",
                "home",
                "commercial-kitchen",
                "lab",
                "research",
                "biotech",
                "pharma",
                "medical",
                "artisan",
                "pastry",
                "aviation",
                "runway",
                "terminal",
                "manufacturing",
                "warehouse",
                "office",
                "hospital",
                "school",
                "retail",
                "farm",
                "datacenter",
                "factory",
                "workshop",
                "garage",
                "gym",
                "studio",
                "theater",
                "library",
                "garden",
                "greenhouse",
                "clinic",
                "spa",
                "hotel",
            ],
        },
        "actors": {
            "type": "integer",
            "minimum": 1,
            "description": "Legacy: Total number of actors (use actorTypes for new environments)",
        },
        "actorTypes": {
            "type": "object",
            "description": "Definition of different actor types and their counts",
            "patternProperties": {
                "^[a-z0-9-]+$": {
                    "type": "object",
                    "required": ["name", "count"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Human-readable name for the actor type",
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Number of actors of this type",
                        },
                        "description": {
                            "type": "string",
                            "description": "Description of the actor type's role",
                        },
                        "qualifications": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of qualifications this actor type possesses",
                        },
                    },
                }
            },
        },
        "resourceConstraints": {
            "type": "array",
            "description": "List of resource constraints for this environment",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["task", "maxConcurrent", "description"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Name of the task/resource type",
                    },
                    "maxConcurrent": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of concurrent uses of this resource",
                    },
                    "actorsRequired": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Number of actors required for this task",
                    },
                    "qualifiedActorTypes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Actor types qualified to perform this task",
                    },
                    "description": {
                        "type": "string",
                        "description": "Description of the resource or equipment",
                    },
                },
            },
        },
        "metadata": {
            "type": "object",
            "description": "Additional metadata about the environment",
        },
    },
    "anyOf": [{"required": ["actors"]}, {"required": ["actorTypes"]}],
}

# Type-specific task definitions for different environment types
ENVIRONMENT_TYPE_TASKS = {
    "kitchen": {
        "required_tasks": ["stove-burner", "prep-work", "cleanup"],
        "common_tasks": [
            "stove-burner",
            "oven",
            "microwave",
            "toaster",
            "prep-work",
            "plating",
            "cleanup",
            "mixer",
            "refrigerator",
            "dishwasher",
            "grill",
            "fryer",
            "steamer",
            "food-processor",
            "walk-in-cooler",
            "freezer",
            "expediting",
            "service-window",
            "prep-station",
            "cold-prep",
            "grill-station",
            "pastry-oven",
            "dishwashing",
        ],
        "actor_types": [
            "home-cook",
            "chef",
            "cook",
            "prep-cook",
            "head-chef",
            "sous-chef",
            "line-cook",
            "dishwasher",
            "chef-de-partie",
            "commis-chef",
            "kitchen-porter",
            "pastry-chef",
            "expediter",
        ],
    },
    "laboratory": {
        "required_tasks": ["bench-space", "cleanup"],
        "common_tasks": [
            "fume-hood",
            "biosafety-cabinet",
            "incubator",
            "centrifuge",
            "microscope",
            "spectrophotometer",
            "pcr-machine",
            "hplc",
            "mass-spectrometer",
            "flow-cytometer",
            "bench-space",
            "prep-work",
            "autoclave",
            "freezer-80",
            "liquid-nitrogen",
            "data-analysis",
            "cleanup",
        ],
        "actor_types": [
            "principal-investigator",
            "lab-manager",
            "senior-scientist",
            "postdoc",
            "grad-student",
            "lab-technician",
            "undergrad",
        ],
    },
    "bakery": {
        "required_tasks": ["oven", "mixer", "work-bench"],
        "common_tasks": [
            "oven",
            "convection-oven",
            "proofer",
            "mixer",
            "dough-sheeter",
            "work-bench",
            "prep-work",
            "decorating-station",
            "cooling-rack",
            "packaging",
            "cleanup",
            "refrigeration",
            "freezer",
            "laminator",
        ],
        "actor_types": ["head-baker", "pastry-chef", "baker", "assistant-baker"],
    },
    "airport": {
        "required_tasks": ["runway", "gate"],
        "common_tasks": [
            "runway",
            "gate",
            "baggage-handling",
            "check-in-counter",
            "security-checkpoint",
            "air-traffic-control",
            "ground-crew",
            "fueling-station",
            "maintenance-hangar",
            "cargo-terminal",
            "customs",
            "immigration",
        ],
        "actor_types": [
            "pilot",
            "air-traffic-controller",
            "ground-crew",
            "baggage-handler",
            "security-officer",
            "check-in-agent",
            "maintenance-technician",
        ],
    },
}

# Additional type aliases
ENVIRONMENT_TYPE_TASKS.update(
    {
        "home": ENVIRONMENT_TYPE_TASKS["kitchen"],
        "restaurant": ENVIRONMENT_TYPE_TASKS["kitchen"],
        "commercial-kitchen": ENVIRONMENT_TYPE_TASKS["kitchen"],
        "lab": ENVIRONMENT_TYPE_TASKS["laboratory"],
        "research": ENVIRONMENT_TYPE_TASKS["laboratory"],
        "biotech": ENVIRONMENT_TYPE_TASKS["laboratory"],
        "pharma": ENVIRONMENT_TYPE_TASKS["laboratory"],
        "artisan": ENVIRONMENT_TYPE_TASKS["bakery"],
        "pastry": ENVIRONMENT_TYPE_TASKS["bakery"],
        "aviation": ENVIRONMENT_TYPE_TASKS["airport"],
        "runway": ENVIRONMENT_TYPE_TASKS["airport"],
        "terminal": ENVIRONMENT_TYPE_TASKS["airport"],
    }
)


class EnvironmentValidator:
    """Validator for environment catalog files."""

    def __init__(self):
        """Initialize the validator with schemas."""
        self.base_schema = BASE_ENVIRONMENT_SCHEMA
        self.type_tasks = ENVIRONMENT_TYPE_TASKS

    def validate_environment(self, environment_data: Dict[str, Any]) -> List[str]:
        """
        Validate an environment against both base schema and type-specific requirements.

        Args:
            environment_data: The environment data to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        # Validate against base schema
        try:
            jsonschema.validate(environment_data, self.base_schema)
        except jsonschema.ValidationError as e:
            errors.append(f"Schema validation error: {e.message}")
            return errors  # Don't continue if basic schema is invalid

        # Get environment type for type-specific validation
        env_type = environment_data.get("type", "")

        # Validate type-specific requirements
        type_errors = self._validate_type_specific(environment_data, env_type)
        errors.extend(type_errors)

        # Validate actor types consistency
        actor_errors = self._validate_actor_types(environment_data, env_type)
        errors.extend(actor_errors)

        # Validate resource constraints
        resource_errors = self._validate_resource_constraints(environment_data)
        errors.extend(resource_errors)

        return errors

    def _validate_type_specific(
        self, environment_data: Dict[str, Any], env_type: str
    ) -> List[str]:
        """Validate type-specific requirements."""
        errors = []

        if env_type not in self.type_tasks:
            # Don't error for unknown types, just warn
            return []

        type_config = self.type_tasks[env_type]
        resource_constraints = environment_data.get("resourceConstraints", [])
        task_names = {constraint["task"] for constraint in resource_constraints}

        # Check for required tasks
        required_tasks = set(type_config.get("required_tasks", []))
        missing_required = required_tasks - task_names
        if missing_required:
            errors.append(
                f"Missing required tasks for {env_type}: {', '.join(missing_required)}"
            )

        # Check for unexpected tasks (warn only)
        common_tasks = set(type_config.get("common_tasks", []))
        unexpected_tasks = task_names - common_tasks
        if unexpected_tasks:
            # This is a warning, not an error
            errors.append(
                f"Warning: Uncommon tasks for {env_type}: {', '.join(unexpected_tasks)}"
            )

        return errors

    def _validate_actor_types(
        self, environment_data: Dict[str, Any], env_type: str
    ) -> List[str]:
        """Validate actor types are appropriate for environment type."""
        errors = []

        if env_type not in self.type_tasks:
            return []

        type_config = self.type_tasks[env_type]
        expected_actor_types = set(type_config.get("actor_types", []))

        # Check actor types (if using new format)
        if "actorTypes" in environment_data:
            actual_actor_types = set(environment_data["actorTypes"].keys())

            # Validate actor type names are reasonable for this environment
            unexpected_actors = actual_actor_types - expected_actor_types
            if unexpected_actors:
                errors.append(
                    f"Warning: Uncommon actor types for {env_type}: {', '.join(unexpected_actors)}"
                )

        return errors

    def _validate_resource_constraints(
        self, environment_data: Dict[str, Any]
    ) -> List[str]:
        """Validate resource constraints for consistency."""
        errors = []

        resource_constraints = environment_data.get("resourceConstraints", [])
        actor_types = environment_data.get("actorTypes", {})

        # Check that qualified actor types exist
        all_actor_type_ids = set(actor_types.keys())
        if environment_data.get("actors"):
            all_actor_type_ids.add("generic")  # Legacy support

        for constraint in resource_constraints:
            qualified_types = constraint.get("qualifiedActorTypes", [])
            for actor_type in qualified_types:
                if actor_type not in all_actor_type_ids:
                    errors.append(
                        f"Task '{constraint['task']}' references unknown actor type: {actor_type}"
                    )

        # Validate actors vs actorsRequired totals don't exceed capacity
        total_actors = 0
        if "actorTypes" in environment_data:
            total_actors = sum(
                actor_info["count"] for actor_info in actor_types.values()
            )
        elif "actors" in environment_data:
            total_actors = environment_data["actors"]

        max_required = 0
        for constraint in resource_constraints:
            actors_required = constraint.get("actorsRequired", 0)
            max_concurrent = constraint.get("maxConcurrent", 1)
            max_required = max(max_required, actors_required * max_concurrent)

        if max_required > total_actors:
            errors.append(
                f"Resource constraints require up to {max_required} actors but only {total_actors} available"
            )

        return errors

    def validate_environment_file(self, file_path: str) -> List[str]:
        """
        Validate an environment file.

        Args:
            file_path: Path to the environment file

        Returns:
            List of validation errors
        """
        try:
            with open(file_path, "r") as f:
                if file_path.endswith(".json"):
                    data = json.load(f)
                else:
                    import yaml

                    data = yaml.safe_load(f)

            return self.validate_environment(data)

        except FileNotFoundError:
            return [f"File not found: {file_path}"]
        except json.JSONDecodeError as e:
            return [f"JSON decode error: {e}"]
        except Exception as e:
            return [f"Error reading file: {e}"]

    def get_environment_type_info(self, env_type: str) -> Optional[Dict[str, Any]]:
        """Get information about a specific environment type."""
        return self.type_tasks.get(env_type)

    def list_supported_types(self) -> List[str]:
        """Get list of supported environment types."""
        return list(self.type_tasks.keys())

    def suggest_tasks_for_type(self, env_type: str) -> List[str]:
        """Suggest common tasks for an environment type."""
        if env_type in self.type_tasks:
            return self.type_tasks[env_type].get("common_tasks", [])
        return []

    def suggest_actor_types_for_type(self, env_type: str) -> List[str]:
        """Suggest actor types for an environment type."""
        if env_type in self.type_tasks:
            return self.type_tasks[env_type].get("actor_types", [])
        return []


def validate_all_environments(
    environments_dir: str = "environments",
) -> Dict[str, List[str]]:
    """
    Validate all environment files in a directory.

    Args:
        environments_dir: Directory containing environment files

    Returns:
        Dictionary mapping file names to validation errors
    """
    validator = EnvironmentValidator()
    results = {}

    env_path = Path(environments_dir)
    if not env_path.exists():
        return {"error": ["Environments directory not found"]}

    for file_path in env_path.glob("*.json"):
        errors = validator.validate_environment_file(str(file_path))
        if errors:
            results[file_path.name] = errors

    for file_path in env_path.glob("*.yaml"):
        errors = validator.validate_environment_file(str(file_path))
        if errors:
            results[file_path.name] = errors

    for file_path in env_path.glob("*.yml"):
        errors = validator.validate_environment_file(str(file_path))
        if errors:
            results[file_path.name] = errors

    return results


if __name__ == "__main__":
    # Example usage and testing
    validator = EnvironmentValidator()

    print("Environment Schema Validation System")
    print("=" * 50)

    print(f"\nSupported environment types: {len(validator.list_supported_types())}")
    for env_type in sorted(validator.list_supported_types()):
        info = validator.get_environment_type_info(env_type)
        required = info.get("required_tasks", []) if info else []
        print(
            f"  {env_type:20} - Required: {', '.join(required) if required else 'None'}"
        )

    # Test validation on existing environment files
    print(f"\nValidating existing environment files...")
    validation_results = validate_all_environments()

    if not validation_results:
        print("✓ All environment files are valid!")
    else:
        for filename, errors in validation_results.items():
            print(f"\n{filename}:")
            for error in errors:
                print(f"  - {error}")

    # Test the specific environment types requested
    print(f"\nEnvironment type suggestions:")
    for env_type in ["kitchen", "laboratory", "bakery", "airport"]:
        tasks = validator.suggest_tasks_for_type(env_type)
        actors = validator.suggest_actor_types_for_type(env_type)
        print(f"\n{env_type}:")
        print(f"  Suggested tasks: {', '.join(tasks[:5])}...")
        print(f"  Suggested actors: {', '.join(actors)}")
