"""Synthetic graphs for the MCP response-budget tests.

Built directly from nodes and edges rather than by indexing source, so the sizes they produce
are exact and do not depend on name resolution. ``hub_store`` is the shape that makes a tool
result large (one symbol with hundreds of referrers); ``wide_store`` is a repository with far
more files than fit in one answer.
"""

from __future__ import annotations

from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore

#: The symbol every caller and test in :func:`hub_store` references.
HUB = "pkg.core.hub"


def symbol(qualified_name: str, unit: str, *, line: int = 1, signature: str) -> Node:
    """An exported top-level function node at ``unit``:``line``."""
    return Node.create(
        repo_id="budget",
        qualified_name=qualified_name,
        kind=NodeKind.SYMBOL,
        name=qualified_name.rsplit(".", 1)[-1],
        unit=unit,
        signature=signature,
        location=Location(path=unit, start_line=line, end_line=line + 9),
        visibility=Visibility.EXPORTED,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash=qualified_name,
        is_top_level=True,
    )


def _call(src: Node, dst: Node, line: int) -> Edge:
    return Edge(
        type=EdgeType.CALLS,
        src=src.id,
        dst=dst.id,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        source_location=SourceLocation(path=src.unit, line=line),
    )


def hub_store(callers: int = 300, files: int = 60, tests: int = 60) -> InMemoryStore:
    """One function, ``pkg.core.hub``, referenced by ``callers`` functions and ``tests`` tests.

    Callers are ``pkg.mod_NNN.caller_MMM``, spread evenly over ``files`` units; every tenth has
    eight call sites, the rest one. Tests are ``tests.test_hub_NNN.test_calls_hub``, one per file.
    With the defaults the hub has 360 referrers in 120 files.
    """
    hub = symbol(HUB, "pkg/core.py", signature="def hub(value):")
    units: dict[str, tuple[list[Node], list[Edge]]] = {"pkg/core.py": ([hub], [])}

    for index in range(callers):
        module = index % files
        unit = f"pkg/mod_{module:03d}.py"
        line = 10 + (index // files) * 20
        caller = symbol(
            f"pkg.mod_{module:03d}.caller_{index:03d}",
            unit,
            line=line,
            signature=f"def caller_{index:03d}():",
        )
        sites = 8 if index % 10 == 0 else 1
        nodes, edges = units.setdefault(unit, ([], []))
        nodes.append(caller)
        edges.extend(_call(caller, hub, line + 1 + site) for site in range(sites))

    for index in range(tests):
        unit = f"tests/test_hub_{index:03d}.py"
        test = symbol(
            f"tests.test_hub_{index:03d}.test_calls_hub",
            unit,
            line=5,
            signature="def test_calls_hub():",
        )
        units[unit] = ([test], [_call(test, hub, 6)])

    store = InMemoryStore()
    store.bulk_load((unit, nodes, edges) for unit, (nodes, edges) in units.items())
    return store


def wide_store(units: int = 2_000) -> InMemoryStore:
    """A repository of ``units`` files laid out ``svc/area_AA/pkg_BB/file_CCC.py``.

    Twenty areas, ten packages per area, ten files per package; every file defines six exported
    top-level functions and nothing references anything.
    """
    loaded: list[tuple[str, list[Node], list[Edge]]] = []
    for index in range(units):
        area, package, file_no = index // 100, (index // 10) % 10, index % 10
        unit = f"svc/area_{area:02d}/pkg_{package:02d}/file_{file_no:03d}.py"
        module = unit[:-3].replace("/", ".")
        nodes = [
            symbol(f"{module}.fn_{k}", unit, line=1 + k * 10, signature=f"def fn_{k}(value):")
            for k in range(6)
        ]
        loaded.append((unit, nodes, []))

    store = InMemoryStore()
    store.bulk_load(loaded)
    return store
