"""The EXTRACTED-only traversal chokepoint (spec §4.3, risk R7).

Every gate-relevant traversal goes through here, and here is the *only* place that decides
what the gate may follow: EXTRACTED-confidence edges of the v0 structural types. Reserved
CFG/PDG edges are deliberately excluded from :data:`GATE_EDGE_TYPES` so that even once they
are populated (Phase 2, where they are deterministic but over-approximate) the gate cannot
traverse them without an explicit, deliberate policy change in this one module.
"""

from __future__ import annotations

from cartogate.schema.enums import Confidence, EdgeType
from cartogate.schema.nodes import Node
from cartogate.store.base import EdgeTypeFilter, StoreInterface

#: The structural edge types the hard gate is allowed to traverse. Excludes the reserved
#: control_flow/control_dep/data_dep slots and the advisory inferred_relates edge.
GATE_EDGE_TYPES: frozenset[EdgeType] = frozenset(
    {
        EdgeType.CALLS,
        EdgeType.IMPORTS,
        EdgeType.DEPENDS_ON,
        EdgeType.DEFINES,
        EdgeType.REFERENCES,
        EdgeType.INHERITS,
        EdgeType.IMPLEMENTS,
    }
)

#: Edge types that mean "this node references the target symbol" — a name occurrence: a call,
#: a bare reference, an import (incl. a package re-export), or a subclass/implementation. Shared
#: by ``find_references``, ``blast_radius``, and FLAG's ``suggest_tests`` so the notion of a
#: "reference" stays consistent (one source of truth, no per-tool drift). Excludes ``defines``
#: (a container relationship, not a reference) and ``depends_on`` (module-level, not symbol-level).
REFERENCE_EDGE_TYPES: frozenset[EdgeType] = frozenset(
    {
        EdgeType.CALLS,
        EdgeType.REFERENCES,
        EdgeType.IMPORTS,
        EdgeType.INHERITS,
        EdgeType.IMPLEMENTS,
    }
)

#: Edge types that mean "this test *exercises* the target symbol" — for FLAG's ``suggest_tests``.
#: A test calls it, uses it as a value, or subclasses/implements it (a test double). Deliberately
#: excludes ``imports``: a test that merely imports a symbol (e.g. for unrelated setup) does not
#: exercise it, so counting imports here would select irrelevant tests (lower precision).
EXERCISE_EDGE_TYPES: frozenset[EdgeType] = REFERENCE_EDGE_TYPES - {EdgeType.IMPORTS}

#: The only confidence tier a hard BLOCK may rest on.
_GATE_CONFIDENCE = (Confidence.EXTRACTED,)


class GatingTraversal:
    """Store traversals constrained to gate-safe edges (EXTRACTED + structural)."""

    def __init__(self, store: StoreInterface) -> None:
        self._store = store

    def callers(
        self, node_id: str, depth: int = 1, edge_types: EdgeTypeFilter = None
    ) -> list[Node]:
        """Symbols that depend on ``node_id`` (its blast radius), gate-safe edges only."""
        return self._store.callers_of(
            node_id,
            depth=depth,
            edge_types=self._gate_types(edge_types),
            confidence=_GATE_CONFIDENCE,
        )

    def callees(
        self, node_id: str, depth: int = 1, edge_types: EdgeTypeFilter = None
    ) -> list[Node]:
        """Symbols ``node_id`` depends on, gate-safe edges only."""
        return self._store.callees_of(
            node_id,
            depth=depth,
            edge_types=self._gate_types(edge_types),
            confidence=_GATE_CONFIDENCE,
        )

    # Phase 2: when cfg/pdg extractors land, add a provenance filter here so the gate also
    # excludes over-approximate cfg/pdg facts (BLOCKABLE_PROVENANCES) — today the edge-type
    # filter alone suffices because those edge types are unpopulated (see F-27).

    @staticmethod
    def _gate_types(requested: EdgeTypeFilter) -> frozenset[EdgeType]:
        """Intersect a caller's requested types with the allowed gate set (default = all)."""
        if requested is None:
            return GATE_EDGE_TYPES
        return frozenset(requested) & GATE_EDGE_TYPES
