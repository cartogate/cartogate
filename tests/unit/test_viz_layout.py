"""Tests for the multi-view layout engine (file graph + the three relationship views)."""

from __future__ import annotations

import math

from tests.conftest import MakeSymbol

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance
from cartogate.viz.layout import (
    _BASE_INNER,
    _NODE_SPACING,
    VIEWS,
    _intra_offsets,
    _ring_radius,
    _unit_radii,
    build_file_graph,
    compute_layout,
)


def _edge(src: str, dst: str, edge_type: EdgeType = EdgeType.CALLS) -> Edge:
    return Edge(
        type=edge_type, src=src, dst=dst, provenance=Provenance.LSP, confidence=Confidence.EXTRACTED
    )


def _module(make_symbol: MakeSymbol, qname: str, unit: str) -> object:
    return make_symbol(qname, unit=unit).model_copy(update={"kind": NodeKind.MODULE})


def test_build_file_graph_aggregates_cross_file_only(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.a.f", unit="pkg/a.py")
    b = make_symbol("pkg.b.g", unit="pkg/b.py")
    a2 = make_symbol("pkg.a.h", unit="pkg/a.py")
    edges = [
        _edge(a.id, b.id),  # cross-file a->b
        _edge(a.id, b.id),  # again -> weight 2
        _edge(a.id, a2.id),  # intra-file, ignored
    ]
    fg = build_file_graph([a, b, a2], edges)
    assert set(fg.nodes) == {"pkg/a.py", "pkg/b.py"}
    assert fg.has_edge("pkg/a.py", "pkg/b.py")
    assert fg["pkg/a.py"]["pkg/b.py"]["weight"] == 2
    assert not fg.has_edge("pkg/a.py", "pkg/a.py")  # no self loop from intra-file edge


def test_compute_layout_covers_every_node_and_view(make_symbol: MakeSymbol) -> None:
    nodes = [
        _module(make_symbol, "pkg.a", "pkg/a.py"),
        make_symbol("pkg.a.f", unit="pkg/a.py"),
        _module(make_symbol, "pkg.b", "pkg/b.py"),
        make_symbol("pkg.b.g", unit="pkg/b.py"),
    ]
    edges = [_edge(nodes[1].id, nodes[3].id)]
    result = compute_layout(nodes, edges)

    assert set(result.positions) == set(VIEWS)
    for view in VIEWS:
        assert set(result.positions[view]) == {n.id for n in nodes}  # every node placed
        assert set(result.fills[view]) == {n.id for n in nodes}  # every node coloured
        assert result.legends[view]  # a non-empty colour key


def test_dependency_layers_are_monotonic(make_symbol: MakeSymbol) -> None:
    # entry.py -> mid.py -> base.py  (entry depends on mid depends on base)
    entry = make_symbol("entry.run", unit="entry.py")
    mid = make_symbol("mid.work", unit="mid.py")
    base = make_symbol("base.util", unit="base.py")
    edges = [_edge(entry.id, mid.id), _edge(mid.id, base.id)]
    result = compute_layout([entry, mid, base], edges)

    xs = result.positions["dependency"]
    # A dependency sits in a different layer (different x column) than its dependent.
    assert xs[entry.id][0] != xs[mid.id][0] != xs[base.id][0]


def test_layout_is_deterministic_including_communities(make_symbol: MakeSymbol) -> None:
    nodes = [make_symbol(f"pkg{i % 3}.f{i}", unit=f"pkg{i % 3}/m{i}.py") for i in range(9)]
    edges = [_edge(nodes[i].id, nodes[i + 1].id) for i in range(8)]
    assert compute_layout(nodes, edges) == compute_layout(nodes, edges)
    # ...and independent of edge input order (the file graph is built in a stable order).
    assert compute_layout(nodes, edges) == compute_layout(nodes, list(reversed(edges)))


def test_mutual_imports_do_not_crash(make_symbol: MakeSymbol) -> None:
    a = make_symbol("a.f", unit="a.py")
    b = make_symbol("b.g", unit="b.py")
    # a <-> b cyclic dependency; condensation must keep dependency layering safe.
    result = compute_layout([a, b], [_edge(a.id, b.id), _edge(b.id, a.id)])
    assert set(result.positions["dependency"]) == {a.id, b.id}
    # the SCC itself is surfaced for the cycles overlay (review must-add: pin the value)
    assert result.cycle_units == (("a.py", "b.py"),)


def test_globe_centers_lie_on_the_sphere(make_symbol: MakeSymbol) -> None:
    """Every unit's globe centre satisfies x^2+y^2+z^2 == radius^2 (Fibonacci sphere)."""
    nodes = [make_symbol(f"m{i}.f", unit=f"m{i}.py") for i in range(7)]
    result = compute_layout(nodes, [])
    assert result.globe_radius > 0 and len(result.globe_centers) == 7
    for x, y, z in result.globe_centers.values():
        assert math.isclose(x * x + y * y + z * z, result.globe_radius**2, rel_tol=1e-9)


def test_intra_offsets_hierarchy_module_centre_top_inner_nested_outer(
    make_symbol: MakeSymbol,
) -> None:
    module = _module(make_symbol, "pkg.mod", "pkg/mod.py")
    top = make_symbol("pkg.mod.Foo", unit="pkg/mod.py", is_top_level=True)
    nested = make_symbol("pkg.mod.Foo.bar", unit="pkg/mod.py", is_top_level=False)
    members = [module, top, nested]
    offsets = _intra_offsets(members, {"pkg/mod.py": _unit_radii(members)})

    def radius(node_id: str) -> float:
        return math.hypot(*offsets[node_id])

    assert radius(module.id) == 0.0  # module at the cluster centre
    assert radius(top.id) < radius(nested.id)  # top-level nearer the centre than nested


def test_ring_radius_grows_with_count_to_prevent_overlap(make_symbol: MakeSymbol) -> None:
    assert _ring_radius(_BASE_INNER, 0) == _BASE_INNER
    assert _ring_radius(_BASE_INNER, 1) == _BASE_INNER  # one node needs no extra room
    assert _ring_radius(_BASE_INNER, 30) > _BASE_INNER  # crowded ring grows to fit
    # A file with many symbols keeps its two rings separated by at least the node spacing.
    crowded = [_module(make_symbol, "p.m", "p/m.py")]
    crowded += [
        make_symbol(f"p.m.f{i}", unit="p/m.py", is_top_level=(i % 2 == 0)) for i in range(40)
    ]
    inner, outer = _unit_radii(crowded)
    assert outer >= inner + _NODE_SPACING
