"""In-memory, warm-resident store backed by a NetworkX ``MultiDiGraph`` (spec §8.3).

The v0 default backend. It implements the Glean-style immutable-stacking model in the
simplest faithful way: visible facts are kept per unit, and any mutation (upsert / hide /
replace) moves the affected unit's prior facts into a retained hidden history and rebuilds
the visible graph + lookup indices from the surviving units. Rebuilds happen at index time
(off the latency-sensitive gate path), so the cost is acceptable for v0; the §8.6 trip-wire
governs migration to a typed-indexed backend.

Read queries are wrapped in ``query_traversal`` spans when a recorder is supplied, so every
later gate can assert "the query emitted a span with node/edge counts" (spec §8.5).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import networkx as nx

from cartogate.instrument import NULL_SPAN_HANDLE, NullSpanHandle, Phase, SpanRecorder
from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Language
from cartogate.schema.nodes import Node
from cartogate.schema.signature import normalize_signature
from cartogate.store.base import (
    ConfidenceFilter,
    Direction,
    EdgeTypeFilter,
    FileRegion,
    StoreInterface,
    SubGraph,
)


@dataclass(frozen=True, slots=True)
class _UnitFacts:
    """The nodes and edges owned by a single unit (file/module)."""

    nodes: tuple[Node, ...] = field(default_factory=tuple)
    edges: tuple[Edge, ...] = field(default_factory=tuple)


def _as_frozenset_types(edge_types: EdgeTypeFilter) -> frozenset[EdgeType] | None:
    return None if edge_types is None else frozenset(edge_types)


def _as_frozenset_conf(confidence: ConfidenceFilter) -> frozenset[Confidence] | None:
    return None if confidence is None else frozenset(confidence)


class InMemoryStore(StoreInterface):
    """A warm-resident, unit-tagged in-memory store."""

    def __init__(self, *, recorder: SpanRecorder | None = None) -> None:
        self._recorder = recorder
        # Visible facts, keyed by unit (insertion order preserved for determinism).
        self._visible_units: dict[str, _UnitFacts] = {}
        # Retained, invisible history of facts that were hidden or replaced.
        self._hidden_units: list[tuple[str, _UnitFacts]] = []
        # Derived state, rebuilt on every mutation.
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._nodes_by_id: dict[str, Node] = {}
        self._qname_to_id: dict[str, str] = {}
        self._sig_to_ids: dict[tuple[Language, str], set[str]] = {}

    # ------------------------------------------------------------------ #
    # Instrumentation helper
    # ------------------------------------------------------------------ #

    @contextmanager
    def _span(self, name: str) -> Iterator[NullSpanHandle]:
        if self._recorder is None:
            yield NULL_SPAN_HANDLE
        else:
            with self._recorder.span(Phase.QUERY_TRAVERSAL, name=name) as handle:
                yield handle  # type: ignore[misc]

    # ------------------------------------------------------------------ #
    # Incremental writes (Glean-style immutable stacking)
    # ------------------------------------------------------------------ #

    def upsert_unit(self, unit: str, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        self._commit(hide=(unit,), set_unit=(unit, _UnitFacts(tuple(nodes), tuple(edges))))

    def bulk_load(self, units: Iterable[tuple[str, Iterable[Node], Iterable[Edge]]]) -> None:
        """Upsert many units with a **single** derived-state rebuild (spec §8.6).

        Per-unit ``upsert_unit`` rebuilds the whole graph+indices on every call, so indexing M
        units is O(M × N); this stages all the units first and runs :meth:`_build_derived` exactly
        once, making the initial index O(N). The visible result is identical to upserting each unit
        in turn (last-writer-wins per unit name, prior facts retained in hidden history).
        """
        candidate = dict(self._visible_units)
        moved: list[tuple[str, _UnitFacts]] = []
        for unit, nodes, edges in units:
            prior = candidate.get(unit)
            if prior is not None:
                moved.append((unit, prior))
            candidate[unit] = _UnitFacts(tuple(nodes), tuple(edges))

        derived = self._build_derived(candidate)  # one rebuild for all units (may raise -> no-op)

        self._visible_units = candidate
        self._hidden_units.extend(moved)
        self._graph, self._nodes_by_id, self._qname_to_id, self._sig_to_ids = derived

    def hide_units(self, units: Iterable[str]) -> None:
        self._commit(hide=tuple(units), set_unit=None)

    def replace_unit(self, unit: str, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        # Explicit re-index: hide prior facts, then stack the new ones. (Same net effect
        # as upsert_unit, kept distinct to mirror the spec's named operations.)
        self._commit(hide=(unit,), set_unit=(unit, _UnitFacts(tuple(nodes), tuple(edges))))

    def _commit(
        self,
        *,
        hide: Iterable[str],
        set_unit: tuple[str, _UnitFacts] | None,
    ) -> None:
        """Apply a unit mutation atomically.

        Builds the candidate visible state and its derived graph/indices first; only if
        that succeeds (no duplicate-id conflict) are the visible units, hidden history,
        and derived state committed. A failed mutation leaves the store untouched.
        """
        candidate = dict(self._visible_units)
        moved: list[tuple[str, _UnitFacts]] = []
        for unit in hide:
            prior = candidate.pop(unit, None)
            if prior is not None:
                moved.append((unit, prior))
        if set_unit is not None:
            unit_name, facts = set_unit
            candidate[unit_name] = facts

        derived = self._build_derived(candidate)  # may raise ValueError -> nothing committed

        self._visible_units = candidate
        self._hidden_units.extend(moved)
        self._graph, self._nodes_by_id, self._qname_to_id, self._sig_to_ids = derived

    @staticmethod
    def _build_derived(
        units: dict[str, _UnitFacts],
    ) -> tuple[
        nx.MultiDiGraph, dict[str, Node], dict[str, str], dict[tuple[Language, str], set[str]]
    ]:
        """Build the visible graph + lookup indices from a set of visible units (pure)."""
        graph: nx.MultiDiGraph = nx.MultiDiGraph()
        nodes_by_id: dict[str, Node] = {}
        qname_to_id: dict[str, str] = {}
        sig_to_ids: dict[tuple[Language, str], set[str]] = {}

        for unit, facts in units.items():
            for node in facts.nodes:
                if node.id in nodes_by_id:
                    # Two simultaneously-visible units claim the same node id. Silent
                    # last-writer-wins would drop a fact without a hidden-history record,
                    # breaking the Glean retention guarantee — so we refuse it loudly and
                    # let the engine (which controls unit dispatch) resolve the conflict.
                    raise ValueError(
                        f"duplicate node id {node.id!r} (qualified_name "
                        f"{node.qualified_name!r}) across active units; unit {unit!r} "
                        "collides with an already-visible unit"
                    )
                graph.add_node(node.id, node=node)
                nodes_by_id[node.id] = node
                qname_to_id[node.qualified_name] = node.id
                # The signature index feeds the duplicate gate (BLOCK), so it must only ever
                # contain EXTRACTED facts (risk R7) — an INFERRED node with a signature must
                # never be a blockable duplicate — and only TOP-LEVEL functions/classes:
                # methods share names across classes legitimately and are not "duplicates".
                if (
                    node.signature is not None
                    and node.confidence is Confidence.EXTRACTED
                    and node.is_top_level
                ):
                    # Key by language too, so a Python and a TypeScript symbol that normalize to
                    # the same signature are not treated as duplicates of each other.
                    key = (node.language, normalize_signature(node.signature, node.language))
                    sig_to_ids.setdefault(key, set()).add(node.id)
        for facts in units.values():
            for edge in facts.edges:
                # Only connect edges whose endpoints are visible nodes.
                if edge.src in nodes_by_id and edge.dst in nodes_by_id:
                    graph.add_edge(edge.src, edge.dst, edge=edge)

        return graph, nodes_by_id, qname_to_id, sig_to_ids

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def get_symbol(self, qualified_name: str) -> Node | None:
        node_id = self._qname_to_id.get(qualified_name)
        return None if node_id is None else self._nodes_by_id.get(node_id)

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes_by_id.get(node_id)

    def exists(self, signature: str, language: Language = Language.PYTHON) -> bool:
        # Normalize the query too (idempotent) so callers can pass a raw or canonical form.
        key = (language, normalize_signature(signature, language))
        return bool(self._sig_to_ids.get(key))

    def find_symbols_by_signature(
        self, signature: str, language: Language = Language.PYTHON
    ) -> list[Node]:
        key = (language, normalize_signature(signature, language))
        return [self._nodes_by_id[i] for i in self._sig_to_ids.get(key, set())]

    def units(self) -> set[str]:
        return set(self._visible_units)

    def edge_count(self) -> int:
        """Number of visible edges (for health/stats reporting)."""
        return int(self._graph.number_of_edges())

    def iter_unit_facts(self) -> Iterator[tuple[str, tuple[Node, ...], tuple[Edge, ...]]]:
        """Yield ``(unit, nodes, edges)`` for every visible unit (its already-extracted facts).

        Lets a caller carry a unit's facts forward without re-extracting them — the daemon's
        incremental refresh reuses the unchanged units' facts and re-parses only what changed.
        """
        for unit, facts in self._visible_units.items():
            yield unit, facts.nodes, facts.edges

    def neighbors(
        self,
        node_id: str,
        edge_types: EdgeTypeFilter = None,
        direction: Direction = Direction.OUT,
        confidence: ConfidenceFilter = None,
    ) -> list[Edge]:
        types = _as_frozenset_types(edge_types)
        confs = _as_frozenset_conf(confidence)
        with self._span("neighbors") as handle:
            edges = list(self._incident_edges(node_id, direction, types, confs))
            handle.set_counts(node_count=1, edge_count=len(edges))
            return edges

    def callees_of(
        self,
        node_id: str,
        depth: int = 1,
        edge_types: EdgeTypeFilter = None,
        confidence: ConfidenceFilter = None,
    ) -> list[Node]:
        with self._span("callees_of") as handle:
            reached, edges_walked = self._bfs(node_id, Direction.OUT, depth, edge_types, confidence)
            nodes = [self._nodes_by_id[i] for i in reached]
            handle.set_counts(node_count=len(nodes), edge_count=edges_walked)
            return nodes

    def callers_of(
        self,
        node_id: str,
        depth: int = 1,
        edge_types: EdgeTypeFilter = None,
        confidence: ConfidenceFilter = None,
    ) -> list[Node]:
        with self._span("callers_of") as handle:
            reached, edges_walked = self._bfs(node_id, Direction.IN, depth, edge_types, confidence)
            nodes = [self._nodes_by_id[i] for i in reached]
            handle.set_counts(node_count=len(nodes), edge_count=edges_walked)
            return nodes

    def changed_set(self, regions: Iterable[FileRegion]) -> list[str]:
        region_list = list(regions)
        with self._span("changed_set") as handle:
            hits: list[str] = []
            for node in self._nodes_by_id.values():
                loc = node.location
                for region in region_list:
                    if region.path == loc.path and _ranges_overlap(
                        loc.start_line, loc.end_line, region.start_line, region.end_line
                    ):
                        hits.append(node.id)
                        break
            handle.set_counts(node_count=len(hits), edge_count=0)
            return hits

    def subgraph(
        self,
        node_ids: Iterable[str],
        edge_types: EdgeTypeFilter = None,
        confidence: ConfidenceFilter = None,
    ) -> SubGraph:
        wanted = {i for i in node_ids if i in self._nodes_by_id}
        types = _as_frozenset_types(edge_types)
        confs = _as_frozenset_conf(confidence)
        with self._span("subgraph") as handle:
            nodes = tuple(self._nodes_by_id[i] for i in wanted)
            edges = tuple(
                edge
                for edge in self._all_edges()
                if edge.src in wanted
                and edge.dst in wanted
                and _edge_passes(edge, types, confs)
            )
            handle.set_counts(node_count=len(nodes), edge_count=len(edges))
            return SubGraph(nodes=nodes, edges=edges)

    # ------------------------------------------------------------------ #
    # Inspection helpers (used by tests to assert retention/visibility)
    # ------------------------------------------------------------------ #

    def visible_node_ids(self) -> set[str]:
        """Ids of all currently visible nodes."""
        return set(self._nodes_by_id)

    def hidden_node_ids(self) -> set[str]:
        """Ids retained in the hidden history (may overlap visible if a unit re-added them)."""
        return {node.id for _unit, facts in self._hidden_units for node in facts.nodes}

    # ------------------------------------------------------------------ #
    # Internal traversal primitives
    # ------------------------------------------------------------------ #

    def _incident_edges(
        self,
        node_id: str,
        direction: Direction,
        types: frozenset[EdgeType] | None,
        confs: frozenset[Confidence] | None,
    ) -> Iterator[Edge]:
        if node_id not in self._graph:
            return
        if direction in (Direction.OUT, Direction.BOTH):
            for _src, _dst, data in self._graph.out_edges(node_id, data=True):
                edge: Edge = data["edge"]
                if _edge_passes(edge, types, confs):
                    yield edge
        if direction in (Direction.IN, Direction.BOTH):
            for _src, _dst, data in self._graph.in_edges(node_id, data=True):
                edge = data["edge"]
                if _edge_passes(edge, types, confs):
                    yield edge

    def _bfs(
        self,
        start: str,
        direction: Direction,
        depth: int,
        edge_types: EdgeTypeFilter,
        confidence: ConfidenceFilter,
    ) -> tuple[list[str], int]:
        """Breadth-first reach from ``start`` up to ``depth`` hops, excluding ``start``.

        Returns the reached node ids (in discovery order) and the true number of edges
        walked, so the caller can tag its span with an accurate edge count.
        """
        types = _as_frozenset_types(edge_types)
        confs = _as_frozenset_conf(confidence)
        if start not in self._graph or depth < 1:
            return [], 0
        seen: set[str] = {start}
        order: list[str] = []
        edges_walked = 0
        frontier = [start]
        for _ in range(depth):
            next_frontier: list[str] = []
            for current in frontier:
                for edge in self._incident_edges(current, direction, types, confs):
                    edges_walked += 1
                    nxt = edge.dst if direction is Direction.OUT else edge.src
                    if nxt not in seen:
                        seen.add(nxt)
                        order.append(nxt)
                        next_frontier.append(nxt)
            frontier = next_frontier
            if not frontier:
                break
        return order, edges_walked

    def _all_edges(self) -> Iterator[Edge]:
        for _src, _dst, data in self._graph.edges(data=True):
            yield data["edge"]


def _edge_passes(
    edge: Edge,
    types: frozenset[EdgeType] | None,
    confs: frozenset[Confidence] | None,
) -> bool:
    if types is not None and edge.type not in types:
        return False
    return not (confs is not None and edge.confidence not in confs)


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end
