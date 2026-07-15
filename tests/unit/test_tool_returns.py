"""Test that tool returns include location, signature, and reference sites for grep-superset.

The brief dict returned by find_references, blast_radius, and other list-tools should carry:
- "location": "{path}:{start_line}" so agents can immediately grep that file:line
- "signature": the function signature (or None) so agents can identify overloads
- "sites": ["{path}:{line}", ...] for find_references entries — the actual call sites where
  that symbol is referenced.

This enables grep-level actionability without requiring a follow-up search.
"""

from __future__ import annotations

import pytest
from tests.conftest import MakeSymbol

from cartogate.mcp.tools import CartogateTools
from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import Confidence, EdgeType, Provenance
from cartogate.store import InMemoryStore


@pytest.fixture
def store_with_callee_and_caller(make_symbol: MakeSymbol):
    """Fixture: callee node and caller node + calls edge with source_location."""
    store = InMemoryStore()

    # Create two nodes: a callee and a caller
    callee = make_symbol(
        "pkg.a.callee",
        signature="def callee(x):",
        unit="src/pkg/a.py",
        start_line=10,
        end_line=12,
    )
    caller = make_symbol(
        "pkg.b.caller",
        signature="def caller():",
        unit="src/pkg/b.py",
        start_line=20,
        end_line=25,
    )

    # Create a CALLS edge from caller to callee with source_location
    edge = Edge(
        type=EdgeType.CALLS,
        src=caller.id,
        dst=callee.id,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        source_location=SourceLocation(path="src/pkg/b.py", line=42),
    )

    store.upsert_unit("src/pkg/a.py", [callee], [])
    store.upsert_unit("src/pkg/b.py", [caller], [edge])

    return store, callee, caller


def test_brief_carries_location_and_signature(
    store_with_callee_and_caller: tuple, make_symbol: MakeSymbol
) -> None:
    """CartogateTools.find_references entries should have 'location' and 'signature' keys."""
    store, callee, caller = store_with_callee_and_caller
    tools = CartogateTools(store)

    result = tools.find_references("pkg.a.callee")

    assert result["found"] is True
    assert result["count"] == 1
    assert len(result["references"]) == 1

    ref = result["references"][0]
    # These are the NEW required keys
    assert "location" in ref, f"missing 'location' key in {ref}"
    assert "signature" in ref, f"missing 'signature' key in {ref}"
    # Verify format of location is "path:line"
    assert ref["location"] == "src/pkg/b.py:20"
    assert ref["signature"] == "def caller():"


def test_reference_sites(store_with_callee_and_caller: tuple) -> None:
    """find_references entries should carry 'sites': ["path:line", ...] per reference location."""
    store, callee, caller = store_with_callee_and_caller
    tools = CartogateTools(store)

    result = tools.find_references("pkg.a.callee")

    assert result["found"] is True
    ref = result["references"][0]
    assert "sites" in ref, f"missing 'sites' key in {ref}"
    assert isinstance(ref["sites"], list)
    assert len(ref["sites"]) == 1
    assert ref["sites"][0] == "src/pkg/b.py:42"


def test_reference_sites_exclude_inferred(make_symbol: MakeSymbol) -> None:
    """Sites must rest only on EXTRACTED edges (risk R7): an INFERRED reference edge's
    source_location must never leak into the result, even for an otherwise-real caller."""
    store = InMemoryStore()
    callee = make_symbol("pkg.a.callee", signature="def callee(x):", unit="src/pkg/a.py",
                         start_line=10, end_line=12)
    caller = make_symbol("pkg.b.caller", signature="def caller():", unit="src/pkg/b.py",
                         start_line=20, end_line=25)
    extracted = Edge(
        type=EdgeType.CALLS, src=caller.id, dst=callee.id,
        provenance=Provenance.TREE_SITTER, confidence=Confidence.EXTRACTED,
        source_location=SourceLocation(path="src/pkg/b.py", line=42),
    )
    inferred = Edge(
        type=EdgeType.CALLS, src=caller.id, dst=callee.id,
        provenance=Provenance.SEMANTIC_SKILL, confidence=Confidence.INFERRED,
        source_location=SourceLocation(path="src/pkg/b.py", line=99),
    )
    store.upsert_unit("src/pkg/a.py", [callee], [])
    store.upsert_unit("src/pkg/b.py", [caller], [extracted, inferred])

    result = CartogateTools(store).find_references("pkg.a.callee")

    ref = result["references"][0]
    assert ref["sites"] == ["src/pkg/b.py:42"]  # the INFERRED :99 must not appear


def test_reference_sites_multiple(make_symbol: MakeSymbol) -> None:
    """A caller with multiple reference edges should have all sites listed."""
    store = InMemoryStore()

    callee = make_symbol("pkg.a.callee", signature="def callee():", unit="src/pkg/a.py")
    caller = make_symbol("pkg.b.caller", signature="def caller():", unit="src/pkg/b.py")

    # Multiple edges from caller to callee at different lines
    edges = [
        Edge(
            type=EdgeType.CALLS,
            src=caller.id,
            dst=callee.id,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
            source_location=SourceLocation(path="src/pkg/b.py", line=10),
        ),
        Edge(
            type=EdgeType.CALLS,
            src=caller.id,
            dst=callee.id,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
            source_location=SourceLocation(path="src/pkg/b.py", line=20),
        ),
        Edge(
            type=EdgeType.CALLS,
            src=caller.id,
            dst=callee.id,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
            source_location=SourceLocation(path="src/pkg/b.py", line=30),
        ),
    ]

    store.upsert_unit("src/pkg/a.py", [callee], [])
    store.upsert_unit("src/pkg/b.py", [caller], edges)

    tools = CartogateTools(store)
    result = tools.find_references("pkg.a.callee")

    assert result["found"] is True
    ref = result["references"][0]
    assert ref["sites"] == ["src/pkg/b.py:10", "src/pkg/b.py:20", "src/pkg/b.py:30"]


def test_reference_sites_none_location(make_symbol: MakeSymbol) -> None:
    """A caller with edges that have no source_location should get empty sites list."""
    store = InMemoryStore()

    callee = make_symbol("pkg.a.callee", signature="def callee():", unit="src/pkg/a.py")
    caller = make_symbol("pkg.b.caller", signature="def caller():", unit="src/pkg/b.py")

    # Edge with no source_location
    edge = Edge(
        type=EdgeType.CALLS,
        src=caller.id,
        dst=callee.id,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        source_location=None,
    )

    store.upsert_unit("src/pkg/a.py", [callee], [])
    store.upsert_unit("src/pkg/b.py", [caller], [edge])

    tools = CartogateTools(store)
    result = tools.find_references("pkg.a.callee")

    assert result["found"] is True
    ref = result["references"][0]
    assert ref["sites"] == []


def test_blast_radius_briefs_carry_location(
    store_with_callee_and_caller: tuple,
) -> None:
    """blast_radius affected entries should also carry 'location' and 'signature'."""
    store, callee, caller = store_with_callee_and_caller
    tools = CartogateTools(store)

    result = tools.blast_radius("pkg.a.callee")

    assert result["found"] is True
    assert result["count"] == 1
    assert len(result["affected"]) == 1

    affected = result["affected"][0]
    assert "location" in affected
    assert "signature" in affected
    assert affected["location"] == "src/pkg/b.py:20"
    assert affected["signature"] == "def caller():"
