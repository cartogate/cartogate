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
        # PR C — the contract/ledger surface. Both take no required argument: an agent that has
        # to know what to ask for won't ask.
        "contract_status": [],
        "gate_history": [],
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


def test_docs_tool_count_matches_the_surface() -> None:
    """The published tool count is hardcoded in prose, so it silently drifts every time the
    surface grows — it sat at "13" through two releases that shipped more. Pin it: doc drift is
    the exact failure Cartogate exists to catch, and shipping it in our own docs is a bad look.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    expected = f"{len(TOOL_SPECS)} "
    markers = (
        "deterministic tools", "serves all", "What the agent gets", "tools are deterministic",
    )
    # CLAUDE.md is stripped from the public projection, so its absence is normal, not a failure —
    # a test that assumed it existed would ship a RED suite to the public repo.
    counted = ("docs/INTEGRATIONS.md", "CLAUDE.md")
    present = [root / rel for rel in counted if (root / rel).is_file()]
    assert present, "no counted docs found at all — the test has lost its target"
    stale = [
        f"{path.name}: {line.strip()}"
        for path in present
        for line in path.read_text(encoding="utf-8").splitlines()
        if any(m in line for m in markers) and expected not in line
    ]
    assert not stale, (
        f"tool count is stale (surface is {len(TOOL_SPECS)}):\n" + "\n".join(stale)
    )
