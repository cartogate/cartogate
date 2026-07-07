"""Section 3 gate — the EXTRACTED-only traversal chokepoint (risk R7).

Gating traversal is the single place that decides what the gate is allowed to follow:
only EXTRACTED structural edges. An INFERRED edge — or a reserved CFG/PDG edge, even if
it were EXTRACTED — must never contribute to a gate decision. This is the negative test
that keeps a future edge source from silently becoming blockable.
"""

from __future__ import annotations

from tests.conftest import MakeSymbol

from cartogate.engine.traversal import GATE_EDGE_TYPES, GatingTraversal
from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Provenance
from cartogate.store import InMemoryStore


def _edge(src: str, dst: str, edge_type: EdgeType, confidence: Confidence) -> Edge:
    prov = (
        Provenance.TREE_SITTER
        if confidence is Confidence.EXTRACTED
        else Provenance.SEMANTIC_SKILL
    )
    return Edge(type=edge_type, src=src, dst=dst, provenance=prov, confidence=confidence)


def test_inferred_edge_never_traversed(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    a = make_symbol("pkg.a", signature="def a():")
    b = make_symbol("pkg.b", signature="def b():")
    c = make_symbol("pkg.c", signature="def c():")
    edges = [
        _edge(a.id, b.id, EdgeType.CALLS, Confidence.EXTRACTED),
        _edge(a.id, c.id, EdgeType.INFERRED_RELATES, Confidence.INFERRED),
    ]
    store.upsert_unit("pkg/a.py", [a, b, c], edges)
    traversal = GatingTraversal(store)

    callees = {n.id for n in traversal.callees(a.id, depth=1)}
    assert callees == {b.id}  # c (INFERRED) excluded


def test_gate_set_is_exactly_the_structural_types() -> None:
    # The gate may traverse ONLY the v0 structural edge types. Every non-structural type
    # (reserved CFG/PDG, analysis, cross-repo, advisory) must be absent — so an accidental
    # addition of any of them to GATE_EDGE_TYPES is caught here.
    assert {
        EdgeType.CALLS,
        EdgeType.IMPORTS,
        EdgeType.DEPENDS_ON,
        EdgeType.DEFINES,
        EdgeType.REFERENCES,
        EdgeType.INHERITS,
        EdgeType.IMPLEMENTS,
    } == GATE_EDGE_TYPES
    non_structural = {
        EdgeType.CONTROL_FLOW,
        EdgeType.CONTROL_DEP,
        EdgeType.DATA_DEP,
        EdgeType.TESTS,
        EdgeType.DOCUMENTS,
        EdgeType.EXPOSES,
        EdgeType.CONSUMES,
        EdgeType.INFERRED_RELATES,
    }
    assert non_structural & GATE_EDGE_TYPES == set()


def test_callers_extracted_only(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    a = make_symbol("pkg.a", signature="def a():")
    b = make_symbol("pkg.b", signature="def b():")
    store.upsert_unit(
        "pkg/a.py",
        [a, b],
        [_edge(a.id, b.id, EdgeType.CALLS, Confidence.EXTRACTED)],
    )
    traversal = GatingTraversal(store)
    callers = {n.id for n in traversal.callers(b.id, depth=1)}
    assert callers == {a.id}
