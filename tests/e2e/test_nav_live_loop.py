"""Live-browser E2E: the full nav loop against a real generated viz, BOTH drivers.

The release gate the Stage-3 arc promised: extract → viz → serve → navigate by
the map → capture machine-manifested evidence — per driver, in CI, on every PR.
This suite is also the live cross-driver conformance proof the SeleniumDriver
review deferred here: identical map, identical app, both adapters must land on
the same states and produce equivalent evidence.

Marked ``e2e`` (deselected locally by default; CI runs ``pytest -m e2e`` with
chromium installed). Skips per-driver when the extra isn't installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.conftest import free_port

from cartogate.nav.schema import parse_navmap

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
NAVMAP_PATH = REPO / "tests" / "fixtures" / "nav" / "viz-navmap.json"

# Non-blank captures are the point (the pilot's blank-capture bug): a real
# rendered viz screenshot is far larger than the 4 KiB blanks were.
MIN_REAL_SCREENSHOT_BYTES = 10_000


@pytest.fixture(scope="module")
def served_viz(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Generate a viz from a small fixture tree and serve it at /viz.html."""
    serve_dir = tmp_path_factory.mktemp("viz-serve")
    out_dir = serve_dir / "build"
    build = subprocess.run(
        [
            sys.executable, "-m", "cartogate.cli", "viz",
            str(REPO / "tests" / "fixtures" / "sample_js"),
            "--format", "html", "--out-dir", str(out_dir),
        ],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr
    (serve_dir / "viz.html").write_bytes((out_dir / "graph.html").read_bytes())

    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=serve_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    import socket

    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    try:
        yield f"http://localhost:{port}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def _make_driver(kind: str, base_url: str):
    if kind == "playwright":
        pytest.importorskip("playwright")
        from cartogate.nav.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(base_url=base_url)
    pytest.importorskip("selenium")
    from cartogate.nav.selenium_driver import SeleniumDriver

    return SeleniumDriver(base_url=base_url)


@pytest.mark.parametrize("driver_kind", ["playwright", "selenium"])
def test_full_loop_navigate_verify_capture(
    driver_kind: str, served_viz: str, tmp_path: Path
) -> None:
    """where/goto/capture across both declared states, evidence machine-made."""
    from cartogate.nav.runtime import Navigator

    navmap = parse_navmap(json.loads(NAVMAP_PATH.read_text(encoding="utf-8")))
    driver = _make_driver(driver_kind, served_viz)
    try:
        nav = Navigator(driver, navmap)
        out = tmp_path / "out"
        out.mkdir()

        families = nav.capture("viz.families", out)
        globe = nav.capture("viz.globe", out)

        # State truth: the globe capture's URL carries the fragment the map declares.
        assert "v=globe" in globe["url"]
        # Machine-made manifest (the Sonnet b-4 seam, live):
        manifest = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert [e["name"] for e in manifest["captures"]] == [
            "viz_families.png", "viz_globe.png",
        ]
        # Non-blank, distinct evidence (the pilot's settle-bug regression, live):
        fam_bytes = (out / "viz_families.png").read_bytes()
        globe_bytes = (out / "viz_globe.png").read_bytes()
        assert len(fam_bytes) >= MIN_REAL_SCREENSHOT_BYTES
        assert len(globe_bytes) >= MIN_REAL_SCREENSHOT_BYTES
        assert fam_bytes != globe_bytes
        # Round-trip: where() agrees with the last capture's state.
        assert nav.where() == "viz.globe"
        # The families capture URL must NOT carry the globe fragment —
        # evidence distinguishes states (the old assertion here echoed
        # capture() input back and was vacuous; review Low).
        assert "v=globe" not in families["url"]
    finally:
        close = getattr(driver, "close", None)
        if close is not None:
            close()


@pytest.mark.parametrize("driver_kind", ["playwright", "selenium"])
def test_cli_check_tour_flow(driver_kind: str, served_viz: str) -> None:
    """`cartogate nav check --flow tour` passes end-to-end per driver."""
    if driver_kind == "playwright":
        pytest.importorskip("playwright")
        driver_args: list[str] = []
    else:
        pytest.importorskip("selenium")
        driver_args = ["--driver", "selenium"]
    result = subprocess.run(
        [
            sys.executable, "-m", "cartogate.cli", "nav", "check",
            "--map", str(NAVMAP_PATH), "--flow", "tour",
            "--base-url", served_viz, *driver_args,
        ],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


@pytest.mark.parametrize("driver_kind", ["playwright", "selenium"])
def test_crawl_proposes_landmarks_for_a_draft(
    driver_kind: str, served_viz: str, tmp_path: Path
) -> None:
    """crawl on a landmark-less draft proposes real page facts, per driver."""
    draft = {
        "version": 1,
        "app": "cartogate-viz",
        "states": [
            {"id": "viz.families", "url": "/viz.html", "landmarks": [], "affordances": []},
        ],
        "transitions": [],
        "flows": [],
    }
    map_path = tmp_path / "navmap.draft.json"
    map_path.write_text(json.dumps(draft), encoding="utf-8")

    from cartogate.nav.crawler import crawl_verify_propose

    driver = _make_driver(driver_kind, served_viz)
    try:
        report = crawl_verify_propose(map_path, driver)
    finally:
        close = getattr(driver, "close", None)
        if close is not None:
            close()

    assert report.visited == ["viz.families"]
    proposed = json.loads(
        (tmp_path / "navmap.draft.proposed.json").read_text(encoding="utf-8")
    )
    state = proposed["states"][0]
    # The viz's checked Structure radio is a proposed landmark — real page
    # truth, crawled provenance, identical across drivers (shared JS):
    landmark_names = {(lm["role"], lm["name"]) for lm in state["landmarks"]}
    assert ("radio", "Structure") in landmark_names
    assert all(lm["provenance"] == "crawled" for lm in state["landmarks"])
    # Affordances include the other view radios with a usable css fallback:
    radios = [a for a in state["affordances"] if a["role"] == "radio"]
    assert any(a["name"] == "Globe" and "css" in a for a in radios)


@pytest.fixture(scope="module")
def served_site(tmp_path_factory: pytest.TempPathFactory):
    """A 2-page static site: index links to page2 (GET nav) AND fires a POST on
    load — the discovery guard must abort the POST while following the link."""
    site = tmp_path_factory.mktemp("disco-site")
    (site / "index.html").write_text(
        "<!doctype html><html><body><h1>Home</h1>"
        '<a href="/page2.html">Page Two</a>'
        "<script>fetch('/mutate',{method:'POST'}).catch(()=>{});</script>"
        "</body></html>",
        encoding="utf-8",
    )
    (site / "page2.html").write_text(
        "<!doctype html><html><body><h1>Page Two</h1>"
        '<a href="/">Home</a></body></html>',
        encoding="utf-8",
    )
    # A target=_blank link + a popup that fires a POST on load — the guard must
    # cover the popup (context-scoped route), not just the opener page.
    (site / "popup-opener.html").write_text(
        "<!doctype html><html><body><h1>Opener</h1>"
        '<a href="/poster.html" target="_blank">Open</a></body></html>',
        encoding="utf-8",
    )
    (site / "poster.html").write_text(
        "<!doctype html><html><body><h1>Poster</h1>"
        "<script>fetch('/popup-mutate',{method:'POST'}).catch(()=>{});</script>"
        "</body></html>",
        encoding="utf-8",
    )
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=site, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    import socket

    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    try:
        yield f"http://localhost:{port}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_discovery_aborts_post_and_finds_the_new_page(
    served_site: str, tmp_path: Path
) -> None:
    """Live: the non-GET abort kills the POST; discovery follows the GET link
    and proposes the new page as a state. PlaywrightDriver only."""
    pytest.importorskip("playwright")
    from cartogate.nav.discover import DiscoveryBudget, crawl_discover
    from cartogate.nav.playwright_driver import PlaywrightDriver

    seed = {
        "version": 1, "app": "site",
        "states": [
            {"id": "home", "url": "/index.html",
             "landmarks": [{"role": "heading", "name": "Home"}], "affordances": []}
        ],
        "transitions": [], "flows": [],
    }
    map_path = tmp_path / "navmap.json"
    map_path.write_text(json.dumps(seed), encoding="utf-8")

    driver = PlaywrightDriver(base_url=served_site)
    try:
        report = crawl_discover(
            map_path, driver, base_url=served_site, budget=DiscoveryBudget(),
        )
        # The mechanical control fired: the POST was aborted in flight.
        assert any("/mutate" in u for u in driver.aborted_requests)
        # Discovery followed the GET link and proposed the new page:
        assert "page2.html" in report.proposed_states
        assert ("home", "page2.html") in report.transitions
    finally:
        driver.close()


def test_discovery_guard_covers_popups(served_site: str) -> None:
    """Live: a target=_blank popup firing a POST is aborted too — the guard is
    context-scoped, not page-scoped (review High 2026-07-21)."""
    pytest.importorskip("playwright")
    from cartogate.nav.playwright_driver import PlaywrightDriver

    driver = PlaywrightDriver(base_url=served_site)
    try:
        driver.block_mutating_requests()
        page = driver._ensure_page()
        page.goto(f"{served_site}/popup-opener.html")
        with page.expect_popup() as popup_info:
            page.get_by_role("link", name="Open").click()
        popup = popup_info.value
        popup.wait_for_load_state()
        assert any("/popup-mutate" in u for u in driver.aborted_requests)
    finally:
        driver.close()
