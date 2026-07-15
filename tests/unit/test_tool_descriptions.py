"""Test tool descriptions are trigger-shaped and surfaces are guarded."""

from __future__ import annotations

import re

from cartogate.mcp.tools import TOOL_SPECS


def test_all_descriptions_trigger_shaped() -> None:
    """Every tool description must lead with a trigger: 'Use', 'Before', 'After', 'First'."""
    trigger_pattern = re.compile(r"^(Use|Before|After|First)\b", re.IGNORECASE)

    for spec in TOOL_SPECS:
        description = spec["description"]
        assert trigger_pattern.match(description), (
            f"Tool '{spec['name']}' description does not start with a trigger:\n"
            f"  '{description[:80]}...'\n"
            f"  Expected to match: ^(Use|Before|After|First)"
        )


def test_nav_tools_contrast_grep() -> None:
    """Navigation tools (find_symbol, find_references, blast_radius) must mention grep."""
    nav_tools = {"find_symbol", "find_references", "blast_radius"}
    grep_pattern = re.compile(r"grep", re.IGNORECASE)

    for spec in TOOL_SPECS:
        if spec["name"] not in nav_tools:
            continue
        description = spec["description"]
        assert grep_pattern.search(description), (
            f"Tool '{spec['name']}' does not mention 'grep' for contrast:\n"
            f"  '{description}'"
        )


def test_surface_unchanged() -> None:
    """Tool names and input_schema 'required' lists must not change (snapshot test)."""
    expected_surfaces = {
        "check_duplicate": ["signature"],
        "blast_radius": ["symbol"],
        "find_symbol": ["qualified_name"],
        "find_references": ["qualified_name"],
        "suggest_tests": [],
        "doc_drift": [],
        "find_cycles": [],
        "find_duplicate_bodies": [],
        "impact_summary": [],
        "localize": ["test"],
        "slice": ["source", "line"],
        "find_dead_code": [],
        "read_symbol": ["qualified_name"],
        "implementations": ["qualified_name"],
        "repo_map": [],
    }

    actual_names = {spec["name"] for spec in TOOL_SPECS}
    expected_names = set(expected_surfaces.keys())
    assert actual_names == expected_names, (
        f"Tool names changed:\n"
        f"  Missing: {expected_names - actual_names}\n"
        f"  Extra: {actual_names - expected_names}"
    )

    for spec in TOOL_SPECS:
        name = spec["name"]
        required = spec["input_schema"].get("required", [])
        expected_required = expected_surfaces[name]
        assert required == expected_required, (
            f"Tool '{name}' required list changed:\n"
            f"  Expected: {expected_required}\n"
            f"  Got: {required}"
        )
