"""Headless-browser regression tests for behaviors string pins cannot see.

The R11 reviews caught a class of bug invisible to substring assertions (a CSS
``pointer-events: none`` silently dead-lettering new click listeners). This module
keeps a minimal executed-DOM guard in the committed suite. Skips cleanly when
Playwright or its browser is unavailable (CI runners without chromium).
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest
from tests.conftest import MakeSymbol

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Provenance
from cartogate.viz.html import to_html

playwright_sync = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
)


def _edge(src: str, dst: str, et: EdgeType = EdgeType.CALLS) -> Edge:
    return Edge(type=et, src=src, dst=dst, provenance=Provenance.LSP,
                confidence=Confidence.EXTRACTED)


def test_structure_labels_actually_drill(make_symbol: MakeSymbol) -> None:
    """R11 AC-17 executed check: clicking a family LABEL and an arc COUNT LABEL both
    enter a drill (the pointer-events regression class)."""
    a = make_symbol("a.f", unit="r/src/a.py")
    b = make_symbol("b.g", unit="r/tests/x.py")
    html = to_html([a, b], [_edge(a.id, b.id)])
    with tempfile.TemporaryDirectory() as td:
        art = pathlib.Path(td) / "graph.html"
        art.write_text(html, encoding="utf-8")
        with playwright_sync.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception:  # noqa: BLE001 — no browser on this runner
                pytest.skip("chromium unavailable")
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(art.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(1000)

            # family label click drills to the subring/solo
            page.evaluate(
                "()=>{const t=document.querySelector('#famlayer text.famlabel');"
                "t.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
            page.wait_for_timeout(800)
            assert page.evaluate(
                "()=>document.body.classList.contains('drilled')")
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)

            # arc count-label click forwards to its famhit and drills the pair
            page.evaluate(
                "()=>{const t=document.querySelector('#famlayer text.arclabel');"
                "t.dispatchEvent(new MouseEvent('click',{bubbles:false}));}")
            page.wait_for_timeout(800)
            assert page.evaluate(
                "()=>document.body.classList.contains('drilled')")
            assert not errors, errors
            browser.close()
