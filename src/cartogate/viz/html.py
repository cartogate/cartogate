"""A single self-contained, offline interactive graph view with switchable layout "views".

The graph is rendered once as inline SVG; positions, fills, cluster-label spots and adjacency for
**every view** are baked in as compact JSON, and a little vanilla JS re-arranges/recolours on view
switch (Relatedness / Dependency / Package), traces a node's connections on hover, and filters
edge types. There are **no external resources** (no CDN, no JS library, no numpy) — the file opens
with a double-click and needs no network. The layouts themselves live in ``viz/layout.py``.

For very large graphs the SVG is capped to ``max_nodes`` (highest-degree kept) with a logged
warning — GraphML/JSON exports keep the full graph.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from collections.abc import Iterable
from html import escape

from cartogate.schema.edges import Edge
from cartogate.schema.enums import NodeKind
from cartogate.schema.nodes import Node
from cartogate.viz.layout import (
    _PALETTE,  # viz-package-private: the shared categorical hues (legend fold honesty)
    VIEWS,
    LayoutResult,
    agg_radius,
    compute_layout,
    ring_centers,
)

_log = logging.getLogger(__name__)

#: How far above a cluster's top a label sits (SVG user units).
_LABEL_LIFT = 14.0
#: Non-identifier characters that must not leak into a CSS class / `data-css` token.
_NON_TOKEN = re.compile(r"[^a-z0-9_-]")

#: Edge hues validated on the dark surface (dataviz skill validator); dash patterns are the
#: REDUNDANT channel so type identity survives colorblindness and grayscale.
_EDGE_COLORS = {
    "calls": "#d95926",
    "imports": "#3987e5",
    "depends_on": "#3987e5",
    "defines": "#1fb2a6",
    "references": "#199e70",
    "inherits": "#9085e9",
    "implements": "#9085e9",
    "documents": "#c98500",
}
_EDGE_DASH = {
    "references": "3 2",
    "inherits": "1 2",
    "implements": "1 2",
    "documents": "0.5 2.5",
}
_DEFAULT_COLOR = "#6b7694"
#: Structural module->own-symbol edges; numerous and noisy, so hidden by default.
_NOISY_EDGE = "defines"
#: Per-view, one-line explanation of the arrangement + colour (static text — no user data).
_VIEW_EXPLAIN = {
    "families": "<b>Structure:</b> the codebase generalised into role groups — arcs show how "
    "the groups relate, and the flowing pulse runs from the group that uses toward the group "
    "it uses.",
    "relatedness": "<b>Communities:</b> files that call or import each other sit together — "
    "colour marks the community.",
    "dependency": "<b>Dependency:</b> column groups are dependency layers — entry points on the "
    "left, the foundations they rely on toward the right (colour = layer).",
    "package": "<b>Package:</b> clusters laid out alphabetically by subpackage (colour = "
    "subpackage).",
    "orbits": "<b>Orbits:</b> dependency layers as rings — foundations at the core, entry "
    "points in the outer orbit (colour = layer).",
    "galaxy": "<b>Galaxy:</b> files spiral out from the centre; communities form the arms "
    "(colour = community).",
    "globe": "<b>Globe:</b> your codebase on a sphere — drag to spin it (colour = community).",
}
#: On-screen names for the view radios (the internal keys stay stable for data/JSON).
_VIEW_LABEL = {
    "families": "Structure",
    "relatedness": "Communities",
    "dependency": "Dependency",
    "package": "Package",
    "orbits": "Orbits",
    "galaxy": "Galaxy",
    "globe": "Globe",
}
assert set(_VIEW_EXPLAIN) == set(VIEWS)  # every view needs an explainer (fail fast on drift)


def _dash_css() -> str:
    """Per-type dash rules in SCREEN pixels: calc(N * var(--dz)) where rescale() sets
    --dz to user-units-per-pixel — gaps keep their structure at any zoom (user report:
    dash spacing scaled with zoom and dissolved the patterns)."""
    rules = []
    for value, dash in sorted(_EDGE_DASH.items()):
        parts = " ".join(f"calc({n} * var(--dz, 1))" for n in dash.split())
        # _dash_css is a PLAIN f-string whose output is substituted (never re-scanned)
        # into the document template — single literal braces (review CRITICAL: quadruple
        # escaping emitted {{ }}, invalid CSS, silently killing every dash pattern)
        rules.append(f"  path.type-{_css_token(value)} {{ stroke-dasharray: {parts}; }}")
    return "\n".join(rules)


def _css_token(value: str) -> str:
    """Coerce a value into a safe CSS class token (keeps SVG class and data-css in lockstep)."""
    return _NON_TOKEN.sub("-", value.lower())


def _cluster_label(unit: str) -> str:
    """A short label for a file cluster, e.g. ``engine/flag`` for ``.../engine/flag.py``."""
    parts = unit.split("/")
    stem = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    return f"{parts[-2]}/{stem}" if len(parts) >= 2 else stem


def _js_json(obj: object) -> str:
    """Serialize for inline `<script>` embedding; neutralize ``</script>``/``<!--``/``-->``."""
    return json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")


def to_html(
    nodes: Iterable[Node], edges: Iterable[Edge], *, title: str = "Cartogate",
    max_nodes: int = 1500, source_root: str = "",
) -> str:
    """Render the graph as a single self-contained interactive HTML document."""
    if max_nodes < 1:
        raise ValueError(f"max_nodes must be >= 1, got {max_nodes}")
    node_list = sorted(nodes, key=lambda n: n.id)
    total_nodes = len(node_list)
    by_id = {n.id: n for n in node_list}
    present = [e for e in edges if e.src in by_id and e.dst in by_id]

    degree: Counter[str] = Counter()
    for edge in present:
        degree[edge.src] += 1
        degree[edge.dst] += 1

    # Names of EVERY symbol, captured before any pruning: search must answer for the
    # whole corpus, not just the rendered cut — an empty result must mean "does not
    # exist", never "was pruned" (R11 AC-1; panel: search silently ignored 87%).
    off_map_names: list[str] = []
    if len(node_list) > max_nodes:
        kept = {
            n.id
            for n in sorted(node_list, key=lambda n: (-degree[n.id], n.qualified_name))[:max_nodes]
        }
        off_map_names = sorted(n.qualified_name for n in node_list if n.id not in kept)
        # bounded: a 100k-symbol repo must not inline megabytes of names — the full
        # graph lives in the JSON/GraphML exports (review MED)
        off_map_names = off_map_names[:5000]
        _log.warning(
            "viz: graph has %d nodes; capping the SVG to the %d highest-degree (dropped %d). "
            "GraphML/JSON exports keep the full graph.",
            len(node_list),
            max_nodes,
            len(node_list) - len(kept),
        )
        node_list = [n for n in node_list if n.id in kept]
        by_id = {n.id: n for n in node_list}
        present = [e for e in present if e.src in by_id and e.dst in by_id]

    present.sort(key=lambda e: (e.src, e.dst, e.type.value))  # byte-reproducible edge/ADJ order
    # Repeated (src, dst, type) pairs used to render as STACKED identical lines (uncontrolled
    # double-opacity + DOM bloat — UX review). Deduped; multiplicity becomes deliberate width.
    deduped: list[tuple[Edge, int]] = []
    for edge in present:
        if deduped and (deduped[-1][0].src, deduped[-1][0].dst, deduped[-1][0].type) == (
            edge.src, edge.dst, edge.type
        ):
            deduped[-1] = (deduped[-1][0], deduped[-1][1] + 1)
        else:
            deduped.append((edge, 1))
    layout = compute_layout(node_list, present)
    default = VIEWS[0]
    units = sorted({n.unit for n in node_list})
    unit_idx = {u: i for i, u in enumerate(units)}
    labels = _label_positions(node_list, layout, units)
    adjacency = _adjacency(present)
    edge_types = sorted({e.type.value for e in present})
    scc_of: dict[str, int] = {}
    for scc_index, members in enumerate(layout.cycle_units):
        for unit in members:
            scc_of[unit] = scc_index

    fam_idx = {f: i for i, f in enumerate(layout.fam_nodes)}
    unit_fam = {u: fam_idx[layout.family_of[u]] for u in units}
    # Locality (visual review R4): cross-file edges are the detail-view spaghetti —
    # intra-unit edges live inside their cluster footprint and always show; of the
    # cross-unit edges only the heaviest _LR_KEEP stay visible at rest, the remainder
    # carry class "lr" and reveal on trace/drill or via the all-edges overlay.
    # (A community-based rule was tried first and hid only ~15% — Louvain communities
    # span the canvas, so "same community" says nothing about visual length.)
    def _is_cross_unit(edge: Edge) -> bool:
        return by_id[edge.src].unit != by_id[edge.dst].unit

    lr_pool = sorted(
        (item for item in deduped if _is_cross_unit(item[0])),
        key=lambda item: (-item[1], item[0].src, item[0].dst, item[0].type.value),
    )
    lr_quiet = {id(item[0]) for item in lr_pool[_LR_KEEP:]}
    svg_nodes = "\n".join(
        _node_svg(
            n, layout, default, degree[n.id], in_cycle=n.unit in scc_of,
            fam=unit_fam[n.unit],
        )
        for n in node_list
    )
    svg_edges = "\n".join(
        _edge_svg(
            e, layout.positions[default], count,
            cyc=(
                by_id[e.src].unit in scc_of
                and scc_of.get(by_id[e.src].unit) == scc_of.get(by_id[e.dst].unit)
                and by_id[e.src].unit != by_id[e.dst].unit
            ),
            fs=unit_fam[by_id[e.src].unit],
            fd=unit_fam[by_id[e.dst].unit],
            lr=id(e) in lr_quiet,
        )
        for e, count in deduped
    )
    # cluster weight = files in the unit + their connections — the declutter engine
    # labels the heaviest clusters first, so the most important names never drop out
    unit_deg: dict[str, int] = dict.fromkeys(units, 0)
    for e in present:
        unit_deg[by_id[e.src].unit] = unit_deg.get(by_id[e.src].unit, 0) + 1
        unit_deg[by_id[e.dst].unit] = unit_deg.get(by_id[e.dst].unit, 0) + 1
    unit_size = Counter(n.unit for n in node_list)
    unit_weight = {u: unit_size[u] + unit_deg.get(u, 0) for u in units}
    svg_labels = "\n".join(
        _label_svg(u, unit_idx[u], labels[default][unit_idx[u]], unit_weight[u])
        for u in units
    )

    # every family lands VISIBLE (user decision round 3); the chips remain the way to
    # focus down to core-only when tracing functional code
    st0 = [2 for _f in layout.fam_nodes]
    svg_subs, sub_payload = _sub_tier(layout, units, deduped, by_id)
    ring_gradients = _ring_gradients(c for _, c in layout.legends["families"])
    return _document(
        title=title,
        default=default,
        canvas=layout.canvas,
        legends=layout.legends,
        edge_types=edge_types,
        svg_nodes=svg_nodes,
        svg_edges=svg_edges,
        svg_labels=svg_labels,
        svg_fams=_fam_svg(layout),
        svg_subs=svg_subs,
        sub_json=_js_json(sub_payload),
        offmap_json=_js_json(off_map_names),
        fam_markers=_fam_markers(edge_types) + "\n" + ring_gradients,
        fam_chips=_fam_chips(layout, st0),
        leg_json=_js_json(layout.legends),
        enc_json=_js_json(layout.enc_groups),
        fam_json=_js_json({
            "names": list(layout.fam_nodes),
            "fills": [c for _, c in layout.legends["families"]],
            "uf": [unit_fam[u] for u in units],
            "c": [
                [round(layout.fam_centers[f][0], 1), round(layout.fam_centers[f][1], 1)]
                for f in layout.fam_nodes
            ],
            "cnt": [layout.fam_counts[f] for f in layout.fam_nodes],
            "m": [[fam_idx[fs], fam_idx[fd], t, c] for fs, fd, t, c in layout.fam_matrix],
            "st0": st0,
        }),
        node_count=len(node_list),
        total_nodes=total_nodes,
        edge_count=len(deduped),
        lr_hidden=len(lr_quiet),
        src_root_json=_js_json(source_root),
        cycle_count=len(layout.cycle_units),
        globe_json=_js_json({
            "r": round(layout.globe_radius, 1),
            "half": round(layout.canvas / 2, 1),
            "units": units,
            "c": [
                [round(c[0], 1), round(c[1], 1), round(c[2], 1)]
                for c in (layout.globe_centers.get(u, (0.0, 0.0, 0.0)) for u in units)
            ],
            "u": {n.id: unit_idx[n.unit] for n in node_list},
        }),
        globe_radius=layout.globe_radius,
        pos_json=_js_json({v: {nid: [round(x, 1), round(y, 1)] for nid, (x, y) in p.items()}
                           for v, p in layout.positions.items()}),
        fill_json=_js_json(layout.fills),
        label_json=_js_json({v: [labels[v][i] for i in range(len(units))] for v in VIEWS}),
        adj_json=_js_json(adjacency),
    )


def _label_positions(
    nodes: list[Node], layout: LayoutResult, units: list[str]
) -> dict[str, list[list[float]]]:
    """Per view, a cluster-label spot per unit index (centroid x, top-of-cluster y, lifted up)."""
    idx = {u: i for i, u in enumerate(units)}
    out: dict[str, list[list[float]]] = {}
    for view in VIEWS:
        spots: list[list[float]] = [[0.0, 0.0] for _ in units]
        grouped: dict[str, list[tuple[float, float]]] = {}
        for node in nodes:
            grouped.setdefault(node.unit, []).append(layout.positions[view][node.id])
        for unit, pts in grouped.items():
            cx = sum(p[0] for p in pts) / len(pts)
            top = min(p[1] for p in pts)
            spots[idx[unit]] = [round(cx, 1), round(top - _LABEL_LIFT, 1)]
        out[view] = spots
    return out


def _adjacency(edges: list[Edge]) -> dict[str, list[str]]:
    adj: dict[str, set[str]] = {}
    for edge in edges:
        adj.setdefault(edge.src, set()).add(edge.dst)
        adj.setdefault(edge.dst, set()).add(edge.src)
    return {node_id: sorted(neighbours) for node_id, neighbours in adj.items()}


def _node_svg(
    node: Node, layout: LayoutResult, view: str, degree: int, *, in_cycle: bool = False,
    fam: int = 0,
) -> str:
    x, y = layout.positions[view][node.id]
    fill = layout.fills[view][node.id]
    is_module = node.kind is NodeKind.MODULE
    radius = (5.5 if is_module else 3.0) + min(degree, 12) * 0.4
    classes = "node" + (" mod" if is_module else "") + (" cyc-n" if in_cycle else "")
    stroke = ' stroke-width="0.5"' if is_module else ""
    # unit:line rides in the tooltip payload — the detail card and the editor deep
    # link both parse it back out (R11 AC-6/AC-24)
    tip = escape(
        f"{node.qualified_name}\n({node.kind.value}) {node.unit}:{node.location.start_line}"
    )
    return (
        f'<circle class="{classes}" data-id="{node.id}" data-r="{radius:.1f}" data-f="{fam}" '
        f'cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{fill}"{stroke}>'
        f"<title>{tip}</title></circle>"
    )


#: Cross-unit edges kept visible at rest (the heaviest by multiplicity; deterministic).
_LR_KEEP = 400


def _edge_svg(
    edge: Edge, pos: dict[str, tuple[float, float]], count: int = 1, *, cyc: bool = False,
    fs: int = 0, fd: int = 0, lr: bool = False,
) -> str:
    x1, y1 = pos[edge.src]
    x2, y2 = pos[edge.dst]
    colour = _EDGE_COLORS.get(edge.type.value, _DEFAULT_COLOR)
    hidden = ' style="display:none"' if edge.type.value == _NOISY_EDGE else ""
    width = min(2.8, 0.9 + 0.35 * math.log2(count)) if count > 1 else 0.9
    classes = (
        f"edge type-{_css_token(edge.type.value)}" + (" cyc" if cyc else "")
        + (" lr" if lr else "")
    )
    # Dash patterns live in per-type CSS scaled by --dz (screen-fixed under zoom), so
    # no inline dasharray and no pathLength — the LED streaks need the real path metric.
    return (
        f'<path class="{classes}" data-src="{edge.src}" '
        f'data-dst="{edge.dst}" data-fs="{fs}" data-fd="{fd}" '
        f'd="M {x1:.1f} {y1:.1f} Q {(x1 + x2) / 2:.1f} {(y1 + y2) / 2:.1f} '
        f'{x2:.1f} {y2:.1f}" fill="none" '
        f'stroke="{colour}" stroke-width="{width:.2f}" opacity="0.72"'
        f' vector-effect="non-scaling-stroke"{hidden} />'
    )


#: Family aggregate circle radius — the layout's shared formula (dust scaling uses it).
def _fam_radius(count: int) -> float:
    return agg_radius(count)


def _shade(hex_colour: str, factor: float) -> str:
    """Lighten (>1) or darken (<1) a #rrggbb colour, clamped — for gradient stops."""
    raw = hex_colour.lstrip("#")
    channels = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return "#" + "".join(f"{max(0, min(255, round(c * factor))):02x}" for c in channels)


def _ring_gradients(colours: Iterable[str]) -> str:
    """A radial gradient per aggregate-circle colour (observatory depth, not flat disks)."""
    return "\n".join(
        f'<radialGradient id="rg-{c.lstrip("#")}" cx="38%" cy="32%" r="75%">'
        f'<stop offset="0%" stop-color="{_shade(c, 1.18)}"/>'
        f'<stop offset="55%" stop-color="{c}"/>'
        f'<stop offset="100%" stop-color="{_shade(c, 0.62)}"/></radialGradient>'
        for c in sorted(set(colours))
    )


#: Tri-state glyphs for the family chips, indexed by state (0 hidden, 1 fringe, 2 full).
_STATE_GLYPH = ("○", "◐", "◉")


def _fam_chips(layout: LayoutResult, st0: list[int]) -> str:
    """Tri-state visibility chips, one per present family (detail views only).

    Click cycles hidden ○ -> fringe ◐ -> full ◉. Fringe shows only the members touching a
    currently-visible node — "the tests covering the code on screen" — the deliberate,
    scoped reveal for the heavy non-core families that land hidden.
    """
    colour_of = dict(
        zip(layout.fam_nodes, (c for _, c in layout.legends["families"]), strict=True)
    )
    return "\n".join(
        f'<button class="chip" data-f="{i}" data-state="{st0[i]}" '
        f'title="{escape(f)} — click cycles: hidden, fringe (only members touching visible '
        f'code), full">'
        f'<span class="dot" style="background:{colour_of[f]}"></span>'
        f'<span class="glyph">{_STATE_GLYPH[st0[i]]}</span> {escape(f)} '
        f'<span class="cnt">{layout.fam_counts[f]:,}</span></button>'
        for i, f in enumerate(layout.fam_nodes)
    )


def _fam_markers(edge_types: Iterable[str]) -> str:
    """One arrowhead marker per edge type (typed colours), shared by every ring tier.

    Markers are the static direction channel — they carry direction even under
    prefers-reduced-motion, when the comet flow is disabled.
    """
    types = sorted(set(edge_types))
    return "\n".join(
        f'<marker id="famarr-{_css_token(t)}" viewBox="0 0 10 10" refX="8" refY="5" '
        f'markerWidth="5.5" markerHeight="5.5" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{_EDGE_COLORS.get(t, _DEFAULT_COLOR)}"/>'
        f"</marker>"
        for t in types
    )


def _ring_svg(
    names: list[str],
    centers: dict[str, tuple[float, float]],
    counts: dict[str, int],
    fills: dict[str, str],
    matrix: Iterable[tuple[str, str, str, int]],
    canvas: float,
    *,
    circle_cls: str,
    label_cls: str,
    arc_cls: str,
    hit_cls: str,
    idx_attr: str,
    src_attr: str,
    dst_attr: str,
    label_of: dict[str, str] | None = None,
) -> str:
    """Aggregate-ring markup: circles + labels + ONE fanned arc per (pair, edge type).

    The shared grammar of the family ring AND each family's subpackage ring (user spec:
    a pair's K types fan symmetrically — middle straight, others left/right; directions
    merge with arrowheads at whichever ends apply and one flow streak per direction).
    Deterministic throughout.
    """
    radius_of = {n: _fam_radius(counts[n]) for n in names}
    idx = {n: i for i, n in enumerate(names)}
    show = label_of or {n: n for n in names}
    merged: dict[tuple[str, str, str], list[int]] = {}  # (A, B, type) -> [a2b, b2a]
    for gs, gd, t, c in matrix:
        a, b = (gs, gd) if idx[gs] < idx[gd] else (gd, gs)
        row = merged.setdefault((a, b, t), [0, 0])
        row[0 if gs == a else 1] += c
    pair_types: dict[tuple[str, str], list[str]] = {}
    for a, b, t in merged:
        pair_types.setdefault((a, b), []).append(t)
    for types in pair_types.values():
        types.sort()
    parts: list[str] = []
    fam_fs = max(13.0, min(30.0, canvas * 0.015))
    arc_fs = max(10.0, min(16.0, canvas * 0.010))
    # Labeling policy (visual review): a dense ring drowning in floating text was the
    # main readability killer. At rest, a ring with more than 12 arcs labels only each
    # pair's HEAVIEST arc; the rest carry class "quiet" and reveal on hover.
    label_all = len(merged) <= 12
    pair_heaviest: dict[tuple[str, str], tuple[str, int]] = {}
    for (a, b, t), (a2b, b2a) in merged.items():
        cur = pair_heaviest.get((a, b))
        if cur is None or a2b + b2a > cur[1] or (a2b + b2a == cur[1] and t < cur[0]):
            pair_heaviest[(a, b)] = (t, a2b + b2a)
    # arrival rank per endpoint: how many arcs already land on this circle
    # (R11 AC-29 — staggers arrowhead depth so shared targets don't smear)
    arrivals: dict[str, int] = dict.fromkeys(names, 0)
    for i, ((a, b, t), (a2b, b2a)) in enumerate(sorted(merged.items())):
        (ax, ay), (bx, by) = centers[a], centers[b]
        dist = math.hypot(bx - ax, by - ay)
        if dist < 1e-6:
            continue
        ux, uy = (bx - ax) / dist, (by - ay) / dist
        nx, ny = -uy, ux
        # symmetric fan around the straight chord: with K types the middle one is
        # straight and the rest arc left/right (user spec — no shared-side corridor)
        k_types = pair_types[(a, b)]
        spread = max(26.0, 0.09 * dist)
        bow = (k_types.index(t) - (len(k_types) - 1) / 2) * spread
        mx, my = (ax + bx) / 2, (ay + by) / 2
        cx = mx + nx * bow
        cy = my + ny * bow
        sd = math.hypot(cx - ax, cy - ay)
        ed = math.hypot(bx - cx, by - cy)
        in_a = 4 + 6 * min(arrivals[a], 3) if b2a else 4
        in_b = 4 + 6 * min(arrivals[b], 3) if a2b else 4
        if b2a:
            arrivals[a] += 1
        if a2b:
            arrivals[b] += 1
        sx = ax + (cx - ax) / sd * (radius_of[a] + in_a)
        sy = ay + (cy - ay) / sd * (radius_of[a] + in_a)
        ex = bx + (cx - bx) / ed * (radius_of[b] + in_b)  # arrowheads clear + stagger
        ey = by + (cy - by) / ed * (radius_of[b] + in_b)
        d = f"M {sx:.1f} {sy:.1f} Q {cx:.1f} {cy:.1f} {ex:.1f} {ey:.1f}"
        d_rev = f"M {ex:.1f} {ey:.1f} Q {cx:.1f} {cy:.1f} {sx:.1f} {sy:.1f}"
        colour = _EDGE_COLORS.get(t, _DEFAULT_COLOR)
        total = a2b + b2a
        width = min(5.0, 1.6 + 0.5 * math.log2(total)) if total > 1 else 1.6
        token = _css_token(t)
        attrs = f'{src_attr}="{idx[a]}" {dst_attr}="{idx[b]}" data-t="{escape(t)}"'
        markers = (f' marker-end="url(#famarr-{token})"' if a2b else "") + (
            f' marker-start="url(#famarr-{token})"' if b2a else ""
        )
        parts.append(
            f'<path class="{arc_cls} type-{token}" {attrs} d="{d}" pathLength="100" '
            f'fill="none" stroke="{colour}" stroke-width="{width:.2f}" '
            f'opacity="0.85"{markers}/>'
        )
        for direction, dd, cnt in (("fwd", d, a2b), ("rev", d_rev, b2a)):
            if not cnt:
                continue
            delay = ((i * 0.37) + (0.9 if direction == "rev" else 0.0)) % 2.6
            # the flow carries the TYPE class too: its white ink must register to the
            # base arc's dash pattern (user report: the pulse crossed the gaps between
            # dashes — the overlay was solid while the arc was dotted)
            parts.append(
                f'<path class="famflow type-{token}" {attrs} d="{dd}" fill="none" '
                f'stroke-width="{min(2.4, width * 0.5 + 0.8):.2f}" data-stagger="{delay:.2f}"/>'
            )
        tip_parts = []
        if a2b:
            tip_parts.append(f"{show[a]} → {show[b]} · {t} · {a2b:,}")
        if b2a:
            tip_parts.append(f"{show[b]} → {show[a]} · {t} · {b2a:,}")
        tip = escape("  |  ".join(tip_parts))
        parts.append(
            f'<path class="{hit_cls}" {attrs} d="{d}" pathLength="100" fill="none" '
            f'stroke="transparent" stroke-width="14"><title>{tip}</title></path>'
        )
        # label rides its arc's own convex side; ranks ALSO stagger along the chord —
        # perpendicular apexes are only spread/2 apart (< a line height), so without the
        # tangential shift adjacent labels collided (review HIGH)
        side = 1.0 if bow >= 0 else -1.0
        rank = k_types.index(t)
        tang = (rank - (len(k_types) - 1) / 2) * min(0.24 * dist, max(56.0, arc_fs * 3.0))
        perp = side * (arc_fs * 0.55 + 5)
        apex_x = 0.25 * sx + 0.5 * cx + 0.25 * ex + nx * perp + ux * tang
        apex_y = 0.25 * sy + 0.5 * cy + 0.25 * ey + ny * perp + uy * tang
        quiet = "" if label_all or pair_heaviest[(a, b)][0] == t else " quiet"
        parts.append(
            f'<text class="arclabel{quiet}" {attrs} x="{apex_x:.1f}" y="{apex_y:.1f}" '
            f'text-anchor="middle" fill="{colour}" style="font-size:{arc_fs:.1f}px">'
            f"{escape(t)} · {total:,}</text>"
        )
    for n in names:
        x, y = centers[n]
        r = radius_of[n]
        count = counts[n]
        tip = escape(f"{n} — {count:,} file" + ("s" if count != 1 else ""))
        parts.append(
            f'<circle class="{circle_cls}" {idx_attr}="{idx[n]}" cx="{x:.1f}" cy="{y:.1f}" '
            f'r="{r:.1f}" fill="url(#rg-{fills[n].lstrip("#")})" '
            f'stroke="{_shade(fills[n], 1.45)}"><title>{tip}</title></circle>'
        )
        parts.append(
            f'<text class="{label_cls}" {idx_attr}="{idx[n]}" x="{x:.1f}" '
            f'y="{y + r + fam_fs + 6:.1f}" text-anchor="middle" '
            f'style="font-size:{fam_fs:.1f}px">{escape(show[n])}'
            f'<tspan class="cnt" dx="7">{count:,}</tspan></text>'
        )
    return "\n".join(parts)


def _fam_svg(layout: LayoutResult) -> str:
    """The families layer — the top aggregate ring (see _ring_svg for the grammar)."""
    colour_of = dict(
        zip(layout.fam_nodes, (c for _, c in layout.legends["families"]), strict=True)
    )
    return _ring_svg(
        list(layout.fam_nodes), layout.fam_centers, layout.fam_counts, colour_of,
        layout.fam_matrix, layout.canvas,
        circle_cls="fam", label_cls="famlabel", arc_cls="famarc", hit_cls="famhit",
        idx_attr="data-f", src_attr="data-fs", dst_attr="data-fd",
    )


def _subgroup_key(unit: str) -> str:
    """The subpackage a unit belongs to: its directory path after the repo prefix."""
    parts = unit.split("/")
    return "/".join(parts[1:-1]) or "(root)"


def _subgroup_label(key: str) -> str:
    """A short display name: the last two path segments carry the identity."""
    parts = key.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else key


def _sub_tier(
    layout: LayoutResult, units: list[str], deduped: list[tuple[Edge, int]],
    by_id: dict[str, Node],
) -> tuple[str, dict[str, object]]:
    """Per-family subpackage rings (round-3 item 4) + the SUB payload.

    A family with >= 2 subpackages gets a baked, hidden ring layer with the exact
    top-level grammar; single-group families keep the constellation fallback. Payload:
    ``g`` = subgroup index per unit index (within its family), ``n`` = display names per
    family.
    """
    unit_idx = {u: i for i, u in enumerate(units)}
    layers: list[str] = []
    g_of_unit = [0] * len(units)
    names_per_fam: list[list[str]] = []
    for fi, f in enumerate(layout.fam_nodes):
        f_units = [u for u in units if layout.family_of[u] == f]
        keys = sorted({_subgroup_key(u) for u in f_units})
        key_idx = {k: i for i, k in enumerate(keys)}
        for u in f_units:
            g_of_unit[unit_idx[u]] = key_idx[_subgroup_key(u)]
        names_per_fam.append([_subgroup_label(k) for k in keys])
        if len(keys) < 2:
            continue
        counts = dict.fromkeys(keys, 0)
        for u in f_units:
            counts[_subgroup_key(u)] += 1
        rows: dict[tuple[str, str, str], int] = {}
        for edge, count in deduped:
            su, du = by_id[edge.src].unit, by_id[edge.dst].unit
            if layout.family_of.get(su) != f or layout.family_of.get(du) != f:
                continue
            gs, gd = _subgroup_key(su), _subgroup_key(du)
            if gs == gd or edge.type.value == _NOISY_EDGE:
                continue
            rows[(gs, gd, edge.type.value)] = rows.get((gs, gd, edge.type.value), 0) + count
        matrix = tuple(
            (gs, gd, t, c) for (gs, gd, t), c in sorted(
                rows.items(), key=lambda kv: (key_idx[kv[0][0]], key_idx[kv[0][1]], kv[0][2])
            )
        )
        fam_colour = dict(
            zip(layout.fam_nodes, (c for _, c in layout.legends["families"]), strict=True)
        )[f]
        centers = ring_centers(tuple(keys), layout.canvas)
        markup = _ring_svg(
            keys, centers, counts, dict.fromkeys(keys, fam_colour), matrix, layout.canvas,
            circle_cls="sub", label_cls="sublabel", arc_cls="subarc", hit_cls="subhit",
            idx_attr="data-g", src_attr="data-gs", dst_attr="data-gd",
            label_of={k: _subgroup_label(k) for k in keys},
        )
        layers.append(
            f'<g class="sublayer" data-f="{fi}" style="display:none">\n{markup}\n</g>'
        )
    payload = {"g": g_of_unit, "n": names_per_fam}
    return "\n".join(layers), payload


def _label_svg(unit: str, unit_index: int, spot: list[float], weight: int) -> str:
    return (
        f'<text class="label" data-ui="{unit_index}" data-w="{weight}" '
        f'x="{spot[0]:.1f}" y="{spot[1]:.1f}" '
        f'text-anchor="middle" font-size="9">{escape(_cluster_label(unit))}</text>'
    )


def _color_key(view: str, items: list[tuple[str, str]], default: str) -> str:
    """Legend rows, capped at 8 with an honest fold line.

    The fold copy is per colour scheme: palette views repeat only when categories exceed
    the palette size (round 10: doubled to 16, so 'colours repeat' now fires far less and
    only when it's actually true — the blind critique flagged the false claim); dependency/
    orbits layers use a continuous one-hue ramp (deeper = darker, distinct not repeated).
    """
    cap = 12  # show more of the (now 16-hue) key before folding
    rows = [
        f'<div class="item"><span class="swatch" style="background:{colour}"></span>'
        f"{escape(label)}</div>"
        for label, colour in items[:cap]
    ]
    if len(items) > cap:
        if view in ("dependency", "orbits"):
            note = "deeper layers (darker on the same ramp)"
        elif len(items) > len(_PALETTE):
            note = "smaller (colours repeat)"  # only true past the palette size
        else:
            note = "smaller (distinct colours)"
        rows.append(f'<div class="item muted">+{len(items) - cap} {note}</div>')
    shown = "" if view == default else ' style="display:none"'
    return f'<div class="legend" id="legend-{view}"{shown}>\n' + "\n".join(rows) + "\n</div>"


def _edge_filter(edge_types: list[str]) -> str:
    rows = []
    for value in edge_types:
        colour = _EDGE_COLORS.get(value, _DEFAULT_COLOR)
        dash = _EDGE_DASH.get(value, "")
        # A real mini-SVG line: shows the ACTUAL dash pattern. (The old border-top hack
        # interpolated the dash numbers where a border-style belongs — invalid CSS, so
        # browsers dropped the swatch for every dashed type; user report: documents /
        # inherits / references had no key.)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        swatch = (
            f'<svg class="swatch-line" width="18" height="4" aria-hidden="true">'
            f'<line x1="0" y1="2" x2="18" y2="2" stroke="{colour}" stroke-width="2"'
            f"{dash_attr}/></svg>"
        )
        checked = "" if value == _NOISY_EDGE else " checked"
        token = _css_token(value)
        hint = (
            ' title="file → its own symbols; structural noise, hidden by default"'
            if value == _NOISY_EDGE
            else ""
        )
        rows.append(
            f'<label class="item"{hint}><input type="checkbox"{checked} data-css="type-{token}">'
            f" {swatch}{escape(value)}</label>"
        )
    return "\n".join(rows)


def _explain_span(view: str, default: str) -> str:
    """A per-view explanation span (static trusted text), hidden unless it's the active view."""
    hidden = "" if view == default else ' style="display:none"'
    return f'<span class="ex" id="ex-{view}"{hidden}>{_VIEW_EXPLAIN[view]}</span>'


def _document(
    *,
    title: str,
    default: str,
    canvas: float,
    legends: dict[str, list[tuple[str, str]]],
    edge_types: list[str],
    svg_nodes: str,
    svg_edges: str,
    svg_labels: str,
    svg_fams: str,
    svg_subs: str,
    sub_json: str,
    offmap_json: str,
    fam_markers: str,
    fam_chips: str,
    fam_json: str,
    leg_json: str,
    enc_json: str,
    node_count: int,
    total_nodes: int,
    edge_count: int,
    lr_hidden: int,
    src_root_json: str,
    cycle_count: int,
    globe_json: str,
    globe_radius: float,
    pos_json: str,
    fill_json: str,
    label_json: str,
    adj_json: str,
) -> str:
    safe_title = escape(title)
    view_radios = "\n".join(
        f'<label class="item"><input type="radio" name="view" value="{v}"'
        f'{" checked" if v == default else ""}> {_VIEW_LABEL[v]}</label>'
        for v in VIEWS
    )
    color_keys = "\n".join(_color_key(v, legends[v], default) for v in VIEWS)
    edge_filter = _edge_filter(edge_types)
    explain_spans = "\n".join(_explain_span(v, default) for v in VIEWS)
    at_rest = (
        f" \u00b7 {edge_count - lr_hidden:,} visible at rest \u2014 hover reveals more"
        if lr_hidden else ""
    )
    if node_count < total_nodes:
        meta_nodes = (
            f"showing the {node_count:,} most-connected of {total_nodes:,} symbols"
            f" · {edge_count:,} edges{at_rest}"
        )
    else:
        meta_nodes = f"{node_count:,} symbols · {edge_count:,} edges{at_rest}"
    lr_keep = _LR_KEEP
    dash_rules = _dash_css()
    cycles_label = f"import cycles ({cycle_count})" if cycle_count else "import cycles (none)"
    cycles_disabled = "" if cycle_count else " disabled"
    svg_class = ' class="families"' if default == "families" else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title} — Cartogate</title>
<style>
  :root {{
    --bg: #090c16; --surface: #0d1120; --raise: #151b2e;
    --ink: #e8edf7; --ink-2: #9aa7c4; --ink-3: #5c688a;
    --accent: #86b6ef; --danger: #ff3b5c;
    --glass: rgba(13, 17, 32, 0.78); --edge-hair: rgba(134, 182, 239, 0.16);
    --font-head: Bahnschrift, "Avenir Next", "Trebuchet MS", sans-serif;
    --font-mono: "Cascadia Code", "JetBrains Mono", Consolas, ui-monospace, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 13px/1.45 var(--font-head); color: var(--ink);
          background: var(--bg); overflow: hidden; }}
  #graph {{ width: 100vw; height: 100vh; cursor: grab; display: block;
    background: radial-gradient(1100px 700px at 32% 24%, var(--raise), var(--surface) 55%,
                var(--bg)); }}
  #graph.grabbing {{ cursor: grabbing; }}

  #panel {{ position: fixed; z-index: 10; top: 14px; left: 14px; width: 224px;
    max-height: 90vh;
    overflow: auto; padding: 14px 16px 12px; border-radius: 14px;
    background: var(--glass); border: 1px solid var(--edge-hair);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5);
    animation: rise .55s cubic-bezier(.2, .9, .3, 1) both; }}
  #panel strong {{ font-size: 15px; letter-spacing: .04em; }}
  #panel .brand {{ color: var(--accent); font-family: var(--font-mono); font-size: 10px;
    text-transform: uppercase; letter-spacing: .22em; margin-bottom: 2px; }}
  #panel h2 {{ font-size: 10px; text-transform: uppercase; letter-spacing: .14em;
    color: var(--ink-3); margin: 14px 0 5px; }}
  #panel .meta {{ color: var(--ink-2); font-family: var(--font-mono); font-size: 11px;
    margin: 3px 0; }}
  #panel .hint {{ color: var(--ink-3); font-size: 10.5px; margin: -4px 0 6px; }}
  .item {{ display: block; cursor: pointer; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; color: var(--ink-2); padding: 1px 0; }}
  .item:hover {{ color: var(--ink); }}
  .item.muted {{ color: var(--ink-3); cursor: default; }}
  .swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 3px;
    margin: 0 6px 0 4px; vertical-align: -1px; }}
  .swatch-line {{ display: inline-block; width: 18px; margin: 0 6px 3px 4px;
    vertical-align: middle; }}
  input[type="checkbox"], input[type="radio"] {{ accent-color: var(--accent); }}
  /* hover name-tag: says what a dot is before you commit to a click (interaction audit:
     hover traced edges but showed no text) */
  #nametag {{ position: fixed; z-index: 40; pointer-events: none; display: none;
    max-width: 340px; padding: 5px 9px; border-radius: 7px;
    background: rgba(16, 20, 28, 0.94); border: 1px solid var(--line);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45); font-family: var(--font-mono);
    font-size: 11.5px; color: var(--ink); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }}
  #nametag .tag-deg {{ color: var(--ink-3); }}
  #toast {{ position: fixed; top: 14px; left: 50%; transform: translateX(-50%);
    z-index: 40; padding: 6px 14px; border-radius: 999px; pointer-events: none;
    background: rgba(16, 20, 28, 0.94); border: 1px solid var(--line);
    font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-2);
    opacity: 0; transition: opacity .35s ease; }}
  #toast.show {{ opacity: 1; }}

  #search {{ width: 100%; margin-top: 8px; padding: 6px 9px; border-radius: 8px;
    border: 1px solid var(--edge-hair); background: rgba(9, 12, 22, 0.7);
    color: var(--ink); font-family: var(--font-mono); font-size: 11.5px; outline: none; }}
  #search:focus {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(134,182,239,.18); }}
  #hits {{ margin: 4px 0 0; padding: 0; list-style: none; max-height: 180px; overflow: auto; }}
  /* results ellipsize at the START: qualified names share their prefix, so the TAIL
     is the distinguishing part (interaction audit: seven hits all read alike) */
  #hits li {{ direction: rtl; text-align: left; padding: 3px 6px; border-radius: 6px;
    cursor: pointer; color: var(--ink-2);
    font-family: var(--font-mono); font-size: 10.5px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }}
  #hits li:hover, #hits li.sel {{ background: rgba(134, 182, 239, 0.14); color: var(--ink); }}
  #hits li.muted {{ cursor: default; color: var(--ink-3); font-style: italic; }}
  #hits li.muted:hover {{ background: none; color: var(--ink-3); }}
  #hint {{ position: fixed; z-index: 9; left: 50%; top: 46%; transform: translateX(-50%);
    display: none; padding: 10px 18px; border-radius: 10px; background: var(--glass);
    border: 1px solid var(--edge-hair); color: var(--ink-2); font-size: 13px;
    backdrop-filter: blur(6px); }}
  body.all-hidden #hint {{ display: block; }}
  #crumb {{ position: fixed; z-index: 10; top: 14px; left: 50%; transform: translateX(-50%);
    display: none; align-items: center; gap: 8px; padding: 6px 14px 6px 8px;
    border-radius: 999px; background: var(--glass); border: 1px solid var(--edge-hair);
    color: var(--ink); font-size: 12.5px; backdrop-filter: blur(6px); }}
  body.drilled #crumb {{ display: flex; }}
  #crumb .back {{ border: 0; background: var(--raise); color: var(--accent);
    border-radius: 50%; width: 22px; height: 22px; cursor: pointer; font-size: 14px;
    line-height: 1; }}
  #crumb .path {{ font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-2); }}

  /* drill (pair/solo) mode: only .dk members render; famlayer yields the stage */
  /* !important is LOAD-BEARING: hiding every drill entry point (famhit/fam circles)
     for the whole drill is what makes a second stageDrill() unschedulable while a
     first one's 620ms timeout is pending (stale-timeout bug class, fixed twice). */
  svg.pair #famlayer {{ display: none !important; }}
  svg.pair circle.node, svg.pair path.edge, svg.pair text.label {{ display: none; }}
  svg.pair circle.node.dk, svg.pair path.edge.dk, svg.pair text.label.dk {{
    display: inline; }}
  svg.pair .fam-off.dk {{ display: inline; }}  /* drill outranks family hiding */
  /* a drilled label must survive a stale inline display:none left by the declutter
     engine in an earlier view — else drilled files render as anonymous dots again
     (review HIGH: the exact bug round 7/8 fixed via the now-removed lod-far rule) */
  svg.pair text.label.dk {{ display: inline !important; }}
  svg.pair path.edge.dk {{ display: inline !important; /* the drill IS these lines —
    they must survive their own type checkbox being unchecked mid-drill (review MED) */
    opacity: 0.8; }}
  /* hover-tracing inside a drill must NOT reveal non-drilled edges (they sit at their
     OLD view positions — the "random edges" the user reported) */
  svg.pair path.edge:not(.dk) {{ display: none !important; }}
  /* direction streaks: white pulses overlaid on SOLID lines (never break the line) —
     JS-created for the bounded traced/drilled sets only */
  path.streak {{ stroke-linecap: butt; fill: none; pointer-events: none; }}
  #detail button.nb.off {{ opacity: 0.5; }}

  #controls {{ position: fixed; z-index: 10; right: 16px; bottom: 56px; display: flex;
    flex-direction: column; gap: 6px; animation: rise .55s .12s both; }}
  #controls button {{ width: 34px; height: 34px; border-radius: 10px; cursor: pointer;
    border: 1px solid var(--edge-hair); background: var(--glass); color: var(--ink);
    font: 15px var(--font-mono); backdrop-filter: blur(10px); }}
  #controls button:hover {{ border-color: var(--accent); color: var(--accent); }}

  #detail {{ position: fixed; z-index: 11; top: 14px; right: 16px; width: 264px;
    max-height: 70vh;
    overflow: auto; padding: 12px 14px; border-radius: 14px; background: var(--glass);
    border: 1px solid var(--edge-hair); backdrop-filter: blur(14px); display: none;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5); }}
  #detail.open {{ display: block; animation: rise .3s both; }}
  #detail .qn {{ font-family: var(--font-mono); font-size: 11.5px; color: var(--ink);
    word-break: break-all; }}
  #detail .kind {{ color: var(--accent); font-family: var(--font-mono); font-size: 10px;
    text-transform: uppercase; letter-spacing: .14em; margin: 6px 0 2px; }}
  #detail .path {{ color: var(--ink-3); font-family: var(--font-mono); font-size: 10px;
    word-break: break-all; }}
  /* neighbours ellipsize at the START — qualified names share their prefix, so the
     TAIL distinguishes them (round 9: same cure as the search results) */
  #detail button.nb {{ display: block; width: 100%; margin: 2px 0;
    padding: 3px 6px; border: 0; border-radius: 6px; background: transparent;
    color: var(--ink-2); font-family: var(--font-mono); font-size: 10.5px; cursor: pointer;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    direction: rtl; text-align: left; }}
  #detail button.nb bdi {{ direction: ltr; unicode-bidi: isolate; }}
  #detail button.nb .tick {{ display: inline-block; width: 7px; height: 7px;
    border-radius: 2px; margin-left: 6px; vertical-align: 0; }}
  #detail a.srclink {{ color: var(--ink-2); text-decoration: none;
    border-bottom: 1px dotted var(--ink-3); }}
  #detail a.srclink:hover {{ color: var(--accent); }}
  #detail .copybtn {{ margin-left: 8px; border: 1px solid var(--line); background:
    transparent; color: var(--ink-3); border-radius: 6px; font-size: 9.5px;
    padding: 1px 6px; cursor: pointer; }}
  #detail .nbhead {{ font-size: 10px; text-transform: uppercase; letter-spacing: .12em;
    color: var(--ink-3); margin: 8px 0 3px; }}
  #detail button.nb.more {{ color: var(--accent); direction: ltr; }}
  #detail button.nb:hover {{ background: rgba(134, 182, 239, 0.14); color: var(--ink); }}
  #detail .close {{ float: right; border: 0; background: none; color: var(--ink-3);
    cursor: pointer; font-size: 14px; }}

  #explain {{ position: fixed; z-index: 9; bottom: 0; left: 0; right: 0; padding: 7px 16px;
    background: linear-gradient(to top, rgba(9, 12, 22, 0.92), rgba(9, 12, 22, 0.6));
    border-top: 1px solid var(--edge-hair); color: var(--ink-2); font-size: 11.5px;
    animation: rise .55s .2s both; }}
  #explain b {{ color: var(--ink); }}

  circle.node {{ cursor: pointer; transition: opacity .25s ease; }}
  svg.morphing circle.node {{ transition: cx .6s ease, cy .6s ease, fill .4s ease; }}
  svg.morphing path.edge {{ transition: d .6s ease; }}  /* Chromium animates d; others jump */
  svg.morphing text.label {{ transition: x .6s ease, y .6s ease; }}
  text.label {{ pointer-events: none; paint-order: stroke; stroke: var(--bg);
    fill: var(--ink-3); font-family: var(--font-mono); letter-spacing: .03em; }}
  circle.node.mod {{ stroke: rgba(232, 237, 247, 0.55); }}

  svg.tracing circle.node {{ opacity: 0.15; }}
  svg.tracing circle.node.keep {{ opacity: 1; }}
  svg.tracing path.edge {{ opacity: 0.18 !important; }}
  /* inside a drill the members ARE the context — dim to readable, not to black
     (interaction audit: hover-in-drill showed a void) */
  svg.pair.tracing path.edge.dk {{ opacity: 0.15 !important; }}
  svg.pair.tracing path.edge.dk.keep {{ opacity: 0.9 !important; }}
  svg.tracing path.streak:not(.tr) {{ opacity: 0.04; }}  /* comets dim with their lines */
  svg.tracing path.edge.keep {{ opacity: 0.95 !important; }}
  svg.tracing text.label {{ opacity: 0.15; }}
  svg.tracing text.label.keep {{ display: inline !important; opacity: 1; }}

  svg.h-cycles path.edge {{ opacity: 0.05; }}
  svg.h-cycles path.edge.cyc {{ stroke: var(--danger); stroke-width: 1.8; opacity: 0.95; }}
  svg.h-cycles .lr.cyc {{ display: inline; }}  /* a hidden cycle edge defeats the overlay */
  svg.h-cycles circle.node {{ opacity: 0.18; }}
  svg.h-cycles circle.node.cyc-n {{ opacity: 1; stroke: var(--danger); stroke-width: 1; }}

  /* globe shows hub labels too (declutter keeps it to the front-facing heavyweights) */
  svg.globe.spinning #labels, svg.resting #labels {{ display: none; }}
  /* spin protection is now a FADE, not a pop (globe review G2): still cheap — fully
     transparent elements skip painting — but the transition removes the visual slam */
  path.edge, path.streak {{ transition: opacity 0.18s ease; }}
  svg.globe.spinning path.edge {{ opacity: 0 !important; }}
  svg.globe.spinning path.streak {{ opacity: 0 !important; }}
  svg.resting path.streak {{ opacity: 0 !important; }}  /* rest-spin: motion IS the show */
  svg.globe #horizon {{ display: inline !important; }}

{dash_rules}
  #famlayer {{ display: none; }}
  svg.families #famlayer {{ display: inline; }}
  svg.families #edges, svg.families #labels {{ display: none; }}
  /* file labels are shown/hidden per-label by the JS declutter engine (weight-ranked,
     screen-space) — reliable at every zoom, replacing the old all-or-nothing gate */
  #enclosures rect {{ pointer-events: none; }}
  #enclosures text {{ font-family: var(--font-head); paint-order: stroke;
    stroke: var(--bg); stroke-width: 3; pointer-events: none; }}
  /* radial group labels (Orbits rings = layers, Galaxy arms = communities) */
  #grouplabels text {{ font-family: var(--font-head); paint-order: stroke;
    stroke: var(--bg); stroke-width: 3.5; pointer-events: none; font-weight: 600; }}
  svg.families #nodes {{ pointer-events: none; opacity: 0.6; }}
  svg.families circle.node {{ r: 2.3px; }}  /* dust dots (CSS geometry, Chromium+FF) */
  #famlayer circle.fam {{ fill-opacity: 0.42; }}  /* the orb is glass; dust shows through */
  svg.families #famlayer {{ animation: unveil .25s ease; }}
  circle.fam {{ cursor: pointer; stroke-width: 1.4; filter: url(#softglow); }}
  .sublayer circle.sub {{ filter: url(#softglow); }}
  text.famlabel .cnt, text.sublabel .cnt {{ fill: var(--ink-3); font-size: 78%; }}
  text.arclabel.quiet {{ display: none; }}
  text.arclabel.quiet.hl {{ display: inline; }}
  /* ring hover focus: the hovered arc (or a circle's incident arcs) stays lit with its
     label; everything else recedes (visual review: hover-first labeling) */
  .focus path.famarc:not(.hl), .focus path.subarc:not(.hl) {{ opacity: 0.18; }}
  /* the comet is WHITE — at a given alpha it reads far brighter than the muted line
     beneath it, so a dimmed line's pulse must be dimmed HARDER than the line (user:
     comets on dimmed edges looked full-strength). ~0.05 reads as genuinely off. */
  .focus path.famflow:not(.hl) {{ opacity: 0.05; }}
  .focus text.arclabel:not(.hl) {{ opacity: 0.05; }}
  .focus circle.fam, .focus circle.sub {{ opacity: 0.45; }}
  .focus circle.hl {{ opacity: 1; }}
  path.famarc.hl, path.subarc.hl {{ opacity: 1; }}
  text.arclabel.hl {{ opacity: 1; }}
  text.famlabel {{ fill: var(--ink); font-family: var(--font-head);
    paint-order: stroke; stroke: var(--bg); stroke-width: 3; }}
  text.arclabel {{ font-family: var(--font-mono); paint-order: stroke;
    stroke: var(--bg); stroke-width: 4; pointer-events: none; }}
  /* the famlayer's count labels are LIVE: they forward hover/click to their arc, so
     the most legible target works instead of shielding it (R11 AC-17) */
  #famlayer text.arclabel {{ pointer-events: auto; cursor: pointer; }}
  path.famflow {{ stroke-linecap: butt; opacity: 0.9; pointer-events: none; }}
  path.famhit {{ pointer-events: stroke; cursor: pointer; }}
  svg.subring #nodes, svg.subring #edges, svg.subring #labels {{ display: none; }}
  svg.subring #famlayer {{ display: none !important; }}
  svg.pair #sublayers {{ display: none !important; }}
  circle.sub {{ cursor: pointer; stroke-width: 1.4; }}
  text.sublabel {{ fill: var(--ink); font-family: var(--font-head);
    paint-order: stroke; stroke: var(--bg); stroke-width: 3; }}
  path.subhit {{ pointer-events: stroke; cursor: pointer; }}

  /* Locality (R4): long-range = cross-community; quiet at rest, answers when asked */
  .lr {{ display: none; }}
  .lr.keep:not(.fam-off), .lr.dk {{ display: inline; }}
  svg.all-edges .lr {{ display: inline; }}
  svg.all-edges .lr.fam-off {{ display: none; }}

  /* Detail views rest QUIET (user + blind critique: the resting edge hairball buried
     the structure). Every edge hides until you hover a dot — which reveals its own
     connections — or flip "show all edges". Families keeps its aggregate arcs; the
     globe keeps its flight arcs; drills show their own members. */
  svg.edges-quiet path.edge {{ display: none !important; }}
  svg.edges-quiet path.edge.keep {{ display: inline !important; }}
  svg.edges-quiet.h-cycles path.edge.cyc {{ display: inline !important; }}

  /* Visibility composition precedence (three mechanisms, one element):
     inline type-checkbox display:none  >  drill .dk show (PR 3; restores inline)
     >  .fam-off hide  >  default. .fam-off is CLASS-based so it never fights the
     inline style the edge-type checkboxes own. */
  .fam-off {{ display: none; }}
  #famlayer .fam-dim {{ opacity: 0.35; }}
  /* a hidden family's LINE stays faintly visible (the map stays honest) but its white
     comet goes fully dark — otherwise the pulse alone advertised the hidden family */
  #famlayer path.famflow.fam-dim {{ opacity: 0.05; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  button.chip {{ display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
    border: 1px solid var(--edge-hair); border-radius: 999px; padding: 2px 9px 2px 6px;
    background: var(--raise); color: var(--ink); font: 11.5px var(--font-head); }}
  button.chip .dot {{ width: 7px; height: 7px; border-radius: 50%; }}
  button.chip .glyph {{ font-size: 10px; color: var(--accent); }}
  button.chip .cnt {{ color: var(--ink-3); font-family: var(--font-mono); font-size: 10px; }}
  button.chip[data-state="0"] {{ opacity: 0.45; }}
  button.chip[data-state="1"] {{ opacity: 0.8; }}

  circle.node.pulse {{ animation: pulse 1.5s ease-out 2; transform-box: fill-box;
    transform-origin: center; }}
  @keyframes pulse {{ 0% {{ transform: scale(1); }} 35% {{ transform: scale(2.1); }}
    100% {{ transform: scale(1); }} }}
  @keyframes rise {{ from {{ opacity: 0; transform: translateY(10px); }}
    to {{ opacity: 1; transform: none; }} }}
  #graph {{ animation: unveil .8s ease backwards; }}
  @keyframes unveil {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @media (prefers-reduced-motion: reduce) {{
    #panel, #controls, #explain, #graph {{ animation: none; }}
    circle.node.pulse {{ animation: none; }}
    path.famflow {{ display: none; }}
    svg.families #famlayer {{ animation: none; }}
    path.streak {{ display: none; }}
  }}
</style>
</head>
<body>
<div id="panel">
  <div class="brand">Cartogate</div>
  <strong>{safe_title}</strong>
  <div class="meta">{meta_nodes}</div>
  <div class="meta" id="usage">drag to pan · scroll to zoom · click a dot to pin details
    · / to search</div>
  <input id="search" type="search" placeholder="find a symbol or file…" autocomplete="off">
  <ul id="hits"></ul>
  <h2>View</h2>
  {view_radios}
  <h2>Families</h2>
  <div class="hint">click a family to cycle: ○ hidden · ◐ fringe
    (only members touching visible code) · ◉ shown</div>
  <div class="chips">
  {fam_chips}
  </div>
  <h2>Edge types</h2>
  {edge_filter}
  <h2>Colour key</h2>
  {color_keys}
  <h2>Overlays</h2>
  <label class="item"><input type="checkbox" id="hl-cycles"
    data-overlay="h-cycles"{cycles_disabled}>
    <span class="swatch" style="background:var(--danger)"></span>{cycles_label}</label>
  <label class="item" title="the flat views rest quiet — hover a dot to reveal its
    links; check this to draw every edge at once">
    <!-- data-lr-keep is write-only documentation of the kept-set size -->
    <input type="checkbox" data-overlay="all-edges" data-lr-keep="{lr_keep}"> show all edges</label>
  <label class="item"><input type="checkbox" checked data-css="label"> file labels</label>
</div>
<svg id="graph"{svg_class} viewBox="0 0 {canvas:.0f} {canvas:.0f}"
     preserveAspectRatio="xMidYMid meet"
     role="img" aria-label="Code graph: {node_count} symbols, {edge_count} relationships">
  <defs>
    <filter id="softglow" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="1.1" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
{fam_markers}
  </defs>
  <circle id="horizon" cx="{canvas / 2:.1f}" cy="{canvas / 2:.1f}" r="{globe_radius:.1f}"
    fill="none" stroke="rgba(134,182,239,0.18)" stroke-width="1"
    vector-effect="non-scaling-stroke" style="display:none"/>
  <g id="enclosures"></g>
  <g id="grouplabels"></g>
  <g id="edges">
{svg_edges}
  </g>
  <g id="nodes" filter="url(#softglow)">
{svg_nodes}
  </g>
  <g id="labels">
{svg_labels}
  </g>
  <g id="famlayer">
{svg_fams}
  </g>
  <g id="sublayers">
{svg_subs}
  </g>
</svg>
<div id="hint">all families hidden — click a family chip to bring code back</div>
<div id="crumb"><button class="back" title="back to structure (Esc)">‹</button>
  <span class="path"></span></div>
<div id="controls">
  <button id="btn-lock" title="lock the globe (stop the idle spin)"
    aria-pressed="false" style="display:none">⏸</button>
  <button id="btn-fit" title="fit to view (0)">⌂</button>
  <button id="btn-in" title="zoom in (+)">+</button>
  <button id="btn-out" title="zoom out (−)">−</button>
</div>
<div id="detail" role="dialog" aria-label="symbol details">
  <button class="close" title="close (Esc)">✕</button>
  <div class="kind"></div>
  <div class="qn"></div>
  <div class="path"></div>
  <h2 style="font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--ink-3);
      margin:10px 0 4px">connections <span class="deg"></span></h2>
  <div class="nbs"></div>
</div>
<div id="nametag" aria-hidden="true"></div>
<div id="toast" aria-live="polite"></div>
<div id="explain">
  <b>How to read this:</b> <span class="intro" id="intro-structure">your codebase generalised
  into role groups — each sphere is a family of files, arcs are typed relationships between
  them. Click a family or an arc to open it.</span><span class="intro" id="intro-flat"
  style="display:none">every dot is one symbol in your code. Dots are grouped into clusters
  — one cluster per file: the file itself at the centre, its top-level functions and classes on
  the inner ring, methods and other nested symbols on the outer ring. Bigger dot = more
  connections. Hover any dot to trace what it calls and what calls it.</span>
  &nbsp;{explain_spans}
</div>
<script>
(function () {{
  const POS = {pos_json}, FILLS = {fill_json}, LABELS = {label_json}, ADJ = {adj_json};
  const GLOBE = {globe_json};
  const FAM = {fam_json};
  const LEG = {leg_json};
  const ENC = {enc_json};
  const SUB = {sub_json};
  const OFFMAP = {offmap_json};
  const SRC_ROOT = {src_root_json};
  const svg = document.getElementById("graph");
  const circles = Array.from(document.querySelectorAll("circle.node"));
  const lines = Array.from(document.querySelectorAll("path.edge"));
  const labels = Array.from(document.querySelectorAll("text.label"));
  const byId = {{}};
  circles.forEach(function (c) {{ byId[c.dataset.id] = c; }});
  const NAME = {{}};
  circles.forEach(function (c) {{
    const t = c.querySelector("title");
    NAME[c.dataset.id] = t ? t.textContent : c.dataset.id; }});
  const linesBy = {{}};
  lines.forEach(function (l) {{
    (linesBy[l.dataset.src] = linesBy[l.dataset.src] || []).push(l);
    (linesBy[l.dataset.dst] = linesBy[l.dataset.dst] || []).push(l); }});

  function currentView() {{
    return document.querySelector('input[name="view"]:checked').value; }}

  // the five flat node-level views rest quiet; families (aggregate arcs) and globe
  // (flight arcs) opt out. edgesAlways = the "show all edges" toggle.
  const QUIET_VIEWS = {{ relatedness: 1, dependency: 1, package: 1, orbits: 1, galaxy: 1 }};
  let edgesAlways = false;

  function applyView(view) {{
    const p = POS[view], f = FILLS[view];
    circles.forEach(function (c) {{ const a = p[c.dataset.id];
      c.setAttribute("cx", a[0]); c.setAttribute("cy", a[1]);
      c.setAttribute("fill", f[c.dataset.id]); }});
    lines.forEach(function (l) {{ const a = p[l.dataset.src], b = p[l.dataset.dst];
      // degenerate Q (control = midpoint) renders straight but keeps the SAME command
      // structure as the globe's flight arcs, so d-transitions interpolate (review MED)
      l.setAttribute("d", "M " + a[0] + " " + a[1]
        + " Q " + (a[0] + b[0]) / 2 + " " + (a[1] + b[1]) / 2
        + " " + b[0] + " " + b[1]); }});
    labels.forEach(function (t) {{ const a = LABELS[view][+t.dataset.ui];
      t.setAttribute("x", a[0]); t.setAttribute("y", a[1]);
      t.dataset.back = "";  // clear any leftover globe far-side flag
    }});
    document.querySelectorAll(".legend").forEach(function (el) {{
      el.style.display = el.id === "legend-" + view ? "" : "none"; }});
    document.querySelectorAll("#explain .ex").forEach(function (el) {{
      el.style.display = el.id === "ex-" + view ? "" : "none"; }});
    document.getElementById("intro-structure").style.display =
      view === "families" ? "" : "none";
    document.getElementById("intro-flat").style.display =
      view === "families" ? "none" : "";
    // flat detail views rest quiet (edges hidden until hover) unless "show all edges" is on
    svg.classList.toggle("edges-quiet", !edgesAlways && !!QUIET_VIEWS[view]);
  }}
  const usage = document.getElementById("usage");
  const USAGE_DEFAULT = usage.textContent;
  const USAGE_GLOBE = "drag to spin \u00b7 shift+drag to pan \u00b7 scroll to zoom "
    + "\u00b7 \u23f8 (top-right) locks the idle spin";
  const USAGE_STRUCTURE = "click a family or arc to open it \u00b7 drag to pan "
    + "\u00b7 scroll to zoom \u00b7 / to search";
  const btnLock = document.getElementById("btn-lock");
  let idleLocked = false;
  btnLock.addEventListener("click", function () {{
    idleLocked = !idleLocked;
    btnLock.setAttribute("aria-pressed", idleLocked);
    btnLock.textContent = idleLocked ? "\u25b6" : "\u23f8";
    stopIdle();
  }});
  function syncUsage() {{
    // instructions must match the view: the landing view's dots are inert dust —
    // its real verbs are family/arc drills (R11 AC-13; usability panel finding 2)
    usage.textContent = currentView() === "globe" ? USAGE_GLOBE
      : currentView() === "families" ? USAGE_STRUCTURE : USAGE_DEFAULT;
    btnLock.style.display = currentView() === "globe" ? "" : "none";
  }}
  document.querySelectorAll('input[name="view"]').forEach(function (r) {{
    // mouse-clicked radios release focus so the next arrow press pans the canvas
    // instead of switching views (R11 AC-11). Keyboard radio navigation ALSO fires a
    // synthetic click (activation behaviour, detail===0) — blur only on REAL pointer
    // clicks (detail>0), or arrow-cycling loses focus after one press (review HIGH).
    r.addEventListener("click", function (e) {{ if (e.detail !== 0) r.blur(); }});
    r.addEventListener("change", function () {{
      if (!r.checked) return;
      syncUsage();
      stopIdle();  // every view entry re-arms the idle-rotation timer
      clearDrill();  // a view switch always leaves the drill (no exit morph)
      hideSubring();
      document.body.classList.remove("drilled");  // review HIGH: crumb stayed stuck
      drillStack.length = 0;
      clearPool(ambientStreaks);  // stale comets must not ride the morph
      svg.classList.add("morphing");
      svg.classList.toggle("globe", r.value === "globe");
      if (r.value !== "globe") resetDepth();
      // Leaving families: reveal the detail layers NOW so nodes visibly explode out of
      // their family blobs. Entering: keep them until the morph lands (they implode into
      // blobs first), then swap to the family layer at the 620ms boundary.
      if (r.value !== "families") svg.classList.remove("families");
      applyView(r.value);
      // Re-derive the view at fire time: a closure over r.value goes STALE when the user
      // switches again inside the 620ms window (review HIGH: the stale timeout re-showed
      // the family layer over another view, and a stale globe project() stomped live
      // positions).
      writeHash();
      glideTo(computeFit(), 620);  // camera moves WITH the morph, not after it
      setTimeout(function () {{
        svg.classList.toggle("families", currentView() === "families");
        svg.classList.remove("morphing");
        if (currentView() === "globe") {{ project(); fit(); }}
        refreshAmbient();
        buildEnclosures();
        buildGroupLabels();
      }}, 620);
    }});
  }});

  // ---- direction streaks: white pulses over SOLID lines (bounded sets only) ----
  // THREE independent pools (hover-trace, drill, ambient) — a shared pool meant
  // untrace() killed the drill's streaks (user report: animations stopped after
  // hovering then leaving a node inside a drill).
  const SVGNS = "http://www.w3.org/2000/svg";
  const traceStreaks = [], drillStreaks = [], ambientStreaks = [];
  // LED-strip pulse (user spec): the streak is a WHITE COPY of its edge — same d,
  // same width, same TYPE DASH (so light never inks the gaps of dashed lines) —
  // revealed through a fixed-length window that travels src -> dst via a WAAPI
  // clip-path animation. Window length is constant (not a fraction of the line), and
  // speed is uniform, so every pulse reads identical regardless of edge length.
  const STREAK_WIN = 64, STREAK_SPEED = 130;  // user units / units-per-second
  function typeToken(l) {{
    const m = l.className.baseVal.match(/type-([a-z0-9_-]+)/);
    return m ? m[1] : "";
  }}
  function addStreak(l, pool, delay) {{
    // trace-pool streaks stay lit while everything else dims with its line
    // inside a drill, only the drilled edges exist — no comets on hidden lines
    if (svg.classList.contains("pair") && !l.classList.contains("dk")) return;
    const lrHidden = l.classList.contains("lr")
      && !svg.classList.contains("all-edges")
      && !l.classList.contains("keep") && !l.classList.contains("dk");
    const hidden = lrHidden || l.style.display === "none"
      || (l.classList.contains("fam-off") && !l.classList.contains("dk"));
    if (hidden) return;
    const d = l.getAttribute("d");
    const nums = d.match(/-?[\d.]+/g).map(Number);
    const ax = nums[0], ay = nums[1], bx = nums[nums.length - 2],
      by = nums[nums.length - 1];
    const len = Math.hypot(bx - ax, by - ay);
    if (len < 4) return;
    const t = document.createElementNS(SVGNS, "path");
    t.setAttribute("class", "streak type-" + typeToken(l));
    t.setAttribute("d", d);
    t.setAttribute("fill", "none");
    t.setAttribute("stroke-width",
      (+(l.getAttribute("stroke-width") || 1) + 0.4).toFixed(2));
    t.setAttribute("vector-effect", "non-scaling-stroke");  // match the base's px width
    if (pool === traceStreaks) t.classList.add("tr");
    bindGlow(t, ax, ay, bx, by, len, delay);
    svg.appendChild(t);  // last child paints on top
    pool.push(t);
  }}
  // The comet is a TRAVELLING GRADIENT stroke (user spec): sharp white at the head
  // fading to transparent along the tail — over the base line, that reads as white
  // melting into the line's own colour. One userSpaceOnUse gradient per glow, slid
  // along the chord with SMIL animateTransform (loops, negative begin = stagger).
  let glowSeq = 0;
  const defsEl = svg.querySelector("defs");
  function bindGlow(el, ax, ay, bx, by, len, delay) {{
    // tail length rides the edge: floor STREAK_WIN, ~35% of the span, capped — long
    // edges get glances-visible comets, short edges stay proportionate (R11 AC-33)
    const W = Math.min(320, Math.max(STREAK_WIN, len * 0.35));
    const ux = (bx - ax) / len, uy = (by - ay) / len;
    const id = "glow-" + (glowSeq++);
    const g = document.createElementNS(SVGNS, "linearGradient");
    g.setAttribute("id", id);
    g.setAttribute("gradientUnits", "userSpaceOnUse");
    // the gradient spans tail -> just past the head; pad stops kill spreadMethod
    // bleed so nothing lights ahead of the head or behind the tail
    g.setAttribute("x1", (ax - ux * W).toFixed(1));
    g.setAttribute("y1", (ay - uy * W).toFixed(1));
    g.setAttribute("x2", (ax + ux * 2).toFixed(1));
    g.setAttribute("y2", (ay + uy * 2).toFixed(1));
    [["0", "0"], ["0.55", "0.28"], ["0.94", "0.95"], ["0.96", "0"], ["1", "0"]]
      .forEach(function (st) {{
        const stop = document.createElementNS(SVGNS, "stop");
        stop.setAttribute("offset", st[0]);
        stop.setAttribute("stop-color", "#e8edf7");
        stop.setAttribute("stop-opacity", st[1]);
        g.appendChild(stop);
      }});
    const a = document.createElementNS(SVGNS, "animateTransform");
    a.setAttribute("attributeName", "gradientTransform");
    a.setAttribute("type", "translate");
    a.setAttribute("from", "0 0");
    a.setAttribute("to", (ux * (len + 2 * W)).toFixed(1) + " "
      + (uy * (len + 2 * W)).toFixed(1));
    a.setAttribute("dur", (((len + 2 * W) / STREAK_SPEED)).toFixed(2) + "s");
    a.setAttribute("repeatCount", "indefinite");
    if (delay) a.setAttribute("begin", delay);
    g.appendChild(a);
    defsEl.appendChild(g);
    el.setAttribute("stroke", "url(#" + id + ")");
    el.dataset.glow = id;
  }}
  // family/sub-ring flows are baked white paths — give each its travelling window
  document.querySelectorAll("path.famflow").forEach(function (f) {{
    const nums = f.getAttribute("d").match(/-?[\d.]+/g).map(Number);
    const ax = nums[0], ay = nums[1];
    const bx = nums[nums.length - 2], by = nums[nums.length - 1];
    const len = Math.hypot(bx - ax, by - ay);
    if (len < 4) return;
    bindGlow(f, ax, ay, bx, by, len, "-" + (f.dataset.stagger || 0) + "s");
  }});
  function clearPool(pool) {{
    pool.forEach(function (t) {{
      if (t.dataset.glow) {{
        const g = document.getElementById(t.dataset.glow);
        if (g) g.remove();
      }}
      t.remove();
    }});
    pool.length = 0;
  }}
  // Ambient directionality for every detail view (user: "all other views have no
  // animations"): streak the widest visible edges, capped for paint cost. Regenerated
  // whenever visibility or positions settle; families view has its own arc flows.
  const AMBIENT_CAP = 450;
  function refreshAmbient() {{
    clearPool(ambientStreaks);
    if (currentView() === "families" && !drill) return;
    if (drill || subFam !== null) return;  // drill/subring pools cover their own flow
    if (svg.classList.contains("edges-quiet")) return;  // no resting edges → no resting comets
    const vis = lines.filter(function (l) {{
      return l.style.display !== "none" && !l.classList.contains("fam-off");
    }});
    vis.sort(function (x, y) {{
      const w = (+y.getAttribute("stroke-width")) - (+x.getAttribute("stroke-width"));
      return w !== 0 ? w : (x.dataset.src + x.dataset.dst)
        .localeCompare(y.dataset.src + y.dataset.dst);
    }});
    vis.slice(0, AMBIENT_CAP).forEach(function (l, i) {{
      addStreak(l, ambientStreaks, "-" + ((i * 0.37) % 1.8).toFixed(1) + "s");
    }});
  }}

  // ---- trace (class-based: O(degree), not O(N)) ----
  let kept = [];
  function trace(id) {{
    stopIdle();  // hovering must not have the globe drift out from under the cursor
    untrace();
    svg.classList.add("tracing");
    const ids = [id].concat(ADJ[id] || []);
    const keptUnits = {{}};
    ids.forEach(function (i) {{ const c = byId[i]; if (c) {{ c.classList.add("keep");
      kept.push(c); keptUnits[GLOBE.u[i]] = 1; }} }});
    // the traced picture must be NAMED: unit labels of kept nodes surface above the
    // declutter while the trace is live (R11 AC-20; dev panel: endpoints anonymous)
    labels.forEach(function (t) {{
      if (keptUnits[+t.dataset.ui]) {{ t.classList.add("keep"); kept.push(t); }}
    }});
    (linesBy[id] || []).forEach(function (l) {{ l.classList.add("keep"); kept.push(l);
      addStreak(l, traceStreaks); }});
  }}
  function untrace() {{
    svg.classList.remove("tracing");
    clearPool(traceStreaks);
    kept.forEach(function (el) {{ el.classList.remove("keep"); }});
    kept = [];
  }}
  // hover name-tag — a floating label so hovering a dot tells you what it is (the
  // detail card still needs a click; this is the at-a-glance answer)
  const nametag = document.getElementById("nametag");
  function showTag(id, ev) {{
    const nm = (NAME[id] || "").split("\\n")[0];
    if (!nm) return;
    const deg = (ADJ[id] || []).length;
    nametag.textContent = nm;
    const sp = document.createElement("span");
    sp.className = "tag-deg";
    sp.textContent = "  · " + deg + (deg === 1 ? " link" : " links");
    nametag.appendChild(sp);
    nametag.style.display = "block";  // before moveTag: it early-returns while hidden,
    moveTag(ev);                      // and offsetWidth needs a laid-out box (review MED)
  }}
  function moveTag(ev) {{
    if (nametag.style.display === "none" || !ev) return;
    const pad = 14;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    const w = nametag.offsetWidth, h = nametag.offsetHeight;
    if (x + w > window.innerWidth - 6) x = ev.clientX - pad - w;
    if (y + h > window.innerHeight - 6) y = ev.clientY - pad - h;
    nametag.style.left = x + "px";
    nametag.style.top = y + "px";
  }}
  function hideTag() {{ nametag.style.display = "none"; }}
  let pinned = null;
  circles.forEach(function (c) {{
    c.addEventListener("mouseenter", function (e) {{
      if (!pinned) trace(c.dataset.id);
      showTag(c.dataset.id, e); }});
    c.addEventListener("mousemove", moveTag);
    c.addEventListener("mouseleave", function () {{ if (!pinned) untrace(); hideTag(); }});
    c.addEventListener("click", function (e) {{ e.stopPropagation(); pin(c.dataset.id); }});
  }});

  // ---- pin + detail card ----
  const detail = document.getElementById("detail");
  // directed neighbour maps: the impact question needs "used by" vs "uses" split
  // apart, with the edge type on each row (R11 AC-4/AC-5; panel: card was
  // direction-blind, making blast-radius reads impossible)
  const inBy = {{}}, outBy = {{}};
  lines.forEach(function (l) {{
    const t = typeToken(l), w = +l.getAttribute("stroke-width") || 1;
    const col = l.getAttribute("stroke") || "#6b7694";
    const o = (outBy[l.dataset.src] = outBy[l.dataset.src] || {{}});
    const i = (inBy[l.dataset.dst] = inBy[l.dataset.dst] || {{}});
    // keep the heaviest edge's type/colour per neighbour pair
    if (!o[l.dataset.dst] || o[l.dataset.dst].w < w) o[l.dataset.dst] = {{ w: w, t: t, c: col }};
    if (!i[l.dataset.src] || i[l.dataset.src].w < w) i[l.dataset.src] = {{ w: w, t: t, c: col }};
  }});
  function nbRow(n, meta) {{
    const b = document.createElement("button");
    b.className = V && !V.has(n) ? "nb off" : "nb";  // dimmed = hidden by family state
    if (meta) {{
      const tick = document.createElement("span");
      tick.className = "tick";
      tick.style.background = meta.c;
      tick.title = meta.t;
      b.appendChild(tick);
    }}
    const bdi = document.createElement("bdi");  // LTR glyph order inside rtl clip
    bdi.textContent = (NAME[n] || n).split("\\n")[0];
    b.appendChild(bdi);
    b.addEventListener("click", function () {{ revealFamilyOf(n); flyTo(n); }});
    return b;
  }}
  function nbGroup(box, title, ids, metaOf) {{
    if (!ids.length) return;
    const h = document.createElement("div");
    h.className = "nbhead";
    h.textContent = title + " · " + ids.length;
    box.appendChild(h);
    const CAP = 40;
    ids.slice(0, CAP).forEach(function (n) {{ box.appendChild(nbRow(n, metaOf[n])); }});
    if (ids.length > CAP) {{
      // no silent truncation: the rest expand on demand (R11 AC-7)
      const more = document.createElement("button");
      more.className = "nb more";
      more.textContent = "show all " + ids.length;
      more.addEventListener("click", function () {{
        ids.slice(CAP).forEach(function (n) {{
          box.insertBefore(nbRow(n, metaOf[n]), more); }});
        more.remove();
      }});
      box.appendChild(more);
    }}
  }}
  function pin(id) {{
    pinned = id;
    trace(id);
    const parts = (NAME[id] || "").split("\\n");
    detail.querySelector(".qn").textContent = parts[0] || id;
    detail.querySelector(".kind").textContent = (parts[1] || "").replace(/[()]/g, "")
      .split(" ")[0] || "symbol";
    const pathLine = (parts[1] || "").split(") ")[1] || "";
    const pe = detail.querySelector(".path");
    pe.textContent = "";
    if (pathLine) {{
      // open-in-editor works from a static file: vscode deep link with abs path:line
      // (R11 AC-24; market panel: graph->source linkage is table stakes)
      const a = document.createElement("a");
      a.className = "srclink";
      a.textContent = pathLine;
      if (SRC_ROOT) {{
        a.href = "vscode://file/" + encodeURI(SRC_ROOT.replace(/\\\\/g, "/")
          + "/" + pathLine);
        a.title = "open in editor";
      }}
      pe.appendChild(a);
      const cp = document.createElement("button");
      cp.className = "copybtn";
      cp.textContent = "copy";
      cp.title = "copy path:line";
      cp.addEventListener("click", function () {{
        // clipboard write is a PROMISE: only report "copied" when it resolves
        // (review MED: a rejected write still flashed "copied")
        navigator.clipboard.writeText(pathLine).then(function () {{
          cp.textContent = "copied";
          setTimeout(function () {{ cp.textContent = "copy"; }}, 1200);
        }}, function () {{ cp.textContent = "copy failed"; }});
      }});
      pe.appendChild(cp);
    }}
    const usedBy = Object.keys(inBy[id] || {{}}).sort();
    const uses = Object.keys(outBy[id] || {{}}).sort();
    detail.querySelector(".deg").textContent =
      "· " + (ADJ[id] || []).length + " unique";
    // a both-ways neighbour legitimately sits in BOTH direction groups (review MED)
    const box = detail.querySelector(".nbs");
    box.textContent = "";
    nbGroup(box, "used by", usedBy, inBy[id] || {{}});
    nbGroup(box, "uses", uses, outBy[id] || {{}});
    detail.classList.add("open");
    // click-pins frame the neighbourhood too (AC-20 covers both pin paths); the
    // glide keeps it gentle, and a flyTo-initiated pin glides to the same target
    glideTo(frameNeighbourhood(id), 450);
    writeHash();
  }}
  function unpin() {{
    pinned = null; untrace(); detail.classList.remove("open"); writeHash(); }}
  detail.querySelector(".close").addEventListener("click", unpin);
  svg.addEventListener("click", function () {{
    if (dragDist > 4) return;  // that was a pan, not a dismissal (AC-9)
    if (pinned) unpin(); }});

  // ---- family tri-state visibility engine ----
  const famState = FAM.st0.slice();  // 0 hidden / 1 fringe / 2 full, per family index
  const glyphs = ["\\u25cb", "\\u25d0", "\\u25c9"];
  let V = null;  // the visible-node id set (null until first recompute)
  function nodeFam(id) {{ return FAM.uf[GLOBE.u[id]]; }}
  function recomputeVisibility() {{
    // Pass 1: FULL = every node of a state-2 family. Pass 2: fringe = state-1 nodes with
    // a neighbour IN FULL — a single pass, never a fixpoint (fringe cannot recruit
    // fringe), so the reveal stays scoped and explainable.
    const full = new Set();
    circles.forEach(function (c) {{
      if (famState[nodeFam(c.dataset.id)] === 2) full.add(c.dataset.id); }});
    V = new Set(full);
    circles.forEach(function (c) {{
      const id = c.dataset.id;
      if (famState[nodeFam(id)] === 1
          && (ADJ[id] || []).some(function (m) {{ return full.has(m); }})) V.add(id);
    }});
    applyVisibility();
  }}
  function applyVisibility() {{
    circles.forEach(function (c) {{
      c.classList.toggle("fam-off", !V.has(c.dataset.id)); }});
    lines.forEach(function (l) {{
      l.classList.toggle("fam-off", !(V.has(l.dataset.src) && V.has(l.dataset.dst))); }});
    const unitVisible = [];
    circles.forEach(function (c) {{
      if (V.has(c.dataset.id)) unitVisible[GLOBE.u[c.dataset.id]] = true; }});
    labels.forEach(function (t) {{
      t.classList.toggle("fam-off", !unitVisible[+t.dataset.ui]); }});
    // the family map always shows every group — hidden ones just dim (the map stays honest)
    document.querySelectorAll("#famlayer circle.fam, #famlayer text.famlabel")
      .forEach(function (el) {{
        el.classList.toggle("fam-dim", famState[+el.dataset.f] === 0); }});
    document.querySelectorAll(
      "#famlayer path.famarc, #famlayer path.famflow, #famlayer text.arclabel")
      .forEach(function (el) {{
        const fs = +el.dataset.fs, fd = +el.dataset.fd;
        if (!isNaN(fs)) el.classList.toggle(
          "fam-dim", famState[fs] === 0 || famState[fd] === 0);
      }});
    // famhit stays undimmed on purpose (tooltip-only today; PR 3 decides drill inertness)
    document.body.classList.toggle("all-hidden", V.size === 0);
    if (pinned) pin(pinned);  // re-derive neighbour-button dim states (review LOW)
    refreshAmbient();
    buildEnclosures();
    buildGroupLabels();
  }}
  function setChipState(f, state) {{
    famState[f] = state;
    const chip = document.querySelector('button.chip[data-f="' + f + '"]');
    if (chip) {{ chip.dataset.state = state;
      chip.querySelector(".glyph").textContent = glyphs[state]; }}
    recomputeVisibility();
  }}
  document.querySelectorAll("button.chip").forEach(function (chip) {{
    chip.addEventListener("click", function () {{
      const f = +chip.dataset.f;
      setChipState(f, (famState[f] + 1) % 3);
    }});
  }});
  // Reveal path from the detail card: clicking a hidden neighbour promotes its family to
  // fringe (the deliberate opt-in), then flies there.
  function revealFamilyOf(id) {{
    const f = nodeFam(id);
    if (famState[f] === 0) setChipState(f, 1);
  }}

  // ---- drill-down: click an arc = the pair view; click a family = the solo view ----
  const crumb = document.getElementById("crumb");
  const modOfUnit = [];  // unit index -> the unit's module-centre circle
  circles.forEach(function (c) {{
    if (c.classList.contains("mod")) modOfUnit[GLOBE.u[c.dataset.id]] = c; }});
  let drill = null;        // {{kind:"pair", fs, fd, t}} | {{kind:"solo", f}}
  let drillRestored = [];  // lines whose unchecked-type inline display we lifted
  function typeBoxChecked(t) {{
    const box = document.querySelector('#panel input[data-css="type-' + t + '"]');
    return !box || box.checked;
  }}
  function lineType(l) {{
    const m = l.className.baseVal.match(/type-([a-z0-9_-]+)/);
    return m ? m[1] : "";
  }}
  function clearDrill() {{  // remove marks/state WITHOUT any morph (radio switches reuse this)
    if (!drill) return;
    drillRestored.forEach(function (l) {{
      if (!typeBoxChecked(lineType(l))) l.style.display = "none"; }});
    drillRestored = [];
    document.querySelectorAll(".dk").forEach(function (el) {{
      el.classList.remove("dk"); }});
    document.querySelectorAll("text.label.apex").forEach(function (el) {{
      el.classList.remove("apex"); }});
    svg.classList.remove("pair");
    document.body.classList.remove("drilled");
    drill = null;
    clearPool(drillStreaks);
    unpin();  // a pinned drill member is about to vanish wholesale (review LOW)
  }}
  function drillPositions(memberIds, sideOf, memberLines) {{
    // PAIR: a bipartite RIBBON — each side is a column of unit clusters, ordered so
    // connected units face each other (one barycentric pass per side). Straight lines
    // then fan cleanly between the columns instead of criss-crossing preserved
    // constellation shapes (user report: overlapping lines in the zoomed view).
    // SOLO (one side empty): the side's constellation re-centres on the stage.
    const P = POS.relatedness;
    const sides = {{ A: [], B: [] }};
    memberIds.forEach(function (id) {{ sides[sideOf(id)].push(id); }});
    const out = {{}};
    const solo = !sides.A.length || !sides.B.length;
    if (solo) {{
      // COMPACT spiral of unit clusters (user report: reusing the global scatter
      // spread a drilled group across the whole canvas — unreadable). Cluster shapes
      // are kept; only their centres are re-laid on a golden-angle spiral.
      const ids = sides.A.length ? sides.A : sides.B;
      const byUnit = {{}};
      ids.forEach(function (id) {{
        (byUnit[GLOBE.u[id]] = byUnit[GLOBE.u[id]] || []).push(id); }});
      const units = Object.keys(byUnit).map(Number).sort(function (a, b) {{
        return byUnit[b].length - byUnit[a].length || a - b; }});
      let maxR = 20;
      const baseOf = {{}};
      units.forEach(function (u) {{
        const mod = modOfUnit[u];
        const base = mod ? P[mod.dataset.id] : P[byUnit[u][0]];
        baseOf[u] = base;
        byUnit[u].forEach(function (id) {{
          maxR = Math.max(maxR,
            Math.hypot(P[id][0] - base[0], P[id][1] - base[1])); }});
      }});
      const step = 2 * maxR + 26;
      units.forEach(function (u, i) {{
        const r = step * Math.sqrt(i);
        const th = i * 2.399963229728653;  // golden angle
        const ux2 = 0.5 * CANVAS + r * Math.cos(th);
        const uy2 = 0.5 * CANVAS + r * Math.sin(th);
        byUnit[u].forEach(function (id) {{
          out[id] = [ux2 + (P[id][0] - baseOf[u][0]),
                     uy2 + (P[id][1] - baseOf[u][1])];
        }});
      }});
      return out;
    }}
    // group each side's members by unit; intra-unit offsets come from the relatedness
    // layout relative to the unit's module centre (the module offset is (0,0))
    function unitsOf(ids) {{
      const m = {{}};
      ids.forEach(function (id) {{
        (m[GLOBE.u[id]] = m[GLOBE.u[id]] || []).push(id); }});
      return m;
    }}
    const uA = unitsOf(sides.A), uB = unitsOf(sides.B);
    // unit-level adjacency from the drilled lines
    const link = {{}};
    memberLines.forEach(function (l) {{
      const a = GLOBE.u[l.dataset.src], b = GLOBE.u[l.dataset.dst];
      (link[a] = link[a] || []).push(b);
      (link[b] = link[b] || []).push(a);
    }});
    function column(unitMap, tx, orderY) {{
      const keys = Object.keys(unitMap).map(Number);
      keys.sort(function (x, y) {{
        const ox = orderY(x), oy = orderY(y);
        return ox !== oy ? ox - oy : x - y;  // deterministic tie-break
      }});
      const slot = {{}}, offR = {{}};
      let total = 0;
      keys.forEach(function (u) {{
        const mod = modOfUnit[u];
        const base = mod ? P[mod.dataset.id] : P[unitMap[u][0]];
        let r = 0;
        unitMap[u].forEach(function (id) {{
          r = Math.max(r, Math.hypot(P[id][0] - base[0], P[id][1] - base[1])); }});
        offR[u] = r;
        // the slot GROWS to fit the cluster at near-full scale — scaling a ring down
        // compresses sibling spacing below node diameters (user report: overlap blobs)
        slot[u] = Math.max(26 + 9 * Math.ceil(Math.sqrt(unitMap[u].length)),
          1.8 * r + 16);
        total += slot[u];
      }});
      let y = 0.5 * CANVAS - total / 2;
      const centreY = {{}};
      keys.forEach(function (u) {{
        centreY[u] = y + slot[u] / 2;
        const mod = modOfUnit[u];
        const base = mod ? P[mod.dataset.id] : P[unitMap[u][0]];
        const maxOff = offR[u];
        const k2 = maxOff > 0 ? Math.min(0.9, (slot[u] / 2 - 6) / maxOff) : 0.9;
        unitMap[u].forEach(function (id) {{
          out[id] = [tx + k2 * (P[id][0] - base[0]),
                     centreY[u] + k2 * (P[id][1] - base[1])];
        }});
        y += slot[u];
      }});
      return centreY;
    }}
    // Column gap follows the columns' HEIGHT (user report: fixed canvas-fraction
    // anchors collapsed short drills into a wide thin band).
    function colHeight(unitMap) {{
      let total = 0;
      Object.keys(unitMap).forEach(function (u) {{
        total += 26 + 9 * Math.ceil(Math.sqrt(unitMap[u].length)); }});
      return total;
    }}
    const gap = Math.max(220, 0.55 * Math.max(colHeight(uA), colHeight(uB)));
    const xA = 0.5 * CANVAS - gap / 2, xB = 0.5 * CANVAS + gap / 2;
    // pass 1: A by unit index; pass 2: B faces its A partners; pass 3: A faces B
    let yA = column(uA, xA, function (u) {{ return u; }});
    function meanPartnerY(u, other) {{
      const ps = (link[u] || []).map(function (v) {{ return other[v]; }})
        .filter(function (v) {{ return v !== undefined; }});
      if (!ps.length) return 0.5 * CANVAS;
      return ps.reduce(function (a, b) {{ return a + b; }}, 0) / ps.length;
    }}
    const yB = column(uB, xB, function (u) {{ return meanPartnerY(u, yA); }});
    // re-place A facing B; only the position side-effects matter from this pass
    column(uA, xA, function (u) {{ return meanPartnerY(u, yB); }});
    return out;
  }}
  function markApex(memberIds, sideOf) {{
    // a single-unit side is the drill's SUBJECT — its label reads as the title
    const sides = {{ A: {{}}, B: {{}} }};
    memberIds.forEach(function (id) {{
      sides[sideOf(id)][GLOBE.u[id]] = 1; }});
    ["A", "B"].forEach(function (k) {{
      const us = Object.keys(sides[k]);
      if (us.length === 1) {{
        labels.forEach(function (t) {{
          if (+t.dataset.ui === +us[0]) t.classList.add("apex"); }});
      }}
    }});
  }}
  function stageDrill(memberLines, memberIds, unitsIn, sideOf, label) {{
    unpin();
    clearPool(ambientStreaks);
    memberIds.forEach(function (id) {{ const c = byId[id]; if (c) c.classList.add("dk"); }});
    memberLines.forEach(function (l) {{
      l.classList.add("dk");
      if (l.style.display === "none") {{ drillRestored.push(l); l.style.display = ""; }}
    }});
    labels.forEach(function (t) {{
      if (unitsIn.has(+t.dataset.ui)) t.classList.add("dk"); }});
    const pos = drillPositions(memberIds, sideOf, memberLines);
    svg.classList.add("morphing", "pair");
    svg.classList.remove("families");
    memberIds.forEach(function (id) {{ const c = byId[id], a = pos[id];
      if (c && a) {{ c.setAttribute("cx", a[0]); c.setAttribute("cy", a[1]); }} }});
    memberLines.forEach(function (l) {{
      const a = pos[l.dataset.src], b = pos[l.dataset.dst];
      if (a && b) l.setAttribute("d",
        "M " + a[0] + " " + a[1]
        + " Q " + (a[0] + b[0]) / 2 + " " + (a[1] + b[1]) / 2
        + " " + b[0] + " " + b[1]); }});
    labels.forEach(function (t) {{
      if (!t.classList.contains("dk")) return;
      const m = modOfUnit[+t.dataset.ui];
      if (m && pos[m.dataset.id]) {{ t.setAttribute("x", pos[m.dataset.id][0]);
        t.setAttribute("y", pos[m.dataset.id][1] - 16); }} }});
    const myDrill = drill;  // identity, not truthiness: a future entry point must not
    setTimeout(function () {{  // let a stale timeout finish someone else's drill
      if (drill === myDrill) {{
        svg.classList.remove("morphing"); fit();
        memberLines.forEach(function (l) {{  // streaks ride the SETTLED positions
          addStreak(l, drillStreaks); }});
      }}
    }}, 620);
    crumb.querySelector(".path").textContent = label;
    markApex(memberIds, sideOf);
    document.body.classList.add("drilled");
    writeHash();
  }}
  function enterPair(fs, fd, t) {{
    clearDrill();
    drill = {{ kind: "pair", fs: fs, fd: fd, t: t }};
    const memberLines = lines.filter(function (l) {{
      if (!l.classList.contains("type-" + t)) return false;
      const a = +l.dataset.fs, b = +l.dataset.fd;
      return a !== b && ((a === fs && b === fd) || (a === fd && b === fs));
    }});
    if (!memberLines.length) {{ drill = null; return; }}
    const ids = new Set();
    memberLines.forEach(function (l) {{ ids.add(l.dataset.src); ids.add(l.dataset.dst); }});
    const unitsIn = new Set();
    ids.forEach(function (id) {{ unitsIn.add(GLOBE.u[id]); }});
    unitsIn.forEach(function (ui) {{
      const m = modOfUnit[ui]; if (m) ids.add(m.dataset.id); }});
    stageDrill(memberLines, Array.from(ids), unitsIn,
      function (id) {{ return nodeFam(id) === fs ? "A" : "B"; }},
      "structure \u25b8 " + FAM.names[fs] + " \u2194 " + FAM.names[fd] + " \u00b7 " + t
      + " \u00b7 " + memberLines.length + " edges");
  }}
  function enterSolo(f) {{
    clearDrill();
    drill = {{ kind: "solo", f: f }};
    const ids = [];
    circles.forEach(function (c) {{
      if (nodeFam(c.dataset.id) === f) ids.push(c.dataset.id); }});
    if (!ids.length) {{ drill = null; return; }}
    const idSet = new Set(ids);
    const memberLines = lines.filter(function (l) {{
      return idSet.has(l.dataset.src) && idSet.has(l.dataset.dst)
        && l.style.display !== "none";  // solo respects the type checkboxes
    }});
    const unitsIn = new Set();
    ids.forEach(function (id) {{ unitsIn.add(GLOBE.u[id]); }});
    stageDrill(memberLines, ids, unitsIn, function () {{ return "A"; }},
      "structure \u25b8 " + FAM.names[f]);
  }}
  function exitDrill() {{
    if (!drill && subFam === null) return;
    setTimeout(writeHash, 0);  // after the state below settles
    const home = drillStack.pop() || {{ home: "families" }};
    if (!drill && subFam !== null) {{
      // at a subring, going home to the family map
      hideSubring();
      document.body.classList.remove("drilled");
      const r = document.querySelector('input[name="view"][value="families"]');
      r.checked = true;
      r.dispatchEvent(new Event("change"));
      return;
    }}
    clearDrill();
    if (home.home === "subring") {{ showSubring(home.f); return; }}
    // morph home: the families radio is still checked — re-dispatching its change event
    // replays the full entry choreography (morph, class at the 620ms boundary, fit)
    const r = document.querySelector('input[name="view"][value="families"]');
    r.checked = true;
    r.dispatchEvent(new Event("change"));
  }}
  // ---- sub-family tier: a family's SUBPACKAGES as their own aggregate ring ----
  // (round-3 item 4: a solo family was an unreadable hairball; same grammar, one
  //  tier down. drillStack lets Esc pop ONE level at a time.)
  let subFam = null;
  const drillStack = [];
  function sublayerOf(f) {{
    return document.querySelector('.sublayer[data-f="' + f + '"]');
  }}
  function showSubring(f) {{
    const el = sublayerOf(f);
    if (!el) return false;
    if (subFam !== null) hideSubring();
    clearPool(ambientStreaks);
    unpin();
    el.style.display = "";
    svg.classList.add("subring");
    svg.classList.remove("families");
    subFam = f;
    crumb.querySelector(".path").textContent =
      "structure \u25b8 " + FAM.names[f] + " — click a group or arc";
    document.body.classList.add("drilled");
    writeHash();
    fit();
    return true;
  }}
  function hideSubring() {{
    if (subFam === null) return;
    const el = sublayerOf(subFam);
    if (el) el.style.display = "none";
    svg.classList.remove("subring");
    subFam = null;
  }}
  function unitsInGroup(f, g) {{
    const ids = [];
    circles.forEach(function (c) {{
      const ui = GLOBE.u[c.dataset.id];
      if (nodeFam(c.dataset.id) === f && SUB.g[ui] === g) ids.push(c.dataset.id);
    }});
    return ids;
  }}
  function enterSubSolo(f, g) {{
    const ids = unitsInGroup(f, g);
    if (!ids.length) return;
    drillStack.push({{ home: "subring", f: f }});
    hideSubring();
    drill = {{ kind: "subsolo", f: f, g: g }};
    const idSet = new Set(ids);
    const memberLines = lines.filter(function (l) {{
      return idSet.has(l.dataset.src) && idSet.has(l.dataset.dst)
        && l.style.display !== "none";
    }});
    const unitsIn = new Set();
    ids.forEach(function (id) {{ unitsIn.add(GLOBE.u[id]); }});
    stageDrill(memberLines, ids, unitsIn, function () {{ return "A"; }},
      "structure \u25b8 " + FAM.names[f] + " \u25b8 " + SUB.n[f][g]);
  }}
  function enterSubPair(f, gs, gd, t) {{
    drillStack.push({{ home: "subring", f: f }});
    hideSubring();
    drill = {{ kind: "subpair", f: f, gs: gs, gd: gd, t: t }};
    const memberLines = lines.filter(function (l) {{
      if (!l.classList.contains("type-" + t)) return false;
      const gu = SUB.g[GLOBE.u[l.dataset.src]];
      const gv = SUB.g[GLOBE.u[l.dataset.dst]];
      if (nodeFam(l.dataset.src) !== f || nodeFam(l.dataset.dst) !== f) return false;
      return gu !== gv && ((gu === gs && gv === gd) || (gu === gd && gv === gs));
    }});
    if (!memberLines.length) {{ drill = null; drillStack.pop(); showSubring(f); return; }}
    const ids = new Set();
    memberLines.forEach(function (l) {{ ids.add(l.dataset.src); ids.add(l.dataset.dst); }});
    const unitsIn = new Set();
    ids.forEach(function (id) {{ unitsIn.add(GLOBE.u[id]); }});
    unitsIn.forEach(function (ui) {{
      const m = modOfUnit[ui]; if (m) ids.add(m.dataset.id); }});
    stageDrill(memberLines, Array.from(ids), unitsIn,
      function (id) {{ return SUB.g[GLOBE.u[id]] === gs ? "A" : "B"; }},
      "structure \u25b8 " + FAM.names[f] + " \u25b8 " + SUB.n[f][gs] + " \u2194 "
      + SUB.n[f][gd] + " \u00b7 " + t + " \u00b7 " + memberLines.length + " edges");
  }}
  document.querySelectorAll("path.famhit").forEach(function (h) {{
    h.addEventListener("click", function (e) {{
      e.stopPropagation();
      drillStack.push({{ home: "families" }});
      enterPair(+h.dataset.fs, +h.dataset.fd, h.dataset.t);
    }});
  }});
  document.querySelectorAll("#famlayer circle.fam").forEach(function (c) {{
    c.addEventListener("click", function (e) {{
      e.stopPropagation();
      const f = +c.dataset.f;
      if (showSubring(f)) {{ drillStack.push({{ home: "families" }}); return; }}
      drillStack.push({{ home: "families" }});
      enterSolo(f);  // single-subpackage family: the constellation fallback
    }});
  }});
  document.querySelectorAll(".sublayer circle.sub").forEach(function (c) {{
    c.addEventListener("click", function (e) {{
      e.stopPropagation();
      enterSubSolo(+c.closest(".sublayer").dataset.f, +c.dataset.g);
    }});
  }});
  document.querySelectorAll(".sublayer path.subhit").forEach(function (h) {{
    h.addEventListener("click", function (e) {{
      e.stopPropagation();
      enterSubPair(+h.closest(".sublayer").dataset.f,
        +h.dataset.gs, +h.dataset.gd, h.dataset.t);
    }});
  }});
  crumb.querySelector(".back").addEventListener("click", exitDrill);
  // labels forward clicks to what they name — the name is the largest, most legible
  // target on screen (R11 AC-17; usability panel: labels looked clickable but were
  // dead AND shielded the arc beneath)
  document.querySelectorAll("#famlayer text.famlabel").forEach(function (t) {{
    t.style.cursor = "pointer";
    t.addEventListener("click", function (e) {{
      e.stopPropagation();
      const c = document.querySelector(
        '#famlayer circle.fam[data-f="' + t.dataset.f + '"]');
      if (c) c.dispatchEvent(new MouseEvent("click", {{ bubbles: true }}));
    }});
  }});
  document.querySelectorAll("#famlayer text.arclabel").forEach(function (t) {{
    const hit = document.querySelector('#famlayer path.famhit[data-fs="' + t.dataset.fs
      + '"][data-fd="' + t.dataset.fd + '"][data-t="' + t.dataset.t + '"]');
    if (!hit) return;
    ["mouseenter", "mouseleave", "click"].forEach(function (kind) {{
      t.addEventListener(kind, function (e) {{
        e.stopPropagation();
        hit.dispatchEvent(new MouseEvent(kind, {{ bubbles: kind === "click" }}));
      }});
    }});
  }});
  // ring hover: an arc (or a circle's whole neighbourhood) answers when asked —
  // quiet labels reveal, the rest of the ring recedes
  function ringFocus(layer, els) {{
    layer.classList.add("focus");
    els.forEach(function (e) {{ e.classList.add("hl"); }});
  }}
  function ringBlur(layer) {{
    layer.classList.remove("focus");
    layer.querySelectorAll(".hl").forEach(function (e) {{ e.classList.remove("hl"); }});
  }}
  function bindRingHover(layer, srcA, dstA, arcCls) {{
    // arcCls is per-tier (famarc/subarc) — a hardcoded famarc left sub-ring arcs out of
    // the focus effect entirely (review HIGH: silent, 3 of 4 cues still responded)
    layer.querySelectorAll("path." + (srcA === "fs" ? "famhit" : "subhit"))
      .forEach(function (h) {{
        h.addEventListener("mouseenter", function () {{
          const sel = '[data-' + srcA + '="' + h.dataset[srcA] + '"][data-' + dstA
            + '="' + h.dataset[dstA] + '"][data-t="' + h.dataset.t + '"]';
          ringFocus(layer, layer.querySelectorAll(
            "path." + arcCls + sel + ", path.famflow" + sel + ", text.arclabel" + sel));
        }});
        h.addEventListener("mouseleave", function () {{ ringBlur(layer); }});
      }});
    layer.querySelectorAll("circle").forEach(function (c) {{
      const idx = srcA === "fs" ? c.dataset.f : c.dataset.g;
      c.addEventListener("mouseenter", function () {{
        const els = Array.from(layer.querySelectorAll(
          "path." + arcCls + ", path.famflow, text.arclabel")).filter(function (e) {{
            return e.dataset[srcA] === idx || e.dataset[dstA] === idx; }});
        els.push(c);
        ringFocus(layer, els);
      }});
      c.addEventListener("mouseleave", function () {{ ringBlur(layer); }});
    }});
  }}
  bindRingHover(document.getElementById("famlayer"), "fs", "fd", "famarc");
  document.querySelectorAll(".sublayer").forEach(function (sl) {{
    bindRingHover(sl, "gs", "gd", "subarc");
  }});
  document.querySelector('input[name="view"][value="families"]')
    .addEventListener("click", function () {{
      if (drill || subFam !== null) exitDrill(); }});

  // ---- edge-type / label / overlay toggles ----
  document.querySelectorAll("#panel input[data-css]").forEach(function (box) {{
    function sync() {{ const disp = box.checked ? "" : "none";
      document.querySelectorAll("." + box.dataset.css).forEach(function (el) {{
        el.style.display = disp; }}); }}
    box.addEventListener("change", function () {{ sync(); refreshAmbient(); }}); sync();
  }});
  document.querySelectorAll("#panel input[data-overlay]").forEach(function (box) {{
    box.addEventListener("change", function () {{
      svg.classList.toggle(box.dataset.overlay, box.checked);
      // "show all edges" also lifts the quiet-at-rest default in the flat detail views
      if (box.dataset.overlay === "all-edges") {{
        edgesAlways = box.checked;
        svg.classList.toggle("edges-quiet",
          !edgesAlways && !!QUIET_VIEWS[currentView()]);
      }}
      refreshAmbient();  // the visible-edge set may have changed (e.g. all-edges)
    }});
  }});

  // ---- pan / zoom: content-fitted, partially counter-scaled, clamped, rAF-batched ----
  const CANVAS = {canvas:.0f};
  let vb = {{ x: 0, y: 0, w: CANVAS, h: CANVAS }};
  let BASE = CANVAS, MIN_W = CANVAS / 40, MAX_W = CANVAS * 4;
  let raf = false;
  function apply() {{
    if (raf) return;
    raf = true;
    requestAnimationFrame(function () {{
      raf = false;
      svg.setAttribute("viewBox", vb.x + " " + vb.y + " " + vb.w + " " + vb.h);
    }});
  }}
  function rescale() {{
    const r0 = svg.getBoundingClientRect();
    if (r0.width <= 0) return;
    const dz = vb.w / r0.width;  // user units per screen px
    svg.style.setProperty("--dz", dz.toFixed(4));
    const s = Math.min(3, Math.max(0.3, Math.pow(vb.w / BASE, 0.6)));
    circles.forEach(function (c) {{ c.setAttribute("r", (+c.dataset.r * s).toFixed(3)); }});
    // Labels are SCREEN-FIXED (user report: unreadable unless deeply zoomed) and
    // level-of-detail gated: they only render once a cluster spans enough pixels to be
    // worth naming — far out, the family/ring tiers carry the identity instead.
    // two visible steps (R11 AC-28): heavyweight clusters read first
    const px = 11.5 * dz, pxBig = 13.5 * dz;
    labels.forEach(function (t) {{
      const big = (+t.dataset.w || 0) >= LABEL_W_BIG;
      const apex = t.classList.contains("apex");
      t.setAttribute("font-size", (apex ? 15 * dz : big ? pxBig : px).toFixed(3));
      t.style.opacity = big || apex ? "1" : "0.82";
      t.style.strokeWidth = (2.8 * dz).toFixed(3); }});
    const esx = r0.width / vb.w, esy = r0.height / vb.h;
    const encPlaced = [];
    encLayer.querySelectorAll("text").forEach(function (t) {{
      t.setAttribute("font-size", (13 * dz).toFixed(3));
      // a name wider than its territory collides with the neighbours' — the check
      // must be LIVE (review MED: a one-shot prune went stale on zoom/resize)
      if (t.getComputedTextLength() > +t.dataset.maxw) {{ t.style.display = "none"; return; }}
      t.style.display = "";
      // adjacent territories can carry near-identical names that overlap — drop the
      // later one (user round 10: two identically-named territory labels collided)
      const box = labelBox(t, esx, esy);
      for (let i = 0; i < encPlaced.length; i++) {{
        const q = encPlaced[i];
        if (box[0] < q[2] && q[0] < box[2] && box[1] < q[3] && q[1] < box[3]) {{
          t.style.display = "none"; return; }}
      }}
      encPlaced.push(box); }});
    glLayer.querySelectorAll("text").forEach(function (t) {{
      t.setAttribute("font-size", (13.5 * dz).toFixed(3)); }});
    declutterLabels();
  }}
  // Weight-ranked screen-space declutter (user: labels vanished unpredictably at some
  // zooms). The old all-or-nothing CELL gate hid EVERY label at once; instead we label
  // the heaviest clusters first and drop only those that would collide — so the biggest
  // names are always visible and more reveal as you zoom in, in every view.
  const labelsByWeight = labels.slice().sort(function (a, b) {{
    return (+b.dataset.w || 0) - (+a.dataset.w || 0); }});
  const LABEL_W_BIG = labelsByWeight.length
    ? (+labelsByWeight[Math.floor(labelsByWeight.length * 0.15)].dataset.w || 0)
    : 0;
  function labelScreenHalfW(t) {{
    // screen width is zoom-invariant (font is screen-fixed) — measure once, cache
    if (t._shw === undefined) {{
      const dz = vb.w / (svg.getBoundingClientRect().width || vb.w);
      t._shw = dz > 0 ? (t.getComputedTextLength() / dz) / 2 : 30;
    }}
    return t._shw;
  }}
  const H = 15;  // ~label height in screen px
  // one label's screen bbox, computed from its x/y ATTRIBUTES + cached width — the same
  // frame as every other label, so seeding never mixes a painted box with a math box
  // (user round 10: getBoundingClientRect-seeded reserves raced the viewBox during zoom)
  function labelBox(t, sx, sy) {{
    const scrX = (+t.getAttribute("x") - vb.x) * sx;
    const scrY = (+t.getAttribute("y") - vb.y) * sy;
    const hw = labelScreenHalfW(t), M = 3;
    const a = t.getAttribute("text-anchor");
    const l = a === "end" ? scrX - 2 * hw : a === "middle" ? scrX - hw : scrX;
    const r = a === "end" ? scrX : a === "middle" ? scrX + hw : scrX + 2 * hw;
    return [l - M, scrY - H, r + M, scrY + 4];
  }}
  function declutterLabels() {{
    // families uses famlabels; drills/subrings show their own small labelset via CSS
    if (svg.classList.contains("families") || svg.classList.contains("pair")
        || svg.classList.contains("subring")) return;
    const r0 = svg.getBoundingClientRect();
    if (r0.width <= 0) return;
    const sx = r0.width / vb.w, sy = r0.height / vb.h;
    const placed = [];  // screen bboxes of shown labels
    // reserve space for the higher-priority group + territory labels FIRST so a file
    // label yields to them instead of overlapping (user + critique: group/ring names
    // sat on top of file names — the round-9 declutter only de-conflicted file labels)
    document.querySelectorAll("#grouplabels text, #enclosures text").forEach(function (t) {{
      if (t.style.display !== "none") placed.push(labelBox(t, sx, sy));
    }});
    labelsByWeight.forEach(function (t) {{
      if (t.classList.contains("fam-off") && !t.classList.contains("dk")) {{
        t.style.display = "none"; return; }}
      if (t.dataset.back === "1") {{ t.style.display = "none"; return; }}  // globe far side
      const box = labelBox(t, sx, sy);
      // off-screen labels never occupy space
      if (box[2] < 0 || box[0] > r0.width || box[3] < 0 || box[1] > r0.height) {{
        t.style.display = "none"; return; }}
      let hit = false;
      for (let i = 0; i < placed.length; i++) {{
        const p = placed[i];
        if (box[0] < p[2] && p[0] < box[2] && box[1] < p[3] && p[1] < box[3]) {{
          hit = true; break; }}
      }}
      if (hit) {{ t.style.display = "none"; }}
      else {{ t.style.display = ""; placed.push(box); }}
    }});
  }}
  // The live on-screen position: cx/cy as currently set by applyView()/project(). POS holds
  // only the yaw-0 baked layout — after a globe rotation it lies about where nodes ARE
  // (review HIGH: search fly-to panned to the unrotated spot).
  function livePos(id) {{
    const c = byId[id];
    return c ? [+c.getAttribute("cx"), +c.getAttribute("cy")] : null;
  }}
  function computeFit() {{
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    // Families view frames the aggregate circles (labels included via the pad); detail
    // views frame the live node positions.
    // three staging modes: drilled (frame the .dk members), families (frame the
    // aggregate circles — the radio still reads "families" during a drill, so the drill
    // check comes first), detail (frame visible nodes)
    const drilled = svg.classList.contains("pair");
    const subring = !drilled && subFam !== null;
    const fams = !drilled && !subring && currentView() === "families";
    const boxes = drilled
      ? circles.filter(function (c) {{ return c.classList.contains("dk"); }})
      : subring
        ? Array.from(sublayerOf(subFam).querySelectorAll("circle.sub"))
        : fams ? Array.from(document.querySelectorAll("#famlayer circle.fam")) : circles;
    boxes.forEach(function (c) {{
      if (!drilled && !fams && c.classList.contains("fam-off")) return;  // visible only
      const x = +c.getAttribute("cx"), y = +c.getAttribute("cy");
      const r = +c.getAttribute("r") || 0;
      minX = Math.min(minX, x - r); maxX = Math.max(maxX, x + r);
      minY = Math.min(minY, y - r);
      // family labels hang ~30 units below their circle (review LOW: tiny canvases)
      maxY = Math.max(maxY, y + r + (fams ? 30 : 0)); }});
    if (minX === Infinity) return;
    // Chrome-aware framing (review: the panel occludes ~23% of the left edge and the
    // explain bar the bottom — content was centred in the FULL viewport, so labels
    // clipped behind the chrome in every view).
    const pad = Math.max(18, (maxX - minX) * 0.03);
    minX -= pad; maxX += pad; minY -= pad; maxY += pad;
    const w = maxX - minX, h = maxY - minY;
    const rect = svg.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    const panel = document.getElementById("panel").getBoundingClientRect();
    const explain = document.getElementById("explain").getBoundingClientRect();
    const detail = document.getElementById("detail").getBoundingClientRect();
    const padL = Math.max(24, panel.right + 20);
    const padR = Math.max(72, detail.width + 20);  // zoom controls / open detail card
    const padT = document.body.classList.contains("drilled") ? 64 : 24;
    const padB = Math.max(20, explain.height + 14);
    const visW = Math.max(80, rect.width - padL - padR);
    const visH = Math.max(80, rect.height - padT - padB);
    const scale = Math.min(visW / w, visH / h);  // px per user unit
    return {{
      x: minX - padL / scale - (visW / scale - w) / 2,
      y: minY - padT / scale - (visH / scale - h) / 2,
      w: rect.width / scale,
      h: rect.height / scale,
    }};
  }}
  function fit() {{
    const target = computeFit();
    if (!target) return;
    vb = target;
    BASE = vb.w; MIN_W = BASE / 40; MAX_W = BASE * 4;
    apply(); rescale();
  }}
  // Group ENCLOSURES (user: "organise these so they fit a mental model"): each
  // detail view draws a soft rounded territory behind every colour group — the view's
  // own grouping (community / layer / subpackage) becomes readable BEFORE any edge is.
  const encLayer = document.getElementById("enclosures");
  // group the visible circles by a view's enc identity, returned in ascending group order
  function groupCircles(enc) {{
    const groups = {{}};
    circles.forEach(function (c) {{
      if (c.classList.contains("fam-off")) return;
      const gi = enc[0][GLOBE.u[c.dataset.id]];
      (groups[gi] = groups[gi] || []).push(c);
    }});
    return Object.keys(groups).map(Number).sort(function (a, b) {{ return a - b; }})
      .map(function (gi) {{ return [gi, groups[gi]]; }});
  }}
  function buildEnclosures() {{
    encLayer.textContent = "";
    const view = currentView();
    // rectangles are honest only for block/column layouts — radial views (orbits,
    // galaxy) and the sphere would get overlapping boxes that lie about grouping
    const boxy = view === "package" || view === "relatedness" || view === "dependency";
    if (!boxy || drill || subFam !== null) return;
    const enc = ENC[view];
    if (!enc) return;  // defence-in-depth: empty layouts ship no group vocabulary
    groupCircles(enc).forEach(function (pair) {{
      const gi = pair[0], g = pair[1];
      const f = g[0].getAttribute("fill");
      if (g.length < 4) return;  // tiny groups: the dots speak for themselves
      let mnX = 1e18, mnY = 1e18, mxX = -1e18, mxY = -1e18;
      g.forEach(function (c) {{
        const x = +c.getAttribute("cx"), y = +c.getAttribute("cy");
        mnX = Math.min(mnX, x); mxX = Math.max(mxX, x);
        mnY = Math.min(mnY, y); mxY = Math.max(mxY, y);
      }});
      const pad = 26;
      const rect = document.createElementNS(SVGNS, "rect");
      rect.setAttribute("x", (mnX - pad).toFixed(1));
      rect.setAttribute("y", (mnY - pad).toFixed(1));
      rect.setAttribute("width", (mxX - mnX + 2 * pad).toFixed(1));
      rect.setAttribute("height", (mxY - mnY + 2 * pad).toFixed(1));
      rect.setAttribute("rx", "20");
      rect.setAttribute("fill", f);
      rect.setAttribute("fill-opacity", "0.045");
      rect.setAttribute("stroke", f);
      rect.setAttribute("stroke-opacity", "0.22");
      encLayer.appendChild(rect);
      if (enc[1][gi]) {{
        const t = document.createElementNS(SVGNS, "text");
        t.setAttribute("x", (mnX - pad + 10).toFixed(1));
        t.setAttribute("y", (mnY - pad + 18).toFixed(1));  // inside the box, not above
        t.setAttribute("fill", f);
        t.textContent = enc[1][gi];
        t.dataset.maxw = ((mxX - mnX + 2 * pad) - 16).toFixed(1);
        encLayer.appendChild(t);
      }}
    }});
    // dependency columns caption on ONE shared baseline — per-box tops of unequal
    // columns read as jitter (R11 AC-27; aesthetic panel)
    if (view === "dependency") {{
      let top = 1e18;
      encLayer.querySelectorAll("text").forEach(function (t) {{
        top = Math.min(top, +t.getAttribute("y")); }});
      if (top < 1e17) {{
        encLayer.querySelectorAll("text").forEach(function (t) {{
          t.setAttribute("y", top.toFixed(1)); }});
      }}
    }}
    rescale();  // size the fresh labels
  }}
  // Radial group labels (user round 9): Orbits and Galaxy can't wear rectangles, so each
  // dependency-layer ring / community arm gets a floating name in the open space at its
  // edge — Galaxy at the arm's tip (furthest from centre), Orbits at the ring's TOP
  // (a stable spot, since ring members are equidistant so "furthest" is just noise).
  const glLayer = document.getElementById("grouplabels");
  function buildGroupLabels() {{
    glLayer.textContent = "";
    const view = currentView();
    if ((view !== "orbits" && view !== "galaxy") || drill || subFam !== null) return;
    const enc = ENC[view];
    if (!enc) return;
    const cx = CANVAS / 2, cy = CANVAS / 2;
    const r0 = svg.getBoundingClientRect();
    const sx = r0.width / vb.w, sy = r0.height / vb.h;
    const placed = [];  // screen bboxes of already-placed group labels
    groupCircles(enc).forEach(function (pair) {{
      const gi = pair[0], g = pair[1];
      if (g.length < 3 || !enc[1][gi]) return;
      let ax, ay;
      if (view === "galaxy") {{
        // arm tip: the member furthest from centre, nudged outward past the dots
        let bestR = -1;
        g.forEach(function (c) {{
          const x = +c.getAttribute("cx"), y = +c.getAttribute("cy");
          const r = Math.hypot(x - cx, y - cy);
          if (r > bestR) {{ bestR = r; ax = x; ay = y; }}
        }});
        const ux = (ax - cx) / (bestR || 1), uy = (ay - cy) / (bestR || 1);
        ax += ux * 24; ay += uy * 24;
      }} else {{
        // orbits: every ring label sits on ONE consistent bearing (upper-right,
        // -45deg) at its ring's mean radius, tied to the ring by a short tick
        // (R11 AC-27; the old "top member" anchors scattered around the dial)
        let rsum = 0;
        g.forEach(function (c) {{
          rsum += Math.hypot(+c.getAttribute("cx") - cx,
                             +c.getAttribute("cy") - cy); }});
        const rr = rsum / g.length;
        const ang = -Math.PI / 4;
        ax = cx + (rr + 26) * Math.cos(ang);
        ay = cy + (rr + 26) * Math.sin(ang);
        const tick = document.createElementNS(SVGNS, "line");
        tick.setAttribute("x1", (cx + rr * Math.cos(ang)).toFixed(1));
        tick.setAttribute("y1", (cy + rr * Math.sin(ang)).toFixed(1));
        tick.setAttribute("x2", (cx + (rr + 20) * Math.cos(ang)).toFixed(1));
        tick.setAttribute("y2", (cy + (rr + 20) * Math.sin(ang)).toFixed(1));
        tick.setAttribute("stroke", g[0].getAttribute("fill"));
        tick.setAttribute("stroke-opacity", "0.5");
        tick.setAttribute("vector-effect", "non-scaling-stroke");
        glLayer.appendChild(tick);
      }}
      // declutter against already-placed group labels (review MED: none before)
      const hw = ((enc[1][gi].length * 8) / 2), h = 16;
      const scrX = (ax - vb.x) * sx, scrY = (ay - vb.y) * sy;
      const box = [scrX - hw, scrY - h, scrX + hw, scrY + 4];
      for (let i = 0; i < placed.length; i++) {{
        const p = placed[i];
        if (box[0] < p[2] && p[0] < box[2] && box[1] < p[3] && p[1] < box[3]) return;
      }}
      placed.push(box);
      const t = document.createElementNS(SVGNS, "text");
      t.setAttribute("x", ax.toFixed(1));
      t.setAttribute("y", ay.toFixed(1));
      t.setAttribute("text-anchor", view === "galaxy" && ax < cx ? "end"
        : view === "orbits" ? "middle" : "start");
      t.setAttribute("fill", g[0].getAttribute("fill"));
      t.textContent = enc[1][gi];
      glLayer.appendChild(t);
    }});
    rescale();
  }}

  // The camera GLIDES to the new frame in step with the 620ms node morph — fitting only
  // at the end made every view switch land somewhere the transition never pointed at
  // (user report: "the transition doesn't fit the end state").
  let camRaf = null;
  function glideTo(target, ms) {{
    if (!target) return;
    if (camRaf) cancelAnimationFrame(camRaf);
    const from = {{ x: vb.x, y: vb.y, w: vb.w, h: vb.h }};
    const t0 = performance.now();
    function step(now) {{
      const t = Math.min(1, (now - t0) / ms);
      const e = 1 - Math.pow(1 - t, 3);
      vb = {{ x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e,
        w: from.w + (target.w - from.w) * e, h: from.h + (target.h - from.h) * e }};
      apply(); rescale();
      if (t < 1) camRaf = requestAnimationFrame(step);
      else {{ camRaf = null; BASE = vb.w; MIN_W = BASE / 40; MAX_W = BASE * 4; }}
    }}
    camRaf = requestAnimationFrame(step);
  }}
  function zoomBy(k, cx, cy) {{
    k = Math.min(Math.max(k, MIN_W / vb.w), MAX_W / vb.w);
    const mx = cx === undefined ? vb.x + vb.w / 2 : cx;
    const my = cy === undefined ? vb.y + vb.h / 2 : cy;
    vb.x = mx - (mx - vb.x) * k; vb.y = my - (my - vb.y) * k;
    vb.w *= k; vb.h *= k; apply(); rescale();
  }}
  svg.addEventListener("wheel", function (e) {{
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    const mx = vb.x + (e.clientX - r.left) / r.width * vb.w;
    const my = vb.y + (e.clientY - r.top) / r.height * vb.h;
    zoomBy(e.deltaY < 0 ? 0.85 : 1.18, mx, my);
  }}, {{ passive: false }});
  let drag = null, dragDist = 0;
  svg.addEventListener("mousedown", function (e) {{ drag = {{ x: e.clientX, y: e.clientY }};
    dragDist = 0;
    svg.classList.add("grabbing"); }});
  window.addEventListener("mouseup", function () {{
    drag = null; svg.classList.remove("grabbing");
    if (svg.classList.contains("spinning")) {{
      svg.classList.remove("spinning"); project();  // edges rejoin at the new rotation
      refreshAmbient();  // streak endpoints went stale with the rotation
    }}
  }});
  window.addEventListener("mousemove", function (e) {{
    if (!drag) return;
    if (svg.classList.contains("globe") && !e.shiftKey) {{
      svg.classList.add("spinning");
      yaw += (e.clientX - drag.x) * 0.005;
      pitch = Math.max(-1.2, Math.min(1.2, pitch + (e.clientY - drag.y) * 0.005));
      drag = {{ x: e.clientX, y: e.clientY }};
      if (!spinRaf) {{ spinRaf = true; requestAnimationFrame(function () {{
        spinRaf = false; project(true); }}); }}
      return;
    }}
    const r = svg.getBoundingClientRect();
    dragDist += Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y);
    vb.x -= (e.clientX - drag.x) / r.width * vb.w;
    vb.y -= (e.clientY - drag.y) / r.height * vb.h;
    drag = {{ x: e.clientX, y: e.clientY }}; apply();
  }});
  // resize preserves the user's viewpoint: keep centre + width, re-derive height
  // from the new aspect (AC-12; panel: docking a window snapped back to overview)
  window.addEventListener("resize", function () {{
    const r = svg.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    const cy = vb.y + vb.h / 2;
    vb.h = vb.w * (r.height / r.width);
    vb.y = cy - vb.h / 2;
    apply(); rescale();
  }});

  // ---- globe: orthographic projection with drag-to-rotate (vanilla 3D) ----
  let yaw = 0, pitch = 0, spinRaf = false;
  const G_OFF = {{}};
  circles.forEach(function (c) {{
    const id = c.dataset.id;
    const ui = GLOBE.u[id];
    if (ui === undefined) return;
    const base = POS.globe[id];
    const c3 = GLOBE.c[ui];
    // node's flat offset within its cluster = yaw-0 screen pos minus yaw-0 center projection
    G_OFF[id] = [base[0] - (GLOBE.half + c3[0]), base[1] - (GLOBE.half + c3[1])];
  }});
  function project(light) {{
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const P = {{}};
    const D = {{}};
    const R3 = {{}};
    GLOBE.units.forEach(function (u, ui) {{
      const c3 = GLOBE.c[ui];
      const x1 = c3[0] * cy + c3[2] * sy;
      const z1 = -c3[0] * sy + c3[2] * cy;
      const y1 = c3[1] * cp - z1 * sp;
      const z2 = c3[1] * sp + z1 * cp;
      P[ui] = [GLOBE.half + x1, GLOBE.half + y1];
      D[ui] = z2 / (GLOBE.r || 1);  // -1 (back) .. 1 (front)
      R3[ui] = [x1, y1, z2];
    }});
    const nodePos = {{}};
    circles.forEach(function (c) {{
      const id = c.dataset.id, ui = GLOBE.u[id];
      if (ui === undefined) return;
      const d = D[ui];
      const scale = 0.62 + 0.38 * (d + 1) / 2;
      const x = P[ui][0] + G_OFF[id][0] * scale;
      const y = P[ui][1] + G_OFF[id][1] * scale;
      nodePos[id] = [x, y];
      c.setAttribute("cx", x.toFixed(1));
      c.setAttribute("cy", y.toFixed(1));
      c.setAttribute("fill-opacity", d > 0 ? "1" : "0.22");
    }});
    // globe labels ride their unit's projected spot; back-facing ones step aside so the
    // declutter never spends a slot on a name behind the sphere
    labels.forEach(function (t) {{
      const ui = +t.dataset.ui, pp = P[ui];
      if (!pp) return;
      if (D[ui] > 0.1) {{
        t.setAttribute("x", pp[0].toFixed(1));
        t.setAttribute("y", (pp[1] - 16).toFixed(1));
        t.dataset.back = "";
      }} else {{
        t.dataset.back = "1";
      }}
    }});
    if (!light) {{
      // FLIGHT ARCS (globe review G1): edges used to chord straight through the sphere
      // interior — the midpoint now lifts radially outward from the projected centre,
      // scaled by chord length, so relationships fly OVER the surface.
      lines.forEach(function (l) {{
        const a = nodePos[l.dataset.src], b = nodePos[l.dataset.dst];
        if (!a || !b) return;
        // control = the endpoints' 3D midpoint pushed OUT along the sphere normal, so
        // arcs fly ABOVE the surface like flight paths (user: not wrapped on it);
        // longer hops fly proportionally higher
        const ra = R3[GLOBE.u[l.dataset.src]], rb = R3[GLOBE.u[l.dataset.dst]];
        const m = [(ra[0] + rb[0]) / 2, (ra[1] + rb[1]) / 2, (ra[2] + rb[2]) / 2];
        const ml = Math.max(Math.hypot(m[0], m[1], m[2]), (GLOBE.r || 1) * 0.05);
        const c3 = Math.hypot(rb[0] - ra[0], rb[1] - ra[1], rb[2] - ra[2]);
        const h = 1 + 0.22 + 0.45 * (c3 / (2 * (GLOBE.r || 1)));
        const k = (GLOBE.r || 1) * h / ml;
        l.setAttribute("d", "M " + a[0].toFixed(1) + " " + a[1].toFixed(1)
          + " Q " + (GLOBE.half + m[0] * k).toFixed(1) + " "
          + (GLOBE.half + m[1] * k).toFixed(1)
          + " " + b[0].toFixed(1) + " " + b[1].toFixed(1));
        const da = D[GLOBE.u[l.dataset.src]], db = D[GLOBE.u[l.dataset.dst]];
        const depth = (da + db) / 2;  // -1 back .. 1 front
        l.setAttribute("stroke-opacity",
          (0.15 + 0.6 * Math.max(0, depth) + 0.05 * Math.min(0, depth)).toFixed(2));
      }});
      declutterLabels();  // re-rank hub labels after a full (drag-release) projection
    }}
  }}
  // Idle rotation: after 3.5s without interaction in the globe view, the sphere
  // drifts slowly (full re-projection every 3rd frame keeps the flight arcs riding
  // along); any interaction stops it and re-arms the timer. Skipped entirely under
  // prefers-reduced-motion.
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let idleTimer = null, idleRaf = null, idleOn = false, idleFrame = 0;
  function idleStep() {{
    idleRaf = null;
    if (!idleOn) return;
    yaw += 0.0014;
    idleFrame++;
    project(idleFrame % 3 !== 0);
    idleRaf = requestAnimationFrame(idleStep);
  }}
  function startIdle() {{
    if (REDUCED_MOTION || idleLocked || idleOn || currentView() !== "globe" || drill
        || subFam !== null || pinned || svg.classList.contains("spinning")) return;
    idleOn = true;
    svg.classList.add("resting");
    idleRaf = requestAnimationFrame(idleStep);
  }}
  function stopIdle() {{
    const wasOn = idleOn;
    idleOn = false;
    svg.classList.remove("resting");
    if (idleRaf) {{ cancelAnimationFrame(idleRaf); idleRaf = null; }}
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(startIdle, 10000);  // user: 3.5s spun the globe out from under hovers
    if (wasOn) {{ project(); refreshAmbient(); }}  // settle arcs + streaks where we stopped
  }}
  ["mousedown", "wheel", "keydown"].forEach(function (ev) {{
    window.addEventListener(ev, stopIdle, {{ passive: true }});
  }});

  function resetDepth() {{
    yaw = 0; pitch = 0;
    circles.forEach(function (c) {{ c.removeAttribute("fill-opacity"); }});
    lines.forEach(function (l) {{ l.removeAttribute("stroke-opacity"); }});
  }}

  // ---- fly-to + search ----
  let toastTimer = null;
  function toast(msg) {{
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {{ t.classList.remove("show"); }}, 3200);
  }}
  function flyTo(id) {{
    // Nodes are hidden in the families view — flying there would pin against nothing
    // visible (review MED). Hop to the communities view first, then fly once it lands.
    if (currentView() === "families") {{
      const det = document.querySelector('input[name="view"][value="relatedness"]');
      det.checked = true;
      det.dispatchEvent(new Event("change"));
      toast("shown in Communities · press 0 to fit");  // R11 AC-19
      setTimeout(function () {{ flyTo(id); }}, 640);
      return;
    }}
    const to = frameNeighbourhood(id);
    if (!to) return;
    const from = {{ x: vb.x, y: vb.y, w: vb.w, h: vb.h }};
    flyAnimate(from, to, id);
  }}
  function frameNeighbourhood(id) {{
    const a = livePos(id);
    if (!a) return null;
    // frame the node AND its whole neighbourhood: centre on the bbox CENTRE and
    // size BOTH axes, or an asymmetric neighbourhood (the externals hub off to one
    // side) overflows the frame (blind verifier AC-20: 20-50% of neighbours landed
    // outside when the frame centred on the pinned node)
    let mnX = a[0], mxX = a[0], mnY = a[1], mxY = a[1];
    (ADJ[id] || []).forEach(function (n) {{
      const q = livePos(n);
      if (!q) return;
      mnX = Math.min(mnX, q[0]); mxX = Math.max(mxX, q[0]);
      mnY = Math.min(mnY, q[1]); mxY = Math.max(mxY, q[1]);
    }});
    const aspect = vb.h / vb.w;
    const needW = Math.max((mxX - mnX) * 1.15 + 140,
      ((mxY - mnY) * 1.15 + 140) / aspect);
    const targetW = Math.min(Math.max(MIN_W, needW), BASE);
    const ccx = (mnX + mxX) / 2, ccy = (mnY + mxY) / 2;
    return {{ x: ccx - targetW / 2, y: ccy - targetW * aspect / 2,
      w: targetW, h: targetW * aspect }};
  }}
  function flyAnimate(from, to, id) {{
    const t0 = performance.now();
    function step(now) {{
      const t = Math.min(1, (now - t0) / 450);
      const e = 1 - Math.pow(1 - t, 3);
      vb = {{ x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e,
        w: from.w + (to.w - from.w) * e, h: from.h + (to.h - from.h) * e }};
      svg.setAttribute("viewBox", vb.x + " " + vb.y + " " + vb.w + " " + vb.h);
      rescale();
      if (t < 1) requestAnimationFrame(step);
      else {{
        pin(id);
        const c = byId[id];
        if (c) {{ c.classList.remove("pulse"); void c.getBBox(); c.classList.add("pulse"); }}
      }}
    }}
    requestAnimationFrame(step);
  }}
  const searchBox = document.getElementById("search");
  const hitsBox = document.getElementById("hits");
  const INDEX = circles.map(function (c) {{
    return [c.dataset.id, (NAME[c.dataset.id] || "").toLowerCase()]; }});
  // OFFMAP: names of every symbol below the rendered cut — search answers for the
  // WHOLE corpus; "no result" must mean "does not exist" (R11 AC-1/AC-2, panel)
  const OFFMAP_LC = OFFMAP.map(function (n) {{ return n.toLowerCase(); }});
  function mutedRow(text) {{
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = text;
    hitsBox.appendChild(li);
  }}
  searchBox.addEventListener("input", function () {{
    const q = searchBox.value.trim().toLowerCase();
    hitsBox.textContent = "";
    if (q.length < 2) return;
    let shown = 0, hidden = 0, extra = 0;
    for (let i = 0; i < INDEX.length; i++) {{
      if (INDEX[i][1].indexOf(q) !== -1) {{
        if (V && !V.has(INDEX[i][0])) {{ hidden++; continue; }}  // family-filtered out
        if (shown >= 20) {{ extra++; continue; }}
        const li = document.createElement("li");
        const bdi = document.createElement("bdi");  // keep LTR glyph order inside
        bdi.textContent = (NAME[INDEX[i][0]] || "").split("\\n")[0];
        li.appendChild(bdi);
        li.dataset.id = INDEX[i][0];
        li.addEventListener("click", function () {{
          flyTo(li.dataset.id); hitsBox.textContent = ""; searchBox.value = ""; }});
        hitsBox.appendChild(li);
        shown++;
      }}
    }}
    if (extra > 0) mutedRow("+" + extra + " more — keep typing");  // AC-8
    if (hidden > 0) mutedRow("+" + hidden + " hidden by family filters");
    let off = 0;
    for (let i = 0; i < OFFMAP_LC.length && off < 5; i++) {{
      if (OFFMAP_LC[i].indexOf(q) !== -1) {{
        const li = document.createElement("li");
        li.className = "muted";
        const bdi = document.createElement("bdi");
        bdi.textContent = OFFMAP[i];
        li.appendChild(bdi);
        li.appendChild(document.createTextNode(" — below the top-"
          + circles.length + " cut"));
        hitsBox.appendChild(li);
        off++;
      }}
    }}
    if (!shown && !hidden && !off) mutedRow("no matches in this codebase");  // AC-2
  }});

  // ---- controls + keyboard ----
  document.getElementById("btn-fit").addEventListener("click", fit);
  document.getElementById("btn-in").addEventListener("click", function () {{ zoomBy(0.8); }});
  document.getElementById("btn-out").addEventListener("click", function () {{ zoomBy(1.25); }});
  window.addEventListener("keydown", function (e) {{
    if (e.target === searchBox) {{
      // full keyboard flow: arrows move the highlight, Enter selects (R11 AC-18)
      if (e.key === "Escape") {{ searchBox.blur(); hitsBox.textContent = ""; }}
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") {{
        e.preventDefault();
        const rows = Array.from(hitsBox.querySelectorAll("li[data-id]"));
        if (!rows.length) return;
        let i = rows.findIndex(function (r) {{ return r.classList.contains("sel"); }});
        rows.forEach(function (r) {{ r.classList.remove("sel"); }});
        i = e.key === "ArrowDown" ? Math.min(i + 1, rows.length - 1)
          : Math.max(i - 1, 0);
        rows[i].classList.add("sel");
        rows[i].scrollIntoView({{ block: "nearest" }});
      }} else if (e.key === "Enter") {{
        e.preventDefault();
        const sel = hitsBox.querySelector("li.sel[data-id]")
          || hitsBox.querySelector("li[data-id]");
        if (sel) sel.dispatchEvent(new MouseEvent("click", {{ bubbles: true }}));
      }}
      return;
    }}
    // focused form controls own their NAVIGATION keys — an arrow press must not both
    // change the view AND pan the canvas (R11 AC-11); global shortcuts (/ 0 Esc) stay
    if ((e.target instanceof HTMLInputElement || e.target instanceof HTMLButtonElement)
        && (e.key.indexOf("Arrow") === 0 || e.key === " " || e.key === "Enter")) {{
      return;
    }}

    if (e.key === "/") {{ e.preventDefault(); searchBox.focus(); }}
    else if (e.key === "0") fit();
    else if (e.key === "+" || e.key === "=") zoomBy(0.8);
    else if (e.key === "-") zoomBy(1.25);
    else if (e.key === "Escape") {{
      // one layer per press: pinned card first, then drill levels (R11 AC-10)
      if (pinned) unpin();
      else if (drill || subFam !== null) exitDrill(); }}
    else if (e.key.indexOf("Arrow") === 0) {{
      const d = vb.w * 0.08;
      if (e.key === "ArrowLeft") vb.x -= d; else if (e.key === "ArrowRight") vb.x += d;
      else if (e.key === "ArrowUp") vb.y -= d; else vb.y += d;
      apply();
    }}
  }});

  // ---- deep links: view/pin/drill state rides the URL fragment (R11 AC-25) ----
  let restoringHash = false;
  function writeHash() {{
    if (restoringHash) return;
    const parts = ["v=" + currentView()];
    if (pinned) parts.push("p=" + encodeURIComponent(pinned));
    if (drill) {{
      if (drill.kind === "pair") parts.push("d=pair." + drill.fs + "." + drill.fd
        + "." + drill.t);
      else if (drill.kind === "solo") parts.push("d=solo." + drill.f);
      else if (drill.kind === "subsolo") parts.push("d=subsolo." + drill.f
        + "." + drill.g);
      else if (drill.kind === "subpair") parts.push("d=subpair." + drill.f + "."
        + drill.gs + "." + drill.gd + "." + drill.t);
    }} else if (subFam !== null) {{
      parts.push("d=subring." + subFam);
    }}
    history.replaceState(null, "", "#" + parts.join("&"));
  }}
  function restoreHash() {{
    const h = location.hash.replace(/^#/, "");
    if (!h) return;
    const kv = {{}};
    h.split("&").forEach(function (seg) {{
      const i = seg.indexOf("=");
      if (i > 0) kv[seg.slice(0, i)] = decodeURIComponent(seg.slice(i + 1));
    }});
    restoringHash = true;
    const radio = kv.v
      && document.querySelector('input[name="view"][value="' + kv.v + '"]');
    if (radio && !radio.checked) {{
      radio.checked = true;
      radio.dispatchEvent(new Event("change"));
    }}
    // drills/pins land after the 620ms morph settles
    setTimeout(function () {{
      // a drill segment is only meaningful in the structure view — writeHash never
      // pairs it otherwise; hand-edited URLs must not build hybrid layouts (review MED)
      const d = ((!kv.v || kv.v === "families") ? (kv.d || "") : "").split(".");
      if (d[0] === "subring") showSubring(+d[1]);
      else if (d[0] === "pair") {{ enterPair(+d[1], +d[2], d[3]);
        if (drill) drillStack.push({{ home: "families" }}); }}
      else if (d[0] === "solo") {{ enterSolo(+d[1]);
        if (drill) drillStack.push({{ home: "families" }}); }}
      else if (d[0] === "subsolo") {{ enterSubSolo(+d[1], +d[2]);
        if (drill) drillStack.push({{ home: "families" }}); }}
      else if (d[0] === "subpair") {{ enterSubPair(+d[1], +d[2], +d[3], d[4]);
        if (drill) drillStack.push({{ home: "families" }}); }}
      setTimeout(function () {{
        if (kv.p && byId[kv.p]) pin(kv.p);
        restoringHash = false;
        writeHash();
      }}, d[0] ? 900 : 0);
    }}, radio ? 700 : 0);
  }}

  recomputeVisibility();  // family defaults apply before first paint
  syncUsage();
  stopIdle();  // arms the idle-rotation timer (no-ops outside the globe view)
  restoreHash();  // deep-linked state re-enters after boot (R11 AC-25)
  fit();  // initial view: content-fitted, aspect-corrected
}})();
</script>
</body>
</html>"""
