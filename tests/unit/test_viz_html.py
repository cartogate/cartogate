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
    # Fully offline: no external resource references.
    assert "http://" not in html and "https://" not in html


def test_html_has_all_three_views_and_embedded_data(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.alpha", unit="pkg/a.py")
    b = make_symbol("pkg.beta", unit="pkg/b.py")
    html = to_html([a, b], [_edge(a.id, b.id)])

    assert html.count('type="radio" name="view"') == 6  # one radio per view (incl. shapes)
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
    assert ".toFixed(2)" not in html  # the rounding-to-invisible class is gone
    assert "vb.w / CANVAS" not in html  # sizes scale against the FITTED base, not canvas


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
    assert 'stroke-width="0.90"' in html  # 0.6 + 0.3*log2(2)

    single = to_html([a, b], [_edge(a.id, b.id)])
    assert 'stroke-width="0.60"' in single


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
    # a 10-deep import chain -> 10 dependency layers -> the layer legend folds at 8
    nodes = [make_symbol(f"m{i}.f", unit=f"m{i}.py") for i in range(10)]
    edges = [_edge(nodes[i].id, nodes[i + 1].id) for i in range(9)]
    html = to_html(nodes, edges)
    assert "deeper layers (darker on the same ramp)" in html
    assert html.count("smaller (colours repeat)") <= html.count('class="legend"') - 2


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
