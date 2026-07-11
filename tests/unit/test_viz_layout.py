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


def test_family_matrix_is_directed_typed_and_cross_family_only(
    make_symbol: MakeSymbol,
) -> None:
    a = make_symbol("a.f", unit="r/src/a.py")  # core
    b = make_symbol("b.g", unit="r/tests/x.py")  # tests
    c = make_symbol("c.h", unit="r/src/c.py")  # core
    edges = [_edge(b.id, a.id), _edge(b.id, a.id), _edge(a.id, c.id)]  # intra-core excluded
    result = compute_layout([a, b, c], edges)
    assert result.fam_matrix == (("tests", "core", "calls", 2),)
    assert result.family_of == {"r/src/a.py": "core", "r/src/c.py": "core",
                                "r/tests/x.py": "tests"}
    assert result.fam_counts == {"core": 2, "tests": 1}


def test_family_centers_are_disjoint_and_in_canvas(make_symbol: MakeSymbol) -> None:
    units = ["r/src/a.py", "r/tests/x.py", "r/docs/d.md", "r/scripts/s.py",
             "r/.github/w.yml", "r/examples/e.py"]
    nodes = [make_symbol(f"m{i}.f", unit=u) for i, u in enumerate(units)]
    result = compute_layout(nodes, [])
    centers = result.fam_centers
    assert len(centers) == 6
    names = list(centers)
    for i, f1 in enumerate(names):
        x1, y1 = centers[f1]
        assert 0 < x1 < result.canvas and 0 < y1 < result.canvas
        for f2 in names[i + 1:]:
            x2, y2 = centers[f2]
            assert math.hypot(x1 - x2, y1 - y2) > 60


def test_families_view_scales_dust_to_the_family_orb(
    make_symbol: MakeSymbol,
) -> None:
    """Tuning round 4: families-view nodes are STAR DUST filling ~80% of their family
    orb (was a 0.15 blob) — every node sits within agg_radius of its family centre."""
    from cartogate.viz.layout import agg_radius

    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    result = compute_layout([a, b], [])
    for node in (a, b):
        fam = result.family_of[node.unit]
        cx, cy = result.fam_centers[fam]
        px, py = result.positions["families"][node.id]
        r = agg_radius(result.fam_counts[fam])
        assert math.hypot(px - cx, py - cy) <= 0.8 * r + 1e-9


def test_single_family_repo_has_no_arcs(make_symbol: MakeSymbol) -> None:
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/src/b.py")
    result = compute_layout([a, b], [_edge(a.id, b.id)])
    assert result.fam_matrix == ()
    assert list(result.fam_centers) == ["core"]


def test_globe_hubs_take_the_equator(make_symbol: MakeSymbol) -> None:
    """Globe review G4: a hyper-connected unit at a pole fanned its edges into a
    convergence fountain — the heaviest units now take the most equatorial slots."""
    hub = make_symbol("hub.f", unit="r/src/hub.py")
    leaves = [make_symbol(f"l{i}.f", unit=f"r/src/l{i}.py") for i in range(6)]
    edges = [_edge(leaf.id, hub.id) for leaf in leaves]
    result = compute_layout([hub, *leaves], edges)
    hub_y = abs(result.globe_centers["r/src/hub.py"][1])
    leaf_ys = [abs(result.globe_centers[f"r/src/l{i}.py"][1]) for i in range(6)]
    assert hub_y <= min(leaf_ys)  # the hub sits closest to the equator plane


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


def test_package_view_groups_are_disjoint_blocks(make_symbol: MakeSymbol) -> None:
    """Round 8 (user): the row-wrap grid interleaved subpackages, so territory boxes
    overlapped — group-major shelf packing gives every group a disjoint block."""
    from cartogate.viz.layout import _subpackage

    symbols = []
    for i in range(6):
        symbols.append(make_symbol(f"a{i}.f", unit=f"r/alpha/m{i}.py"))
        symbols.append(make_symbol(f"b{i}.f", unit=f"r/beta/m{i}.py"))
    result = compute_layout(symbols, [])
    boxes: dict[str, list[float]] = {}
    for unit, (x, y) in {
        s.unit: result.positions["package"][s.id] for s in symbols
    }.items():
        g = _subpackage(unit)
        b = boxes.setdefault(g, [x, y, x, y])
        b[0] = min(b[0], x)
        b[1] = min(b[1], y)
        b[2] = max(b[2], x)
        b[3] = max(b[3], y)
    (a, b) = (boxes["r/alpha"], boxes["r/beta"]) if "r/alpha" in boxes else (
        list(boxes.values())[0], list(boxes.values())[1])
    overlap = not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
    assert not overlap, f"group blocks overlap: {a} vs {b}"


def test_block_packing_shared_shelf_uses_tallest_block(make_symbol: MakeSymbol) -> None:
    """Review LOW: lock the shared-shelf path — when blocks of different heights share
    a shelf, the wrap must advance by the TALLEST block, keeping shelves disjoint."""
    from cartogate.viz.layout import _subpackage

    symbols = [make_symbol("a0.f", unit="r/alpha/m.py")]
    symbols += [make_symbol(f"b{i}.f", unit=f"r/beta/m{i}.py") for i in range(9)]
    symbols += [make_symbol(f"c{i}.f", unit=f"r/gamma/m{i}.py") for i in range(9)]
    result = compute_layout(symbols, [])
    boxes: dict[str, list[float]] = {}
    for sym in symbols:
        x, y = result.positions["package"][sym.id]
        g = _subpackage(sym.unit)
        b = boxes.setdefault(g, [x, y, x, y])
        b[0] = min(b[0], x)
        b[1] = min(b[1], y)
        b[2] = max(b[2], x)
        b[3] = max(b[3], y)
    names = sorted(boxes)
    for i, a_name in enumerate(names):
        for b_name in names[i + 1:]:
            a, b = boxes[a_name], boxes[b_name]
            overlap = not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
            assert not overlap, f"{a_name} overlaps {b_name}: {a} vs {b}"


def test_enc_groups_separate_palette_colliding_groups(make_symbol: MakeSymbol) -> None:
    """Review MED: the original bug — two groups whose palette indices collide mod 8
    must still get DISTINCT territory group ids (identity, not colour)."""
    symbols = [
        make_symbol(f"g{i}.f", unit=f"r/pkg{i:02d}/m.py") for i in range(10)
    ]
    result = compute_layout(symbols, [])
    ids, names = result.enc_groups["package"]
    assert len(set(ids)) == 10  # every subpackage its own territory
    assert len(names) == 10
    # distinct subpackages must get distinct territory ids regardless of any
    # colour-index collision in the palette — identity is not colour
    assert ids[0] != ids[8]


def test_community_labels_are_unique(make_symbol: MakeSymbol) -> None:
    """Blind critique: two legend rows read identically because two communities shared a
    dominant subpackage. Names must be unique (collisions get a #n disambiguator)."""
    from cartogate.viz.layout import _community_labels
    # two single-file communities with the same subpackage -> same base label
    comm_of = {"pkg/a.py": 0, "pkg/b.py": 1, "other/c.py": 2}
    labels = _community_labels(3, comm_of)
    assert len(set(labels)) == 3  # all distinct
    assert any("(#1)" in n for n in labels) and any("(#2)" in n for n in labels)


def test_palette_has_sixteen_distinct_hues() -> None:
    """Round 10: palette doubled to 16 to cut colour repetition (blind critique)."""
    from cartogate.viz.layout import _PALETTE
    assert len(_PALETTE) == 16
    assert len(set(_PALETTE)) == 16  # no accidental duplicate hex
