#!/usr/bin/env python3
"""
Environment Icons Index

This module provides a mapping of environment types to appropriate FontAwesome icons
for use in visualizations and user interfaces.
"""

from typing import Dict, List, Optional

# Environment Types to FontAwesome Icons Mapping
ENVIRONMENT_ICONS: Dict[str, str] = {
    # Kitchen environments
    "kitchen": "fa-utensils",
    "home": "fa-house",
    "restaurant": "fa-utensils",
    "commercial-kitchen": "fa-fire-burner",
    # Laboratory environments
    "laboratory": "fa-flask",
    "lab": "fa-flask",
    "research": "fa-microscope",
    "biotech": "fa-dna",
    "pharma": "fa-pills",
    "medical": "fa-user-doctor",
    # Bakery environments
    "bakery": "fa-bread-slice",
    "artisan": "fa-wheat-awn",
    "pastry": "fa-cake-candles",
    # Airport environments
    "airport": "fa-plane",
    "aviation": "fa-plane-departure",
    "runway": "fa-plane-arrival",
    "terminal": "fa-building",
    # Additional environment types
    "manufacturing": "fa-industry",
    "warehouse": "fa-warehouse",
    "office": "fa-building",
    "hospital": "fa-hospital",
    "school": "fa-graduation-cap",
    "retail": "fa-store",
    "farm": "fa-tractor",
    "datacenter": "fa-server",
    "factory": "fa-gear",
    "workshop": "fa-screwdriver-wrench",
    "garage": "fa-car",
    "gym": "fa-dumbbell",
    "studio": "fa-microphone",
    "theater": "fa-masks-theater",
    "library": "fa-book",
    "garden": "fa-seedling",
    "greenhouse": "fa-leaf",
    "clinic": "fa-stethoscope",
    "spa": "fa-spa",
    "hotel": "fa-bed",
}

# Default icon for unknown environment types
DEFAULT_ENVIRONMENT_ICON = "fa-building"

# Icon categories for organization
ICON_CATEGORIES: Dict[str, List[str]] = {
    "food_service": [
        "kitchen",
        "restaurant",
        "commercial-kitchen",
        "bakery",
        "artisan",
        "pastry",
        "home",
    ],
    "research_science": [
        "laboratory",
        "lab",
        "research",
        "biotech",
        "pharma",
        "medical",
    ],
    "transportation": ["airport", "aviation", "runway", "terminal"],
    "industrial": ["manufacturing", "warehouse", "factory", "workshop", "garage"],
    "commercial": ["office", "retail", "store", "hotel"],
    "healthcare": ["hospital", "clinic", "spa"],
    "educational": ["school", "library"],
    "entertainment": ["gym", "studio", "theater"],
    "agricultural": ["farm", "garden", "greenhouse"],
    "technology": ["datacenter"],
}


def get_environment_icon(environment_type: str) -> str:
    """
    Get the FontAwesome icon class for a given environment type.

    Args:
        environment_type: The type of environment (e.g., 'kitchen', 'laboratory')

    Returns:
        FontAwesome icon class string (e.g., 'fa-utensils')
    """
    if not environment_type:
        return DEFAULT_ENVIRONMENT_ICON

    # Normalize the environment type (lowercase, handle common variations)
    normalized_type = environment_type.lower().strip()

    # Direct lookup
    if normalized_type in ENVIRONMENT_ICONS:
        return ENVIRONMENT_ICONS[normalized_type]

    # Try partial matches for compound names
    for env_type, icon in ENVIRONMENT_ICONS.items():
        if env_type in normalized_type or normalized_type in env_type:
            return icon

    # Return default if no match found
    return DEFAULT_ENVIRONMENT_ICON


def get_environment_icon_with_prefix(environment_type: str, prefix: str = "fas") -> str:
    """
    Get the complete FontAwesome icon class with prefix for a given environment type.

    Args:
        environment_type: The type of environment
        prefix: FontAwesome prefix (e.g., 'fas', 'far', 'fab')

    Returns:
        Complete FontAwesome icon class string (e.g., 'fas fa-utensils')
    """
    icon = get_environment_icon(environment_type)
    return f"{prefix} {icon}"


def get_icon_category(environment_type: str) -> Optional[str]:
    """
    Get the category that an environment type belongs to.

    Args:
        environment_type: The type of environment

    Returns:
        Category name or None if not found
    """
    normalized_type = environment_type.lower().strip() if environment_type else ""

    for category, types in ICON_CATEGORIES.items():
        if normalized_type in types:
            return category

    return None


def list_environment_types() -> List[str]:
    """
    Get a list of all supported environment types.

    Returns:
        List of environment type strings
    """
    return list(ENVIRONMENT_ICONS.keys())


def list_environment_types_by_category() -> Dict[str, List[str]]:
    """
    Get all environment types organized by category.

    Returns:
        Dictionary mapping category names to lists of environment types
    """
    return ICON_CATEGORIES.copy()


def search_environment_types(query: str) -> List[str]:
    """
    Search for environment types that match a query string.

    Args:
        query: Search query string

    Returns:
        List of matching environment types
    """
    query_lower = query.lower().strip()
    matches = []

    for env_type in ENVIRONMENT_ICONS.keys():
        if query_lower in env_type.lower():
            matches.append(env_type)

    return sorted(matches)


# Validation function to ensure all referenced icons exist in FontAwesome
def validate_icons() -> Dict[str, bool]:
    """
    Validate that all icons in the mapping are valid FontAwesome icons.
    Note: This is a basic check based on common FontAwesome patterns.
    For complete validation, you would need to check against the FontAwesome icon list.

    Returns:
        Dictionary mapping icon names to validation status
    """
    validation_results = {}

    # Common FontAwesome icon patterns for basic validation
    valid_patterns = [
        "fa-",
        "fa-solid",
        "fa-regular",
        "fa-light",
        "fa-thin",
        "fa-duotone",
        "fa-brands",
    ]

    for env_type, icon in ENVIRONMENT_ICONS.items():
        # Basic validation: check if it starts with fa-
        is_valid = icon.startswith("fa-") and len(icon) > 3
        validation_results[icon] = is_valid

    return validation_results


if __name__ == "__main__":
    # Example usage and testing
    print("Environment Icons Index")
    print("=" * 40)

    # Test the specific environment types requested
    test_types = ["kitchen", "laboratory", "bakery", "airport"]

    print("\nRequested Environment Types:")
    for env_type in test_types:
        icon = get_environment_icon(env_type)
        full_icon = get_environment_icon_with_prefix(env_type)
        category = get_icon_category(env_type)
        print(f"  {env_type:12} -> {icon:20} ({full_icon}) [Category: {category}]")

    print(f"\nTotal supported environment types: {len(list_environment_types())}")

    print("\nEnvironment Types by Category:")
    for category, types in list_environment_types_by_category().items():
        print(f"  {category:15}: {', '.join(types)}")

    print(f"\nDefault icon: {DEFAULT_ENVIRONMENT_ICON}")

    # Icon validation
    print("\nIcon Validation Results:")
    validation = validate_icons()
    invalid_icons = [icon for icon, valid in validation.items() if not valid]
    if invalid_icons:
        print(f"  Invalid icons found: {invalid_icons}")
    else:
        print("  All icons appear to be valid FontAwesome patterns")
