"""Tests for the self-contained interactive multi-view HTML renderer."""

from __future__ import annotations

import pathlib

import pytest
from tests.conftest import MakeSymbol

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Provenance
from cartogate.viz.html import to_html


def _edge(src: str, dst: str) -> Edge:
    return Edge(
        type=EdgeType.CALLS,
        src=src,
        dst=dst,
        provenance=Provenance.LSP,
        confidence=Confidence.EXTRACTED,
    )


def test_html_is_a_self_contained_offline_document(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.alpha", unit="pkg/a.py")
    b = make_symbol("pkg.beta", unit="pkg/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)], title="Demo")

    assert "<!DOCTYPE html>" in html and "</html>" in html and "<svg" in html
    assert "pkg.alpha" in html  # node appears (in a tooltip)
    assert 'class="label"' in html  # clusters annotated with module labels
    # Fully offline: no external resource references. (The SVG namespace URI is an XML
    # identifier consumed by createElementNS, never fetched — the one allowed "http".)
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in html


def test_html_has_all_three_views_and_embedded_data(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.alpha", unit="pkg/a.py")
    b = make_symbol("pkg.beta", unit="pkg/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)])

    assert html.count('type="radio" name="view"') == 7  # one radio per view (incl. families)
    assert 'value="families" checked' in html  # families is the landing view
    for view in ("Communities", "Dependency", "Package"):  # display names (UX copy pass)
        assert view in html
    assert "Colour key" in html
    # Per-view position/fill/adjacency data embedded for the JS switcher.
    assert "const POS" in html and "FILLS =" in html and "ADJ =" in html
    for view in ("relatedness", "dependency", "package"):
        assert f'"{view}"' in html


def test_html_hides_noisy_defines_edges_by_default(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.mod", unit="pkg/m.py")
    b = make_symbol("pkg.mod.f", unit="pkg/m.py")
    html = to_html([a, b], [Edge(
        type=EdgeType.DEFINES, src=a.id, dst=b.id,
        provenance=Provenance.TREE_SITTER, confidence=Confidence.EXTRACTED,
    )])
    # the defines edge is rendered hidden and its filter checkbox starts unchecked
    assert 'type-defines" data-src' in html and 'style="display:none"' in html
    assert 'data-css="type-defines"> ' in html  # no " checked" before this input


def test_html_escapes_untrusted_node_text(make_symbol: MakeSymbol) -> None:
    evil = make_symbol("pkg.</title><script>alert(1)</script>", unit="x.py")
    html = to_html([evil], [])
    assert "</title><script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_html_escapes_untrusted_unit_path(make_symbol: MakeSymbol) -> None:
    # The unit drives the cluster label and the tooltip (never raw markup, never raw in the JS).
    evil = make_symbol("pkg.x", unit="</text><script>alert(1)</script>/x.py")
    html = to_html([evil], [])
    assert "</text><script>alert(1)" not in html
    # direct assertions, independent of tag-split games (review: the split check could pass
    # vacuously): the raw payload appears NOWHERE, and the JSON blobs carry the neutralized
    # form _js_json guarantees.
    assert "</script>alert" not in html
    assert "\\u003cscript\\u003ealert(1)" in html  # the unit path IS in the data, neutralized


def test_html_fits_the_initial_view_to_content(make_symbol: MakeSymbol) -> None:
    """Field report: the initial view was a small blob in an ocean of margin — the square
    layout canvas letterboxed into a widescreen viewport. The document must fit the
    viewBox to the CONTENT bounding box (aspect-corrected) on load, view switch, and
    window resize."""
    html = to_html([make_symbol("pkg.a"), make_symbol("pkg.b")], [_edge("pkg.a", "pkg.b")])
    assert "function fit(" in html
    assert html.count("fit()") >= 3  # initial + view switch + resize
    assert 'addEventListener("resize"' in html


def test_html_zoom_keeps_the_graph_visible(make_symbol: MakeSymbol) -> None:
    """Field report: the graph disappeared when zooming in — full counter-scaling kept
    nodes at constant screen size while spacing exploded, and toFixed(2) rounded radii
    and strokes to 0.00 at depth. The scale factor must be PARTIAL (nodes grow on screen
    as you zoom in), CLAMPED, and the zoom range itself bounded."""
    html = to_html([make_symbol("pkg.a"), make_symbol("pkg.b")], [_edge("pkg.a", "pkg.b")])
    assert "Math.pow" in html  # partial (sub-linear) counter-scaling, not s^1
    assert "MIN_W" in html and "MAX_W" in html  # zoom range clamped
    assert "vb.w / CANVAS" not in html  # sizes scale against the FITTED base, not canvas
    # dash patterns are SCREEN-fixed: per-type CSS via calc(N * --dz), --dz set on zoom
    assert 'svg.style.setProperty("--dz"' in html
    # the FULL rule with single braces — the quadruple-brace escaping bug emitted
    # literal {{ }} (invalid CSS) and silently killed every dash pattern (review CRIT)
    assert ("path.type-references { stroke-dasharray: "
            "calc(3 * var(--dz, 1)) calc(2 * var(--dz, 1)); }") in html
    assert "{{ stroke-dasharray" not in html


def test_generated_js_is_syntactically_valid(make_symbol: MakeSymbol) -> None:
    """The script is emitted from an f-string template — a single un-doubled brace makes
    a document that passes every substring pin but is dead on arrival in a browser.
    node --check is the real guard (review of the fit/zoom fix); skipped without node."""
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    html = to_html([make_symbol("pkg.a"), make_symbol("pkg.b")], [_edge("pkg.a", "pkg.b")])
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(match.group(1))
        path = fh.name
    try:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_chrome_stays_above_the_svg(make_symbol: MakeSymbol) -> None:
    """Field bug (browser-verified): the svg's FILLED fade-in kept a permanent stacking
    context, painting the graph over the fixed panel — controls visible <1s then gone.
    The chrome must carry explicit z-index and the unveil animation must not fill forward."""
    html = to_html([make_symbol("pkg.a")], [])
    assert "z-index: 10" in html  # panel + controls
    assert "unveil .8s ease backwards" in html
    assert "unveil .8s ease both" not in html


def test_cycle_members_are_marked_in_the_svg(make_symbol: MakeSymbol) -> None:
    """Mutually-importing units form an SCC; their nodes carry cycle classes for the
    overlay, and intra-SCC cross-unit edges are tagged (review must-add: zero coverage)."""
    a = make_symbol("a.f", unit="a.py")
    b = make_symbol("b.g", unit="b.py")
    html = to_html([a, b], [_edge(a.id, b.id), _edge(b.id, a.id)])
    assert 'class="node cyc-n"' in html  # both members marked for the overlay
    assert 'class="edge type-calls cyc"' in html  # the intra-SCC cross-unit edges too

    acyclic = to_html([a, b], [_edge(a.id, b.id)])
    # the CSS selectors are always in the stylesheet — assert on class ATTRIBUTES only
    assert 'cyc-n"' not in acyclic and 'type-calls cyc"' not in acyclic


def test_globe_payload_is_embedded(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.alpha", unit="pkg/a.py")
    b = make_symbol("pkg.beta", unit="pkg/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)])
    assert "const GLOBE" in html
    assert '"r":' in html and '"units":' in html and '"half":' in html


def test_duplicate_edges_fold_into_one_wider_line(make_symbol: MakeSymbol) -> None:
    """N identical (src, dst, type) edges render as ONE line whose width grows with
    log2(count) — not N stacked lines (review must-add: dedupe had no pin)."""
    a = make_symbol("pkg.alpha", unit="pkg/a.py")
    b = make_symbol("pkg.beta", unit="pkg/b.py")
    html = to_html([a, b], [_edge(a.id, b.id), _edge(a.id, b.id)])
    assert html.count(f'data-src="{a.id}"') == 1
    assert 'stroke-width="1.25"' in html  # 0.9 + 0.35*log2(2) (legibility pass widths)

    single = to_html([a, b], [_edge(a.id, b.id)])
    assert 'stroke-width="0.90"' in single


def test_fly_to_and_fit_use_live_positions(make_symbol: MakeSymbol) -> None:
    """Review HIGH: flyTo()/fit() read the yaw-0 baked POS table, so search in a ROTATED
    globe panned to where the node WOULD be unrotated. Both must read the live cx/cy."""
    html = to_html([make_symbol("pkg.a")], [])
    assert "function livePos(id)" in html
    assert "const a = livePos(id);" in html
    assert "POS[currentView()][id]" not in html


def test_layer_legend_fold_does_not_claim_colours_repeat(make_symbol: MakeSymbol) -> None:
    """Review MED: dependency/orbits use a continuous ramp — deeper layers get DISTINCT
    darker colours, so the palette views' "colours repeat" fold copy was false there."""
    # a 14-deep import chain -> 14 dependency layers -> the layer legend folds (cap 12)
    nodes = [make_symbol(f"m{i}.f", unit=f"m{i}.py") for i in range(14)]
    edges = [_edge(nodes[i].id, nodes[i + 1].id) for i in range(13)]
    html = to_html(nodes, edges)
    assert "deeper layers (darker on the same ramp)" in html
    assert html.count("smaller (colours repeat)") <= html.count('class="legend"') - 2


def test_fam_payload_and_layer_embedded(make_symbol: MakeSymbol) -> None:
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    html = to_html([a, b], [_edge(b.id, a.id)])
    assert "const FAM" in html and '"names":' in html and '"st0":' in html
    assert 'id="famlayer"' in html and 'class="fam"' in html and 'class="famlabel"' in html
    assert 'class="famarc' in html and 'class="famflow' in html
    assert 'data-stagger="' in html  # flow windows stagger via WAAPI, not CSS delays
    # the flow overlay carries the type class: its white ink registers to the base
    # arc's dash pattern (user report: the pulse crossed the dash gaps)
    assert 'class="famflow type-calls"' in html
    assert "@keyframes comet" not in html  # WAAPI windows replaced the CSS march
    assert 'marker id="famarr-calls"' in html
    assert "marker-start" in html or "marker-end" in html  # whichever direction applies


def test_comet_flow_respects_reduced_motion(make_symbol: MakeSymbol) -> None:
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "prefers-reduced-motion" in html
    assert "path.famflow { display: none; }" in html  # arrows carry direction instead


def test_family_arcs_are_directed_and_counted(make_symbol: MakeSymbol) -> None:
    a = make_symbol("a.f", unit="r/src/a.py")  # core = family index 0
    b = make_symbol("b.g", unit="r/tests/x.py")  # tests = family index 1 (present order)
    html = to_html([a, b], [_edge(b.id, a.id), _edge(b.id, a.id)])
    assert 'data-fs="0" data-fd="1"' in html  # canonical pair orientation (core, tests)
    assert "tests → core · calls · 2" in html  # tooltip carries the REAL direction+count
    # node circles carry their family index; edges carry both endpoint families
    assert 'data-f="0"' in html and 'data-f="1"' in html


def test_svg_lands_with_the_families_class_baked(make_symbol: MakeSymbol) -> None:
    """The CSS show/hide machinery for the landing state keys off the svg class — it must
    be in the static markup, not only applied by JS (review must-add)."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert '<svg id="graph" class="families"' in html


def test_st0_defaults_every_family_visible(make_symbol: MakeSymbol) -> None:
    """Round 3: every family lands visible (st0 all 2) — pinned on a single-family repo
    (the old no-core fallback branch is gone)."""
    html = to_html([make_symbol("t.f", unit="r/tests/x.py")], [])
    assert '"st0":[2]' in html


def test_family_chips_render_tri_state_defaults(make_symbol: MakeSymbol) -> None:
    """One chip per present family; core lands full (state 2), everything else hidden (0);
    the glyph and count render; clicking cycles hidden -> fringe -> full (JS)."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    c = make_symbol("c.h", unit="r/docs/d.md")
    html = to_html([a, b, c], [])
    # count on the BUTTON markup — the stylesheet also matches bare data-state substrings
    assert html.count('<button class="chip"') == 3
    assert html.count('data-state="2" title=') == 3  # ALL families land visible (round 3)
    assert html.count('data-state="0" title=') == 0
    assert "Families</h2>" in html or ">Families<" in html


def test_family_visibility_engine_is_single_pass_from_full(
    make_symbol: MakeSymbol,
) -> None:
    """The fringe rule: state-1 nodes join ONLY via a neighbour in a FULL family — a
    fixpoint regression (fringe recruiting fringe) would change this expression."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "function recomputeVisibility()" in html
    assert "full.has(" in html  # fringe checks membership in FULL, never in V
    assert ".fam-off { display: none; }" in html
    assert "recomputeVisibility();" in html  # runs at boot before the first fit


def test_family_labels_carry_the_dim_hook(make_symbol: MakeSymbol) -> None:
    """Review HIGH: famlabel lacked data-f, so hidden families dimmed their circle but not
    their label — the map lied. Both elements must carry the family index."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    html = to_html([a, b], [])
    assert html.count('<text class="famlabel" data-f=') == 2


def test_all_hidden_state_shows_a_recovery_hint(make_symbol: MakeSymbol) -> None:
    """Review MED (plan deviation): cycling every family to hidden froze the canvas with
    no explanation — the hint element and its body-class wiring must exist."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert 'id="hint"' in html and "all families hidden" in html
    assert "all-hidden" in html  # body class toggled by the visibility engine


def test_search_reports_hidden_matches(make_symbol: MakeSymbol) -> None:
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "hidden by family filters" in html


def test_breadcrumb_and_drill_scaffolding(make_symbol: MakeSymbol) -> None:
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    html = to_html([a, b], [_edge(b.id, a.id)])
    assert 'id="crumb"' in html
    assert "function enterPair(" in html and "function enterSolo(" in html
    assert "function exitDrill(" in html
    assert 'class="famhit"' in html  # the hit paths gain their listener in this PR


def test_direction_streaks_overlay_solid_lines(make_symbol: MakeSymbol) -> None:
    """User feedback: never BREAK a line into dashes to show flow — a white streak runs
    over the solid line instead. Streaks are JS-created for the bounded traced/drilled
    sets, ride pathLength=100 for uniform speed, and vanish under reduced motion (the
    lines themselves stay intact)."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "path.streak" in html and "function addStreak(" in html
    # THREE independent pools: untrace() must never kill drill/ambient streaks (user
    # report: all animation stopped after hover -> blank inside a drill)
    assert "addStreak(l, drillStreaks); });" in html
    assert "addStreak(l, traceStreaks); });" in html
    assert "function refreshAmbient()" in html and "AMBIENT_CAP" in html
    # LED-strip model (user spec): the streak is a WHITE COPY carrying the SAME type
    # dash (light never inks the gaps), revealed by a fixed-length travelling clip
    # window at uniform speed — no more length-proportional marching segment
    assert "const STREAK_WIN = 64, STREAK_SPEED = 130;" in html  # R11 AC-33 tail
    # comet v3 (user spec): a TRAVELLING GRADIENT stroke — sharp white head fading to
    # transparent over the base line's own colour; SMIL slides the gradient; streaks
    # keep the type dash class (light only on ink) and constant screen width
    assert "function bindGlow(" in html and '"userSpaceOnUse"' in html
    assert '"animateTransform"' in html and '"gradientTransform"' in html
    assert 't.setAttribute("vector-effect", "non-scaling-stroke");' in html
    assert 't.setAttribute("class", "streak type-" + typeToken(l));' in html
    assert "@keyframes comet" not in html and "clipPath" not in html
    assert "svg.pair path.edge.dk" in html  # drill visibility rule remains
    # the old line-breaking dance is gone
    assert "cometline" not in html
    assert "path.streak { display: none; }" in html  # reduced motion
    # the family-arc flow uses the same per-element gradient stroke (bound at boot)
    assert "path.famflow { stroke-linecap: butt;" in html


def test_esc_priority_search_then_pin_then_drill(make_symbol: MakeSymbol) -> None:
    """R11 AC-10 updated the order: a pinned card closes FIRST, then drill levels —
    one layer per Esc press (usability panel: Esc double-dismissed inside a drill)."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "if (pinned) unpin();" in html
    assert "else if (drill || subFam !== null) exitDrill();" in html


def test_drill_overrides_family_hiding(make_symbol: MakeSymbol) -> None:
    """A drilled member from a HIDDEN family must render: the .dk override outranks
    .fam-off (the composition precedence documented in the stylesheet)."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "svg.pair .fam-off.dk" in html


def test_drill_invariants_are_pinned(make_symbol: MakeSymbol) -> None:
    """Three load-bearing structures the drill review traced (each guards a fixed or
    unreachable-by-construction bug): famlayer's !important full-drill hiding, the
    clear-before-switch ordering in the radio handler, and drilled-before-families in
    fit(). A regression in any silently reopens a traced defect class."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "svg.pair #famlayer { display: none !important; }" in html
    assert 'clearDrill();  // a view switch always leaves the drill' in html
    assert 'svg.classList.contains("pair")' in html  # fit() checks drilled FIRST
    # the pair's member lines survive their own type checkbox mid-drill (review MED)
    assert "display: inline !important;" in html


def test_family_arcs_fan_symmetrically_one_per_type(make_symbol: MakeSymbol) -> None:
    """User spec (round 3): ONE arc per (pair, type); a pair's K types fan symmetrically
    around the straight chord — middle type straight, others arcing left/right. Both
    directions of a type merge into that single arc."""
    import re

    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    edges = []
    for et in (EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.REFERENCES):
        edges.append(Edge(type=et, src=a.id, dst=b.id, provenance=Provenance.LSP,
                          confidence=Confidence.EXTRACTED))
        edges.append(Edge(type=et, src=b.id, dst=a.id, provenance=Provenance.LSP,
                          confidence=Confidence.EXTRACTED))
    html = to_html([a, b], edges)
    paths = re.findall(
        r'class="famarc[^"]*"[^>]*? d="M ([-\d.]+) ([-\d.]+) Q ([-\d.]+) ([-\d.]+) '
        r"([-\d.]+) ([-\d.]+)\"", html)
    assert len(paths) == 3  # one per TYPE, directions merged (was 6 directed rows)
    offsets = []
    for sx, sy, qx, qy, ex, ey in ((float(v) for v in p) for p in paths):
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        # signed perpendicular offset of the control point from the chord
        dx, dy = ex - sx, ey - sy
        norm = (dx * dx + dy * dy) ** 0.5
        offsets.append(((qx - mx) * (-dy) + (qy - my) * dx) / norm)
    offsets.sort()
    assert abs(offsets[1]) < 1e-6  # the middle type runs STRAIGHT
    # the outer two mirror each other; R11 AC-29 staggers ARRIVAL trims per rank
    # (so shared-target arrowheads don't smear), which shifts the measured chord
    # midpoint by a few units — symmetry now holds within that stagger, not exactly
    assert abs(offsets[0] + offsets[2]) < 10
    assert offsets[0] < -20 < 20 < offsets[2]  # and genuinely spread apart
    # merged directions: both arrowheads on the one arc
    assert "marker-start" in html and "marker-end" in html
    # adjacent fan labels must never crowd (review HIGH: apexes sit spread/2 apart —
    # the tangential rank stagger keeps every pair of labels >= a line height apart)
    labels = re.findall(
        r'class="arclabel"[^>]*? x="([-\d.]+)" y="([-\d.]+)"[^>]*?font-size:([\d.]+)', html)
    assert len(labels) == 3
    pts = [(float(x), float(y), float(fs)) for x, y, fs in labels]
    for i in range(3):
        for j in range(i + 1, 3):
            dist2 = (pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2
            assert dist2 ** 0.5 >= pts[i][2], (i, j, dist2 ** 0.5)


def test_streaks_and_edges_fade_while_the_globe_spins(make_symbol: MakeSymbol) -> None:
    """Globe review G2: display:none popped 1,900 edges in/out on every drag — spin
    protection is a FADE now (transparent elements still skip painting), and it must
    cover streaks too (the older HIGH: stale streaks floated during spins)."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "svg.globe.spinning path.edge { opacity: 0 !important; }" in html
    assert "svg.globe.spinning path.streak { opacity: 0 !important; }" in html
    assert "path.edge, path.streak { transition: opacity 0.18s ease; }" in html

def test_edge_type_keys_render_for_dashed_types(make_symbol: MakeSymbol) -> None:
    """User report: documents/inherits/references had no legend key — the old border-top
    swatch interpolated the dash NUMBERS as a border-style (invalid CSS, swatch dropped).
    Swatches are now real mini-SVG lines carrying the actual stroke-dasharray."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/src/b.py")
    edges = [Edge(type=t, src=a.id, dst=b.id, provenance=Provenance.LSP,
                  confidence=Confidence.EXTRACTED)
             for t in (EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.INHERITS)]
    html = to_html([a, b], edges)
    assert html.count('<svg class="swatch-line"') == 3  # one real swatch per type
    assert 'stroke="#199e70" stroke-width="2" stroke-dasharray="3 2"' in html  # references
    assert "border-top:2px" not in html  # the invalid-CSS generator is gone


def test_documents_and_defines_have_distinct_visible_colours(
    make_symbol: MakeSymbol,
) -> None:
    """User report: documents/defines were near-identical grays (documents even collided
    with the DEFAULT colour). Amber and teal now — and neither equals the default."""
    from cartogate.viz.html import _DEFAULT_COLOR, _EDGE_COLORS

    assert _EDGE_COLORS["documents"] == "#c98500"
    assert _EDGE_COLORS["defines"] == "#1fb2a6"
    assert _DEFAULT_COLOR not in (_EDGE_COLORS["documents"], _EDGE_COLORS["defines"])


def test_arcs_and_families_are_labeled_on_the_graph(make_symbol: MakeSymbol) -> None:
    """User ask: name things ON the graph. Every arc carries a type-and-count label at
    its apex; family labels scale with the canvas instead of vanishing at ring zoom."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    html = to_html([a, b], [_edge(b.id, a.id), _edge(b.id, a.id)])
    assert '<text class="arclabel"' in html
    assert "calls · 2</text>" in html
    assert 'class="famlabel" data-f="0"' in html and "font-size:" in html


def test_drill_streaks_survive_hidden_families(make_symbol: MakeSymbol) -> None:
    """User report: no directionality in the drill — .dk members of HIDDEN families carry
    .fam-off (display overridden by .dk), and addStreak wrongly treated them as invisible."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert '!l.classList.contains("dk")' in html  # the fam-off skip exempts drilled lines


def test_pair_drill_uses_the_bipartite_ribbon(make_symbol: MakeSymbol) -> None:
    """User report: lines still overlapped in the zoomed view — the pair drill now lays
    each side out as a COLUMN of unit clusters, barycentrically ordered so connected
    units face each other."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "meanPartnerY" in html and "function column(" in html
    assert "const xA = 0.5 * CANVAS - gap / 2" in html  # height-proportional columns


def test_subfamily_tier_bakes_a_ring_per_multi_group_family(
    make_symbol: MakeSymbol,
) -> None:
    """Round-3 item 4: drilling a family shows its SUBPACKAGES as an aggregate ring
    (same fan-arc grammar as the top level), not a hairball of every member node."""
    a = make_symbol("a.f", unit="r/src/pkg/engine/a.py")
    b = make_symbol("b.g", unit="r/src/pkg/store/b.py")
    c = make_symbol("c.h", unit="r/tests/x.py")
    html = to_html([a, b, c], [_edge(a.id, b.id), _edge(c.id, a.id)])
    assert 'class="sublayer"' in html  # core has 2 subgroups -> gets a ring
    assert 'class="sub"' in html and 'class="sublabel"' in html
    assert 'class="subarc' in html and 'class="subhit"' in html
    assert "const SUB" in html
    # tests family has ONE subgroup -> no ring baked for it (falls back to constellation)
    assert html.count('class="sublayer"') == 1


def test_subfamily_drill_functions_are_wired(make_symbol: MakeSymbol) -> None:
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "function enterSubPair(" in html and "function enterSubSolo(" in html
    assert "drillStack" in html  # Esc pops one level at a time


def test_view_switch_fully_leaves_the_subring(make_symbol: MakeSymbol) -> None:
    """Review HIGH: switching views away from a subring left the crumb pill stuck with a
    dead back button ("drilled" body class never removed; families-radio re-click only
    checked drill). Both exit paths pinned."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert 'classList.remove("drilled");  // review HIGH' in html
    assert "if (drill || subFam !== null) exitDrill();" in html


def test_subgroup_tooltips_carry_the_full_path(make_symbol: MakeSymbol) -> None:
    """Short labels keep only the last two segments — two deep subgroups can collide
    (review MED). The hover tooltip must carry the FULL key so they stay tellable."""
    a = make_symbol("a.f", unit="r/src/pkg/core/handlers/a.py")
    b = make_symbol("b.g", unit="r/src/pkg/api/handlers/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)])
    assert "<title>src/pkg/core/handlers —" in html
    assert "<title>src/pkg/api/handlers —" in html


def test_evil_subgroup_path_is_escaped_in_the_ring(make_symbol: MakeSymbol) -> None:
    """Subgroup names derive from unit paths (attacker-controlled): the ring markup
    itself must escape them, not just the JSON payload (review must-add)."""
    evil = make_symbol("a.f", unit="r/<script>alert(1)</script>/x.py")
    other = make_symbol("b.g", unit="r/clean/y.py")
    html = to_html([evil, other], [_edge(evil.id, other.id)])
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html  # escaped into the sublabel/tooltip markup


def test_fit_reserves_the_chrome(make_symbol: MakeSymbol) -> None:
    """Visual review R1: content was centred in the FULL viewport — labels clipped
    behind the panel/explain bar in every view. fit() must reserve the chrome."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert 'document.getElementById("panel").getBoundingClientRect()' in html
    assert 'document.getElementById("explain").getBoundingClientRect()' in html
    assert 'document.getElementById("detail").getBoundingClientRect()' in html
    # the centring algebra itself (review LOW: a silent formula edit must not pass)
    assert "const scale = Math.min(visW / w, visH / h);" in html
    assert "x: minX - padL / scale - (visW / scale - w) / 2," in html


def test_dense_rings_use_quiet_labels_and_hover_reveal(make_symbol: MakeSymbol) -> None:
    """Visual review R2: >12 arcs in a ring -> only each pair's heaviest arc labels at
    rest; the rest are quiet and reveal on hover (ring focus engine)."""
    nodes = [make_symbol(f"m{i}.f", unit=u) for i, u in enumerate(
        [f"r/{d}/x.py" for d in ("src", "tests", "docs", "scripts")])]
    edges = []
    ids = [n.id for n in nodes]
    for i in range(4):
        for j in range(4):
            if i != j:
                for et in (EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.REFERENCES):
                    edges.append(Edge(type=et, src=ids[i], dst=ids[j],
                                      provenance=Provenance.LSP,
                                      confidence=Confidence.EXTRACTED))
    html = to_html(nodes, edges)  # 6 pairs x 3 types = 18 arcs > 12
    assert html.count('class="arclabel quiet"') == 12  # all but each pair's heaviest
    assert "function bindRingHover(" in html and 'classList.add("hl")' in html
    # BOTH tiers participate (review HIGH: a hardcoded famarc left subarc out silently)
    assert ".focus path.famarc:not(.hl), .focus path.subarc:not(.hl)" in html
    assert 'bindRingHover(sl, "gs", "gd", "subarc");' in html


def test_ring_circles_have_observatory_depth(make_symbol: MakeSymbol) -> None:
    """Visual review R3: flat disks -> radial-gradient spheres with glow; counts muted."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "<radialGradient id=" in html
    assert 'fill="url(#rg-' in html
    assert "filter: url(#softglow);" in html
    assert '<tspan class="cnt"' in html


def test_genuinely_heavier_arc_wins_the_rest_label(make_symbol: MakeSymbol) -> None:
    """Review MED: only the lexicographic tie-break branch had coverage — a genuinely
    heavier (non-lexicographically-first) type must keep the at-rest label."""
    import re

    nodes = [make_symbol(f"m{i}.f", unit=u) for i, u in enumerate(
        [f"r/{d}/x.py" for d in ("src", "tests", "docs", "scripts")])]
    edges = []
    ids = [n.id for n in nodes]
    for i in range(4):
        for j in range(4):
            if i != j:
                for et in (EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.REFERENCES):
                    reps = 3 if et is EdgeType.REFERENCES else 1  # references dominates
                    for _ in range(reps):
                        edges.append(Edge(type=et, src=ids[i], dst=ids[j],
                                          provenance=Provenance.LSP,
                                          confidence=Confidence.EXTRACTED))
    html = to_html(nodes, edges)
    loud = re.findall(r'class="arclabel" [^>]*data-t="([a-z]+)"', html)
    assert set(loud) == {"references"}  # heaviest wins every pair, not "calls"


def test_shade_clamps_at_the_channel_bounds() -> None:
    from cartogate.viz.html import _shade

    assert _shade("#ffffff", 1.35) == "#ffffff"  # ceiling
    assert _shade("#000000", 0.62) == "#000000"  # floor
    assert _shade("#3987e5", 1.0) == "#3987e5"  # identity


def test_globe_edges_fly_over_the_surface(make_symbol: MakeSymbol) -> None:
    """Globe review G1: edges chorded straight through the sphere — the projection now
    lifts each edge's midpoint radially outward (flight arcs), and edges are <path>
    elements everywhere (straight d in flat views)."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/src/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)])
    assert '<path class="edge type-calls"' in html and "<line class=" not in html
    assert 'd="M ' in html  # straight segments at rest
    # the TRUE-3D flight path: control = endpoints' 3D midpoint pushed out along the
    # sphere normal; longer hops fly higher (user: above the surface, missile-style)
    assert '+ " Q " + (GLOBE.half + m[0] * k).toFixed(1)' in html
    assert "const h = 1 + 0.22 + 0.45 * (c3 / (2 * (GLOBE.r || 1)));" in html


def test_globe_idle_rotation_is_wired_and_reduced_motion_safe(
    make_symbol: MakeSymbol,
) -> None:
    """Globe review G5 (the original resting-animation ask): slow idle drift, armed on
    every view entry, stopped by interaction, skipped under prefers-reduced-motion."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "function startIdle()" in html and "function stopIdle()" in html
    assert 'window.matchMedia("(prefers-reduced-motion: reduce)").matches' in html
    assert "if (REDUCED_MOTION || idleLocked || idleOn" in html
    assert 'id="btn-lock"' in html  # the user's globe lock
    assert "setTimeout(startIdle, 10000);" in html  # 10s dwell (3.5s was aggressive)
    assert "stopIdle();  // every view entry re-arms the idle-rotation timer" in html


def test_usage_hint_tells_the_truth_per_view(make_symbol: MakeSymbol) -> None:
    """Globe review G3: the panel said "drag to pan" while globe-drag spins."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert 'id="usage"' in html and "function syncUsage()" in html
    assert "drag to spin" in html


def test_long_range_edges_rest_hidden_but_answer_when_asked(
    make_symbol: MakeSymbol,
) -> None:
    """Visual review R4: detail views drowned in cross-file spaghetti. Cross-unit
    edges beyond the kept heaviest set carry class "lr" and rest hidden; tracing,
    pinning, or drilling reveals them, and an overlay checkbox shows everything."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/src/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)])
    assert ".lr { display: none; }" in html  # rest state
    assert ".lr.keep:not(.fam-off), .lr.dk" in html  # trace/drill reveal
    assert 'data-overlay="all-edges"' in html  # the explicit show-everything switch
    assert "svg.all-edges .lr" in html


def test_locality_keeps_the_heaviest_long_range_edges(make_symbol: MakeSymbol) -> None:
    """The kept set is deterministic: the K heaviest long-range edges stay visible at
    rest (width = multiplicity), everything else goes quiet."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert 'data-lr-keep="400"' in html  # the kept-set size is disclosed on the toggle


def test_cycles_overlay_reveals_hidden_cycle_edges(make_symbol: MakeSymbol) -> None:
    """Review HIGH: cycle edges are cross-unit by construction, so they can land in the
    rest-hidden set — the overlay would then highlight red nodes with INVISIBLE edges
    between them. The overlay must force lr cycle edges visible."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "svg.h-cycles .lr.cyc { display: inline; }" in html


def test_meta_line_discloses_the_rest_count(make_symbol: MakeSymbol) -> None:
    """Review MED: the panel said "1,905 edges" while only ~660 rendered at rest — the
    meta line now qualifies whenever edges are rest-hidden."""
    # >400 cross-unit deduped edges forces a non-empty hidden set
    nodes = [make_symbol(f"m{i}.f", unit=f"r/src/m{i}.py") for i in range(30)]
    ids = [n.id for n in nodes]
    edges = [_edge(ids[i], ids[j]) for i in range(30) for j in range(30) if i != j]
    html = to_html(nodes, edges)  # 870 cross-unit edges -> 470 hidden
    # R11 AC-21: the copy explains itself instead of the bare "(N at rest)"
    assert "400 visible at rest \\u2014 hover reveals more" in html.replace(
        "—", "\\u2014")


def test_round7_enclosures_camera_glide_and_comet_dimming(
    make_symbol: MakeSymbol,
) -> None:
    """Round 7 (user): detail views need a mental model -> labeled group territories;
    view morphs must land where the camera points -> the viewBox glides WITH the node
    morph; tracing must dim ambient comets along with their lines."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    # enclosures: territory layer behind the edges, built from each view's colour
    # groups, named from the legend payload
    assert '<g id="enclosures"></g>' in html and "function buildEnclosures()" in html
    # territories only where rectangles are honest (round 8: radial views got
    # overlapping boxes that lied about grouping)
    assert 'const boxy = view === "package" || view === "relatedness"' in html
    # interaction-audit fix: tracing inside a drill dims to readable context, not black
    assert "svg.pair.tracing path.edge.dk { opacity: 0.15 !important; }" in html
    # search hits keep their distinguishing TAIL visible (start-ellipsis via rtl+bdi)
    assert "#hits li { direction: rtl; text-align: left;" in html
    assert 'document.createElement("bdi")' in html
    # the overflow check is LIVE in rescale() (review MED: one-shot prune went stale)
    assert "t.getComputedTextLength() > +t.dataset.maxw" in html
    assert "const LEG = " in html
    # territories group by TRUE group ids, not fill (palette repeats merged distinct
    # groups into one giant overlapping box — user report)
    assert "const ENC = " in html
    assert "const gi = enc[0][GLOBE.u[c.dataset.id]];" in html
    # camera glide: fit target extracted, tweened in step with the 620ms morph
    assert "function computeFit()" in html and "function glideTo(target, ms)" in html
    assert "glideTo(computeFit(), 620);" in html
    # tracing dims every comet that is not the trace's own
    assert "svg.tracing path.streak:not(.tr) { opacity: 0.04; }" in html
    # white comets on dimmed lines must dim HARDER than the line (user: 0.35 read as
    # full-strength because white at 0.35 alpha >> a muted line at 0.35)
    assert ".focus path.famflow:not(.hl) { opacity: 0.05; }" in html
    assert "#famlayer path.famflow.fam-dim { opacity: 0.05; }" in html
    assert 'if (pool === traceStreaks) t.classList.add("tr");' in html
    # the landing view is presented as Structure (key stays "families")
    assert "> Structure</label>" in html


def test_round9_hover_tag_pincard_tails_radial_labels(
    make_symbol: MakeSymbol,
) -> None:
    """Round 9 (user): hover shows a name-tag before you click; pin-card neighbours and
    search hits keep their distinguishing tail; Orbits/Galaxy get grouping labels."""
    html = to_html(
        [make_symbol("a.f", unit="r/src/a.py"),
         make_symbol("b.g", unit="r/tests/x.py")],
        [],
    )
    # hover name-tag
    assert 'id="nametag"' in html and "function showTag(id, ev)" in html
    # pin-card neighbours ellipsize at the start (rtl + bdi), same cure as search
    assert "#detail button.nb {" in html and "direction: rtl; text-align: left; }" in html
    assert "#detail button.nb bdi { direction: ltr;" in html
    # radial group labels for Orbits (layers) and Galaxy (communities)
    assert '<g id="grouplabels"></g>' in html and "function buildGroupLabels()" in html
    assert '"orbits":' in html and '"galaxy":' in html  # enc vocab extended
    # review HIGH: a drilled label must beat a stale inline display:none from declutter
    assert "svg.pair text.label.dk { display: inline !important; }" in html
    # review MED: hover tag sets display before positioning; group labels declutter
    assert 'nametag.style.display = "block";  // before moveTag' in html
    assert "function groupCircles(enc)" in html


def test_empty_graph_renders_without_error() -> None:
    """Round 9 review LOW: the new label-weight loop + enc vocab must be empty-safe."""
    html = to_html([], [])
    assert "<svg" in html and 'data-w="' not in html


def test_flat_views_rest_quiet_edges_reveal_on_hover(make_symbol: MakeSymbol) -> None:
    """Round 10 (user + critique): the flat detail views were resting edge-hairballs.
    They now rest QUIET — edges hidden until a node is hovered (trace adds .keep) or the
    'show all edges' toggle lifts it. Families/globe keep their arcs."""
    html = to_html(
        [make_symbol("a.f", unit="r/src/a.py"), make_symbol("b.g", unit="r/src/b.py")],
        [_edge("a.f", "b.g")],
    )
    assert "svg.edges-quiet path.edge { display: none !important; }" in html
    assert "svg.edges-quiet path.edge.keep { display: inline !important; }" in html
    # the five flat views opt in; families + globe stay out
    assert ("const QUIET_VIEWS = { relatedness: 1, dependency: 1, package: 1, "
            "orbits: 1, galaxy: 1 };") in html
    assert 'if (svg.classList.contains("edges-quiet")) return;' in html  # no resting comets
    assert "show all edges</label>" in html  # the discoverable toggle


def test_panel_surfaces_family_hint_and_edge_types_first(make_symbol: MakeSymbol) -> None:
    """Round 10 (blind critique): the tri-state chips were title-only and the edge-type
    filters fell below the long colour key. A visible hint explains the cycle, and the
    edge-type section now precedes the colour key so it never drops below the fold."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "click a family to cycle: ○ hidden · ◐ fringe" in html
    assert html.index("<h2>Edge types</h2>") < html.index("<h2>Colour key</h2>")


def test_r11_finishing(make_symbol: MakeSymbol) -> None:
    """R11 PR3: label halos, shared caption baseline, mass hierarchy, arrow stagger,
    apex titles, globe depth gradient, ghost dim floor, length-scaled comet tails
    (AC-26..AC-33; measurable parts probe-verified in probe_pr3.py)."""
    html = to_html(
        [make_symbol("a.f", unit="r/src/a.py"), make_symbol("b.g", unit="r/tests/x.py")],
        [_edge("a.f", "b.g")],
    )
    assert "stroke-width: 4; pointer-events: none; }" in html  # AC-26 halo
    assert "svg.tracing path.edge { opacity: 0.18 !important; }" in html  # AC-32
    assert (".focus path.famarc:not(.hl), .focus path.subarc:not(.hl)"
            " { opacity: 0.18; }") in html
    assert "const W = Math.min(320, Math.max(STREAK_WIN, len * 0.35));" in html  # AC-33
    assert "0.15 + 0.6 * Math.max(0, depth)" in html  # AC-31 gradient
    assert "function markApex(memberIds, sideOf)" in html  # AC-30
    assert 'view === "dependency"' in html  # AC-27a shared baseline block exists


def test_r11_arrowhead_stagger(make_symbol: MakeSymbol) -> None:
    """AC-29: arcs converging on one circle land at staggered depths."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    edges = []
    for et in (EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.REFERENCES):
        edges.append(Edge(type=et, src=a.id, dst=b.id, provenance=Provenance.LSP,
                          confidence=Confidence.EXTRACTED))
    html = to_html([a, b], edges)
    import re
    ends = re.findall(
        r'class="famarc[^"]*"[^>]*? d="M [-\d.]+ [-\d.]+ Q [-\d.]+ [-\d.]+ '
        r"([-\d.]+) ([-\d.]+)\"", html)
    # three arcs into the same circle must not share an endpoint
    assert len(set(ends)) == len(ends) >= 2


def test_r11_five_minutes_and_links(make_symbol: MakeSymbol) -> None:
    """R11 PR2: instructions match the view, one name everywhere, keyboard-complete
    search, narrated fly-to, source links, hash deep links (AC-13..AC-25).
    All 14 criteria probe-verified live (probe_pr2.py)."""
    html = to_html(
        [make_symbol("a.f", unit="r/src/a.py"), make_symbol("b.g", unit="r/tests/x.py")],
        [_edge("a.f", "b.g")],
        source_root="C:/repo",
    )
    # AC-13/15: landing usage + one name
    assert "USAGE_STRUCTURE" in html and "click a family or arc to open it" in html
    assert 'title="back to structure (Esc)"' in html
    assert '"families ▸' not in html  # the old two-name split is gone
    # AC-14: view-conditional intro
    assert 'id="intro-structure"' in html and 'id="intro-flat"' in html
    # AC-16/17: affordances + label forwarding
    assert "path.famhit { pointer-events: stroke; cursor: pointer; }" in html
    assert "#famlayer text.famlabel" in html and "#famlayer text.arclabel" in html
    # AC-18: keyboard search
    assert 'rows[i].classList.add("sel");' in html
    # AC-19/20: toast + neighbourhood framing + kept labels
    assert "function toast(msg)" in html and "shown in Communities" in html
    assert "svg.tracing text.label.keep { display: inline !important; opacity: 1; }"         in html
    # AC-21/22/23 copy
    # AC-21 wording is exercised in test_meta_line_discloses_the_rest_count (a graph
    # small enough not to cut edges has no at-rest line at all)
    assert "(only members touching visible code)" in html
    assert '" edges");' in html
    # AC-24: source links
    assert "const SRC_ROOT = " in html and '"vscode://file/"' in html
    # blind verifier AC-20/AC-24 loop-back: bbox-centred framing + parent-joined root
    assert "function frameNeighbourhood(id)" in html
    assert "const ccx = (mnX + mxX) / 2, ccy = (mnY + mxY) / 2;" in html
    assert 'cp.className = "copybtn";' in html
    # AC-25: deep links
    assert "function writeHash()" in html and "function restoreHash()" in html
    assert 'history.replaceState(null, "", "#" + parts.join("&"));' in html


def test_r11_trust_package(make_symbol: MakeSymbol) -> None:
    """R11 PR1 (final round): search answers for the whole corpus, the detail card is
    direction-aware with no silent truncation, and the input layer respects intent
    (drag is not dismissal, Esc peels one layer, focused controls own their keys,
    resize keeps the viewpoint). Spec: .dev/R11-SPEC.md AC-1..AC-12."""
    html = to_html(
        [make_symbol("a.f", unit="r/src/a.py"), make_symbol("b.g", unit="r/src/b.py")],
        [_edge("a.f", "b.g")],
    )
    # AC-1/2/8: whole-corpus search with honest states
    assert "const OFFMAP = " in html
    assert '"no matches in this codebase"' in html
    assert '"+" + extra + " more — keep typing"' in html or "keep typing" in html
    # AC-4/5/7: direction split + type ticks + expander
    assert "const inBy = {}, outBy = {};" in html
    assert 'nbGroup(box, "used by", usedBy, inBy[id] || {});' in html
    assert 'nbGroup(box, "uses", uses, outBy[id] || {});' in html
    assert '"show all " + ids.length' in html
    assert 'tick.className = "tick";' in html
    # AC-6: line numbers ride the tooltip payload
    assert ":{node.location.start_line}" not in html  # f-string resolved, not literal
    # AC-9: drag is not dismissal
    assert "if (dragDist > 4) return;" in html
    # AC-10: pinned card closes before drill pops
    assert "// one layer per press: pinned card first, then drill levels" in html
    assert html.index("if (pinned) unpin();") < html.index(
        "else if (drill || subFam !== null) exitDrill();")
    # AC-11: mouse-clicked radios blur (keyboard TAB users keep native arrows)
    # keyboard radio navigation fires synthetic clicks (detail===0) — only REAL
    # pointer clicks blur, or arrow-cycling loses focus after one press (review HIGH)
    assert 'if (e.detail !== 0) r.blur();' in html
    # AC-11: focused controls own navigation keys
    assert "e.target instanceof HTMLButtonElement" in html
    # AC-12: resize preserves the viewpoint (no fit-on-resize)
    assert 'window.addEventListener("resize", fit)' not in html
    assert "vb.h = vb.w * (r.height / r.width);" in html


def test_r11_tooltip_carries_line_numbers(make_symbol: MakeSymbol) -> None:
    """AC-6/AC-24 substrate: node tooltips end with unit:start_line."""
    sym = make_symbol("pkg.f", unit="r/src/a.py")
    html = to_html([sym], [])
    assert f") r/src/a.py:{sym.location.start_line}<" in html


def test_labels_declutter_reliably_across_zoom(make_symbol: MakeSymbol) -> None:
    """Round 9 (user): labels vanished unpredictably at some zooms — the old
    all-or-nothing CELL/lod-far gate hid every label at once. They are now screen-fixed
    (11.5px) and decluttered per-label by weight: the heaviest clusters are always
    labelled and more reveal as you zoom in, in every view including the globe."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "const px = 11.5 * dz, pxBig = 13.5 * dz;" in html  # AC-28 two steps
    assert "const LABEL_W_BIG = " in html
    # the all-or-nothing gate is gone
    assert 'svg.classList.toggle("lod-far"' not in html
    assert "svg.lod-far #labels" not in html
    # weight-ranked screen-space declutter drives visibility
    assert "function declutterLabels()" in html
    # round 10: ALL label kinds declutter together in one coordinate frame — file
    # labels yield to reserved group/territory labels, no cross-kind overlap
    assert "function labelBox(t, sx, sy)" in html
    assert '#grouplabels text, #enclosures text").forEach' in html
    assert "const labelsByWeight = labels.slice().sort(" in html
    assert 'data-w="' in html  # per-cluster weight baked in
    # globe shows hub labels (only hidden while actively spinning/resting)
    assert "svg.globe.spinning #labels, svg.resting #labels { display: none; }" in html
    assert 't.dataset.back === "1"' in html  # back-facing globe labels step aside


def test_drill_layouts_are_compact(make_symbol: MakeSymbol) -> None:
    """Round 5 (user): solo drills reused the GLOBAL scatter (group members landed a
    canvas apart); ribbon columns sat at fixed canvas fractions (short drills collapsed
    into a thin band). Solo = golden-angle spiral of unit clusters; ribbon gap follows
    column height."""
    html = to_html([make_symbol("a.f", unit="r/src/a.py")], [])
    assert "2.399963229728653" in html  # the golden-angle spiral
    assert "const r = step * Math.sqrt(i);" in html  # full-step spacing: no overlap
    # polish 6 (user): drills show ONLY drilled edges; no comets on hidden lines
    assert "svg.pair path.edge:not(.dk) { display: none !important; }" in html
    assert 'if (svg.classList.contains("pair") && !l.classList.contains("dk")) return;' in html
    assert "1.8 * r + 16);" in html  # ribbon slots grow to the cluster, never compress
    assert "const gap = Math.max(220, 0.55 * Math.max(colHeight(uA), colHeight(uB)));" in html
    assert "0.4 * CANVAS" not in html  # the fixed anchors are gone


def test_html_is_deterministic(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.a", unit="a.py")
    b = make_symbol("pkg.b", unit="b.py")
    edges = [_edge(a.id, b.id)]
    assert to_html([a, b], edges) == to_html([a, b], edges)  # repeatable
    assert to_html([a, b], edges) == to_html([b, a], edges)  # input order independent


def test_html_rejects_nonpositive_max_nodes(make_symbol: MakeSymbol) -> None:
    with pytest.raises(ValueError, match="max_nodes"):
        to_html([make_symbol("pkg.a", unit="a.py")], [], max_nodes=0)


def test_html_caps_large_graphs_and_logs(
    make_symbol: MakeSymbol, caplog: pytest.LogCaptureFixture
) -> None:
    nodes = [make_symbol(f"pkg.f{i}", unit="m.py") for i in range(10)]
    with caplog.at_level("WARNING"):
        html = to_html(nodes, [], max_nodes=3)
    assert "capping" in caplog.text.lower()  # no silent truncation
    assert html.count("data-r=") == 3  # node circles only (the horizon ring is chrome)
