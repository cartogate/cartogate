"""Architecture gate — whole-program structural invariants (spec §10 P2, F-11).

The duplicate/contract gates reason about one symbol; this reasons about the *shape* of the
dependency graph as a whole. The first such check is **module-level import-cycle detection**: a
circular dependency between packages, which is invisible to a single-file AST view (it only exists
once you join the imports across files) — the canonical "impossible without the graph" check.

Like every gate it rests only on EXTRACTED facts (R7): it reads ``imports`` edges at the
``EXTRACTED`` confidence tier. Output is deterministic — each cycle is rotated to start at its
lexicographically smallest module and the list of cycles is sorted — so a CI run is reproducible.
"""

from __future__ import annotations

import networkx as nx

from cartogate.schema.enums import Confidence, EdgeType, NodeKind
from cartogate.schema.nodes import Node
from cartogate.store.base import StoreInterface

#: Cap on cycles enumerated — ``simple_cycles`` is exponential on a pathologically dense graph, so
#: a query can't be allowed to hang the server. Real module graphs have a handful of cycles, far
#: under this; a result at the cap is flagged ``truncated`` by the tool layer.
CYCLE_LIMIT = 1000

#: Cap on enumerated cycle *length*. Unbounded ``simple_cycles`` (Johnson's algorithm) can spend an
#: enormous amount of time before yielding even one cycle in a large strongly-connected component —
#: enough to look like a hang. Bounding the length keeps each cycle cheap to find; the SCC-level
#: fallback (see :func:`find_cycles`) still surfaces any component whose only cycles exceed it, so a
#: real circular dependency is never silently dropped. Real import cycles are far shorter than this.
CYCLE_LENGTH_BOUND = 16


def find_cycles(
    store: StoreInterface,
    *,
    limit: int = CYCLE_LIMIT,
    length_bound: int = CYCLE_LENGTH_BOUND,
) -> list[list[str]]:
    """Return the module-level import cycles in the graph, each as a list of module qnames.

    An ``imports`` edge runs from the importing module to an imported symbol (or module); each end
    is projected to its owning module, and a cycle in the resulting module dependency graph is a
    circular dependency. Self-edges (a module importing its own symbol) are ignored. Returns an
    empty list when the dependency graph is acyclic.

    Reads only EXTRACTED ``imports`` edges (R7), so an INFERRED edge can never invent a phantom
    cycle. **Bounded for responsiveness:** enumeration runs per strongly-connected component
    (acyclic parts can't contribute a cycle) and each cycle is length-capped at ``length_bound`` —
    without this, ``simple_cycles`` on a large SCC can run effectively forever. A component whose
    every cycle exceeds the bound is still reported once, as its member modules, so **no cyclic
    component (SCC) is ever silently dropped**. (Caveat: within a component that has both short and
    over-bound cycles, a module appearing *only* in the over-bound cycles may be absent from the
    reported paths — rare in real import graphs, and acceptable for an advisory view.) The total is
    capped at ``limit``. Under both caps the output is deterministic (each cycle canonicalized,
    the list sorted).
    """
    ids = store.visible_node_ids()
    sub = store.subgraph(ids, edge_types=(EdgeType.IMPORTS,), confidence=(Confidence.EXTRACTED,))
    by_id = {n.id: n for n in sub.nodes}
    # Module qnames, longest first, so the most specific module wins the prefix match below.
    modules = sorted(
        (n.qualified_name for n in sub.nodes if n.kind is NodeKind.MODULE),
        key=len,
        reverse=True,
    )

    graph: nx.DiGraph = nx.DiGraph()
    for edge in sub.edges:
        src = _module_of(by_id.get(edge.src), modules)
        dst = _module_of(by_id.get(edge.dst), modules)
        if src is not None and dst is not None and src != dst:
            graph.add_edge(src, dst)

    cycles: list[list[str]] = []
    # Only non-trivial SCCs can hold a cycle; enumerate within each, length-bounded, so a
    # pathological component can't hang the query.
    for component in nx.strongly_connected_components(graph):
        if len(component) < 2:
            continue  # a lone node has no cycle (self-edges were excluded above)
        found = False
        for cycle in nx.simple_cycles(graph.subgraph(component), length_bound=length_bound):
            cycles.append(_canonical(cycle))
            found = True
            if len(cycles) >= limit:
                return sorted(cycles)
        if not found:  # every cycle here is longer than the bound — surface the component itself
            cycles.append(_canonical(sorted(component)))
            if len(cycles) >= limit:
                return sorted(cycles)
    return sorted(cycles)


def _module_of(node: Node | None, modules: list[str]) -> str | None:
    """The in-repo module a node belongs to, or ``None`` (external package / unknown).

    A module node is its own module; a symbol projects to the longest module qname that prefixes
    its qualified name (so ``pkg.mod.Class.method`` -> ``pkg.mod``). Every other node kind
    (``external_package``, file, etc.) returns ``None`` — not owned by an in-repo module.
    """
    if node is None:
        return None
    if node.kind is NodeKind.MODULE:
        return node.qualified_name
    if node.kind is NodeKind.SYMBOL:
        for module in modules:  # longest-first
            if node.qualified_name == module or node.qualified_name.startswith(module + "."):
                return module
    return None  # external_package and anything not owned by an in-repo module


def _canonical(cycle: list[str]) -> list[str]:
    """Rotate a cycle to start at its lexicographically smallest module, for stable output."""
    start = cycle.index(min(cycle))
    return cycle[start:] + cycle[:start]
