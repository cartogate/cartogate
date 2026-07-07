"""Layout engine for the viz HTML — multiple relationship-driven "views".

Each view keeps the readable per-file clusters + intra-cluster hierarchy (module centre →
top-level ring → nested ring); a view only changes **where each cluster sits** and **how nodes
are coloured**, so position becomes meaningful and the same structure can be compared across
arrangements. All three layouts are pure-Python and deterministic (no numpy):

- ``package``     — clusters on an alphabetical grid; colour by subpackage.
- ``dependency``  — clusters in layers by who-depends-on-whom (SCC condensation + topological
                    generations, so import cycles are safe); colour by layer (gradient).
- ``relatedness`` — clusters grouped by community (seeded Louvain on the file graph); colour by
                    community. This is the default view.

Cluster sizes and the grid cell adapt to content (a file's rings grow with its symbol count, and
the cell fits the largest cluster), so nodes don't overlap; the canvas grows to suit and the
viewer pans/zooms.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

import networkx as nx
from networkx.algorithms.community import louvain_communities

from cartogate.schema.edges import Edge
from cartogate.schema.enums import EdgeType, NodeKind
from cartogate.schema.nodes import Node

#: Selector order; the first entry is the default view.
VIEWS = ("relatedness", "dependency", "package", "orbits", "galaxy", "globe")

_PAD = 40.0
_R_MAX = 10.5  # >= renderer's max node radius (5.5 + 0.4*12 = 10.3)
# Spacing is sized off the node diameter so neither siblings on a ring nor adjacent rings can
# overlap, even at max node size: centre-to-centre >= 2 * _R_MAX (+ margin).
_NODE_SPACING = 2 * _R_MAX + 3  # min centre-to-centre between sibling nodes on a ring
_BASE_INNER = 2 * _R_MAX + 4  # inner ring clears the centre module
_BASE_OUTER = _BASE_INNER + _NODE_SPACING  # outer ring clears the inner ring
_CLUSTER_GAP = 34.0  # gap between adjacent cluster footprints
#: Edge types that count as a file-level dependency for the cluster layout. Deliberately distinct
#: from engine ``REFERENCE_EDGE_TYPES`` — this is file-topology, not symbol references, and it omits
#: ``IMPLEMENTS`` (no extra layout value over ``INHERITS``); keep them independent on purpose.
_DEP_EDGES = frozenset({EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.REFERENCES, EdgeType.INHERITS})

#: Categorical palette (hex literals, never user data) for subpackages and communities.
_PALETTE = (
    # Validated dark-surface categorical (dataviz reference instance; fixed order, never
    # re-cycled per entity — communities beyond 8 repeat WITH a legend disclosure).
    "#3987e5",
    "#199e70",
    "#c98500",
    "#008300",
    "#9085e9",
    "#e66767",
    "#d55181",
    "#d95926",
)
#: Diverging gradient (blue → pale → red) for dependency layers.
_GRAD_STOPS = (
    # Sequential ONE-hue blue ramp (light entry points -> deep foundations), monotonic
    # lightness on the dark surface.
    (0.0, (0xB7, 0xD3, 0xF6)),
    (0.5, (0x39, 0x87, 0xE5)),
    (1.0, (0x10, 0x42, 0x81)),
)


@dataclass
class LayoutResult:
    """Everything the renderer needs: per-view node positions, fills, legends, and canvas size."""

    positions: dict[str, dict[str, tuple[float, float]]]  # view -> node_id -> (x, y)
    fills: dict[str, dict[str, str]]  # view -> node_id -> hex colour
    legends: dict[str, list[tuple[str, str]]]  # view -> [(label, hex colour)]
    canvas: float  # square SVG canvas side (user units)
    #: File-level import cycles (each SCC with >1 member), for the health overlay.
    cycle_units: tuple[tuple[str, ...], ...] = ()
    #: 3D sphere centers per unit (globe view) + the sphere radius, for JS rotation.
    globe_centers: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    globe_radius: float = 0.0


def _community_label(index: int, comm_of: dict[str, int]) -> str:
    """Name a community by its dominant subpackage + size — "engine · 12 files" beats
    "community 13" (UX review: the number carried zero information scent)."""
    members = [u for u, c in comm_of.items() if c == index]
    dominant = Counter(_subpackage(u) for u in members).most_common(1)[0][0]
    return f"{dominant} · {len(members)} file" + ("s" if len(members) != 1 else "")


def _subpackage(unit: str) -> str:
    slash = unit.rfind("/")
    return unit[:slash] if slash != -1 else unit


def _is_top(node: Node) -> bool:
    return node.kind is not NodeKind.MODULE and node.is_top_level


def _is_nested(node: Node) -> bool:
    return node.kind is not NodeKind.MODULE and not node.is_top_level


def build_file_graph(nodes: Iterable[Node], edges: Iterable[Edge]) -> nx.DiGraph:
    """A weighted file-level dependency graph: units + aggregated cross-file dependency edges."""
    node_list = list(nodes)
    by_id = {n.id: n for n in node_list}
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_nodes_from(sorted({n.unit for n in node_list}))
    # Insert edges in a stable order so the graph (hence seeded Louvain) is input-order independent.
    for edge in sorted(edges, key=lambda e: (e.src, e.dst, e.type.value)):
        if edge.type not in _DEP_EDGES:
            continue
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None or src.unit == dst.unit:
            continue  # intra-file or dangling edges don't shape the file graph
        if graph.has_edge(src.unit, dst.unit):
            graph[src.unit][dst.unit]["weight"] += 1
        else:
            graph.add_edge(src.unit, dst.unit, weight=1)
    return graph


def compute_layout(nodes: Iterable[Node], edges: Iterable[Edge]) -> LayoutResult:
    """Compute node positions + fills + legends for all views, with adaptive non-overlap spacing."""
    node_list = list(nodes)
    if not node_list:
        return LayoutResult(
            positions={v: {} for v in VIEWS},
            fills={v: {} for v in VIEWS},
            legends={v: [] for v in VIEWS},
            canvas=2 * _PAD,
            cycle_units=(),
        )

    by_unit: dict[str, list[Node]] = {}
    for node in node_list:
        by_unit.setdefault(node.unit, []).append(node)
    units = sorted(by_unit)
    file_graph = build_file_graph(node_list, edges)

    radii = {u: _unit_radii(by_unit[u]) for u in units}
    cell = 2 * max(r[1] + _R_MAX for r in radii.values()) + _CLUSTER_GAP

    comm_order, comm_of = _community_order(units, file_graph)
    layer_of, cycle_units = _layer_of(units, file_graph)

    grid_c, grid_dim = _flow_centers(units, cell)
    comm_c, comm_dim = _flow_centers(comm_order, cell)
    layer_c, layer_dim = _layer_centers(layer_of, cell)
    orbit_c, orbit_dim = _orbit_centers(layer_of, cell)
    galaxy_c, galaxy_dim = _galaxy_centers(comm_order, cell)
    globe3, globe_r, globe_dim = _globe_centers(comm_order, cell)
    canvas = 2 * _PAD + max(grid_dim, comm_dim, layer_dim, orbit_dim, galaxy_dim, globe_dim)
    half = canvas / 2
    globe_c = {u: (half + x, half + y) for u, (x, y, _z) in globe3.items()}  # yaw-0 projection

    offsets = _intra_offsets(node_list, radii)
    centers = {
        "package": grid_c,
        "dependency": layer_c,
        "relatedness": comm_c,
        "orbits": orbit_c,
        "galaxy": galaxy_c,
        "globe": globe_c,
    }
    positions = {
        view: {
            n.id: (centers[view][n.unit][0] + offsets[n.id][0],
                   centers[view][n.unit][1] + offsets[n.id][1])
            for n in node_list
        }
        for view in VIEWS
    }

    subpkgs = sorted({_subpackage(u) for u in units})
    subpkg_color = {g: _PALETTE[i % len(_PALETTE)] for i, g in enumerate(subpkgs)}
    n_layers = max(layer_of.values()) + 1
    n_comms = max(comm_of.values()) + 1
    comm_fill = {n.id: _PALETTE[comm_of[n.unit] % len(_PALETTE)] for n in node_list}
    layer_fill = {
        n.id: _gradient(layer_of[n.unit] / max(n_layers - 1, 1)) for n in node_list
    }
    fills = {
        "package": {n.id: subpkg_color[_subpackage(n.unit)] for n in node_list},
        "dependency": layer_fill,
        "relatedness": comm_fill,
        "orbits": layer_fill,
        "galaxy": comm_fill,
        "globe": comm_fill,
    }
    layer_key = [
        (
            "layer 0 (entry points)" if i == 0
            else f"layer {i} (foundations)" if i == n_layers - 1
            else f"layer {i}",
            _gradient(i / max(n_layers - 1, 1)),
        )
        for i in range(n_layers)
    ]
    comm_key = [
        (_community_label(i, comm_of), _PALETTE[i % len(_PALETTE)]) for i in range(n_comms)
    ]
    legends = {
        "package": [(g, subpkg_color[g]) for g in subpkgs],
        "dependency": layer_key,
        "relatedness": comm_key,
        "orbits": layer_key,
        "galaxy": comm_key,
        "globe": comm_key,
    }
    return LayoutResult(
        positions=positions, fills=fills, legends=legends, canvas=canvas,
        cycle_units=cycle_units,
        globe_centers=globe3,
        globe_radius=globe_r,
    )


# --------------------------------------------------------------------------- #
# Cluster sizing + placement
# --------------------------------------------------------------------------- #


def _ring_radius(base: float, count: int) -> float:
    """A ring big enough to seat ``count`` siblings ``_NODE_SPACING`` apart (never below base)."""
    if count <= 1:
        return base
    return max(base, count * _NODE_SPACING / (2 * math.pi))


def _unit_radii(members: list[Node]) -> tuple[float, float]:
    inner = _ring_radius(_BASE_INNER, sum(1 for n in members if _is_top(n)))
    outer = _ring_radius(_BASE_OUTER, sum(1 for n in members if _is_nested(n)))
    # Floor the outer ring so the two rings never collapse together, whatever the nested count.
    return inner, max(outer, inner + _NODE_SPACING)


def _flow_centers(order: list[str], cell: float) -> tuple[dict[str, tuple[float, float]], float]:
    """Place units in a left-to-right, top-to-bottom grid flow at ``cell`` spacing."""
    cols = max(1, math.ceil(math.sqrt(len(order))))
    centers = {
        u: (_PAD + cell * (i % cols + 0.5), _PAD + cell * (i // cols + 0.5))
        for i, u in enumerate(order)
    }
    rows = math.ceil(len(order) / cols)
    return centers, max(cols, rows) * cell


def _layer_centers(
    layer_of: dict[str, int], cell: float
) -> tuple[dict[str, tuple[float, float]], float]:
    """Column GROUPS by dependency layer (x), each layer wrapped into sub-columns.

    The old single-column-per-layer stacking let one 200-file layer inflate the shared canvas
    to ~100k units (UX review) — every view opened as an ocean of whitespace. Layers now wrap
    at ``max_rows`` (~sqrt of the unit count, floored at 8), reading left-to-right as before
    but staying near-square overall.
    """
    by_layer: dict[int, list[str]] = {}
    for unit, layer_idx in layer_of.items():
        by_layer.setdefault(layer_idx, []).append(unit)
    max_rows = max(8, math.ceil(math.sqrt(len(layer_of))))
    centers: dict[str, tuple[float, float]] = {}
    x_off = 0.0
    tallest = 0
    for layer_idx in sorted(by_layer):
        members = sorted(by_layer[layer_idx])
        subcols = math.ceil(len(members) / max_rows)
        rows = math.ceil(len(members) / subcols)
        tallest = max(tallest, rows)
        for j, unit in enumerate(members):
            sub, row = divmod(j, rows)
            centers[unit] = (
                _PAD + x_off + cell * (sub + 0.5),
                _PAD + cell * (row + 0.5),
            )
        x_off += cell * subcols + cell * 0.5  # half-cell gap between layer groups
    return centers, max(x_off, tallest * cell)


def _orbit_centers(
    layer_of: dict[str, int], cell: float
) -> tuple[dict[str, tuple[float, float]], float]:
    """Dependency layers as CONCENTRIC RINGS — foundations at the core, entry points in the
    outer orbit (the layer metaphor made literal; "constellation" request, 2026-07-06)."""
    n_layers = max(layer_of.values()) + 1 if layer_of else 1
    by_layer: dict[int, list[str]] = {}
    for unit, k in layer_of.items():
        by_layer.setdefault(k, []).append(unit)
    # ring radius: innermost ring for the DEEPEST layer; ensure ring circumference fits members
    radii: dict[int, float] = {}
    r = cell * 0.9
    for depth in range(n_layers):
        layer_idx = n_layers - 1 - depth  # deepest first (innermost)
        members = by_layer.get(layer_idx, [])
        needed = len(members) * cell / (2 * math.pi) if members else 0.0
        r = max(r + cell, needed)
        radii[layer_idx] = r
    size = 2 * (r + cell)
    cx = cy = _PAD + size / 2
    centers: dict[str, tuple[float, float]] = {}
    for layer_idx, members in by_layer.items():
        members.sort()
        ring = radii[layer_idx]
        for j, unit in enumerate(members):
            angle = 2 * math.pi * j / len(members) + layer_idx * 0.35  # de-align spokes
            centers[unit] = (cx + ring * math.cos(angle), cy + ring * math.sin(angle))
    return centers, size


def _galaxy_centers(
    order: list[str], cell: float
) -> tuple[dict[str, tuple[float, float]], float]:
    """Golden-angle spiral (a Fermat galaxy) — community-sorted order forms arms."""
    golden = math.pi * (3 - math.sqrt(5))
    scale = cell * 0.62
    max_r = scale * math.sqrt(max(len(order), 1))
    size = 2 * (max_r + cell)
    cx = cy = _PAD + size / 2
    centers: dict[str, tuple[float, float]] = {}
    for i, unit in enumerate(order):
        r = scale * math.sqrt(i + 0.6)
        theta = i * golden
        centers[unit] = (cx + r * math.cos(theta), cy + r * math.sin(theta))
    return centers, size


def _globe_centers(
    order: list[str], cell: float
) -> tuple[dict[str, tuple[float, float, float]], float, float]:
    """Fibonacci sphere in 3D — the JS projects and rotates it. Returns (xyz per unit, sphere
    radius, canvas size). The 2D "globe" view positions are the yaw-0 orthographic projection,
    so the standard view-morph animates INTO the sphere before the JS takes over rotation."""
    n = max(len(order), 1)
    radius = max(cell * 2.2, cell * math.sqrt(n) * 0.62)
    golden = math.pi * (3 - math.sqrt(5))
    centers3: dict[str, tuple[float, float, float]] = {}
    for i, unit in enumerate(order):
        y = 1 - 2 * (i + 0.5) / n  # -1..1
        ring = math.sqrt(max(0.0, 1 - y * y))
        theta = i * golden
        centers3[unit] = (
            radius * ring * math.cos(theta),
            radius * y,
            radius * ring * math.sin(theta),
        )
    size = 2 * (radius + cell)
    return centers3, radius, size


def _layer_of(
    units: list[str], file_graph: nx.DiGraph
) -> tuple[dict[str, int], tuple[tuple[str, ...], ...]]:
    """Dependency layer per unit, plus the import CYCLES (multi-member SCCs) the condensation
    finds along the way — previously computed and thrown away (UX review: the health overlay
    is the product's thesis on screen)."""
    condensed = nx.condensation(file_graph)  # collapses cycles -> a DAG
    layer_of: dict[str, int] = {}
    cycles: list[tuple[str, ...]] = []
    for layer_idx, scc_ids in enumerate(nx.topological_generations(condensed)):
        for scc in scc_ids:
            members = sorted(condensed.nodes[scc]["members"])
            if len(members) > 1:
                cycles.append(tuple(members))
            for unit in members:
                layer_of[unit] = layer_idx
    for unit in units:
        layer_of.setdefault(unit, 0)
    return layer_of, tuple(sorted(cycles))


def _community_order(
    units: list[str], file_graph: nx.DiGraph
) -> tuple[list[str], dict[str, int]]:
    # Precondition: non-empty graph (compute_layout early-returns on no nodes).
    undirected = file_graph.to_undirected()
    raw = (
        louvain_communities(undirected, seed=7, weight="weight")
        if undirected.number_of_nodes()
        else []
    )
    communities = sorted((sorted(c) for c in raw), key=lambda c: (-len(c), c[0]))
    comm_of: dict[str, int] = {}
    for comm_idx, members in enumerate(communities):
        for unit in members:
            comm_of[unit] = comm_idx
    for unit in units:
        comm_of.setdefault(unit, 0)
    order = sorted(units, key=lambda u: (comm_of[u], u))  # same-community units are adjacent
    return order, comm_of


def _intra_offsets(
    nodes: list[Node], radii: dict[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    by_unit: dict[str, list[Node]] = {}
    for node in sorted(nodes, key=lambda n: (n.unit, not n.is_top_level, n.qualified_name, n.id)):
        by_unit.setdefault(node.unit, []).append(node)

    offsets: dict[str, tuple[float, float]] = {}
    for unit, members in by_unit.items():
        inner, outer = radii[unit]
        for node in members:
            if node.kind is NodeKind.MODULE:
                offsets[node.id] = (0.0, 0.0)  # module at the cluster centre
        _ring_offsets(offsets, [n for n in members if _is_top(n)], inner)
        _ring_offsets(offsets, [n for n in members if _is_nested(n)], outer)
    return offsets


def _ring_offsets(
    offsets: dict[str, tuple[float, float]], members: list[Node], radius: float
) -> None:
    count = len(members)
    for i, node in enumerate(members):
        angle = 2 * math.pi * i / count if count else 0.0
        offsets[node.id] = (radius * math.cos(angle), radius * math.sin(angle))


def _gradient(t: float) -> str:
    t = min(1.0, max(0.0, t))
    for (t0, c0), (t1, c1) in zip(_GRAD_STOPS, _GRAD_STOPS[1:], strict=False):
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            rgb = tuple(round(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
            return "#{:02x}{:02x}{:02x}".format(*rgb)
    return "#{:02x}{:02x}{:02x}".format(*_GRAD_STOPS[-1][1])
