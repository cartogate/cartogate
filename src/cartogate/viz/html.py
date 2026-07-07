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
from cartogate.viz.layout import VIEWS, LayoutResult, compute_layout

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
    "defines": "#4a5069",
    "references": "#199e70",
    "inherits": "#9085e9",
    "implements": "#9085e9",
    "documents": "#6b7694",
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
    "relatedness": "Communities",
    "dependency": "Dependency",
    "package": "Package",
    "orbits": "Orbits",
    "galaxy": "Galaxy",
    "globe": "Globe",
}
assert set(_VIEW_EXPLAIN) == set(VIEWS)  # every view needs an explainer (fail fast on drift)


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
    nodes: Iterable[Node], edges: Iterable[Edge], *, title: str = "Cartogate", max_nodes: int = 1500
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

    if len(node_list) > max_nodes:
        kept = {
            n.id
            for n in sorted(node_list, key=lambda n: (-degree[n.id], n.qualified_name))[:max_nodes]
        }
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

    svg_nodes = "\n".join(
        _node_svg(n, layout, default, degree[n.id], in_cycle=n.unit in scc_of)
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
        )
        for e, count in deduped
    )
    svg_labels = "\n".join(
        _label_svg(u, unit_idx[u], labels[default][unit_idx[u]]) for u in units
    )

    return _document(
        title=title,
        default=default,
        canvas=layout.canvas,
        legends=layout.legends,
        edge_types=edge_types,
        svg_nodes=svg_nodes,
        svg_edges=svg_edges,
        svg_labels=svg_labels,
        node_count=len(node_list),
        total_nodes=total_nodes,
        edge_count=len(deduped),
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
    node: Node, layout: LayoutResult, view: str, degree: int, *, in_cycle: bool = False
) -> str:
    x, y = layout.positions[view][node.id]
    fill = layout.fills[view][node.id]
    is_module = node.kind is NodeKind.MODULE
    radius = (5.5 if is_module else 3.0) + min(degree, 12) * 0.4
    classes = "node" + (" mod" if is_module else "") + (" cyc-n" if in_cycle else "")
    stroke = ' stroke-width="0.5"' if is_module else ""
    tip = escape(f"{node.qualified_name}\n({node.kind.value}) {node.unit}")
    return (
        f'<circle class="{classes}" data-id="{node.id}" data-r="{radius:.1f}" cx="{x:.1f}" '
        f'cy="{y:.1f}" r="{radius:.1f}" fill="{fill}"{stroke}>'
        f"<title>{tip}</title></circle>"
    )


def _edge_svg(
    edge: Edge, pos: dict[str, tuple[float, float]], count: int = 1, *, cyc: bool = False
) -> str:
    x1, y1 = pos[edge.src]
    x2, y2 = pos[edge.dst]
    colour = _EDGE_COLORS.get(edge.type.value, _DEFAULT_COLOR)
    hidden = ' style="display:none"' if edge.type.value == _NOISY_EDGE else ""
    dash = _EDGE_DASH.get(edge.type.value)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    width = min(2.0, 0.6 + 0.3 * math.log2(count)) if count > 1 else 0.6
    classes = f"edge type-{_css_token(edge.type.value)}" + (" cyc" if cyc else "")
    return (
        f'<line class="{classes}" data-src="{edge.src}" '
        f'data-dst="{edge.dst}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{colour}" stroke-width="{width:.2f}" opacity="0.5"'
        f'{dash_attr} vector-effect="non-scaling-stroke"{hidden} />'
    )


def _label_svg(unit: str, unit_index: int, spot: list[float]) -> str:
    return (
        f'<text class="label" data-ui="{unit_index}" x="{spot[0]:.1f}" y="{spot[1]:.1f}" '
        f'text-anchor="middle" font-size="9">{escape(_cluster_label(unit))}</text>'
    )


def _color_key(view: str, items: list[tuple[str, str]], default: str) -> str:
    """Legend rows, capped at 8 with an honest fold line.

    The fold copy is per colour scheme: palette views (communities/package/galaxy/globe)
    genuinely repeat their 8 hues, but dependency/orbits layers use a continuous one-hue
    ramp — deeper layers get distinct darker colours, they just aren't listed (review MED:
    claiming "colours repeat" there was factually wrong).
    """
    cap = 8
    rows = [
        f'<div class="item"><span class="swatch" style="background:{colour}"></span>'
        f"{escape(label)}</div>"
        for label, colour in items[:cap]
    ]
    if len(items) > cap:
        note = (
            "deeper layers (darker on the same ramp)"
            if view in ("dependency", "orbits")
            else "smaller (colours repeat)"
        )
        rows.append(f'<div class="item muted">+{len(items) - cap} {note}</div>')
    shown = "" if view == default else ' style="display:none"'
    return f'<div class="legend" id="legend-{view}"{shown}>\n' + "\n".join(rows) + "\n</div>"


def _edge_filter(edge_types: list[str]) -> str:
    rows = []
    for value in edge_types:
        colour = _EDGE_COLORS.get(value, _DEFAULT_COLOR)
        dash = _EDGE_DASH.get(value, "")
        style = f"border-top:2px {dash or 'solid'} {colour}"
        checked = "" if value == _NOISY_EDGE else " checked"
        token = _css_token(value)
        hint = (
            ' title="file → its own symbols; structural noise, hidden by default"'
            if value == _NOISY_EDGE
            else ""
        )
        rows.append(
            f'<label class="item"{hint}><input type="checkbox"{checked} data-css="type-{token}">'
            f' <span class="swatch-line" style="{style}"></span>{escape(value)}</label>'
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
    node_count: int,
    total_nodes: int,
    edge_count: int,
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
    if node_count < total_nodes:
        meta_nodes = (
            f"showing the {node_count:,} most-connected of {total_nodes:,} symbols"
            f" · {edge_count:,} edges"
        )
    else:
        meta_nodes = f"{node_count:,} symbols · {edge_count:,} edges"
    cycles_label = f"import cycles ({cycle_count})" if cycle_count else "import cycles (none)"
    cycles_disabled = "" if cycle_count else " disabled"
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
  .item {{ display: block; cursor: pointer; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; color: var(--ink-2); padding: 1px 0; }}
  .item:hover {{ color: var(--ink); }}
  .item.muted {{ color: var(--ink-3); cursor: default; }}
  .swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 3px;
    margin: 0 6px 0 4px; vertical-align: -1px; }}
  .swatch-line {{ display: inline-block; width: 18px; margin: 0 6px 3px 4px;
    vertical-align: middle; }}
  input[type="checkbox"], input[type="radio"] {{ accent-color: var(--accent); }}

  #search {{ width: 100%; margin-top: 8px; padding: 6px 9px; border-radius: 8px;
    border: 1px solid var(--edge-hair); background: rgba(9, 12, 22, 0.7);
    color: var(--ink); font-family: var(--font-mono); font-size: 11.5px; outline: none; }}
  #search:focus {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(134,182,239,.18); }}
  #hits {{ margin: 4px 0 0; padding: 0; list-style: none; max-height: 180px; overflow: auto; }}
  #hits li {{ padding: 3px 6px; border-radius: 6px; cursor: pointer; color: var(--ink-2);
    font-family: var(--font-mono); font-size: 10.5px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }}
  #hits li:hover, #hits li.sel {{ background: rgba(134, 182, 239, 0.14); color: var(--ink); }}

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
  #detail button.nb {{ display: block; width: 100%; text-align: left; margin: 2px 0;
    padding: 3px 6px; border: 0; border-radius: 6px; background: transparent;
    color: var(--ink-2); font-family: var(--font-mono); font-size: 10.5px; cursor: pointer;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  #detail button.nb:hover {{ background: rgba(134, 182, 239, 0.14); color: var(--ink); }}
  #detail .close {{ float: right; border: 0; background: none; color: var(--ink-3);
    cursor: pointer; font-size: 14px; }}

  #explain {{ position: fixed; z-index: 9; bottom: 0; left: 0; right: 0; padding: 7px 16px;
    background: linear-gradient(to top, rgba(9, 12, 22, 0.92), rgba(9, 12, 22, 0.6));
    border-top: 1px solid var(--edge-hair); color: var(--ink-2); font-size: 11.5px;
    animation: rise .55s .2s both; }}
  #explain b {{ color: var(--ink); }}

  circle.node {{ transition: opacity .25s ease; }}
  svg.morphing circle.node {{ transition: cx .6s ease, cy .6s ease, fill .4s ease; }}
  svg.morphing line.edge {{ transition: x1 .6s ease, y1 .6s ease, x2 .6s ease, y2 .6s ease; }}
  svg.morphing text.label {{ transition: x .6s ease, y .6s ease; }}
  text.label {{ pointer-events: none; paint-order: stroke; stroke: var(--bg);
    fill: var(--ink-3); font-family: var(--font-mono); letter-spacing: .03em; }}
  circle.node.mod {{ stroke: rgba(232, 237, 247, 0.55); }}

  svg.tracing circle.node {{ opacity: 0.07; }}
  svg.tracing circle.node.keep {{ opacity: 1; }}
  svg.tracing line.edge {{ opacity: 0.03 !important; }}
  svg.tracing line.edge.keep {{ opacity: 0.95 !important; }}
  svg.tracing text.label {{ opacity: 0.15; }}

  svg.h-cycles line.edge {{ opacity: 0.05; }}
  svg.h-cycles line.edge.cyc {{ stroke: var(--danger); stroke-width: 1.8; opacity: 0.95; }}
  svg.h-cycles circle.node {{ opacity: 0.18; }}
  svg.h-cycles circle.node.cyc-n {{ opacity: 1; stroke: var(--danger); stroke-width: 1; }}

  svg.globe #labels {{ display: none; }}
  svg.globe.spinning line.edge {{ display: none; }}
  svg.globe #horizon {{ display: inline !important; }}

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
  }}
</style>
</head>
<body>
<div id="panel">
  <div class="brand">Cartogate</div>
  <strong>{safe_title}</strong>
  <div class="meta">{meta_nodes}</div>
  <div class="meta">drag to pan · scroll to zoom · click a dot to pin details · / to search</div>
  <input id="search" type="search" placeholder="find a symbol or file…" autocomplete="off">
  <ul id="hits"></ul>
  <h2>View</h2>
  {view_radios}
  <h2>Colour key</h2>
  {color_keys}
  <h2>Edge types</h2>
  {edge_filter}
  <h2>Overlays</h2>
  <label class="item"><input type="checkbox" id="hl-cycles"
    data-overlay="h-cycles"{cycles_disabled}>
    <span class="swatch" style="background:var(--danger)"></span>{cycles_label}</label>
  <label class="item"><input type="checkbox" checked data-css="label"> file labels</label>
</div>
<svg id="graph" viewBox="0 0 {canvas:.0f} {canvas:.0f}" preserveAspectRatio="xMidYMid meet"
     role="img" aria-label="Code graph: {node_count} symbols, {edge_count} relationships">
  <defs>
    <filter id="softglow" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="1.1" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  <circle id="horizon" cx="{canvas / 2:.1f}" cy="{canvas / 2:.1f}" r="{globe_radius:.1f}"
    fill="none" stroke="rgba(134,182,239,0.18)" stroke-width="1"
    vector-effect="non-scaling-stroke" style="display:none"/>
  <g id="edges">
{svg_edges}
  </g>
  <g id="nodes" filter="url(#softglow)">
{svg_nodes}
  </g>
  <g id="labels">
{svg_labels}
  </g>
</svg>
<div id="controls">
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
<div id="explain">
  <b>How to read this:</b> every dot is one symbol in your code. Dots are grouped into clusters
  — one cluster per file: the file itself at the centre, its top-level functions and classes on
  the inner ring, methods and other nested symbols on the outer ring. Bigger dot = more
  connections. Hover any dot to trace what it calls and what calls it. &nbsp;{explain_spans}
</div>
<script>
(function () {{
  const POS = {pos_json}, FILLS = {fill_json}, LABELS = {label_json}, ADJ = {adj_json};
  const GLOBE = {globe_json};
  const svg = document.getElementById("graph");
  const circles = Array.from(document.querySelectorAll("circle.node"));
  const lines = Array.from(document.querySelectorAll("line.edge"));
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

  function applyView(view) {{
    const p = POS[view], f = FILLS[view];
    circles.forEach(function (c) {{ const a = p[c.dataset.id];
      c.setAttribute("cx", a[0]); c.setAttribute("cy", a[1]);
      c.setAttribute("fill", f[c.dataset.id]); }});
    lines.forEach(function (l) {{ const a = p[l.dataset.src], b = p[l.dataset.dst];
      l.setAttribute("x1", a[0]); l.setAttribute("y1", a[1]);
      l.setAttribute("x2", b[0]); l.setAttribute("y2", b[1]); }});
    labels.forEach(function (t) {{ const a = LABELS[view][+t.dataset.ui];
      t.setAttribute("x", a[0]); t.setAttribute("y", a[1]); }});
    document.querySelectorAll(".legend").forEach(function (el) {{
      el.style.display = el.id === "legend-" + view ? "" : "none"; }});
    document.querySelectorAll("#explain .ex").forEach(function (el) {{
      el.style.display = el.id === "ex-" + view ? "" : "none"; }});
  }}
  document.querySelectorAll('input[name="view"]').forEach(function (r) {{
    r.addEventListener("change", function () {{
      if (!r.checked) return;
      svg.classList.add("morphing");
      svg.classList.toggle("globe", r.value === "globe");
      if (r.value !== "globe") resetDepth();
      applyView(r.value);
      setTimeout(function () {{
        svg.classList.remove("morphing"); fit();
        if (r.value === "globe") project();
      }}, 620);
    }});
  }});

  // ---- trace (class-based: O(degree), not O(N)) ----
  let kept = [];
  function trace(id) {{
    untrace();
    svg.classList.add("tracing");
    const ids = [id].concat(ADJ[id] || []);
    ids.forEach(function (i) {{ const c = byId[i]; if (c) {{ c.classList.add("keep");
      kept.push(c); }} }});
    (linesBy[id] || []).forEach(function (l) {{ l.classList.add("keep"); kept.push(l); }});
  }}
  function untrace() {{
    svg.classList.remove("tracing");
    kept.forEach(function (el) {{ el.classList.remove("keep"); }});
    kept = [];
  }}
  let pinned = null;
  circles.forEach(function (c) {{
    c.addEventListener("mouseenter", function () {{ if (!pinned) trace(c.dataset.id); }});
    c.addEventListener("mouseleave", function () {{ if (!pinned) untrace(); }});
    c.addEventListener("click", function (e) {{ e.stopPropagation(); pin(c.dataset.id); }});
  }});

  // ---- pin + detail card ----
  const detail = document.getElementById("detail");
  function pin(id) {{
    pinned = id;
    trace(id);
    const parts = (NAME[id] || "").split("\\n");
    detail.querySelector(".qn").textContent = parts[0] || id;
    detail.querySelector(".kind").textContent = (parts[1] || "").replace(/[()]/g, "")
      .split(" ")[0] || "symbol";
    detail.querySelector(".path").textContent = (parts[1] || "").split(") ")[1] || "";
    const nbs = ADJ[id] || [];
    detail.querySelector(".deg").textContent = "· " + nbs.length;
    const box = detail.querySelector(".nbs");
    box.textContent = "";
    nbs.slice(0, 40).forEach(function (n) {{
      const b = document.createElement("button");
      b.className = "nb";
      b.textContent = (NAME[n] || n).split("\\n")[0];
      b.addEventListener("click", function () {{ flyTo(n); }});
      box.appendChild(b);
    }});
    detail.classList.add("open");
  }}
  function unpin() {{
    pinned = null; untrace(); detail.classList.remove("open"); }}
  detail.querySelector(".close").addEventListener("click", unpin);
  svg.addEventListener("click", function () {{ if (pinned) unpin(); }});

  // ---- edge-type / label / overlay toggles ----
  document.querySelectorAll("#panel input[data-css]").forEach(function (box) {{
    function sync() {{ const disp = box.checked ? "" : "none";
      document.querySelectorAll("." + box.dataset.css).forEach(function (el) {{
        el.style.display = disp; }}); }}
    box.addEventListener("change", sync); sync();
  }});
  document.querySelectorAll("#panel input[data-overlay]").forEach(function (box) {{
    box.addEventListener("change", function () {{
      svg.classList.toggle(box.dataset.overlay, box.checked); }});
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
    const s = Math.min(3, Math.max(0.3, Math.pow(vb.w / BASE, 0.6)));
    circles.forEach(function (c) {{ c.setAttribute("r", (+c.dataset.r * s).toFixed(3)); }});
    labels.forEach(function (t) {{ t.setAttribute("font-size", (9 * s).toFixed(3));
      t.style.strokeWidth = (2.5 * s).toFixed(3); }});
  }}
  // The live on-screen position: cx/cy as currently set by applyView()/project(). POS holds
  // only the yaw-0 baked layout — after a globe rotation it lies about where nodes ARE
  // (review HIGH: search fly-to panned to the unrotated spot).
  function livePos(id) {{
    const c = byId[id];
    return c ? [+c.getAttribute("cx"), +c.getAttribute("cy")] : null;
  }}
  function fit() {{
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    circles.forEach(function (c) {{
      const x = +c.getAttribute("cx"), y = +c.getAttribute("cy");
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y); }});
    if (minX === Infinity) return;
    const pad = Math.max(24, (maxX - minX) * 0.06);
    let w = maxX - minX + 2 * pad, h = maxY - minY + 2 * pad;
    const r = svg.getBoundingClientRect();
    const aspect = r.width > 0 && r.height > 0 ? r.width / r.height : 1;
    if (w / h < aspect) {{ const nw = h * aspect; minX -= (nw - w) / 2; w = nw; }}
    else {{ const nh = w / aspect; minY -= (nh - h) / 2; h = nh; }}
    vb = {{ x: minX - pad, y: minY - pad, w: w, h: h }};
    BASE = vb.w; MIN_W = BASE / 40; MAX_W = BASE * 4;
    apply(); rescale();
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
  let drag = null;
  svg.addEventListener("mousedown", function (e) {{ drag = {{ x: e.clientX, y: e.clientY }};
    svg.classList.add("grabbing"); }});
  window.addEventListener("mouseup", function () {{
    drag = null; svg.classList.remove("grabbing");
    if (svg.classList.contains("spinning")) {{
      svg.classList.remove("spinning"); project();  // edges rejoin at the new rotation
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
    vb.x -= (e.clientX - drag.x) / r.width * vb.w;
    vb.y -= (e.clientY - drag.y) / r.height * vb.h;
    drag = {{ x: e.clientX, y: e.clientY }}; apply();
  }});
  window.addEventListener("resize", fit);

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
    GLOBE.units.forEach(function (u, ui) {{
      const c3 = GLOBE.c[ui];
      const x1 = c3[0] * cy + c3[2] * sy;
      const z1 = -c3[0] * sy + c3[2] * cy;
      const y1 = c3[1] * cp - z1 * sp;
      const z2 = c3[1] * sp + z1 * cp;
      P[ui] = [GLOBE.half + x1, GLOBE.half + y1];
      D[ui] = z2 / (GLOBE.r || 1);  // -1 (back) .. 1 (front)
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
    if (!light) {{
      lines.forEach(function (l) {{
        const a = nodePos[l.dataset.src], b = nodePos[l.dataset.dst];
        if (!a || !b) return;
        l.setAttribute("x1", a[0].toFixed(1)); l.setAttribute("y1", a[1].toFixed(1));
        l.setAttribute("x2", b[0].toFixed(1)); l.setAttribute("y2", b[1].toFixed(1));
        const da = D[GLOBE.u[l.dataset.src]], db = D[GLOBE.u[l.dataset.dst]];
        l.setAttribute("stroke-opacity", (da + db) / 2 > 0 ? "0.85" : "0.12");
      }});
    }}
  }}
  function resetDepth() {{
    yaw = 0; pitch = 0;
    circles.forEach(function (c) {{ c.removeAttribute("fill-opacity"); }});
    lines.forEach(function (l) {{ l.removeAttribute("stroke-opacity"); }});
  }}

  // ---- fly-to + search ----
  function flyTo(id) {{
    const a = livePos(id);  // live coords — correct even in a rotated globe (review HIGH)
    if (!a) return;
    const targetW = Math.max(MIN_W, BASE / 16);
    const from = {{ x: vb.x, y: vb.y, w: vb.w, h: vb.h }};
    const to = {{ x: a[0] - targetW / 2, y: a[1] - targetW * (vb.h / vb.w) / 2,
      w: targetW, h: targetW * (vb.h / vb.w) }};
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
  searchBox.addEventListener("input", function () {{
    const q = searchBox.value.trim().toLowerCase();
    hitsBox.textContent = "";
    if (q.length < 2) return;
    let shown = 0;
    for (let i = 0; i < INDEX.length && shown < 20; i++) {{
      if (INDEX[i][1].indexOf(q) !== -1) {{
        const li = document.createElement("li");
        li.textContent = (NAME[INDEX[i][0]] || "").split("\\n")[0];
        li.dataset.id = INDEX[i][0];
        li.addEventListener("click", function () {{
          flyTo(li.dataset.id); hitsBox.textContent = ""; searchBox.value = ""; }});
        hitsBox.appendChild(li);
        shown++;
      }}
    }}
  }});

  // ---- controls + keyboard ----
  document.getElementById("btn-fit").addEventListener("click", fit);
  document.getElementById("btn-in").addEventListener("click", function () {{ zoomBy(0.8); }});
  document.getElementById("btn-out").addEventListener("click", function () {{ zoomBy(1.25); }});
  window.addEventListener("keydown", function (e) {{
    if (e.target === searchBox) {{
      if (e.key === "Escape") {{ searchBox.blur(); hitsBox.textContent = ""; }}
      return;
    }}
    if (e.key === "/") {{ e.preventDefault(); searchBox.focus(); }}
    else if (e.key === "0") fit();
    else if (e.key === "+" || e.key === "=") zoomBy(0.8);
    else if (e.key === "-") zoomBy(1.25);
    else if (e.key === "Escape") unpin();
    else if (e.key.indexOf("Arrow") === 0) {{
      const d = vb.w * 0.08;
      if (e.key === "ArrowLeft") vb.x -= d; else if (e.key === "ArrowRight") vb.x += d;
      else if (e.key === "ArrowUp") vb.y -= d; else vb.y += d;
      apply();
    }}
  }});

  fit();  // initial view: content-fitted, aspect-corrected
}})();
</script>
</body>
</html>"""
