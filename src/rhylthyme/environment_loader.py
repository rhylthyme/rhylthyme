#!/usr/bin/env python3
"""
Environment Loader Module

This module handles loading environment catalogs and providing resource constraints
for programs that reference environments instead of embedding constraints directly.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class EnvironmentLoader:
    """Handles loading and managing environment catalogs."""

    def __init__(self, environments_dir: Optional[str] = None):
        """
        Initialize the environment loader.

        Args:
            environments_dir: Path to the environments directory.
                            If None, checks $RHYLTHYME_ENVIRONMENTS_PATH first,
                            then defaults to 'environments' in the project root.
        """
        if environments_dir is None:
            environments_dir = os.environ.get("RHYLTHYME_ENVIRONMENTS_PATH") or None
        if environments_dir is None:
            # Default to environments directory relative to this file
            module_dir = Path(__file__).parent.parent.parent
            self.environments_dir = module_dir / "environments"
        else:
            self.environments_dir = Path(environments_dir)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def load_environment(self, environment_id: str) -> Dict[str, Any]:
        """
        Load an environment catalog by ID.

        Args:
            environment_id: The ID of the environment to load

        Returns:
            The environment catalog data

        Raises:
            FileNotFoundError: If the environment file is not found
            ValueError: If the environment file is invalid
        """
        # Check cache first
        if environment_id in self._cache:
            return self._cache[environment_id]

        # Try to find the environment file
        environment_file = None

        # First try direct file name
        for ext in [".json", ".yaml", ".yml"]:
            potential_file = self.environments_dir / f"{environment_id}{ext}"
            if potential_file.exists():
                environment_file = potential_file
                break

        # If not found, scan all files for matching environmentId
        if environment_file is None:
            for file_path in self.environments_dir.glob("*"):
                if file_path.suffix in [".json", ".yaml", ".yml"]:
                    try:
                        data = self._load_file(file_path)
                        if data.get("environmentId") == environment_id:
                            environment_file = file_path
                            break
                    except:
                        continue

        if environment_file is None:
            raise FileNotFoundError(
                f"Environment '{environment_id}' not found in {self.environments_dir}"
            )

        # Load the environment data
        environment_data = self._load_file(environment_file)

        # Validate required fields
        if "environmentId" not in environment_data:
            raise ValueError(
                f"Environment file {environment_file} missing required 'environmentId' field"
            )

        if "resourceConstraints" not in environment_data:
            raise ValueError(
                f"Environment file {environment_file} missing required 'resourceConstraints' field"
            )

        # Cache the loaded environment
        self._cache[environment_id] = environment_data

        return environment_data

    def load_environment_by_type(
        self, environment_type: str
    ) -> Optional[Dict[str, Any]]:
        """The default environment for a type (see
        :meth:`get_default_environment_for_type`), or None if there is none."""
        default_env_id = self.get_default_environment_for_type(environment_type)
        if default_env_id:
            try:
                return self.load_environment(default_env_id)
            except (FileNotFoundError, ValueError):
                pass
        return None

    def _load_file(self, file_path: Path) -> Dict[str, Any]:
        """Load a JSON or YAML file."""
        with open(file_path, "r") as f:
            if file_path.suffix == ".json":
                return json.load(f)
            else:  # .yaml or .yml
                return yaml.safe_load(f)

    def get_resource_constraints(self, environment_id: str) -> List[Dict[str, Any]]:
        """
        Get resource constraints for a specific environment.

        Args:
            environment_id: The ID of the environment

        Returns:
            List of resource constraint definitions
        """
        environment = self.load_environment(environment_id)
        return environment.get("resourceConstraints", [])

    def get_environment(self, environment_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the full environment data for a specific environment.

        Args:
            environment_id: The ID of the environment

        Returns:
            The full environment data or None if not found
        """
        try:
            return self.load_environment(environment_id)
        except (FileNotFoundError, ValueError):
            return None

    def list_environments(self) -> List[Dict[str, Any]]:
        """
        List all available environments.

        Returns:
            List of environment summaries with id, name, type, description, and icon
        """
        environments: List[Dict[str, Any]] = []

        if not self.environments_dir.exists():
            return environments

        for file_path in sorted(self.environments_dir.glob("*")):
            if file_path.suffix in [".json", ".yaml", ".yml"]:
                try:
                    data = self._load_file(file_path)
                    if "environmentId" in data:
                        env_type = data.get("type", "Unknown")

                        # Get icon for environment type
                        try:
                            from .environment_icons import get_environment_icon

                            icon = get_environment_icon(env_type)
                        except ImportError:
                            icon = "fa-building"  # Default fallback

                        environments.append(
                            {
                                "id": data["environmentId"],
                                "name": data.get("name", "Unknown"),
                                "type": env_type,
                                "description": data.get("description", ""),
                                "icon": icon,
                                "isDefault": data.get("isDefault", False),
                                "constraintCount": len(
                                    data.get("resourceConstraints", [])
                                ),
                            }
                        )
                except:
                    continue

        return environments

    def list_environments_by_type(self, environment_type: str) -> List[Dict[str, Any]]:
        """
        List all environments that match a specific type.

        Args:
            environment_type: The type of environment to filter by (e.g., 'kitchen', 'laboratory')

        Returns:
            List of environment summaries matching the type
        """
        all_environments = self.list_environments()
        return [env for env in all_environments if env["type"] == environment_type]

    def find_suitable_environments(
        self, program_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Find environments suitable for a program based on its environmentType.

        Args:
            program_data: The program data containing environmentType

        Returns:
            List of suitable environment summaries
        """
        environment_type = program_data.get("environmentType")
        if not environment_type:
            return self.list_environments()

        return self.list_environments_by_type(environment_type)

    def get_default_environment_for_type(self, environment_type: str) -> Optional[str]:
        """
        Get the default environment ID for a given type.

        Args:
            environment_type: The type of environment

        Returns:
            The ID of the default environment for this type, or None if no suitable environment found
        """
        environments = self.list_environments_by_type(environment_type)
        if not environments:
            return None

        # An environment file can mark itself the default for its type
        for env in environments:
            if env.get("isDefault"):
                return env["id"]

        # Otherwise prefer 'standard' or 'default' in the id, then the first one
        for env in environments:
            if "standard" in env["id"].lower() or "default" in env["id"].lower():
                return env["id"]

        return environments[0]["id"]

    def merge_constraints(
        self, program_constraints: List[Dict[str, Any]], environment_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """
        Merge program-specific constraints with environment constraints.

        Program-specific constraints take precedence over environment constraints.

        Args:
            program_constraints: Resource constraints defined in the program
            environment_id: The environment ID to load constraints from

        Returns:
            Merged list of resource constraints
        """
        if environment_id is None:
            return program_constraints

        # Load environment constraints
        try:
            env_constraints = self.get_resource_constraints(environment_id)
        except (FileNotFoundError, ValueError):
            # If environment not found, just use program constraints
            return program_constraints

        # Create a map of program constraints by task
        program_map = {c["task"]: c for c in program_constraints}

        # Start with environment constraints
        merged = []
        for env_constraint in env_constraints:
            task = env_constraint["task"]
            if task in program_map:
                # Program constraint overrides environment
                merged.append(program_map[task])
                del program_map[task]
            else:
                # Use environment constraint
                merged.append(env_constraint)

        # Add any remaining program constraints
        merged.extend(program_map.values())

        return merged


# Global instance for convenient access
_default_loader = None


def get_default_loader() -> EnvironmentLoader:
    """Get the default environment loader instance."""
    global _default_loader
    if _default_loader is None:
        _default_loader = EnvironmentLoader()
    return _default_loader


def load_resource_constraints(program_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Load resource constraints for a program, handling both embedded and environment-based constraints.

    Args:
        program_data: The program data dictionary

    Returns:
        List of resource constraints
    """
    loader = get_default_loader()

    # Get constraints from the program (if any)
    program_constraints = program_data.get("resourceConstraints", [])

    # Get environment ID (if any)
    environment_id = program_data.get("environment")

    # Merge constraints
    return loader.merge_constraints(program_constraints, environment_id)
