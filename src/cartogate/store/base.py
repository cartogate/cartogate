"""The store interface (spec §8.1) — migration is a backend swap, not a rewrite.

Every engine query goes through this ABC, so a future on-disk / out-of-core backend
(Kùzu, indexed SQLite, Glean) can replace the in-memory one without touching the engine.
The incremental methods (``upsert_unit`` / ``hide_units`` / ``replace_unit``) encode the
Glean-style immutable-stacking model: a unit (file/module) is the atomic unit of write,
and an update hides a unit's prior facts and stacks new ones rather than mutating live.

Confidence filtering is a *mechanism* here, not a policy: methods accept an optional
``confidence`` filter and apply it mechanically. The decision to gate on EXTRACTED-only
lives in the engine (the single chokepoint, risk R7), which passes that filter in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Language
from cartogate.schema.nodes import Node


class Direction(StrEnum):
    """Edge traversal direction for :meth:`StoreInterface.neighbors`."""

    OUT = "out"
    IN = "in"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class FileRegion:
    """A changed region of a file (1-based, inclusive line range).

    Produced by the engine's diff parser (Section 3) and consumed by ``changed_set`` to
    map a textual diff onto the nodes it overlaps.
    """

    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class SubGraph:
    """A materialized slice of the graph returned by :meth:`StoreInterface.subgraph`."""

    nodes: tuple[Node, ...] = field(default_factory=tuple)
    edges: tuple[Edge, ...] = field(default_factory=tuple)


# Optional iterables of filters; ``None`` means "no filter" (all types/confidences).
EdgeTypeFilter = Iterable[EdgeType] | None
ConfidenceFilter = Iterable[Confidence] | None


class StoreInterface(ABC):
    """The §8.1 store contract."""

    # --- reads ---

    @abstractmethod
    def get_symbol(self, qualified_name: str) -> Node | None:
        """Return the visible symbol node with ``qualified_name``, or ``None``."""

    @abstractmethod
    def get_node(self, node_id: str) -> Node | None:
        """Return the visible node with ``node_id``, or ``None`` (e.g. to resolve changed ids)."""

    @abstractmethod
    def exists(self, signature: str, language: Language = Language.PYTHON) -> bool:
        """Whether any visible symbol matches ``signature`` in ``language`` (duplicate gate)."""

    @abstractmethod
    def find_symbols_by_signature(
        self, signature: str, language: Language = Language.PYTHON
    ) -> list[Node]:
        """Visible symbols whose signature matches ``signature``/``language`` (duplicate hits)."""

    @abstractmethod
    def units(self) -> set[str]:
        """The set of currently-visible unit names (for INVALIDATE freshness tracking)."""

    @abstractmethod
    def iter_unit_facts(self) -> Iterator[tuple[str, tuple[Node, ...], tuple[Edge, ...]]]:
        """Yield ``(unit, nodes, edges)`` for every visible unit — the carry-forward source for the
        daemon's incremental refresh and for rebuilding a resolution context (F-36)."""

    @abstractmethod
    def callers_of(
        self,
        node_id: str,
        depth: int = 1,
        edge_types: EdgeTypeFilter = None,
        confidence: ConfidenceFilter = None,
    ) -> list[Node]:
        """Nodes that reach ``node_id`` within ``depth`` hops (reverse direction)."""

    @abstractmethod
    def callees_of(
        self,
        node_id: str,
        depth: int = 1,
        edge_types: EdgeTypeFilter = None,
        confidence: ConfidenceFilter = None,
    ) -> list[Node]:
        """Nodes reachable from ``node_id`` within ``depth`` hops (forward direction)."""

    @abstractmethod
    def neighbors(
        self,
        node_id: str,
        edge_types: EdgeTypeFilter = None,
        direction: Direction = Direction.OUT,
        confidence: ConfidenceFilter = None,
    ) -> list[Edge]:
        """Edges incident to ``node_id`` filtered by type/direction/confidence."""

    @abstractmethod
    def changed_set(self, regions: Iterable[FileRegion]) -> list[str]:
        """Ids of nodes whose location overlaps any changed region (diff → node ids)."""

    @abstractmethod
    def subgraph(
        self,
        node_ids: Iterable[str],
        edge_types: EdgeTypeFilter = None,
        confidence: ConfidenceFilter = None,
    ) -> SubGraph:
        """Materialize the induced subgraph over ``node_ids`` (filtered)."""

    @abstractmethod
    def visible_node_ids(self) -> set[str]:
        """The ids of all currently-visible (active, non-hidden) nodes — the whole graph.

        Whole-program checks (e.g. the architecture cycle gate) start here, then ``subgraph`` to
        the edge types they need.
        """

    # --- incremental writes (Glean-style immutable stacking) ---

    @abstractmethod
    def upsert_unit(self, unit: str, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        """Set a unit's visible facts, hiding any prior facts for that unit."""

    def bulk_load(self, units: Iterable[tuple[str, Iterable[Node], Iterable[Edge]]]) -> None:
        """Upsert many units in one shot (the initial index of a repo).

        Semantically identical to calling :meth:`upsert_unit` for each unit, but a backend whose
        write triggers a derived-state rebuild (e.g. the in-memory graph) can override this to
        rebuild **once** instead of once per unit — turning an O(units × facts) index into O(facts)
        (spec §8.6). This default keeps the per-unit semantics for backends that don't need it.
        """
        for unit, nodes, edges in units:
            self.upsert_unit(unit, nodes, edges)

    @abstractmethod
    def hide_units(self, units: Iterable[str]) -> None:
        """Hide all facts owned by ``units`` (retained in history, invisible to queries)."""

    @abstractmethod
    def replace_unit(self, unit: str, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        """Hide a unit's prior facts and stack the new ones (explicit re-index)."""
