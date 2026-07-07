"""Synthetic store builders for the latency/scaling hypotheses (V2, V10).

Mirrors the construction in ``tests/benchmark/test_slo.py`` so the value study measures the
same gate the SLO benchmark does — a warm-resident store populated by direct upserts (index
time is deliberately off the gate's latency budget).
"""

from __future__ import annotations

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore


def build_store(*, n_symbols: int, units: int) -> InMemoryStore:
    """Build a warm store of ``n_symbols`` top-level functions across ``units`` files."""
    store = InMemoryStore()
    per_unit = max(1, n_symbols // units)
    for u in range(units):
        unit = f"pkg/m{u}.py"
        nodes: list[Node] = []
        for j in range(per_unit):
            idx = u * per_unit + j
            nodes.append(
                Node.create(
                    repo_id="bench",
                    qualified_name=f"pkg.m{u}.func_{idx}",
                    kind=NodeKind.SYMBOL,
                    name=f"func_{idx}",
                    unit=unit,
                    signature=f"def func_{idx}(a, b):",
                    location=Location(path=unit, start_line=j + 1, end_line=j + 2),
                    visibility=Visibility.EXPORTED,
                    provenance=Provenance.TREE_SITTER,
                    confidence=Confidence.EXTRACTED,
                    content_hash=str(idx),
                    is_top_level=True,
                )
            )
        edges = [
            Edge(
                type=EdgeType.CALLS,
                src=nodes[j].id,
                dst=nodes[j - 1].id,
                provenance=Provenance.TREE_SITTER,
                confidence=Confidence.EXTRACTED,
            )
            for j in range(1, len(nodes))
        ]
        store.upsert_unit(unit, nodes, edges)
    return store
