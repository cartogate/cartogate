"""Advisory dead-code detection — unreferenced internal symbols (F-67).

Flags top-level **INTERNAL** symbols with no incoming reference of any kind — dead-code
*candidates*. The check is deliberately conservative, and **never blocks**:

- **Internal only.** An exported/public symbol may be used from outside the repo, so an absence of
  in-repo references says nothing about whether it is dead. Only internal symbols, whose entire
  usage is in-repo, can be reasoned about soundly.
- **Top-level only.** A method can be reached by dynamic dispatch through a base type (a call
  resolves to the *base* method, not the override), so an unreferenced method is a false-positive
  risk. We restrict to symbols defined directly by a module (free functions / module-level classes).
- **EXTRACTED only** (R7) — the analysis rests solely on extracted structural facts.
- **Excludes** test files, entrypoints (``main``) and dunders (``__x__``) — these are invoked by a
  runner / the language, not by an in-repo reference, so they are not dead.

Even so, dynamic registration (callbacks, plugins, reflection, framework hooks) can make a flagged
symbol live, so the result is a list of candidates to review — hence advisory, not a gate. One
language-specific gap to know: the Go and Rust resolvers emit a reference edge only for *type*
occurrences, so a function passed as a callback *argument* (e.g. Go ``http.HandleFunc("/", h)``,
Rust ``router.add(handle)``) produces no in-repo reference and an internal ``h``/``handle`` may
appear as a candidate even though it is live. (Python/TS capture bare-name value references, so
they are spared.)
"""

from __future__ import annotations

from dataclasses import dataclass

from cartogate.engine.traversal import REFERENCE_EDGE_TYPES
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Visibility
from cartogate.store.base import Direction, StoreInterface

#: Entrypoint names invoked by the runtime, not by an in-repo reference.
_EXCLUDED_NAMES = frozenset({"main"})


@dataclass(frozen=True, slots=True)
class DeadSymbol:
    """A dead-code candidate: an unreferenced top-level internal symbol."""

    qualified_name: str
    location: str  # path:line


def _is_test_path(path: str) -> bool:
    """Conservative cross-language test-file heuristic (test_*, *_test, *.test/.spec, /test(s)/)."""
    norm = path.replace("\\", "/").lower()
    base = norm.rsplit("/", 1)[-1]
    return (
        "/test/" in norm
        or "/tests/" in norm
        or base.startswith("test_")
        or base.startswith("conftest")
        or "_test." in base
        or ".test." in base
        or ".spec." in base
    )


def _is_excluded(name: str, path: str) -> bool:
    """True when the symbol should be excluded: an entrypoint name, a dunder, or a test file."""
    if name in _EXCLUDED_NAMES:
        return True
    if name.startswith("__") and name.endswith("__"):  # dunders are invoked implicitly
        return True
    return _is_test_path(path)


def _is_top_level(store: StoreInterface, node_id: str) -> bool:
    """True when the symbol is defined directly by a MODULE (a free function / module-level class),
    not by a class (a method) — methods carry a dynamic-dispatch false-positive risk."""
    definers = store.neighbors(node_id, edge_types={EdgeType.DEFINES}, direction=Direction.IN)
    for edge in definers:
        container = store.get_node(edge.src)
        if container is not None and container.kind is NodeKind.MODULE:
            return True
    return False


def find_unreferenced_internal(store: StoreInterface) -> list[DeadSymbol]:
    """Top-level internal symbols with no incoming reference — dead-code candidates, sorted by
    qualified name (deterministic). Advisory only; see the module docstring for the conservatism."""
    out: list[DeadSymbol] = []
    for node_id in store.visible_node_ids():
        node = store.get_node(node_id)
        if node is None or node.kind is not NodeKind.SYMBOL:
            continue
        if node.confidence is not Confidence.EXTRACTED:
            continue
        if node.visibility is not Visibility.INTERNAL:
            continue
        if _is_excluded(node.name, node.location.path):
            continue
        if not _is_top_level(store, node_id):
            continue
        # Any incoming reference (call / use / import / inherit / implement) means it is live. We
        # do NOT filter by confidence: an INFERRED reference should still spare it (conservative).
        refs = store.neighbors(
            node_id, edge_types=REFERENCE_EDGE_TYPES, direction=Direction.IN
        )
        if not refs:
            out.append(
                DeadSymbol(
                    node.qualified_name,
                    f"{node.location.path}:{node.location.start_line}",
                )
            )
    return sorted(out, key=lambda dead: dead.qualified_name)
